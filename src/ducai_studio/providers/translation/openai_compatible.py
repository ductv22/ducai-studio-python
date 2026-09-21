from __future__ import annotations

import json
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

from ducai_studio.domain.jobs import thaw_json
from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)

from .contracts import (
    AuthenticationRequired,
    ProviderAccessBlocked,
    ProviderResponse,
    QuotaExhausted,
    ResponseFormatFailure,
    TransientProviderFailure,
    TranslationRequest,
)
from .gemini_api import HttpResponse, HttpTransport, UrllibHttpTransport


@dataclass(frozen=True, slots=True)
class OpenAICompatiblePreset:
    provider_id: str
    label: str
    base_url: str
    default_model: str
    json_mode: bool = True
    extra_body: Mapping[str, Any] = field(default_factory=dict)


OPENAI_COMPATIBLE_PRESETS: Mapping[str, OpenAICompatiblePreset] = {
    "deepseek-api": OpenAICompatiblePreset(
        "deepseek-api",
        "DeepSeek API",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        extra_body={"thinking": {"type": "disabled"}},
    ),
    "tokenhub-api": OpenAICompatiblePreset(
        "tokenhub-api",
        "Hy3 · Tencent TokenHub",
        "https://tokenhub.tencentmaas.com/v1",
        "hy3",
    ),
    "qwen-api": OpenAICompatiblePreset(
        "qwen-api",
        "Qwen · Alibaba Model Studio",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3.7-flash",
        extra_body={"enable_thinking": False},
    ),
    "glm-api": OpenAICompatiblePreset(
        "glm-api",
        "GLM · Zhipu AI",
        "https://open.bigmodel.cn/api/paas/v4",
        "glm-5.2",
        extra_body={"thinking": {"type": "disabled"}},
    ),
    "openai-compatible": OpenAICompatiblePreset(
        "openai-compatible",
        "OpenAI-compatible tùy chỉnh",
        "",
        "",
        json_mode=True,
    ),
}

OPENAI_COMPATIBLE_PROVIDER_IDS = frozenset(OPENAI_COMPATIBLE_PRESETS)


def preset_for(provider_id: str) -> OpenAICompatiblePreset:
    try:
        return OPENAI_COMPATIBLE_PRESETS[provider_id.strip().casefold()]
    except KeyError as exc:
        raise ValueError(f"Unknown OpenAI-compatible provider: {provider_id}") from exc


