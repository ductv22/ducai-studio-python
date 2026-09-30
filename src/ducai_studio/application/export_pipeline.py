from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from ducai_studio.domain.effects import (
    ExportSettings,
    ExportSnapshot,
    RenderClip,
    RenderConformSegment,
    RenderOverlay,
    RenderSceneText,
    RenderSubtitle,
    RenderSubtitleBlur,
)
from ducai_studio.domain.project import (
    OverlayKind,
    ProjectSnapshot,
    SubtitleCue,
    TrackKind,
    dialogue_cues,
    scene_text_cues,
)

SCENE_TEXT_FONT_FAMILIES = {
    "calligraphy": "Playwrite VN",
    "calligraphy_soft": "Dancing Script",
    "calligraphy_bold": "Lobster",
    "calligraphy_ink": "Agbalumo",
    "calligraphy_brush": "Comforter Brush",
    "calligraphy_water": "Water Brush",
    "calligraphy_splash": "Splash",
    "calligraphy_hurricane": "Hurricane",
}


def _cue_font_family(style_hint: str, fallback: str = "") -> str:
    return SCENE_TEXT_FONT_FAMILIES.get(style_hint, fallback)


class MediaProbe(Protocol):
    def probe(self, media_path: str | Path): ...


def build_export_snapshot(
    project: ProjectSnapshot,
    *,
    settings: ExportSettings,
    media_probe: MediaProbe,
) -> ExportSnapshot:
    media = {item.id: item for item in project.media}
    video_clips = [
        (track, clip)
        for track in project.tracks
        if track.kind is TrackKind.VIDEO and track.visible
        for clip in track.clips
    ]
    if len(video_clips) != 1:
        raise ValueError(
            "The first export release supports exactly one covering video clip; "
            "multi-clip timelines are not silently flattened"
        )
    video_track, video_clip = video_clips[0]
    if video_clip.media_id is None or video_clip.source_range is None:
        raise ValueError("Export video clip has no source media range")
    video_asset = media.get(video_clip.media_id)
    if video_asset is None:
        raise ValueError("Export video clip references missing media")
    video_path = Path(video_asset.path).expanduser().resolve(strict=True)
    probe = media_probe.probe(video_path)
    if not probe.has_video or probe.width is None or probe.height is None:
        raise ValueError("Export source is not a decodable video")
    start_us = video_clip.timeline_range.start_us
    end_us = video_clip.timeline_range.end_us
    video = _render_clip(video_track.kind, video_track.muted, video_clip, video_path)
    conform_plan = project.voice_conform.plan if project.voice_conform is not None else None
    conform_segments: tuple[RenderConformSegment, ...] = ()
    if conform_plan is not None:
        if (
            video_clip.source_range.start_us != 0
            or video_clip.source_range.end_us != conform_plan.source_duration_us
        ):
            raise ValueError("Voice conform currently requires the full source video clip")
        start_us = 0
        end_us = conform_plan.output_duration_us
        video = RenderClip(
            video.clip_id,
            video.track_id,
            video.track_kind,
            video.path,
            start_us,
            end_us,
            video.source_start_us,
            video.source_end_us,
            1,
            1,
            video.volume,
            video.muted,
        )
        conform_segments = tuple(
            RenderConformSegment(
                item.source_start_us,
                item.source_end_us,
                item.output_start_us,
                item.output_end_us,
                item.video_speed,
            )
            for item in conform_plan.segments
        )

    audio: list[RenderClip] = []
    for track in project.tracks:
        if track.kind not in {
            TrackKind.ORIGINAL_AUDIO,
            TrackKind.VOICE,
            TrackKind.BACKGROUND_MUSIC,
        }:
            continue
        for clip in track.clips:
            if clip.media_id is None or clip.source_range is None:
                continue
            if (
                clip.timeline_range.end_us <= start_us
                or clip.timeline_range.start_us >= end_us
            ):
                continue
            asset = media.get(clip.media_id)
            if asset is None:
                raise ValueError(f"Export audio clip references missing media: {clip.id}")
            path = Path(asset.path).expanduser().resolve(strict=True)
            rendered = _render_clip(track.kind, track.muted, clip, path)
            if conform_plan is not None and track.kind is TrackKind.ORIGINAL_AUDIO:
                rendered = RenderClip(
                    rendered.clip_id,
                    rendered.track_id,
                    rendered.track_kind,
                    rendered.path,
                    conform_plan.source_to_output_us(rendered.timeline_start_us),
                    conform_plan.source_to_output_us(rendered.timeline_end_us),
                    rendered.source_start_us,
                    rendered.source_end_us,
                    rendered.speed_numerator,
                    rendered.speed_denominator,
                    rendered.volume,
                    rendered.muted,
                )
            audio.append(rendered)

    overlays: list[RenderOverlay] = []
    subtitle_blurs: list[RenderSubtitleBlur] = []
    for overlay in project.overlays:
        if (
            not overlay.enabled
            or overlay.range.end_us <= start_us
            or overlay.range.start_us >= end_us
        ):
            continue
        if (
            overlay.kind is OverlayKind.BLUR
            and overlay.tracking_mode == "source_subtitle"
        ):
            for cue in project.source_cues:
                cue_start = max(
                    video_clip.timeline_range.start_us,
                    overlay.range.start_us,
                    cue.range.start_us,
                )
                cue_end = min(
                    video_clip.timeline_range.end_us,
                    overlay.range.end_us,
                    cue.range.end_us,
                )
                if cue_end <= cue_start:
                    continue
                x, y, width, height, region_source = _subtitle_blur_geometry(
                    cue,
                    padding_x=overlay.padding_x,
                    padding_y=overlay.padding_y,
                    fallback_y=overlay.y,
                )
                if conform_plan is not None:
                    cue_start = conform_plan.source_to_output_us(cue_start)
                    cue_end = conform_plan.source_to_output_us(cue_end)
                subtitle_blurs.append(
                    RenderSubtitleBlur(
                        cue.id,
                        cue.index,
                        cue_start,
                        cue_end,
                        x,
                        y,
                        width,
                        height,
                        overlay.blur_radius,
                        overlay.opacity,
                        region_source,
                        overlay.concealment_mode,
                    )
                )
            continue
        if (
            overlay.kind is OverlayKind.BLUR
            and overlay.tracking_mode == "ocr_watermark"
        ):
            overlay_start = max(
                video_clip.timeline_range.start_us,
                overlay.range.start_us,
            )
            overlay_end = min(
                video_clip.timeline_range.end_us,
                overlay.range.end_us,
            )
            if conform_plan is not None:
                overlay_start = conform_plan.source_to_output_us(overlay_start)
                overlay_end = conform_plan.source_to_output_us(overlay_end)
            subtitle_blurs.append(
                RenderSubtitleBlur(
                    overlay.id,
                    len(subtitle_blurs) + 1,
                    overlay_start,
                    overlay_end,
                    overlay.x,
                    overlay.y,
                    overlay.width,
                    overlay.height,
                    overlay.blur_radius,
                    overlay.opacity,
                    "ocr",
                    overlay.concealment_mode,
                )
            )
            continue
        asset_path = ""
        if overlay.asset_id:
            asset = media.get(overlay.asset_id)
            if asset is None:
                raise ValueError(f"Export overlay references missing media: {overlay.id}")
            asset_path = str(Path(asset.path).expanduser().resolve(strict=True))
        if overlay.kind.value == "logo" and not asset_path:
            raise ValueError("Enabled logo overlay has no image asset")
        overlay_start = max(video_clip.timeline_range.start_us, overlay.range.start_us)
        overlay_end = min(video_clip.timeline_range.end_us, overlay.range.end_us)
        if conform_plan is not None:
            overlay_start = conform_plan.source_to_output_us(overlay_start)
            overlay_end = conform_plan.source_to_output_us(overlay_end)
        overlays.append(
            RenderOverlay(
                overlay.id,
                overlay.kind.value,
                overlay_start,
                overlay_end,
                overlay.x,
                overlay.y,
                overlay.width,
                overlay.height,
                asset_path,
                overlay.opacity,
                overlay.blur_radius,
            )
        )

    raw_cues = project.translated_cues if project.translated_cues else project.source_cues
    if settings.subtitle_style.enabled and not raw_cues:
        raise ValueError("Subtitle burn-in is enabled but the project has no subtitles")
    subtitles = []
    scene_texts: list[RenderSceneText] = []
    source_start_us = video_clip.timeline_range.start_us
    source_end_us = video_clip.timeline_range.end_us
    for cue in dialogue_cues(raw_cues):
        if cue.range.end_us <= source_start_us or cue.range.start_us >= source_end_us:
            continue
        cue_start = max(source_start_us, cue.range.start_us)
        cue_end = min(source_end_us, cue.range.end_us)
        if conform_plan is not None:
            cue_start = conform_plan.source_to_output_us(cue_start)
            cue_end = conform_plan.source_to_output_us(cue_end)
        subtitles.append(
            RenderSubtitle(
                cue.id,
                cue.index,
                cue_start,
                cue_end,
                _scene_text_layout(cue.text, cue.orientation),
                max(
                    1,
                    round(settings.subtitle_style.font_size * cue.text_font_size / 12),
                ),
                cue.orientation,
                cue.background_color,
                cue.text_color,
                *(cue.panel_box if cue.panel_box is not None else (-1.0, -1.0, 0.0, 0.0)),
                _cue_font_family(cue.style_hint),
                cue.letter_spacing_percent,
                cue.font_bold,
                cue.font_italic,
                cue.text_opacity_percent,
                cue.background_opacity_percent,
            )
        )
    source_scene_by_id = {cue.id: cue for cue in scene_text_cues(project.source_cues)}
    render_width = int(settings.output_width or probe.width)
    render_height = int(settings.output_height or probe.height)
    render_width -= render_width % 2
    render_height -= render_height % 2
    for cue in scene_text_cues(raw_cues):
        if cue.range.end_us <= source_start_us or cue.range.start_us >= source_end_us:
            continue
        source_geometry = source_scene_by_id.get(cue.id)
        geometry = (
            source_geometry
            if source_geometry is not None and source_geometry.visual_box is not None
            else cue
        )
        scene_texts.append(
            _render_scene_text(
                cue,
                geometry=geometry,
                render_width=render_width,
                render_height=render_height,
                clip_start_us=source_start_us,
                clip_end_us=source_end_us,
                conform_plan=conform_plan,
            )
        )
    return ExportSnapshot(
        4 if scene_texts else (3 if subtitle_blurs else (2 if conform_segments else 1)),
        project.id,
        project.revision,
        start_us,
        end_us,
        int(probe.width),
        int(probe.height),
        video,
        tuple(audio),
        tuple(overlays),
        tuple(subtitles),
        settings,
        conform_segments,
        tuple(subtitle_blurs),
        tuple(scene_texts),
    )


