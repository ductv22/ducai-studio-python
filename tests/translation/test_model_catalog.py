from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from ducai_studio.providers.translation.model_catalog import (
    ModelCatalogError,
    discover_models,
    parse_model_page,
)


def test_gemini_catalog_filters_capabilities_and_normalizes_names():
    models, token = parse_model_page({"models": [
        {"name": "models/gemini-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/bad", "supportedGenerationMethods": None},
    ], "nextPageToken": "next"}, gemini=True)
    assert models == ["gemini-flash"] and token == "next"


@pytest.mark.parametrize("status", [200, 401, 403, 429, 302])
def test_catalog_uses_real_http_and_sanitizes_failures(status):
    requests = []
    secret = "catalog-test-secret"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/leaked")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [
                {"id": "text-model"}, {"id": secret},
                {"id": "text-model"}, {"id": "bad\nvalue"},
            ], "error": secret}).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1"
        if status == 200:
            assert discover_models("openai-compatible", secret, base_url=base) == {
                "models": ["text-model"], "partial": False,
            }
        else:
            with pytest.raises(ModelCatalogError) as caught:
                discover_models("openai-compatible", secret, base_url=base)
            assert secret not in str(caught.value)
        assert requests == [("/v1/models", "Bearer " + secret)]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_cancelled_catalog_does_not_start_network():
    with pytest.raises(ModelCatalogError, match="Đã dừng"):
        discover_models("gemini-api", "test-key", cancelled=lambda: True)


def test_gemini_catalog_only_offers_gemini_text_translation_models():
    names = ["gemini-3.5-flash", "gemini-2.5-pro", "gemini-flash-latest",
             "gemini-3.1-flash-lite", "gemini-2.5-flash-image", "gemini-3.8-flash-tts",
             "gemini-3.5-transcribe", "gemini-2.5-computer-use-preview",
             "antigravity-preview-latest", "deep-research-pro-preview", "qwen-plus"]
    models, _ = parse_model_page({"models": [
        {"name": "models/" + name, "supportedGenerationMethods": ["generateContent"]}
        for name in names
    ]}, gemini=True)
    assert models == names[:4]
