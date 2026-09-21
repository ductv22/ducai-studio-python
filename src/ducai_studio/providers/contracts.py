from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ducai_studio.domain.jobs import JsonValue, freeze_json


class ProviderKind(StrEnum):
    MEDIA_PROBE = "media_probe"
    OCR = "ocr"
    TRANSLATION = "translation"
    VOICE = "voice"
    PREVIEW = "preview"
    EXPORT = "export"


class HealthState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NEEDS_CONFIGURATION = "needs_configuration"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    values: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", freeze_json(dict(self.values)))


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider_id: str
    state: HealthState
    checked_at: str
    summary: str
    details: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.summary.strip():
            raise ValueError("Provider health requires an id and summary")
        object.__setattr__(self, "details", freeze_json(dict(self.details)))


@runtime_checkable
class Provider(Protocol):
    provider_id: str
    kind: ProviderKind

    def capabilities(self) -> ProviderCapabilities: ...

    def health(self) -> ProviderHealth: ...


@runtime_checkable
class PreviewProvider(Provider, Protocol):
    """Describe a preview backend without leaking its transport into the UI."""

    qml_component: str


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[ProviderKind, str], Provider] = {}

    def register(self, provider: Provider) -> None:
        key = (provider.kind, provider.provider_id.strip().lower())
        if not key[1]:
            raise ValueError("Provider id cannot be empty")
        if key in self._providers:
            identity = f"{provider.kind.value}/{provider.provider_id}"
            raise ValueError(f"Provider already registered: {identity}")
        self._providers[key] = provider

    def get(self, kind: ProviderKind, provider_id: str) -> Provider:
        key = (kind, provider_id.strip().lower())
        try:
            return self._providers[key]
        except KeyError as exc:
            raise LookupError(f"Unknown provider: {kind.value}/{provider_id}") from exc

    def list(self, kind: ProviderKind | None = None) -> tuple[Provider, ...]:
        providers = [
            provider
            for (provider_kind, _), provider in self._providers.items()
            if kind is None or provider_kind is kind
        ]
        return tuple(sorted(providers, key=lambda item: (item.kind.value, item.provider_id)))
