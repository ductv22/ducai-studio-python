from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    Overlay,
    OverlayKind,
    ProjectSnapshot,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.domain.timeline import project_range

DEFAULT_BLUR_ID = "overlay-default-blur"
DEFAULT_LOGO_ID = "overlay-default-logo"
DEFAULT_LOGO_MEDIA_ID = "media-default-logo"


def ensure_default_overlays(
    snapshot: ProjectSnapshot,
    *,
    logo_path: str | Path | None = None,
    preset: dict[str, object] | None = None,
) -> ProjectSnapshot:
    timeline = project_range(snapshot)
    if timeline is None:
        return snapshot
    media = list(snapshot.media)
    logo_asset_id: str | None = None
    if logo_path:
        path = Path(logo_path).expanduser().resolve()
        if path.is_file():
            media = [item for item in media if item.id != DEFAULT_LOGO_MEDIA_ID]
            media.append(
                MediaAsset(
                    DEFAULT_LOGO_MEDIA_ID,
                    str(path),
                    MediaKind.IMAGE,
                    timeline.duration_us,
                )
            )
            logo_asset_id = DEFAULT_LOGO_MEDIA_ID
    raw_preset = preset or {}
    blur_preset = raw_preset.get("blur", {})
    logo_preset = raw_preset.get("logo", {})
    if not isinstance(blur_preset, dict) or not isinstance(logo_preset, dict):
        raise ValueError("Overlay preset entries must be objects")
    existing = {overlay.id: overlay for overlay in snapshot.overlays}
    blur = existing.get(
        DEFAULT_BLUR_ID,
        Overlay(
            DEFAULT_BLUR_ID,
            OverlayKind.BLUR,
            timeline,
            _preset_number(blur_preset, "x", 0.04),
            _preset_number(blur_preset, "y", 0.82),
            _preset_number(blur_preset, "width", 0.92),
            _preset_number(blur_preset, "height", 0.15),
            enabled=_preset_bool(blur_preset, "enabled", True),
            blur_radius=_preset_int(blur_preset, "blur_radius", 22),
        ),
    )
    logo = existing.get(
        DEFAULT_LOGO_ID,
        Overlay(
            DEFAULT_LOGO_ID,
            OverlayKind.LOGO,
            timeline,
            _preset_number(logo_preset, "x", 0.82),
            _preset_number(logo_preset, "y", 0.035),
            _preset_number(logo_preset, "width", 0.15),
            _preset_number(logo_preset, "height", 0.085),
            logo_asset_id,
            enabled=logo_asset_id is not None
            and _preset_bool(logo_preset, "enabled", True),
            opacity=_preset_number(logo_preset, "opacity", 0.9),
        ),
    )
    blur = replace(blur, range=TimeRange(timeline.start_us, timeline.end_us))
    logo = replace(
        logo,
        range=TimeRange(timeline.start_us, timeline.end_us),
        asset_id=logo_asset_id or logo.asset_id,
        enabled=logo.enabled if logo.asset_id else logo_asset_id is not None,
    )
    remaining = [
        overlay
        for overlay in snapshot.overlays
        if overlay.id not in {DEFAULT_BLUR_ID, DEFAULT_LOGO_ID}
    ]
    return replace(snapshot, media=tuple(media), overlays=(blur, logo, *remaining))