def export_snapshot_json(snapshot: ExportSnapshot) -> dict:
    return asdict(snapshot)


def _render_clip(kind, muted, clip, path: Path) -> RenderClip:
    assert clip.source_range is not None
    return RenderClip(
        clip.id,
        clip.track_id,
        kind.value,
        str(path),
        clip.timeline_range.start_us,
        clip.timeline_range.end_us,
        clip.source_range.start_us,
        clip.source_range.end_us,
        clip.speed.numerator,
        clip.speed.denominator,
        clip.volume,
        muted,
    )


def _subtitle_blur_geometry(
    cue: SubtitleCue,
    *,
    padding_x: float,
    padding_y: float,
    fallback_y: float,
) -> tuple[float, float, float, float, str]:
    if cue.visual_box is not None:
        x, y, width, height = cue.visual_box
        source = cue.visual_box_source or "ocr"
    else:
        lines = [line.strip() for line in cue.text.splitlines() if line.strip()] or [cue.text]
        longest = max(len(line) for line in lines)
        width = min(0.92, max(0.24, longest * 0.043))
        height = min(0.24, max(0.075, len(lines) * 0.068))
        x = (1.0 - width) / 2.0
        y = min(max(0.0, fallback_y), 1.0 - height)
        source = "estimated"
    left = max(0.0, x - padding_x)
    top = max(0.0, y - padding_y)
    right = min(1.0, x + width + padding_x)
    bottom = min(1.0, y + height + padding_y)
    return left, top, right - left, bottom - top, source


