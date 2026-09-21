from __future__ import annotations

import json

import pytest

from ducai_studio.providers.translation.contracts import (
    ProviderAccessBlocked,
    QuotaExhausted,
    ResponseFormatFailure,
    TransientProviderFailure,
    TranslationPurpose,
    TranslationRequest,
)
from ducai_studio.providers.translation.openai_compatible import (
    HttpResponse,
    OpenAICompatibleProvider,
    validate_base_url,
)


def _request() -> TranslationRequest:
    return TranslationRequest(
        "chunk-1",
        TranslationPurpose.TRANSLATE,
        "Return JSON translations for these cues",
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


def _success(text: str = '{"translations":[]}') -> HttpResponse:
    return HttpResponse(
        200,
        json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {"prompt_tokens": 10},
            }
        ).encode(),
        {},
    )


def _error(status: int, *, code: str = "", message: str = "") -> HttpResponse:
    return HttpResponse(
        status,
        json.dumps({"error": {"status": "error", "code": code, "message": message}}).encode(),
        {"retry-after": "4"},
    )


def _provider(transport: SequenceTransport, references: tuple[str, ...] = ("a",)):
    return OpenAICompatibleProvider(
        provider_id="deepseek-api",
        model_id="deepseek-v4-flash",
        base_url="https://api.deepseek.com/",
        credential_references=references,
        credential_resolver=lambda reference: f"secret-{reference}",
        transport=transport,
    )


def test_openai_compatible_request_uses_bearer_header_and_json_mode() -> None:
    transport = SequenceTransport([_success("ok")])
    response = _provider(transport).generate(_request())

    call = transport.calls[0]
    assert response.text == "ok"
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret-a"
    assert b"secret-a" not in call["body"]
    payload = json.loads(call["body"])
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}


def test_openai_compatible_rotates_after_verified_quota_or_auth_failure() -> None:
    transport = SequenceTransport(
        [_error(429, code="insufficient_quota"), _error(401), _success("second")]
    )
    resolved: list[str] = []
    provider = OpenAICompatibleProvider(
        provider_id="tokenhub-api",
        model_id="hy3",
        base_url="https://tokenhub.tencentmaas.com/v1",
        credential_references=("first", "second", "third"),
        credential_resolver=lambda reference: resolved.append(reference) or reference,
        transport=transport,
    )

    assert provider.generate(_request()).text == "second"
    assert resolved == ["first", "second", "third"]


def test_gateway_block_is_not_misreported_as_authentication_or_rotated() -> None:
    gateway_block = HttpResponse(
        403,
        json.dumps(
            {
                "status": 403,
                "error_code": 1010,
            }
        ).encode(),
        {"server": "cloudflare"},
    )
    transport = SequenceTransport([gateway_block, _success("must-not-run")])
    provider = _provider(transport, ("a", "b"))

    with pytest.raises(ProviderAccessBlocked, match="không phải lỗi API key"):
        provider.generate(_request())

    assert len(transport.calls) == 1


def test_explicit_403_permission_failure_still_rotates_credentials() -> None:
    transport = SequenceTransport(
        [_error(403, code="permission_denied"), _success("second")]
    )
    provider = _provider(transport, ("a", "b"))

    assert provider.generate(_request()).text == "second"
    assert len(transport.calls) == 2


def test_unverified_429_does_not_rotate() -> None:
    transport = SequenceTransport([_error(429, code="busy")])
    provider = _provider(transport, ("a", "b"))

    with pytest.raises(TransientProviderFailure, match="không xác nhận quota"):
        provider.generate(_request())
    assert len(transport.calls) == 1


def test_quota_word_without_exhaustion_proof_does_not_rotate() -> None:
    transport = SequenceTransport([_error(429, message="quota service temporarily busy")])
    provider = _provider(transport, ("a", "b"))

    with pytest.raises(TransientProviderFailure, match="không xác nhận quota"):
        provider.generate(_request())
    assert len(transport.calls) == 1


def test_malformed_or_empty_response_is_rejected() -> None:
    malformed = HttpResponse(
        200,
        b'{"choices":[{"finish_reason":"stop","message":{"content":null}}]}',
        {},
    )
    with pytest.raises(ResponseFormatFailure, match="phản hồi rỗng"):
        _provider(SequenceTransport([malformed])).generate(_request())


def test_all_quota_keys_stop_after_one_pool_pass() -> None:
    transport = SequenceTransport(
        [_error(429, code="rate_limit_exceeded"), _error(402, code="quota_exceeded")]
    )
    with pytest.raises(QuotaExhausted) as captured:
        _provider(transport, ("a", "b")).generate(_request())
    assert captured.value.verified is True
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com/v1",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?key=secret",
        "https://example.com/v1#fragment",
        "https://example.com /v1",
        "https://example.com\\v1",
        "https://example.com:99999/v1",
        "https://example.com:not-a-port/v1",
        "",
    ],
)
def test_base_url_rejects_unsafe_or_missing_endpoints(raw: str) -> None:
    with pytest.raises(ValueError):
        validate_base_url(raw)


def test_local_http_endpoint_is_allowed_for_custom_provider() -> None:
    assert validate_base_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/v1"
