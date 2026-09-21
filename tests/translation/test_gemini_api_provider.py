from __future__ import annotations

import json
import urllib.request

import pytest

from ducai_studio.providers.translation.contracts import (
    AuthenticationRequired,
    QuotaExhausted,
    TransientProviderFailure,
    TranslationPurpose,
    TranslationRequest,
)
from ducai_studio.providers.translation.gemini_api import (
    GeminiApiProvider,
    HttpResponse,
    UrllibHttpTransport,
)


def _request() -> TranslationRequest:
    return TranslationRequest(
        "chunk-1",
        TranslationPurpose.TRANSLATE,
        "Dịch các cue sau",
        (1, 2),
        response_schema={"type": "object"},
    )


class SequenceTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout_seconds,
            }
        )
        return self.responses.pop(0)


class StubUrlResponse:
    status = 200
    headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def read() -> bytes:
        return b"{}"


def _success(text: str = '{"translations":[]}') -> HttpResponse:
    return HttpResponse(
        200,
        json.dumps(
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": text}]},
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10},
            }
        ).encode(),
        {},
    )


def _quota() -> HttpResponse:
    return HttpResponse(
        429,
        json.dumps(
            {
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "violations": [
                                {
                                    "quotaMetric": (
                                        "generativelanguage.googleapis.com/request_count"
                                    ),
                                    "quotaId": (
                                        "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
                                    ),
                                }
                            ]
                        }
                    ],
                }
            }
        ).encode(),
        {"retry-after": "12"},
    )


def _invalid_api_key() -> HttpResponse:
    return HttpResponse(
        400,
        json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": "API key not valid. Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "API_KEY_INVALID",
                            "domain": "googleapis.com",
                        }
                    ],
                }
            }
        ).encode(),
        {},
    )


def test_urllib_transport_adds_compatible_default_headers_without_compression(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return StubUrlResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    response = UrllibHttpTransport().post(
        "https://example.com/v1/chat/completions",
        headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        body=b'{"model":"test"}',
        timeout_seconds=30,
    )

    request = captured["request"]
    headers = {key.casefold(): value for key, value in request.header_items()}
    assert response.status == 200
    assert headers["user-agent"].startswith("DUCAI-STUDIO/")
    assert headers["accept"] == "application/json"
    assert headers["authorization"] == "Bearer test-secret"
    assert "accept-encoding" not in headers
    assert "test-secret" not in request.full_url
    assert b"test-secret" not in request.data


def test_urllib_transport_preserves_case_insensitive_header_overrides(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        return StubUrlResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    UrllibHttpTransport().post(
        "https://example.com/v1/chat/completions",
        headers={"user-agent": "Custom-Client/1", "accept": "application/problem+json"},
        body=b"{}",
        timeout_seconds=30,
    )

    request = captured["request"]
    headers = {key.casefold(): value for key, value in request.header_items()}
    assert headers["user-agent"] == "Custom-Client/1"
    assert headers["accept"] == "application/problem+json"


def test_api_key_is_header_only_and_never_written_into_body_or_url() -> None:
    transport = SequenceTransport([_success("ok")])
    provider = GeminiApiProvider(
        model_id="gemini-test",
        credential_references=("key-a",),
        credential_resolver=lambda _reference: "super-secret-key",
        transport=transport,
    )

    response = provider.generate(_request())

    call = transport.calls[0]
    assert response.text == "ok"
    assert call["headers"]["x-goog-api-key"] == "super-secret-key"
    assert b"super-secret-key" not in call["body"]
    assert "super-secret-key" not in call["url"]


def test_provider_rotates_to_next_reference_only_after_verified_quota() -> None:
    transport = SequenceTransport([_quota(), _success("second-key")])
    secrets = {"project-a": "key-a", "project-b": "key-b"}
    resolved: list[str] = []
    provider = GeminiApiProvider(
        model_id="gemini-test",
        credential_references=tuple(secrets),
        credential_resolver=lambda reference: (
            resolved.append(reference) or secrets[reference]
        ),
        transport=transport,
    )

    response = provider.generate(_request())

    assert response.text == "second-key"
    assert resolved == ["project-a", "project-b"]


def test_unverified_429_does_not_rotate_key() -> None:
    transport = SequenceTransport(
        [
            HttpResponse(
                429,
                b'{"error":{"code":429,"status":"UNKNOWN"}}',
                {},
            ),
            _success("must-not-run"),
        ]
    )
    resolved: list[str] = []
    provider = GeminiApiProvider(
        model_id="gemini-test",
        credential_references=("a", "b"),
        credential_resolver=lambda reference: resolved.append(reference) or reference,
        transport=transport,
    )

    with pytest.raises(TransientProviderFailure, match="không xác nhận quota"):
        provider.generate(_request())

    assert resolved == ["a"]
    assert len(transport.calls) == 1


def test_auth_failure_rotates_to_the_next_named_key() -> None:
    transport = SequenceTransport(
        [
            HttpResponse(
                401,
                b'{"error":{"code":401,"status":"UNAUTHENTICATED"}}',
                {},
            ),
            _success("second-key"),
        ]
    )
    resolved: list[str] = []
    provider = GeminiApiProvider(
        model_id="gemini-test",
        credential_references=("a", "b"),
        credential_resolver=lambda reference: resolved.append(reference) or reference,
        transport=transport,
    )

    response = provider.generate(_request())

    assert response.text == "second-key"
    assert resolved == ["a", "b"]


def test_official_api_key_invalid_error_rotates_to_the_next_named_key() -> None:
    transport = SequenceTransport([_invalid_api_key(), _success("second-key")])
    resolved: list[str] = []
    provider = GeminiApiProvider(
        model_id="gemini-test",
        credential_references=("expired", "working"),
        credential_resolver=lambda reference: resolved.append(reference) or reference,
        transport=transport,
    )

    response = provider.generate(_request())

    assert response.text == "second-key"
    assert resolved == ["expired", "working"]


def test_missing_credential_reference_rotates_without_exposing_the_secret() -> None:
    transport = SequenceTransport([_success("second-key")])
    resolved: list[str] = []

    def resolve(reference: str) -> str:
        resolved.append(reference)
        if reference == "missing":
            raise LookupError("not found")
        return "secret-b"

    provider = GeminiApiProvider(
        model_id="gemini-test",
        credential_references=("missing", "project-b"),
        credential_resolver=resolve,
        transport=transport,
    )

    response = provider.generate(_request())

    assert response.text == "second-key"
    assert resolved == ["missing", "project-b"]


def test_all_authentication_failures_stop_after_one_finite_pool_pass() -> None:
    unauthorized = HttpResponse(
        401,
        b'{"error":{"code":401,"status":"UNAUTHENTICATED"}}',
        {},
    )
    transport = SequenceTransport([unauthorized, unauthorized])
    provider = GeminiApiProvider(
        model_id="gemini-test",
        credential_references=("a", "b"),
        credential_resolver=lambda reference: reference,
        transport=transport,
    )

    with pytest.raises(AuthenticationRequired, match="Tất cả API key"):
        provider.generate(_request())

    assert len(transport.calls) == 2


def test_all_verified_project_quotas_stop_without_an_infinite_loop() -> None:
    transport = SequenceTransport([_quota(), _quota()])
    provider = GeminiApiProvider(
        model_id="gemini-test",
        credential_references=("a", "b"),
        credential_resolver=lambda reference: reference,
        transport=transport,
    )

    with pytest.raises(QuotaExhausted) as captured:
        provider.generate(_request())

    assert captured.value.verified is True
    assert len(transport.calls) == 2