def validate_base_url(value: str, *, required: bool = True) -> str:
    """Validate an endpoint without allowing credentials or query data in its URL."""

    raw = value.strip().rstrip("/")
    if not raw:
        if required:
            raise ValueError("OpenAI-compatible API requires a base URL")
        return ""
    if any(character.isspace() or ord(character) < 32 or character == "\\" for character in raw):
        raise ValueError("OpenAI-compatible base URL contains invalid characters")
    try:
        parsed = urllib.parse.urlparse(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OpenAI-compatible base URL is malformed") from exc
    allowed_schemes = {"https"}
    if hostname and hostname.casefold() in {"localhost", "127.0.0.1", "::1"}:
        allowed_schemes.add("http")
    if parsed.scheme.casefold() not in allowed_schemes or not hostname:
        raise ValueError("OpenAI-compatible base URL must use HTTPS (or local HTTP)")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("OpenAI-compatible base URL has an invalid port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("OpenAI-compatible base URL cannot contain credentials or query data")
    return raw


def _headers(value: Message | None) -> dict[str, str]:
    return (
        {key.casefold(): item for key, item in value.items()}
        if value is not None
        else {}
    )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _error_identity(body: bytes) -> tuple[str, str, str]:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return "", "", ""
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        return "", "", ""
    status = str(error.get("status", "")).casefold()
    code = str(error.get("code", "")).casefold()
    message = str(error.get("message", "")).casefold()
    return status, code, message


def _gateway_access_blocked(status: int, body: bytes) -> bool:
    if status not in {400, 401, 403, 406, 409, 429}:
        return False
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    error_code = str(payload.get("error_code", "")).casefold()
    error_name = str(payload.get("error_name", "")).casefold()
    category = str(payload.get("error_category", "")).casefold()
    return error_code in {"1010", "1020"} or payload.get("cloudflare_error") is True or any(
        token in value
        for value in (error_code, error_name, category)
        for token in (
            "browser_signature_banned",
            "cloudflare_error",
            "waf_blocked",
            "cdn_blocked",
        )
    )


def _is_verified_quota(status: int, error_status: str, code: str, message: str) -> bool:
    if status not in {402, 403, 429}:
        return False
    explicit_tokens = (
        "insufficient_quota",
        "quota_exceeded",
        "resource_exhausted",
        "rate_limit_exceeded",
        "billing_hard_limit",
        "credits_exhausted",
    )
    if any(token in value for value in (error_status, code) for token in explicit_tokens):
        return True
    quota_exhaustion_phrases = (
        "quota exceeded",
        "quota_exceeded",
        "quota exhausted",
        "quota limit reached",
        "quota limit exceeded",
        "insufficient quota",
        "out of quota",
        "credits exhausted",
        "billing hard limit",
    )
    return any(phrase in message for phrase in quota_exhaustion_phrases)


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        return ""
    pieces: list[str] = []
    for part in value:
        if isinstance(part, str):
            pieces.append(part)
        elif isinstance(part, Mapping):
            text = part.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "".join(pieces)


class OpenAICompatibleProvider:
    kind = ProviderKind.TRANSLATION
    provider_version = "openai-compatible-chat-v1"

    def __init__(
        self,
        *,
        provider_id: str,
        model_id: str,
        base_url: str,
        credential_references: Sequence[str],
        credential_resolver: Callable[[str], str],
        transport: HttpTransport | None = None,
        timeout_seconds: float = 300.0,
        json_mode: bool = True,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        provider_id = provider_id.strip().casefold()
        if provider_id not in OPENAI_COMPATIBLE_PROVIDER_IDS:
            raise ValueError("Unknown OpenAI-compatible provider")
        if not model_id.strip():
            raise ValueError("OpenAI-compatible model is required")
        references = tuple(value.strip() for value in credential_references if value.strip())
        if not references:
            raise ValueError("OpenAI-compatible credential references are required")
        if len(references) != len(set(references)):
            raise ValueError("OpenAI-compatible credential references must be unique")
        if isinstance(json_mode, bool) is False:
            raise ValueError("OpenAI-compatible JSON mode must be boolean")
        self.provider_id = provider_id
        self.model_id = model_id.strip()
        self.base_url = validate_base_url(base_url)
        self.credential_references = references
        self.credential_resolver = credential_resolver
        self.transport = transport or UrllibHttpTransport()
        self.timeout_seconds = timeout_seconds
        self.json_mode = json_mode
        configured_extra_body = (
            preset_for(provider_id).extra_body if extra_body is None else extra_body
        )
        if any(
            key in {"model", "messages", "temperature", "stream", "response_format"}
            for key in configured_extra_body
        ):
            raise ValueError("OpenAI-compatible extra body cannot override request fields")
        self.extra_body = dict(configured_extra_body)
        self._active_index = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {
                "structured_json": self.json_mode,
                "credential_pool_size": len(self.credential_references),
                "rotates_on_verified_quota": True,
                "rotates_on_authentication_failure": True,
                "openai_compatible_chat_completions": True,
            }
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_id,
            HealthState.READY,
            "not-probed",
            "OpenAI-compatible API đã cấu hình bằng tham chiếu Credential Manager.",
            {"base_url": self.base_url, "model_id": self.model_id},
        )

    def close(self) -> None:
        return

    def generate(self, request: TranslationRequest) -> ProviderResponse:
        exhausted: list[str] = []
        for offset in range(len(self.credential_references)):
            index = (self._active_index + offset) % len(self.credential_references)
            reference = self.credential_references[index]
            try:
                key = self.credential_resolver(reference)
            except (LookupError, OSError):
                continue
            if not key.strip():
                continue
            try:
                result = self._send(request, key)
            except QuotaExhausted as exc:
                if not exc.verified:
                    raise
                exhausted.append(reference)
                continue
            except AuthenticationRequired:
                continue
            self._active_index = index
            return result
        if exhausted:
            raise QuotaExhausted(
                "Tất cả API key khả dụng đã hết quota; cache được giữ nguyên.",
                verified=True,
            )
        raise AuthenticationRequired(
            "Tất cả API key đã lưu đều thiếu, sai, hết hiệu lực hoặc không có quyền dùng model."
        )

    def _send(self, request: TranslationRequest, key: str) -> ProviderResponse:
        body_payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": request.temperature,
            "max_tokens": max(1_024, min(16_384, max(1, len(request.expected_ids)) * 96)),
            "stream": False,
        }
        if self.json_mode:
            body_payload["response_format"] = {"type": "json_object"}
        if self.extra_body:
            body_payload.update(self.extra_body)
        if request.response_schema and not self.json_mode:
            body_payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "translation_response",
                    "strict": True,
                    "schema": thaw_json(request.response_schema),
                },
            }
        body = json.dumps(body_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            response = self.transport.post(
                f"{self.base_url}/chat/completions",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                body=body,
                timeout_seconds=self.timeout_seconds,
            )
        except (TimeoutError, urllib.error.URLError, OSError, ValueError) as exc:
            raise TransientProviderFailure(
                "OpenAI-compatible API mất kết nối hoặc quá thời gian; sẽ thử lại đúng chunk."
            ) from exc
        if response.status != 200:
            self._raise_http_failure(response)
        try:
            payload = json.loads(response.body.decode("utf-8"))
            choices = payload["choices"]
            choice = choices[0]
            message = choice["message"]
            text = _content_text(message.get("content"))
            finish_reason = str(choice.get("finish_reason", "stop")).casefold()
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ResponseFormatFailure(
                "OpenAI-compatible API trả cấu trúc phản hồi không hợp lệ."
            ) from exc
        if finish_reason not in {"stop", "completed", "finish_reason_unspecified", "none"}:
            raise ResponseFormatFailure(
                f"OpenAI-compatible API dừng phản hồi trước khi hoàn tất ({finish_reason})."
            )
        if not text.strip():
            raise ResponseFormatFailure("OpenAI-compatible API trả phản hồi rỗng.")
        usage = payload.get("usage", {})
        return ProviderResponse(
            request.request_id,
            text,
            self.provider_id,
            self.model_id,
            usage if isinstance(usage, dict) else {},
        )

    @staticmethod
    def _raise_http_failure(response: HttpResponse) -> None:
        error_status, code, message = _error_identity(response.body)
        retry_after = _retry_after(response.headers)
        if _is_verified_quota(response.status, error_status, code, message):
            raise QuotaExhausted(
                "OpenAI-compatible API xác nhận đã chạm hạn mức hoặc billing limit.",
                verified=True,
                retry_after_seconds=retry_after,
            )
        if _gateway_access_blocked(response.status, response.body):
            raise ProviderAccessBlocked(
                "Gateway/CDN chặn kết nối HTTP của ứng dụng; không phải lỗi API key."
            )
        auth_tokens = (
            "invalid_api_key",
            "api_key_invalid",
            "authentication",
            "unauthorized",
            "unauthenticated",
            "permission",
            "forbidden",
            "access_denied",
        )
        if response.status in {401, 403} or any(
            token in value for value in (error_status, code, message) for token in auth_tokens
        ):
            raise AuthenticationRequired(
                "OpenAI-compatible API key thiếu, sai, hết hiệu lực hoặc không có quyền."
            )
        if response.status == 429:
            raise TransientProviderFailure(
                "OpenAI-compatible API trả 429 nhưng không xác nhận quota; không tự xoay key.",
                retry_after_seconds=retry_after,
            )
        if response.status in {408, 425} or response.status >= 500 or error_status in {
            "unavailable",
            "internal",
            "aborted",
            "timeout",
        }:
            raise TransientProviderFailure(
                f"OpenAI-compatible API tạm thời không khả dụng (HTTP {response.status}).",
                retry_after_seconds=retry_after,
            )
        raise ResponseFormatFailure(
            f"OpenAI-compatible API từ chối yêu cầu (HTTP {response.status})."
        )
