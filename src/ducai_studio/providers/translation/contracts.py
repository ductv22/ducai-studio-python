from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ducai_studio.domain.jobs import JsonValue, freeze_json
from ducai_studio.providers.contracts import Provider


class TranslationPurpose(StrEnum):
    FILM_BIBLE = "film_bible"
    TRANSLATE = "translate"
    QA = "qa"


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    request_id: str
    purpose: TranslationPurpose
    prompt: str
    expected_ids: tuple[int, ...] = ()
    temperature: float = 0.2
    response_schema: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.prompt.strip():
            raise ValueError("Translation request id and prompt are required")
        if len(self.expected_ids) != len(set(self.expected_ids)):
            raise ValueError("Expected translation ids must be unique")
        if any(type(value) is not int or value <= 0 for value in self.expected_ids):
            raise ValueError("Expected translation ids must be positive integers")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("Translation temperature must be between 0 and 2")
        object.__setattr__(self, "response_schema", freeze_json(dict(self.response_schema)))


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    request_id: str
    text: str
    provider_id: str
    model_id: str
    usage: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.request_id, self.text, self.provider_id, self.model_id)
        ):
            raise ValueError("Provider response identity and text are required")
        object.__setattr__(self, "usage", freeze_json(dict(self.usage)))


class TranslationProviderFailure(RuntimeError):
    """A bounded, user-visible transport failure with no credential material."""

    code = "translation_provider_failure"
    retryable = False

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AuthenticationRequired(TranslationProviderFailure):
    code = "authentication_required"


class ChallengeRequired(TranslationProviderFailure):
    code = "challenge_required"


class ProviderAccessBlocked(TranslationProviderFailure):
    code = "provider_access_blocked"


class QuotaExhausted(TranslationProviderFailure):
    code = "quota_exhausted"

    def __init__(
        self,
        message: str,
        *,
        verified: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, retry_after_seconds=retry_after_seconds)
        self.verified = verified


class TransientProviderFailure(TranslationProviderFailure):
    code = "transient_provider_failure"
    retryable = True


class ResponseFormatFailure(TranslationProviderFailure):
    code = "response_format_failure"
    retryable = True


@runtime_checkable
class TranslationProvider(Provider, Protocol):
    model_id: str

    def generate(self, request: TranslationRequest) -> ProviderResponse: ...

    def close(self) -> None: ...
