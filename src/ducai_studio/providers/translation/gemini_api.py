from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from typing import Any, Protocol

from ducai_studio import __version__
from ducai_studio.domain.jobs import thaw_json
from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)

from .contracts import (
    AuthenticationRequired,
    ProviderResponse,
    QuotaExhausted,
    ResponseFormatFailure,
    TransientProviderFailure,
    TranslationRequest,
)

_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_HTTP_HEADERS = {
    "Accept": "application/json",
    "User-Agent": f"DUCAI-STUDIO/{__version__}",
}


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class UrllibHttpTransport:
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> HttpResponse:
        request_headers = dict(headers)
        existing = {key.casefold() for key in request_headers}
        for key, value in _DEFAULT_HTTP_HEADERS.items():
            if key.casefold() not in existing:
                request_headers[key] = value
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    int(response.status),
                    response.read(),
                    {key.casefold(): value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                int(exc.code),
                exc.read(),
                _headers(exc.headers),
            )


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


def _error_identity(body: bytes) -> tuple[str, str]:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return "", ""
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        return "", ""
    status = str(error.get("status", "")).upper()
    details = error.get("details", [])
    reasons: list[str] = []
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            reasons.append(str(detail.get("reason", "")))
            for violation in detail.get("violations", []) or []:
                if isinstance(violation, dict):
                    reasons.append(str(violation.get("quotaMetric", "")))
                    reasons.append(str(violation.get("quotaId", "")))
    return status, " ".join(reasons).casefold()


class GeminiApiProvider:
    provider_id = "gemini-api"
    kind = ProviderKind.TRANSLATION
    provider_version = "generate-content-v1beta-2026-06"

    def __init__(
        self,
        *,
        model_id: str,
        credential_references: Sequence[str],
        credential_resolver: Callable[[str], str],
        transport: HttpTransport | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        references = tuple(value.strip() for value in credential_references if value.strip())
        if not model_id.strip() or not references:
            raise ValueError("Gemini API model and credential references are required")
        if len(references) != len(set(references)):
            raise ValueError("Gemini API credential references must be unique")
        self.model_id = model_id.strip()
        self.credential_references = references
        self.credential_resolver = credential_resolver
        self.transport = transport or UrllibHttpTransport()
        self.timeout_seconds = timeout_seconds
        self._active_index = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {
                "structured_json": True,
                "credential_pool_size": len(self.credential_references),
                "rotates_on_verified_quota": True,
                "rotates_on_authentication_failure": True,
            }
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_id,
            HealthState.READY,
            "not-probed",
            "Gemini API đã cấu hình bằng tham chiếu Credential Manager.",
            {"model_id": self.model_id},
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
        generation: dict[str, Any] = {
            "temperature": request.temperature,
            "responseMimeType": "application/json",
        }
        if request.response_schema:
            generation["responseSchema"] = thaw_json(request.response_schema)
        body = json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
                "generationConfig": generation,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            response = self.transport.post(
                f"{_API_ROOT}/{urllib.parse.quote(self.model_id, safe='-._')}:generateContent",
                headers={"Content-Type": "application/json", "x-goog-api-key": key},
                body=body,
                timeout_seconds=self.timeout_seconds,
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            raise TransientProviderFailure(
                "Gemini API mất kết nối hoặc quá thời gian; sẽ thử lại đúng chunk."
            ) from exc
        if response.status != 200:
            self._raise_http_failure(response)
        try:
            payload = json.loads(response.body.decode("utf-8"))
            candidates = payload["candidates"]
            candidate = candidates[0]
            finish_reason = str(candidate.get("finishReason", "STOP"))
            parts = candidate["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ResponseFormatFailure("Gemini API trả cấu trúc phản hồi không hợp lệ.") from exc
        if finish_reason not in {"STOP", "FINISH_REASON_UNSPECIFIED"}:
            raise ResponseFormatFailure(
                f"Gemini API dừng phản hồi trước khi hoàn tất ({finish_reason})."
            )
        if not text.strip():
            raise ResponseFormatFailure("Gemini API trả phản hồi rỗng.")
        usage = payload.get("usageMetadata", {})
        return ProviderResponse(
            request.request_id,
            text,
            self.provider_id,
            self.model_id,
            usage if isinstance(usage, dict) else {},
        )

    @staticmethod
    def _raise_http_failure(response: HttpResponse) -> None:
        status, error_detail = _error_identity(response.body)
        retry_after = _retry_after(response.headers)
        verified_quota = response.status == 429 and (
            status == "RESOURCE_EXHAUSTED"
            or "quota" in error_detail
            or "rate" in error_detail
        )
        if verified_quota:
            raise QuotaExhausted(
                "Gemini API xác nhận đã chạm hạn mức project.",
                verified=True,
                retry_after_seconds=retry_after,
            )
        invalid_key = any(
            token in error_detail
            for token in ("api_key_invalid", "api_key_expired", "key_expired")
        )
        if response.status == 401 or status == "UNAUTHENTICATED" or invalid_key:
            raise AuthenticationRequired("Gemini API key thiếu, sai hoặc đã hết hiệu lực.")
        if response.status == 403 or status == "PERMISSION_DENIED":
            raise AuthenticationRequired("Gemini API key không có quyền dùng model này.")
        if response.status == 429:
            raise TransientProviderFailure(
                "Gemini API trả 429 nhưng không xác nhận quota; không tự xoay key.",
                retry_after_seconds=retry_after,
            )
        if response.status >= 500 or status in {"UNAVAILABLE", "INTERNAL", "ABORTED"}:
            raise TransientProviderFailure(
                f"Gemini API tạm thời không khả dụng (HTTP {response.status}).",
                retry_after_seconds=retry_after,
            )
        raise ResponseFormatFailure(
            f"Gemini API từ chối yêu cầu (HTTP {response.status}, {status or 'UNKNOWN'})."
        )