def _render_scene_text(
    cue: SubtitleCue,
    *,
    geometry: SubtitleCue,
    render_width: int,
    render_height: int,
    clip_start_us: int,
    clip_end_us: int,
    conform_plan,
) -> RenderSceneText:
    if geometry.visual_box is None:
        raise ValueError(
            f"Chữ trong cảnh cue {cue.index} chưa có vùng OCR để che và đặt bản dịch."
        )
    glyph_x, glyph_y, glyph_width, glyph_height = geometry.visual_box
    if geometry.panel_box is not None:
        panel_x, panel_y, panel_width, panel_height = geometry.panel_box
    else:
        padding_x = max(0.012, glyph_width * 0.12)
        padding_y = max(0.008, glyph_height * 0.16)
        panel_x = max(0.0, glyph_x - padding_x)
        panel_y = max(0.0, glyph_y - padding_y)
        panel_right = min(1.0, glyph_x + glyph_width + padding_x)
        panel_bottom = min(1.0, glyph_y + glyph_height + padding_y)
        panel_width = panel_right - panel_x
        panel_height = panel_bottom - panel_y
    rendered_text = _scene_text_layout(cue.text, geometry.orientation)
    font_size = _scene_text_font_size(
        rendered_text,
        orientation=geometry.orientation,
        panel_width_px=max(1, round(panel_width * render_width)),
        panel_height_px=max(1, round(panel_height * render_height)),
        glyph_width_px=max(1, round(glyph_width * render_width)),
        glyph_height_px=max(1, round(glyph_height * render_height)),
        font_scale=geometry.scene_font_scale * geometry.text_font_size / 12.0,
        style_hint=geometry.style_hint,
    )
    cue_start = max(clip_start_us, cue.range.start_us)
    cue_end = min(clip_end_us, cue.range.end_us)
    if conform_plan is not None:
        cue_start = conform_plan.source_to_output_us(cue_start)
        cue_end = conform_plan.source_to_output_us(cue_end)
    return RenderSceneText(
        cue.id,
        cue.index,
        cue_start,
        cue_end,
        rendered_text,
        glyph_x,
        glyph_y,
        glyph_width,
        glyph_height,
        panel_x,
        panel_y,
        panel_width,
        panel_height,
        geometry.orientation,
        geometry.style_hint,
        _cue_font_family(geometry.style_hint, "Be Vietnam Pro"),
        font_size,
        geometry.background_color,
        geometry.visual_box_source or "ocr",
        geometry.review_state,
        30 if geometry.review_state == "accepted" else 24,
        geometry.text_color,
        geometry.background_padding_x,
        geometry.background_padding_y,
        geometry.background_style,
        geometry.letter_spacing_percent,
        geometry.font_bold,
        geometry.font_italic,
        geometry.text_opacity_percent,
        geometry.background_opacity_percent,
        geometry.fade_enabled,
        geometry.fade_duration_ms,
    )


