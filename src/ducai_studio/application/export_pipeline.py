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
    RenderSubtitle,
)
from ducai_studio.domain.project import ProjectSnapshot, TrackKind


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
    for overlay in project.overlays:
        if (
            not overlay.enabled
            or overlay.range.end_us <= start_us
            or overlay.range.start_us >= end_us
        ):
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
    source_start_us = video_clip.timeline_range.start_us
    source_end_us = video_clip.timeline_range.end_us
    for cue in raw_cues:
        if cue.range.end_us <= source_start_us or cue.range.start_us >= source_end_us:
            continue
        cue_start = max(source_start_us, cue.range.start_us)
        cue_end = min(source_end_us, cue.range.end_us)
        if conform_plan is not None:
            cue_start = conform_plan.source_to_output_us(cue_start)
            cue_end = conform_plan.source_to_output_us(cue_end)
        subtitles.append(RenderSubtitle(cue.id, cue.index, cue_start, cue_end, cue.text))
    return ExportSnapshot(
        2 if conform_segments else 1,
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
