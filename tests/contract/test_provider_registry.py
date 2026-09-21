from dataclasses import dataclass

import pytest

from ducai_studio.providers.contracts import (
    HealthState,
    PreviewProvider,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
    ProviderRegistry,
)
from ducai_studio.providers.preview import QtMultimediaPreviewProvider


@dataclass
class FakeProvider:
    provider_id: str = "paddle-fast"
    kind: ProviderKind = ProviderKind.OCR

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities({"gpu": True})

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, HealthState.READY, "now", "Ready")


def test_registry_resolves_provider_by_kind_and_id() -> None:
    registry = ProviderRegistry()
    provider = FakeProvider()
    registry.register(provider)
    assert registry.get(ProviderKind.OCR, "PADDLE-FAST") is provider


def test_registry_rejects_duplicate_provider() -> None:
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeProvider())


def test_provider_capability_and_health_payloads_are_immutable() -> None:
    source = {"languages": ["zh", "vi"]}
    capabilities = ProviderCapabilities(source)
    health = ProviderHealth("paddle-fast", HealthState.READY, "now", "Ready", source)
    source["languages"].append("en")

    assert capabilities.values["languages"] == ("zh", "vi")
    assert health.details["languages"] == ("zh", "vi")
    with pytest.raises(TypeError, match="immutable"):
        capabilities.values["mode"] = "fast"


def test_qt_multimedia_preview_implements_replaceable_provider_contract() -> None:
    provider = QtMultimediaPreviewProvider()

    assert isinstance(provider, PreviewProvider)
    assert provider.kind is ProviderKind.PREVIEW
    assert provider.qml_component == "TimelinePreview"
    assert provider.capabilities().values["qml_scene_graph"] is True
    assert provider.capabilities().values["external_audio_mix"] is False
    assert provider.health().state is HealthState.READY