def _scene_text_layout(text: str, orientation: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    compact = " ".join(lines)
    if orientation != "vertical":
        return "\n".join(lines) or compact
    words = compact.split()
    return "\n".join(words if len(words) > 1 else tuple(compact))


def _scene_text_font_size(
    text: str,
    *,
    orientation: str,
    panel_width_px: int,
    panel_height_px: int,
    glyph_width_px: int,
    glyph_height_px: int,
    font_scale: float = 1.0,
    style_hint: str = "display",
) -> int:
    lines = [line for line in text.splitlines() if line] or [text]
    longest = max(1, max(len(line) for line in lines))
    # Qt Quick and libass resolve the bundled Playwrite variable font with very
    # different metrics.  These libass ratios come from raster probes of the
    # packaged font; QML uses its own Qt-specific ratios.  Keeping engine-aware
    # fit metrics produces comparable visible bounds instead of making the ASS
    # export roughly 2.5x smaller than Preview.  The panel clip remains the
    # final no-overflow safety net.
    if style_hint.startswith("calligraphy"):
        width_ratio = 0.90
        line_height_ratio = 0.95
    else:
        width_ratio = 1.28
        line_height_ratio = 1.32
    if orientation == "vertical":
        panel_by_width = panel_width_px * 0.88 / max(1.0, longest * width_ratio)
        panel_by_height = panel_height_px * 0.88 / max(
            1.25, len(lines) * line_height_ratio
        )
        source_size = min(
            glyph_width_px * 0.92,
            glyph_height_px / max(1.0, len(lines) * 1.08),
        )
    else:
        panel_by_width = panel_width_px * 0.88 / max(1.0, longest * width_ratio)
        panel_by_height = panel_height_px * 0.88 / max(
            1.0, len(lines) * line_height_ratio
        )
        source_size = glyph_height_px * 0.92
    panel_fit = min(panel_by_width, panel_by_height)
    automatic_size = min(source_size, panel_fit)
    # The current editor exposes a Word-like 1–100 size. ``12`` preserves the
    # automatic OCR fit, while larger/smaller values scale it monotonically.
    # Legacy 0.5–2.0 scene scale remains multiplied in.
    scaled_size = automatic_size * max(1 / 24, min(50 / 3, float(font_scale)))
    # The panel is a placement handle, not a hidden font-size clamp. The video
    # frame remains the final paint boundary, so moving or shrinking the handle
    # never silently reduces an explicitly selected Word-like font size.
    safe_render_max = max(
        panel_width_px,
        panel_height_px,
        glyph_width_px,
        glyph_height_px,
    ) * 8
    return max(1, int(min(scaled_size, safe_render_max)))