class EffectsEditor:
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot

    def _commit(self, candidate: ProjectSnapshot) -> ProjectSnapshot:
        self.snapshot = replace(candidate, revision=self.snapshot.revision + 1)
        return self.snapshot

    def update_overlay(
        self,
        overlay_id: str,
        *,
        x: float | None = None,
        y: float | None = None,
        width: float | None = None,
        height: float | None = None,
        start_us: int | None = None,
        end_us: int | None = None,
        enabled: bool | None = None,
        opacity: float | None = None,
        blur_radius: int | None = None,
    ) -> ProjectSnapshot:
        selected = next(
            (overlay for overlay in self.snapshot.overlays if overlay.id == overlay_id),
            None,
        )
        if selected is None:
            raise LookupError(f"Unknown overlay: {overlay_id}")
        new_range = selected.range
        if start_us is not None or end_us is not None:
            new_range = TimeRange(
                selected.range.start_us if start_us is None else start_us,
                selected.range.end_us if end_us is None else end_us,
            )
        updated = replace(
            selected,
            range=new_range,
            x=selected.x if x is None else x,
            y=selected.y if y is None else y,
            width=selected.width if width is None else width,
            height=selected.height if height is None else height,
            enabled=selected.enabled if enabled is None else enabled,
            opacity=selected.opacity if opacity is None else opacity,
            blur_radius=(selected.blur_radius if blur_radius is None else blur_radius),
        )
        overlays = tuple(
            updated if overlay.id == overlay_id else overlay
            for overlay in self.snapshot.overlays
        )
        return self._commit(replace(self.snapshot, overlays=overlays))

    def set_logo_asset(self, logo_path: str | Path) -> ProjectSnapshot:
        path = Path(logo_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError(path)
        timeline = project_range(self.snapshot)
        if timeline is None:
            raise ValueError("Project has no timeline for a logo")
        media = tuple(
            item for item in self.snapshot.media if item.id != DEFAULT_LOGO_MEDIA_ID
        ) + (
            MediaAsset(
                DEFAULT_LOGO_MEDIA_ID,
                str(path),
                MediaKind.IMAGE,
                timeline.duration_us,
            ),
        )
        overlays = tuple(
            replace(
                overlay,
                asset_id=DEFAULT_LOGO_MEDIA_ID,
                enabled=True,
            )
            if overlay.id == DEFAULT_LOGO_ID
            else overlay
            for overlay in self.snapshot.overlays
        )
        if not any(overlay.id == DEFAULT_LOGO_ID for overlay in overlays):
            seeded = ensure_default_overlays(replace(self.snapshot, media=media), logo_path=path)
            overlays = seeded.overlays
            media = seeded.media
        return self._commit(replace(self.snapshot, media=media, overlays=overlays))

    def add_blur(self, overlay_id: str) -> ProjectSnapshot:
        if not overlay_id.strip() or any(
            overlay.id == overlay_id for overlay in self.snapshot.overlays
        ):
            raise ValueError("Blur overlay id must be unique")
        timeline = project_range(self.snapshot)
        if timeline is None:
            raise ValueError("Project has no timeline for a blur region")
        blur_count = sum(
            1 for overlay in self.snapshot.overlays if overlay.kind is OverlayKind.BLUR
        )
        offset = min(0.24, blur_count * 0.035)
        overlay = Overlay(
            overlay_id,
            OverlayKind.BLUR,
            TimeRange(timeline.start_us, timeline.end_us),
            min(0.45, 0.08 + offset),
            min(0.76, 0.58 + offset),
            0.44,
            0.16,
            enabled=True,
            blur_radius=22,
        )
        return self._commit(
            replace(self.snapshot, overlays=(*self.snapshot.overlays, overlay))
        )

    def delete_overlay(self, overlay_id: str) -> ProjectSnapshot:
        selected = next(
            (overlay for overlay in self.snapshot.overlays if overlay.id == overlay_id),
            None,
        )
        if selected is None:
            raise LookupError(f"Unknown overlay: {overlay_id}")
        if selected.id in {DEFAULT_BLUR_ID, DEFAULT_LOGO_ID}:
            raise ValueError("Default overlays can be disabled but not deleted")
        return self._commit(
            replace(
                self.snapshot,
                overlays=tuple(
                    overlay for overlay in self.snapshot.overlays if overlay.id != overlay_id
                ),
            )
        )

    def reset_defaults(self, *, logo_path: str | Path | None = None) -> ProjectSnapshot:
        seeded = ensure_default_overlays(
            replace(
                self.snapshot,
                overlays=tuple(
                    overlay
                    for overlay in self.snapshot.overlays
                    if overlay.id not in {DEFAULT_BLUR_ID, DEFAULT_LOGO_ID}
                ),
            ),
            logo_path=logo_path,
        )
        return self._commit(seeded)


def _preset_number(values: dict, key: str, default: float) -> float:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Overlay preset {key} must be numeric")
    return float(value)


def _preset_int(values: dict, key: str, default: int) -> int:
    value = values.get(key, default)
    if type(value) is not int:
        raise ValueError(f"Overlay preset {key} must be an integer")
    return value


def _preset_bool(values: dict, key: str, default: bool) -> bool:
    value = values.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"Overlay preset {key} must be boolean")
    return value
