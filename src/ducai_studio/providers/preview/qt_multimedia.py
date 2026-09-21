from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)


@dataclass(frozen=True, slots=True)
class QtMultimediaPreviewProvider:
    """QML-native preview selected for the first rebuild release."""

    provider_id: str = "qt-multimedia"
    kind: ProviderKind = ProviderKind.PREVIEW
    qml_component: str = "TimelinePreview"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {
                "qml_scene_graph": True,
                "play_pause": True,
                "seek": True,
                "mute": True,
                "timeline_position": True,
                "external_audio_mix": False,
                "external_subtitle_render": False,
            }
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            state=HealthState.READY,
            checked_at=datetime.now(UTC).isoformat(),
            summary="Qt Multimedia sẵn sàng cho preview QML",
        )
