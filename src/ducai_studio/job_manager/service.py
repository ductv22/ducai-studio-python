from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, replace
from enum import Enum
from hashlib import sha256
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from ducai_studio.application.checkpoint_commands import CommitCheckpointCommand
from ducai_studio.application.effects_commands import EffectsEditor
from ducai_studio.application.export_pipeline import (
    build_export_snapshot,
    export_snapshot_json,
)
from ducai_studio.application.job_commands import decode_job_spec
from ducai_studio.application.media_commands import MediaImportService, MediaProbe
from ducai_studio.application.ocr_pipeline import OCR_AGGREGATION_VERSION
from ducai_studio.application.subtitle_commands import (
    SubtitleEditor,
    allocate_scene_text_lanes,
)
from ducai_studio.application.subtitle_finalizer import render_srt
from ducai_studio.application.timeline_commands import TimelineEditor
from ducai_studio.application.translation_pipeline import (
    ChunkState,
    TranslationLedger,
    TranslationQaIssue,
    TranslationQaSeverity,
    contains_cjk,
    ledger_from_dict,
    ledger_to_dict,
    translation_qa_snapshot,
)
from ducai_studio.application.voice_pipeline import (
    VoicePipelineSettings,
    build_voice_timeline_rows,
    measured_voice_timing_issues,
)
from ducai_studio.domain.artifacts import ArtifactState
from ducai_studio.domain.effects import ExportSettings, SubtitleStyle, VideoFilterSettings
from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.domain.jobs import (
    TERMINAL_JOB_STATES,
    JobKind,
    JobRecord,
    JobState,
    redact_sensitive_text,
    thaw_json,
)
from ducai_studio.domain.multi_video import (
    MultiBatchState,
    MultiItemState,
    MultiStage,
    worker_progress_fraction,
)
from ducai_studio.domain.project import (
    CUE_BACKGROUND_STYLES,
    CUE_ORIENTATIONS,
    CUE_REVIEW_STATES,
    CUE_ROLES,
    CUE_STYLE_HINTS,
    SCENE_TEXT_DEFAULT_BACKGROUND_COLOR,
    SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT,
    SCENE_TEXT_DEFAULT_BACKGROUND_STYLE,
    SCENE_TEXT_DEFAULT_FADE_DURATION_MS,
    SCENE_TEXT_DEFAULT_FADE_ENABLED,
    SCENE_TEXT_DEFAULT_FONT_SIZE,
    SCENE_TEXT_DEFAULT_STYLE_HINT,
    SCENE_TEXT_DEFAULT_TEXT_COLOR,
    TRANSLATION_STYLE_PRESETS,
    MediaKind,
    Overlay,
    OverlayKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
    TranslationPreferences,
    TranslationStylePreset,
    dialogue_cues,
    subtitle_ranges_overlap,
    subtitle_track_number,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.domain.timeline_edit import (
    build_scan_plan,
    project_for_timeline,
    range_on_scan_timeline,
    range_on_timeline,
    scan_time_to_base_us,
    timeline_to_base_us,
)
from ducai_studio.domain.voice_conform import (
    VoiceConformCueInput,
    VoiceConformSettings,
    VoiceTimingMode,
    build_voice_conform_plan,
    decode_voice_conform_plan,
)
from ducai_studio.infrastructure.checkpoint_repository import CheckpointRepository
from ducai_studio.infrastructure.credential_store import WindowsCredentialStore
from ducai_studio.infrastructure.database import Database
from ducai_studio.infrastructure.database_optimizer import (
    finalize_verified_optimizer_backup,
    optimize_project_database,
)
from ducai_studio.infrastructure.job_repository import JobRepository, utc_now_text
from ducai_studio.infrastructure.legacy_importer import LegacyProjectImporter
from ducai_studio.infrastructure.multi_video_repository import MultiVideoRepository
from ducai_studio.infrastructure.project_repository import ProjectRepository
from ducai_studio.infrastructure.runtime_diagnostics import run_runtime_diagnostics
from ducai_studio.infrastructure.runtime_program import runtime_program_path
from ducai_studio.infrastructure.whisper_runtime import (
    WHISPER_MODEL_ID,
    WHISPER_RUNTIME_VERSION,
    whisper_model_fingerprint,
    whisper_model_path,
    whisper_runtime_is_ready,
)
from ducai_studio.providers.media_probe import FfprobeProvider
from ducai_studio.providers.ocr import DEFAULT_SUBTITLE_BANDS
from ducai_studio.providers.translation.openai_compatible import (
    OPENAI_COMPATIBLE_PROVIDER_IDS,
    preset_for,
    validate_base_url,
)
from ducai_studio.providers.tts import TtsCueRequest, VoiceCatalog, discover_data_root
from ducai_studio.providers.tts.vieneu import profile_fingerprint

TRANSLATION_API_PROVIDER_IDS = frozenset({"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS})

_TTS_INPUT_FINGERPRINT_SCHEMA = "ducai-tts-input-v2"
_TTS_INPUT_SETTING_KEYS = (
    "voice_id",
    "voice_identity",
    "reference_denoise",
    "engine_speed",
    "voice_read_speed",
    "target_lufs",
    "pipeline_settings",
    "qa_non_blocking",
    "defer_timing_conform",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _translation_cue_payload(cue: SubtitleCue) -> dict[str, object]:
    return {
        "id": cue.id,
        "index": cue.index,
        "start_us": cue.range.start_us,
        "end_us": cue.range.end_us,
        "text": cue.text,
        "language": cue.language,
        "revision": cue.revision,
        "source": cue.source,
        "confidence": cue.confidence,
        "evidence_ids": list(cue.evidence_ids),
        "visual_box": list(cue.visual_box) if cue.visual_box is not None else None,
        "visual_box_source": cue.visual_box_source,
        "role": cue.role,
        "orientation": cue.orientation,
        "style_hint": cue.style_hint,
        "panel_box": list(cue.panel_box) if cue.panel_box is not None else None,
        "background_color": cue.background_color,
        "text_color": cue.text_color,
        "review_state": cue.review_state,
        "scene_font_scale": cue.scene_font_scale,
        "text_font_size": cue.text_font_size,
        "subtitle_track_id": cue.subtitle_track_id,
        "background_padding_x": cue.background_padding_x,
        "background_padding_y": cue.background_padding_y,
        "background_style": cue.background_style,
        "letter_spacing_percent": cue.letter_spacing_percent,
        "font_bold": cue.font_bold,
        "font_italic": cue.font_italic,
        "text_opacity_percent": cue.text_opacity_percent,
        "background_opacity_percent": cue.background_opacity_percent,
        "fade_enabled": cue.fade_enabled,
        "fade_duration_ms": cue.fade_duration_ms,
    }


def _tts_input_fingerprint(
    cues: list[dict[str, Any]],
    *,
    provider_id: str,
    provider_version: str,
    model_id: str,
    provider_config: Mapping[str, Any],
) -> str:
    """Fingerprint every semantic input that can change production speech.

    Runtime paths, output paths, timeouts and batching are deliberately absent:
    they affect execution but not the voice waveform. Voice identity already
    carries the enrolled profile fingerprint for cloned voices.
    """

    return stable_fingerprint(
        {
            "schema": _TTS_INPUT_FINGERPRINT_SCHEMA,
            "cues": cues,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "model_id": model_id,
            "settings": {
                key: provider_config[key]
                for key in _TTS_INPUT_SETTING_KEYS
                if key in provider_config
            },
        }
    )


def _voice_subtitle_pairs(
    snapshot: ProjectSnapshot,
) -> tuple[tuple[SubtitleCue, SubtitleCue], ...]:
    """Join saved subtitle identities without depending on presentation order."""
    source = snapshot.source_cues
    translated = snapshot.translated_cues
    if len(source) != len(translated):
        raise ValueError("Bản dịch chưa khớp đủ các cue gốc; hãy kiểm tra các cue chưa dịch.")
    translated_by_id = {cue.id: cue for cue in translated}
    if len({cue.id for cue in source}) != len(source) or len(translated_by_id) != len(translated):
        raise ValueError("Dữ liệu phụ đề bị lặp mã cue; không thể ghép giọng một cách an toàn.")
    pairs = []
    for cue in source:
        translation = translated_by_id.get(cue.id)
        if translation is None or translation.index != cue.index:
            raise ValueError(
                f"Cue {cue.index}: bản dịch chưa khớp định danh với cue gốc; "
                "hãy kiểm tra cặp phụ đề này."
            )
        pairs.append((cue, translation))
    return tuple(pairs)


def _apply_ocr_cue_regions(
    snapshot: ProjectSnapshot,
    qa: Mapping[str, object],
) -> ProjectSnapshot:
    """Attach OCR cue geometry and repeat-proven platform-mark masks."""

    def normalized_box(value: object) -> tuple[float, float, float, float] | None:
        if (
            not isinstance(value, list)
            or len(value) != 4
            or not all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in value
            )
        ):
            return None
        x, y, width, height = (float(item) for item in value)
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            return None
        if x + width > 1.000001 or y + height > 1.000001:
            return None
        return x, y, width, height

    def enum_value(value: object, fallback: str, allowed: frozenset[str]) -> str:
        return value if isinstance(value, str) and value in allowed else fallback

    def color_value(value: object, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        if len(value) != 7 or not value.startswith("#"):
            return fallback
        try:
            int(value[1:], 16)
        except ValueError:
            return fallback
        return value.upper()

    def integer_value(value: object, fallback: int, minimum: int, maximum: int) -> int:
        if type(value) is not int:
            return fallback
        return max(minimum, min(maximum, value))

    raw_regions = qa.get("cue_regions", [])
    if not isinstance(raw_regions, list):
        raw_regions = []
    by_index: dict[int, Mapping[str, object]] = {
        int(item["index"]): item
        for item in raw_regions
        if isinstance(item, Mapping) and type(item.get("index")) is int
    }
    cues = []
    for cue in snapshot.source_cues:
        region = by_index.get(cue.index)
        if region is None:
            cues.append(cue)
            continue
        box = normalized_box(region.get("box"))
        panel_box = normalized_box(region.get("panel_box"))
        timestamps_match = bool(
            region.get("start_us") == cue.range.start_us
            and region.get("end_us") == cue.range.end_us
        )
        role = enum_value(region.get("role"), cue.role, CUE_ROLES)
        font_size = region.get("text_font_size")
        if type(font_size) is not int or font_size < 1:
            font_size = (
                SCENE_TEXT_DEFAULT_FONT_SIZE
                if role == "scene_text" and not cue.is_scene_text
                else cue.text_font_size
            )
        cues.append(
            replace(
                cue,
                confidence=(float(region["confidence"]) if timestamps_match
                    and isinstance(region.get("confidence"), (int, float))
                    and not isinstance(region.get("confidence"), bool)
                    and 0 <= region["confidence"] <= 1 else cue.confidence),
                evidence_ids=(tuple(dict.fromkeys(region["evidence_ids"]))
                    if timestamps_match and isinstance(region.get("evidence_ids"), list)
                    and all(isinstance(key, str) and key for key in region["evidence_ids"])
                    else cue.evidence_ids),
                visual_box=box if timestamps_match else None,
                visual_box_source="ocr" if timestamps_match and box is not None else "",
                role=(
                    role
                    if timestamps_match
                    else cue.role
                ),
                text_font_size=font_size if timestamps_match else cue.text_font_size,
                orientation=(
                    enum_value(
                        region.get("orientation"), cue.orientation, CUE_ORIENTATIONS
                    )
                    if timestamps_match
                    else cue.orientation
                ),
                style_hint=(
                    enum_value(
                        region.get("style_hint"),
                        SCENE_TEXT_DEFAULT_STYLE_HINT
                        if enum_value(region.get("role"), cue.role, CUE_ROLES)
                        == "scene_text"
                        else cue.style_hint,
                        CUE_STYLE_HINTS,
                    )
                    if timestamps_match
                    else cue.style_hint
                ),
                panel_box=panel_box if timestamps_match else cue.panel_box,
                background_color=(
                    color_value(
                        region.get("background_color"),
                        SCENE_TEXT_DEFAULT_BACKGROUND_COLOR
                        if enum_value(region.get("role"), cue.role, CUE_ROLES)
                        == "scene_text"
                        else cue.background_color,
                    )
                    if timestamps_match
                    else cue.background_color
                ),
                text_color=(
                    color_value(
                        region.get("text_color"),
                        SCENE_TEXT_DEFAULT_TEXT_COLOR
                        if enum_value(region.get("role"), cue.role, CUE_ROLES)
                        == "scene_text"
                        else cue.text_color,
                    )
                    if timestamps_match
                    else cue.text_color
                ),
                background_style=(
                    enum_value(
                        region.get("background_style"),
                        SCENE_TEXT_DEFAULT_BACKGROUND_STYLE
                        if enum_value(region.get("role"), cue.role, CUE_ROLES)
                        == "scene_text"
                        else cue.background_style,
                        CUE_BACKGROUND_STYLES,
                    )
                    if timestamps_match
                    else cue.background_style
                ),
                background_opacity_percent=(
                    integer_value(
                        region.get("background_opacity_percent"),
                        SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT
                        if enum_value(region.get("role"), cue.role, CUE_ROLES)
                        == "scene_text"
                        else cue.background_opacity_percent,
                        0,
                        100,
                    )
                    if timestamps_match
                    else cue.background_opacity_percent
                ),
                fade_enabled=(
                    (
                        region.get("fade_enabled")
                        if type(region.get("fade_enabled")) is bool
                        else (
                            SCENE_TEXT_DEFAULT_FADE_ENABLED
                            if enum_value(region.get("role"), cue.role, CUE_ROLES)
                            == "scene_text"
                            else cue.fade_enabled
                        )
                    )
                    if timestamps_match
                    else cue.fade_enabled
                ),
                fade_duration_ms=(
                    integer_value(
                        region.get("fade_duration_ms"),
                        SCENE_TEXT_DEFAULT_FADE_DURATION_MS,
                        50,
                        2_000,
                    )
                    if timestamps_match
                    else cue.fade_duration_ms
                ),
                review_state=(
                    enum_value(
                        region.get("review_state"), cue.review_state, CUE_REVIEW_STATES
                    )
                    if timestamps_match
                    else cue.review_state
                ),
            )
        )
    source_cues = allocate_scene_text_lanes(cues)
    updated = replace(
        snapshot,
        source_cues=source_cues,
        tracks=_split_subtitle_tracks_by_role(snapshot.tracks, source_cues),
    )

    # Older QA payloads do not carry this key. Preserve their project overlays
    # exactly. A new OCR run does carry it (including an empty list), so its
    # repeat-proven masks replace the previous generated set atomically.
    if "watermark_regions" not in qa:
        return updated
    raw_watermarks = qa.get("watermark_regions")
    if not isinstance(raw_watermarks, list):
        return updated

    retained = tuple(
        overlay
        for overlay in updated.overlays
        if overlay.tracking_mode != "ocr_watermark"
    )
    retained_ids = {overlay.id for overlay in retained}
    generated: list[Overlay] = []
    for index, item in enumerate(raw_watermarks, start=1):
        if not isinstance(item, Mapping):
            continue
        raw_box = item.get("box")
        start_us = item.get("start_us")
        end_us = item.get("end_us")
        sample_count = item.get("sample_count")
        if (
            not isinstance(raw_box, list)
            or len(raw_box) != 4
            or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in raw_box
            )
            or type(start_us) is not int
            or type(end_us) is not int
            or start_us < 0
            or end_us <= start_us
            or type(sample_count) is not int
            or sample_count < 3
            or item.get("source") != "ocr_bilibili_anchor"
        ):
            continue
        try:
            x, y, width, height = (float(value) for value in raw_box)
            overlay_id = f"overlay-ocr-watermark-{index}"
            while overlay_id in retained_ids:
                overlay_id += "-auto"
            retained_ids.add(overlay_id)
            generated.append(
                Overlay(
                    id=overlay_id,
                    kind=OverlayKind.BLUR,
                    range=TimeRange(start_us, end_us),
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    enabled=True,
                    opacity=1.0,
                    blur_radius=46,
                    tracking_mode="ocr_watermark",
                    padding_x=0.0,
                    padding_y=0.0,
                    concealment_mode="cover",
                )
            )
        except (TypeError, ValueError):
            # QA is evidence, not authority over the project contract. Ignore
            # malformed geometry rather than making an otherwise valid OCR job
            # impossible to publish.
            continue
    return replace(updated, overlays=retained + tuple(generated))


def _split_subtitle_tracks_by_role(
    tracks: tuple[TimelineTrack, ...],
    cues: tuple[SubtitleCue, ...],
) -> tuple[TimelineTrack, ...]:
    """Project accepted OCR scene labels onto S2 without inventing new clips.

    The guarded OCR import has just produced one S1 clip for every cue in the
    same order. Reassigning those immutable clips keeps their IDs, source media
    and cache references stable while exposing scene text as a separate lane.
    Legacy/manual subtitle tracks that do not match that contract are retained
    unchanged instead of being guessed at.
    """

    subtitle_tracks = tuple(track for track in tracks if track.kind is TrackKind.SUBTITLE)
    clips = tuple(clip for track in subtitle_tracks for clip in track.clips)
    if not subtitle_tracks or len(clips) != len(cues):
        return tracks
    if any(
        clip.timeline_range != cue.range or clip.source_range != cue.range
        for clip, cue in zip(clips, cues, strict=True)
    ):
        return tracks
    clips_by_track: dict[str, list[TimelineClip]] = {}
    for clip, cue in zip(clips, cues, strict=True):
        track_id = cue.subtitle_track_id or ("S1" if cue.is_dialogue else "S2")
        clips_by_track.setdefault(track_id, []).append(
            replace(
                clip,
                track_id=track_id,
                cue_id=cue.id,
                cue_index=cue.index,
                cue_text=cue.text,
            )
        )
    retained = tuple(track for track in tracks if track.kind is not TrackKind.SUBTITLE)
    projected: list[TimelineTrack] = []
    for track_id in sorted(
        clips_by_track,
        key=lambda value: int(value[1:]) if value[1:].isdigit() else 10_000,
    ):
        projected.append(
            TimelineTrack(
                track_id,
                TrackKind.SUBTITLE,
                "Phụ đề nguồn" if track_id == "S1" else "Chữ trong cảnh",
                tuple(clips_by_track[track_id]),
            )
        )
    return retained + tuple(projected)


def _merge_preserved_manual_subtitle_cues(
    previous: ProjectSnapshot,
    imported: ProjectSnapshot,
) -> ProjectSnapshot:
    """Keep user-authored dialogue and scene cues across OCR replacement.

    OCR owns every generated S1/S2 cue, so a rerun must replace those rows.
    Manual cues are project editing data rather than OCR output and are appended
    after the newly imported sequence with fresh contiguous indexes. Scene text
    receives fresh S2+ lanes; dialogue keeps its explicit lane when available
    and moves to the next free lane on collision without losing its spoken role.
    """

    manual_source = [
        cue
        for cue in previous.source_cues
        if cue.source in {"manual_dialogue", "manual_scene_text"}
    ]
    if not manual_source:
        return imported
    manual_ids = {cue.id for cue in manual_source}
    translated_by_id = {
        cue.id: cue
        for cue in previous.translated_cues
        if cue.id in manual_ids
    }
    next_index = len(imported.source_cues) + 1
    preserved_source: list[SubtitleCue] = []
    preserved_translated: list[SubtitleCue] = []
    cue_index_by_id: dict[str, int] = {}
    cue_text_by_id: dict[str, str] = {}
    generated_highest_lane = max(
        (
            int(cue.subtitle_track_id[1:])
            for cue in imported.source_cues
            if cue.is_scene_text
            and cue.subtitle_track_id.startswith("S")
            and cue.subtitle_track_id[1:].isdigit()
        ),
        default=1,
    )
    occupied: dict[int, list[TimeRange]] = {}
    for cue in imported.source_cues:
        lane = subtitle_track_number(
            cue.subtitle_track_id,
            fallback=1 if cue.is_dialogue else 2,
        )
        occupied.setdefault(lane, []).append(cue.range)
    for offset, cue in enumerate(manual_source):
        index = next_index + offset
        if cue.is_scene_text:
            lane = max(2, generated_highest_lane + 1)
        else:
            lane = max(
                1,
                subtitle_track_number(cue.subtitle_track_id, fallback=1),
            )
        while any(
            subtitle_ranges_overlap(cue.range, item)
            for item in occupied.get(lane, ())
        ):
            lane += 1
        occupied.setdefault(lane, []).append(cue.range)
        if cue.is_scene_text:
            generated_highest_lane = max(generated_highest_lane, lane)
        track_id = f"S{lane}"
        normalized = replace(cue, index=index, subtitle_track_id=track_id)
        preserved_source.append(normalized)
        cue_index_by_id[cue.id] = index
        translated = translated_by_id.get(cue.id)
        if translated is not None:
            translated = replace(
                translated,
                index=index,
                range=normalized.range,
                subtitle_track_id=track_id,
            )
            preserved_translated.append(translated)
            cue_text_by_id[cue.id] = translated.text
        else:
            cue_text_by_id[cue.id] = normalized.text

    tracks = []
    for track in imported.tracks:
        if track.kind is not TrackKind.SUBTITLE:
            tracks.append(track)
            continue
        retained_clips = tuple(clip for clip in track.clips if clip.cue_id not in manual_ids)
        if retained_clips:
            tracks.append(replace(track, clips=retained_clips))
    manual_clips_by_track: dict[str, list[TimelineClip]] = {}
    for track in previous.tracks:
        if track.kind is not TrackKind.SUBTITLE:
            continue
        for clip in track.clips:
            if clip.cue_id not in manual_ids:
                continue
            track_id = next(
                (
                    cue.subtitle_track_id
                    for cue in preserved_source
                    if cue.id == clip.cue_id
                ),
                "S2",
            ) or "S2"
            manual_clips_by_track.setdefault(track_id, []).append(
                replace(
                    clip,
                    track_id=track_id,
                    cue_index=cue_index_by_id[clip.cue_id],
                    cue_text=cue_text_by_id[clip.cue_id],
                )
            )
    for track_id, manual_clips in manual_clips_by_track.items():
        track_index = next(
            (
                index
                for index, track in enumerate(tracks)
                if track.kind is TrackKind.SUBTITLE and track.id == track_id
            ),
            None,
        )
        if track_index is None:
            tracks.append(
                TimelineTrack(
                    track_id,
                    TrackKind.SUBTITLE,
                    "Phụ đề thoại"
                    if any(
                        cue.subtitle_track_id == track_id and cue.is_dialogue
                        for cue in preserved_source
                    )
                    else "Chữ trong cảnh",
                    tuple(manual_clips),
                )
            )
            continue
        track = tracks[track_index]
        tracks[track_index] = replace(
            track,
            clips=tuple(
                sorted(
                    (*track.clips, *manual_clips),
                    key=lambda item: (
                        item.timeline_range.start_us,
                        item.timeline_range.end_us,
                        item.id,
                    ),
                )
            ),
        )
    all_source = (*imported.source_cues, *preserved_source)
    roles_by_track: dict[str, set[str]] = {}
    for cue in all_source:
        track_id = cue.subtitle_track_id or ("S1" if cue.is_dialogue else "S2")
        roles_by_track.setdefault(track_id, set()).add(cue.role)
    tracks = [
        replace(
            track,
            name=(
                "Phụ đề thoại"
                if roles_by_track.get(track.id) == {"dialogue"}
                else (
                    "Chữ trong cảnh"
                    if roles_by_track.get(track.id) == {"scene_text"}
                    else "Phụ đề / Chữ trong cảnh"
                )
            ),
        )
        if track.kind is TrackKind.SUBTITLE
        else track
        for track in tracks
    ]
    return replace(
        imported,
        tracks=tuple(tracks),
        source_cues=all_source,
        translated_cues=tuple(preserved_translated),
    )


def _cue_content_changed(previous: SubtitleCue | None, current: SubtitleCue) -> bool:
    if previous is current:
        return False
    if previous is None:
        return True
    if previous.index == current.index:
        return previous != current
    return replace(previous, index=current.index) != current


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_storage_volume(source: Path, target: Path) -> bool:
    """Whether Windows can atomically rename between these two paths."""

    return source.drive.casefold() == target.drive.casefold()


@contextmanager
def _promote_output_files(
    pairs: tuple[tuple[Path, Path], ...],
):
    """Promote accepted worker candidates while preserving rollback safety.

    Workers write job-scoped files so a cancelled or failed rerun cannot touch
    the output currently used by the project.  The manager keeps a short-lived
    backup until the new project revision has been committed.  Legacy jobs
    whose candidate and active paths are identical remain a no-op here.
    """

    token = uuid4().hex
    promoted: list[tuple[Path, Path, Path | None, bool]] = []
    try:
        for raw_candidate, raw_active in pairs:
            candidate = raw_candidate.expanduser().resolve(strict=True)
            active = raw_active.expanduser().resolve()
            if candidate == active:
                continue
            active.parent.mkdir(parents=True, exist_ok=True)
            backup: Path | None = None
            if active.exists():
                backup = active.with_name(f".{active.name}.{token}.previous")
                os.replace(active, backup)
            copied = not _same_storage_volume(candidate, active)
            temporary = active.with_name(f".{active.name}.{token}.promoting")
            try:
                if copied:
                    shutil.copy2(candidate, temporary)
                    os.replace(temporary, active)
                else:
                    os.replace(candidate, active)
            except BaseException:
                temporary.unlink(missing_ok=True)
                if backup is not None and backup.exists():
                    os.replace(backup, active)
                raise
            promoted.append((candidate, active, backup, copied))
        yield
    except BaseException:
        for candidate, active, backup, copied in reversed(promoted):
            if active.exists():
                if copied:
                    active.unlink()
                else:
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(active, candidate)
            if backup is not None and backup.exists():
                os.replace(backup, active)
        raise
    else:
        for candidate, _active, backup, copied in promoted:
            if copied:
                candidate.unlink(missing_ok=True)
            if backup is not None:
                with suppress(OSError):
                    backup.unlink(missing_ok=True)


def _remove_internal_artifact_path(path: Path, root: Path) -> None:
    """Remove one verified job-owned artifact path or fail before DB deletion."""

    resolved_root = root.resolve()
    owned = path.resolve(strict=False)
    if owned.parent != resolved_root:
        raise RuntimeError("Refusing to clean an artifact path outside DUCAI data")

    # Immutable checkpoints are deliberately promoted with mode ``0444`` so a
    # worker cannot mutate a committed artifact. Deletion is an explicitly
    # authorized operation; make only this verified path writable long enough
    # for cleanup. On Windows, otherwise every read-only child raises
    # ``Access is denied`` and the history row remains visible.
    def make_writable(candidate: str | os.PathLike[str]) -> None:
        with suppress(OSError):
            os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)

    def remove_readonly(function, candidate: str, _exc_info) -> None:
        make_writable(candidate)
        function(candidate)

    if owned.is_symlink() or owned.is_file():
        make_writable(owned)
        owned.unlink()
    elif owned.exists():
        shutil.rmtree(owned, onerror=remove_readonly)
    if owned.exists():
        raise RuntimeError(f"Could not remove internal artifact path: {owned}")


def _media_path_identity(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).expanduser().resolve())))


def _first_track_media_path(
    snapshot: ProjectSnapshot,
    media_paths: Mapping[str, str],
    kind: TrackKind,
) -> str:
    for track in snapshot.tracks:
        if track.kind is not kind:
            continue
        for clip in track.clips:
            if clip.media_id is None:
                continue
            path = media_paths.get(clip.media_id, "")
            if path:
                return path
    return ""


def _credential_reference(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Credential reference must be a descriptive name")
    name = value.strip()
    if (
        not name
        or len(name) > 80
        or any(character in name for character in "\r\n\0=")
        or redact_sensitive_text(name) != name
    ):
        raise ValueError("Credential reference must be a descriptive name, not a secret value")
    return name


def _engine_host_path(filename: str) -> Path:
    source_path = Path(__file__).parents[1] / "engine_hosts" / filename
    if source_path.is_file():
        return source_path.resolve()
    packaged_path = runtime_program_path().parent / "engine_hosts" / filename
    compiled_path = packaged_path.with_suffix(".pyc")
    if compiled_path.is_file():
        return compiled_path.resolve()
    return packaged_path.resolve(strict=True)


def _external_runtime_fingerprint(
    runtime: Path,
    *,
    package_patterns: tuple[str, ...] | None = None,
) -> str:
    site_packages = runtime.parents[1] / "Lib" / "site-packages"
    packages: list[dict[str, object]] = []
    if site_packages.is_dir():
        patterns = package_patterns or (
            "paddleocr-*.dist-info",
            "paddlepaddle*-*.dist-info",
            "rapidocr-*.dist-info",
        )
        for pattern in patterns:
            for metadata in sorted(site_packages.glob(pattern)):
                stat = metadata.stat()
                packages.append(
                    {
                        "name": metadata.name,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    }
                )
    runtime_stat = runtime.stat()
    return stable_fingerprint(
        {
            "python": str(runtime),
            "size": runtime_stat.st_size,
            "mtime_ns": runtime_stat.st_mtime_ns,
            "packages": packages,
        }
    )


def _paddle_model_fingerprint() -> str:
    configured_cache = os.environ.get("PADDLE_PDX_CACHE_HOME", "").strip()
    candidates = [
        Path(configured_cache).expanduser() / "official_models" if configured_cache else None,
        discover_data_root() / "paddle_model_cache" / "official_models",
        Path.home() / ".paddlex" / "official_models",
    ]
    root = next(
        (candidate.resolve() for candidate in candidates if candidate and candidate.is_dir()),
        candidates[-1],
    )
    model_names = {
        "PP-OCRv6_tiny_det",
        "PP-OCRv6_small_rec",
        "PP-OCRv6_medium_det",
        "PP-OCRv6_medium_rec",
    }
    entries: list[dict[str, object]] = []
    if root.is_dir():
        for model_name in sorted(model_names):
            model_root = root / model_name
            if not model_root.is_dir():
                continue
            for path in sorted(item for item in model_root.rglob("*") if item.is_file()):
                stat = path.stat()
                entries.append(
                    {
                        "model": model_name,
                        "path": str(path.relative_to(model_root)),
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    }
                )
    return stable_fingerprint(entries)


class JobManagerService:
    def __init__(
        self,
        *,
        database_path: str | Path,
        artifact_root: str | Path,
        media_probe: MediaProbe | None = None,
    ) -> None:
        self.database = Database(database_path)
        self.database.initialize(applied_at=utc_now_text())
        finalize_verified_optimizer_backup(self.database.path)
        self.jobs = JobRepository(self.database)
        self.projects = ProjectRepository(self.database)
        self.multi_videos = MultiVideoRepository(self.database)
        self.multi_videos.ensure_primary()
        self.checkpoints = CheckpointRepository(self.database, artifact_root)
        self.media_probe = media_probe or FfprobeProvider()
        self.media_imports = MediaImportService(self.media_probe)
        self._timeline_editors: dict[str, TimelineEditor] = {}
        self._video_clipboards = {}
        self._subtitle_editors: dict[str, SubtitleEditor] = {}
        self._effects_editors: dict[str, EffectsEditor] = {}
        # Approved production WAVs and their ledger are immutable cache inputs.
        # Keep the validated cue map in memory so moving the conform controls
        # does not re-hash the same 1--2 GB of WAV data for every estimate.
        self._voice_conform_base_cache: dict[
            str,
            tuple[
                str,
                int,
                int,
                int,
                tuple[VoiceConformCueInput, ...],
                tuple[dict[str, Any], ...],
            ],
        ] = {}
        # Manual Multi translation review is derived from the current project
        # revision.  Cache the small CJK/validity result by revision so the
        # lightweight 10 Hz status event never reparses the same snapshot.
        self._multi_manual_review_cache: dict[tuple[str, int, str], dict[str, Any]] = {}

    def recover_startup(self) -> int:
        recovered = self.jobs.reconcile_interrupted()
        self.multi_videos.mark_interrupted()
        return recovered

    def ensure_database_optimization_allowed(self) -> None:
        if self.jobs.active_jobs():
            raise ValueError(
                "Chỉ có thể tối ưu dữ liệu dự án khi không có tác vụ đang chạy hoặc tạm dừng."
            )
        batch_state = str(self.multi_videos.get_batch().get("state", "idle"))
        if batch_state in {"running", "pause_requested"}:
            raise ValueError("Hãy dừng Multi video tại checkpoint an toàn trước khi tối ưu.")

    def optimize_database(
        self,
        *,
        cancel_event: Event | None = None,
        progress_callback: Callable[[dict[str, int | str]], None] | None = None,
    ) -> dict[str, Any]:
        self.ensure_database_optimization_allowed()
        self.projects.flush_all_edits()
        self._timeline_editors.clear()
        self._subtitle_editors.clear()
        self._effects_editors.clear()
        return dict(
            optimize_project_database(
                self.database,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
        )

    def create_job(self, raw: dict[str, Any]) -> dict[str, Any]:
        project_id = raw.get("project_id")
        if isinstance(project_id, str) and self.projects.has_pending_edits(project_id):
            self.projects.flush_edits(project_id)
        return _json_value(asdict(self.jobs.create(decode_job_spec(raw))))

    def _ensure_no_active_stage(self, project_id: str, kind: JobKind) -> None:
        active_job_id = self.jobs.active_id(project_id=project_id, kind=kind)
        if not active_job_id:
            return
        label = {
            JobKind.OCR: "trích xuất SRT",
            JobKind.TRANSLATE: "dịch",
            JobKind.TTS: "lồng tiếng",
        }.get(kind, kind.value)
        raise ValueError(
            f"Dự án đã có một tác vụ {label} đang hoạt động; hãy tạm dừng, "
            "hủy hoặc xử lý tác vụ đó trước."
        )

    def _create_or_reuse_completed_job(
        self,
        raw: dict[str, Any],
        *,
        reuse_completed: bool,
        stage_output: bool = False,
    ) -> dict[str, Any]:
        """Reuse a matching successful job without touching its cache.

        Multi video uses this after a preflight fingerprint check.  Normal UI
        callers retain the historical behavior and always receive a new job.
        """

        spec = decode_job_spec(raw)
        # Staged outputs and TTS replacements also reference immutable revisions.
        # Persist the working input before creating any job record or reusing output.
        if self.projects.has_pending_edits(spec.project_id):
            self.projects.flush_edits(spec.project_id)
        if reuse_completed:
            for candidate in self.jobs.list_project_jobs(project_id=spec.project_id):
                if (
                    candidate.state is JobState.SUCCEEDED
                    and candidate.spec.kind is spec.kind
                    and candidate.spec.provider_id == spec.provider_id
                    and candidate.spec.provider_version == spec.provider_version
                    and candidate.spec.model_id == spec.model_id
                    and candidate.spec.input_fingerprint == spec.input_fingerprint
                    and candidate.spec.settings_fingerprint == spec.settings_fingerprint
                    and self._completed_job_owns_active_output(candidate)
                ):
                    return {**_json_value(asdict(candidate)), "reused": True}
        if not stage_output:
            return {**self.create_job(raw), "reused": False}

        job_id = str(uuid4())
        provider_config = dict(raw.get("provider_config", {}))
        output_raw = provider_config.get("output_path")
        if not isinstance(output_raw, str) or not output_raw.strip():
            raise ValueError("A staged job requires a stable output path")
        active_output = Path(output_raw).expanduser().resolve()
        staging_directory = self.checkpoints.staging_root / job_id
        if not _same_storage_volume(staging_directory, active_output):
            # Keep large final masters on the media volume so accepting a
            # rerun never performs a second multi-gigabyte cross-drive copy.
            # Checkpoints remain in the managed artifact root.
            staging_directory = active_output.parent / ".ducai-staging" / job_id
        provider_config["staging_output_path"] = str(staging_directory / active_output.name)
        qa_raw = provider_config.get("qa_path")
        if isinstance(qa_raw, str) and qa_raw.strip():
            provider_config["staging_qa_path"] = str(staging_directory / Path(qa_raw).name)
        staged_raw = {**raw, "provider_config": provider_config}
        record = self.jobs.create(decode_job_spec(staged_raw), job_id=job_id)
        return {**_json_value(asdict(record)), "reused": False}

    @staticmethod
    def _completed_job_owns_active_output(job: Any) -> bool:
        """Reject reuse when a newer rerun has replaced the stable file."""

        config = job.spec.provider_config
        output_raw = config.get("output_path")
        if not isinstance(output_raw, str) or not output_raw.strip():
            return False
        output = Path(output_raw).expanduser().resolve()
        qa_raw = config.get("qa_path")
        qa_path = (
            Path(qa_raw).expanduser().resolve()
            if isinstance(qa_raw, str) and qa_raw.strip()
            else output.with_name(output.stem + "_QA.json")
        )
        try:
            qa = json.loads(qa_path.read_text(encoding="utf-8-sig"))
            output_stat = output.stat()
        except (OSError, json.JSONDecodeError):
            return False
        stat_matches = bool(
            isinstance(qa, Mapping)
            and qa.get("output_size") == output_stat.st_size
            and qa.get("output_mtime_ns") == output_stat.st_mtime_ns
        )
        # Legacy provenance did not include a stat receipt. SRT/JSON-sized
        # outputs are cheap to verify once; never synchronously re-hash a
        # multi-gigabyte D1 merely to decide whether Multi may reuse it.
        hash_matches = False
        if not stat_matches and output_stat.st_size <= 16 * 1024 * 1024:
            try:
                hash_matches = qa.get("output_sha256") == _file_sha256(output)
            except OSError:
                return False
        return bool(
            isinstance(qa, Mapping)
            and qa.get("status") == "PASS"
            and qa.get("job_id") == job.id
            and qa.get("input_fingerprint") == job.spec.input_fingerprint
            and qa.get("settings_fingerprint") == job.spec.settings_fingerprint
            and (stat_matches or hash_matches)
        )

    def store_credential(self, *, reference: str, secret: str) -> dict[str, Any]:
        name = _credential_reference(reference)
        WindowsCredentialStore().write(name, secret)
        return {"reference": name, "stored": True}

    def list_credentials(self) -> dict[str, Any]:
        references: list[str] = []
        for raw in WindowsCredentialStore().list_references():
            try:
                references.append(_credential_reference(raw))
            except ValueError:
                continue
        return {"references": references}

    def store_credentials(self, *, credentials: object) -> dict[str, Any]:
        if not isinstance(credentials, list) or not 1 <= len(credentials) <= 32:
            raise ValueError("Credential batch must contain between 1 and 32 entries")
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in credentials:
            if not isinstance(item, Mapping):
                raise ValueError("Every credential entry must be an object")
            reference = item.get("reference")
            secret = item.get("secret")
            if not isinstance(secret, str) or not secret:
                raise ValueError("Every credential entry requires an API key")
            name = _credential_reference(reference)
            folded = name.casefold()
            if folded in seen:
                raise ValueError(f"Credential reference is duplicated: {name}")
            seen.add(folded)
            normalized.append((name, secret))
        store = WindowsCredentialStore()
        for reference, secret in normalized:
            store.write(reference, secret)
        return {
            "references": [reference for reference, _secret in normalized],
            "stored": len(normalized),
        }

    def delete_credential(self, *, reference: str) -> dict[str, Any]:
        deleted = WindowsCredentialStore().delete(reference)
        return {"reference": reference.strip(), "deleted": deleted}

    def refresh_translation_credential_references(
        self,
        *,
        job_id: str,
        credential_references: object,
    ) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if (
            job.spec.kind.value != "translate"
            or job.spec.provider_id not in TRANSLATION_API_PROVIDER_IDS
        ):
            raise ValueError("Only API translation jobs accept a refreshed key pool")
        if not isinstance(credential_references, list) or not 1 <= len(credential_references) <= 32:
            raise ValueError("Retry key pool must contain between 1 and 32 references")
        references: list[str] = []
        seen: set[str] = set()
        for raw in credential_references:
            reference = _credential_reference(raw)
            folded = reference.casefold()
            if folded in seen:
                raise ValueError(f"Credential reference is duplicated: {reference}")
            seen.add(folded)
            references.append(reference)
        updated = self.jobs.replace_retry_credential_references(job_id, tuple(references))
        return _json_value(asdict(updated))

    def refresh_translation_web_options(
        self,
        *,
        job_id: str,
        provider_options: object,
    ) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.spec.kind.value != "translate" or job.spec.provider_id != "gemini-web":
            raise ValueError("Only Gemini Web translation jobs accept Web retry options")
        if not isinstance(provider_options, dict):
            raise ValueError("Gemini Web retry options must be an object")
        allowed = {
            "profile_path",
            "headed",
            "guest",
            "temporary_chat",
            "browser_executable",
            "max_chunk_cues",
            "max_chunk_source_chars",
            "provider_timeout_seconds",
            "web_session_retries",
            "web_first_response_timeout_seconds",
            "web_response_stall_timeout_seconds",
            "web_max_requests_per_session",
            "codex_fallback_enabled",
            "qa_mode",
        }
        unknown = set(provider_options) - allowed
        if unknown:
            raise ValueError(f"Unknown Gemini Web retry options: {sorted(unknown)}")
        values = dict(provider_options)
        if "profile_path" in values and (
            not isinstance(values["profile_path"], str) or not values["profile_path"].strip()
        ):
            raise ValueError("Gemini Web retry profile path is invalid")
        for key in ("browser_executable",):
            if key in values and not isinstance(values[key], str):
                raise ValueError(f"Gemini Web retry option {key} must be a string")
        for key in ("headed", "guest", "temporary_chat", "codex_fallback_enabled"):
            if key in values and type(values[key]) is not bool:
                raise ValueError(f"Gemini Web retry option {key} must be boolean")
        if "qa_mode" in values:
            qa_mode = values["qa_mode"]
            if not isinstance(qa_mode, str) or qa_mode.strip().casefold() not in {
                "integrated",
                "independent",
            }:
                raise ValueError("Gemini Web retry QA mode is invalid")
            values["qa_mode"] = qa_mode.strip().casefold()
        integer_limits = {
            "max_chunk_cues": (1, 150),
            "max_chunk_source_chars": (200, 8_000),
            "web_session_retries": (0, 4),
            "web_max_requests_per_session": (1, 20),
        }
        for key, (minimum, maximum) in integer_limits.items():
            if key in values and (
                type(values[key]) is not int or not minimum <= values[key] <= maximum
            ):
                raise ValueError(f"Gemini Web retry option {key} is out of range")
        numeric_limits = {
            "provider_timeout_seconds": (30.0, 3_600.0),
            "web_first_response_timeout_seconds": (10.0, 300.0),
            "web_response_stall_timeout_seconds": (15.0, 300.0),
        }
        for key, (minimum, maximum) in numeric_limits.items():
            if key not in values:
                continue
            raw = values[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"Gemini Web retry option {key} must be numeric")
            if not minimum <= float(raw) <= maximum:
                raise ValueError(f"Gemini Web retry option {key} is out of range")
            values[key] = float(raw)
        updated = self.jobs.replace_retry_web_translation_options(job_id, values)
        return _json_value(asdict(updated))

    def refresh_translation_multi_policy(self, *, job_id: str) -> dict[str, Any]:
        """Adopt unattended Multi QA while preserving the job checkpoint ledger."""

        job = self.jobs.get(job_id)
        if job.spec.kind is not JobKind.TRANSLATE:
            raise ValueError("Only translation jobs accept the Multi QA policy")
        policy = {
            "qa_policy": "multi_auto",
            "cjk_is_blocker": True,
            "auto_compact_overlong_once": True,
            "max_overlong_compaction_passes": 1,
        }
        updated = self.jobs.replace_retry_translation_policy(job_id, policy)
        return _json_value(asdict(updated))

    def create_ocr_job(
        self,
        *,
        project_id: str,
        sample_interval_us: int,
        engine_python: str,
        device: str = "gpu:0",
        rescan_cue_id: str | None = None,
        engine_host_path: str | None = None,
        reuse_completed: bool = False,
        extraction_method: str = "frame_ocr",
        language: str = "zh",
        model_id: str = WHISPER_MODEL_ID,
        model_path: str | None = None,
        scene_text_enabled: bool = True,
    ) -> dict[str, Any]:
        if type(scene_text_enabled) is not bool:
            raise ValueError("Bật/tắt OCR chữ trong cảnh phải là giá trị đúng/sai")
        method = extraction_method.strip().casefold()
        if method not in {"frame_ocr", "whisper_audio"}:
            raise ValueError("Phương thức trích xuất phải là OCR khung hình hoặc Whisper âm thanh")
        if type(sample_interval_us) is not int or not 400_000 <= sample_interval_us <= 1_500_000:
            raise ValueError("OCR sample interval must be between 0.4 and 1.5 seconds")
        if not device.strip():
            raise ValueError("OCR device is required")
        if method == "whisper_audio" and rescan_cue_id is not None:
            raise ValueError("Whisper không hỗ trợ quét lại riêng một cue OCR")
        # A targeted rescan is an explicit repair operation and may coexist
        # with the original OCR job's durable record.  Generic OCR starts are
        # still guarded against duplicate active work.
        if rescan_cue_id is None:
            self._ensure_no_active_stage(project_id, JobKind.OCR)
        runtime = Path(engine_python).expanduser().resolve(strict=True)
        if not runtime.is_file():
            raise ValueError("OCR engine Python must be a file")
        if method == "whisper_audio" and not whisper_runtime_is_ready(runtime):
            raise ValueError(
                "Runtime Whisper chưa đầy đủ hoặc sai phiên bản. Hãy chạy "
                "scripts/setup-whisper-runtime.ps1 rồi thử lại."
            )
        snapshot = self.projects.load(project_id)
        videos = [item for item in snapshot.media if item.kind is MediaKind.VIDEO]
        if not videos:
            raise ValueError("Project has no video for OCR")
        asset = videos[0]
        media_path = Path(asset.path).expanduser().resolve(strict=True)
        media_stat = media_path.stat()
        output_path = media_path.with_suffix("").with_name(media_path.stem + "_ZH_FINAL.srt")
        if snapshot.ocr_dialogue_region is not None:
            dialogue_x, dialogue_y, dialogue_width, dialogue_height = (
                snapshot.ocr_dialogue_region
            )
            bands = [
                {
                    "name": "upper",
                    "x1": dialogue_x,
                    "y1": dialogue_y,
                    "x2": dialogue_x + dialogue_width,
                    "y2": dialogue_y + dialogue_height,
                    "scale": 1.0,
                    "reading_mode": "dialogue",
                }
            ]
            dialogue_region = list(snapshot.ocr_dialogue_region)
        else:
            bands = [asdict(band) for band in DEFAULT_SUBTITLE_BANDS]
            # The scene-text pass owns only pixels outside this dialogue band.
            # Keeping one explicit full-frame box in the job fingerprint makes
            # the S1/S2 routing deterministic for legacy projects too.
            dialogue_region = [0.06, 0.765, 0.88, 0.217]
        provider_config: dict[str, Any] = {
            "media_path": str(media_path),
            "duration_us": asset.duration_us,
            "sample_interval_us": sample_interval_us,
            "engine_python": str(runtime),
            "device": device,
            "extraction_method": method,
            "ocr_aggregation_version": OCR_AGGREGATION_VERSION,
            "scene_text_enabled": scene_text_enabled,
            "engine_batch_size": 12,
            "window_frame_count": 12,
            "bands": bands,
            "dialogue_region": dialogue_region,
            "exclusion_regions": [list(item) for item in snapshot.ocr_exclusion_regions],
            "output_path": str(output_path),
            "engine_runtime_fingerprint": _external_runtime_fingerprint(runtime),
        }
        if method == "frame_ocr":
            scan_plan = build_scan_plan(snapshot)
            if not scan_plan["segments"] or scan_plan["duration_us"] <= 0:
                raise ValueError("Timeline không còn đoạn video hợp lệ để OCR")
            provider_config["scan_plan"] = scan_plan
        if method == "frame_ocr":
            provider_config["model_fingerprint"] = _paddle_model_fingerprint()
        else:
            whisper_device = device.strip().casefold()
            if whisper_device == "gpu:0":
                whisper_device = "auto"
            if whisper_device not in {"auto", "cuda", "cpu"}:
                raise ValueError("Thiết bị Whisper phải là tự động, CUDA hoặc CPU")
            normalized_language = language.strip().casefold() or "zh"
            if normalized_language not in {"zh", "zh-cn", "zh-tw"}:
                raise ValueError("Whisper hiện chỉ hỗ trợ nhận dạng tiếng Trung")
            normalized_model = model_id.strip()
            if normalized_model != WHISPER_MODEL_ID:
                raise ValueError("Whisper hiện chỉ hỗ trợ model large-v3")
            try:
                resolved_model = (
                    Path(model_path).expanduser().resolve(strict=True)
                    if isinstance(model_path, str) and model_path.strip()
                    else whisper_model_path(model_id=normalized_model)
                )
            except OSError as exc:
                raise ValueError(
                    "Không tìm thấy model Whisper large-v3 ở đường dẫn đã chọn."
                ) from exc
            if resolved_model is None:
                raise ValueError(
                    "Chưa cài model Whisper large-v3. Hãy chạy "
                    "scripts/setup-whisper-runtime.ps1 rồi thử lại."
                )
            ffmpeg = Path(shutil.which("ffmpeg") or "").expanduser()
            ffprobe = Path(shutil.which("ffprobe") or "").expanduser()
            if not ffmpeg.is_file() or not ffprobe.is_file():
                raise ValueError("Whisper cần FFmpeg và ffprobe để tách audio nguồn")
            provider_config.update(
                {
                    "device": whisper_device,
                    "language": normalized_language,
                    "model_id": normalized_model,
                    "model_path": str(resolved_model),
                    "model_fingerprint": whisper_model_fingerprint(resolved_model),
                    "ffmpeg_path": str(ffmpeg.resolve()),
                    "ffprobe_path": str(ffprobe.resolve()),
                    "beam_size": 5,
                    "temperature": 0.0,
                    "condition_on_previous_text": False,
                    "word_timestamps": True,
                    "vad_parameters": {
                        "threshold": 0.18,
                        "min_speech_duration_ms": 100,
                        "min_silence_duration_ms": 600,
                        "speech_pad_ms": 500,
                    },
                    "allow_cpu_fallback": True,
                    "engine_runtime_fingerprint": _external_runtime_fingerprint(
                        runtime,
                        package_patterns=(
                            "faster_whisper-*.dist-info",
                            "ctranslate2-*.dist-info",
                            "av-*.dist-info",
                        ),
                    ),
                }
            )
        default_host = _engine_host_path(
            "whisper_engine_host.py" if method == "whisper_audio" else "ocr_engine_host.py"
        )
        provider_config["engine_host_path"] = str(default_host)
        provider_config["engine_host_sha256"] = _file_sha256(default_host)
        if engine_host_path is not None:
            host = Path(engine_host_path).expanduser().resolve(strict=True)
            if not host.is_file():
                raise ValueError("OCR engine host must be a file")
            provider_config["engine_host_path"] = str(host)
            provider_config["engine_host_sha256"] = _file_sha256(host)
        if rescan_cue_id is not None:
            cue = next(
                (item for item in snapshot.source_cues if item.id == rescan_cue_id),
                None,
            )
            if cue is None:
                raise LookupError(f"Unknown subtitle cue: {rescan_cue_id}")
            provider_config.update(
                {
                    "rescan_cue_id": cue.id,
                    "rescan_cue_start_us": cue.range.start_us,
                    "rescan_cue_end_us": cue.range.end_us,
                    "scan_windows": [
                        {
                            "start_us": max(0, cue.range.start_us - 1_200_000),
                            "end_us": min(asset.duration_us, cue.range.end_us + 1_200_000),
                            "reasons": ["user_targeted_rescan"],
                        }
                    ],
                }
            )
            if method == "frame_ocr" and "scan_plan" in provider_config:
                visible = range_on_timeline(snapshot, cue.range)
                if visible is None:
                    raise ValueError("Cue cần quét lại đã nằm trong đoạn video bị bỏ")
                provider_config["rescan_cue_start_us"] = visible.start_us
                provider_config["rescan_cue_end_us"] = visible.end_us
                provider_config["scan_windows"] = [{
                    "start_us": max(0, visible.start_us - 1_200_000),
                    "end_us": min(provider_config["scan_plan"]["duration_us"],
                                  visible.end_us + 1_200_000),
                    "reasons": ["user_targeted_rescan"],
                }]
        raw = {
            "kind": "ocr",
            "project_id": project_id,
            "input_revision": snapshot.revision,
            "provider_id": (
                "faster-whisper-audio"
                if method == "whisper_audio"
                else (
                    "paddle-targeted-rescan"
                    if rescan_cue_id is not None
                    else "paddle-fast-quality-rapid"
                )
            ),
            "provider_version": (
                f"faster-whisper-{WHISPER_RUNTIME_VERSION}"
                if method == "whisper_audio"
                else "paddleocr-3.7.0"
            ),
            "model_id": (
                WHISPER_MODEL_ID
                if method == "whisper_audio"
                else "PP-OCRv6-tiny-small+medium-rescue"
            ),
            "input_fingerprint": stable_fingerprint(
                self._ocr_source_identities(snapshot, provider_config["scan_plan"])
                if method == "frame_ocr" else {
                    "path": str(media_path),
                    "size": media_stat.st_size,
                    "mtime_ns": media_stat.st_mtime_ns,
                    "duration_us": asset.duration_us,
                    "sha256": asset.sha256,
                }
            ),
            "settings_fingerprint": stable_fingerprint(provider_config),
            "provider_config": provider_config,
        }
        return self._create_or_reuse_completed_job(
            raw,
            reuse_completed=reuse_completed,
            stage_output=rescan_cue_id is None,
        )

    @staticmethod
    def _ocr_source_identities(snapshot: ProjectSnapshot, plan: dict) -> list[dict]:
        media = {item.path: item for item in snapshot.media}
        identities = []
        for raw_path in sorted({segment["media_path"] for segment in plan["segments"]}):
            path = Path(raw_path).expanduser().resolve(strict=True)
            stat = path.stat()
            asset = media.get(raw_path)
            identities.append({"path": str(path), "size": stat.st_size,
                               "mtime_ns": stat.st_mtime_ns,
                               "duration_us": asset.duration_us if asset else None,
                               "sha256": asset.sha256 if asset else None})
        return identities

    def create_translation_job(
        self,
        *,
        project_id: str,
        provider_id: str,
        model_id: str,
        provider_options: dict[str, Any] | None = None,
        reuse_completed: bool = False,
    ) -> dict[str, Any]:
        provider_id = provider_id.strip().casefold()
        model_id = model_id.strip()
        supported_provider_ids = {"gemini-web", "gemini-cli", *TRANSLATION_API_PROVIDER_IDS}
        if provider_id not in supported_provider_ids:
            raise ValueError("Translation provider is not supported")
        if not model_id:
            raise ValueError("Translation model is required")
        self._ensure_no_active_stage(project_id, JobKind.TRANSLATE)
        options = dict(provider_options or {})
        snapshot = self.projects.load(project_id)
        if not snapshot.source_cues:
            raise ValueError("Project has no source subtitles to translate")
        subtitle_assets = [item for item in snapshot.media if item.kind is MediaKind.SUBTITLE]
        if subtitle_assets:
            source_path = Path(subtitle_assets[-1].path).expanduser().resolve()
        else:
            videos = [item for item in snapshot.media if item.kind is MediaKind.VIDEO]
            if not videos:
                raise ValueError("Project has no media path for translation output")
            source_path = Path(videos[0].path).expanduser().resolve().with_suffix(".srt")
        output_path = source_path.with_name(source_path.stem + "_codex_vi.srt")
        source_cues = [_translation_cue_payload(cue) for cue in snapshot.source_cues]
        translated_by_id = {cue.id: cue for cue in snapshot.translated_cues}
        preserved_manual_translations = [
            {
                "index": cue.index,
                "text": translated_by_id.get(cue.id, cue).text,
            }
            for cue in snapshot.source_cues
            if cue.source in {"manual_dialogue", "manual_scene_text"}
        ]
        max_chunk_cues = options.pop("max_chunk_cues", 150 if provider_id == "gemini-web" else 80)
        max_chunk_source_chars = options.pop(
            "max_chunk_source_chars", 8_000 if provider_id == "gemini-web" else 6_000
        )
        provider_timeout_seconds = options.pop(
            "provider_timeout_seconds",
            900 if provider_id == "gemini-web" else 300,
        )
        banned_latin_terms = options.pop("banned_latin_terms", [])
        qa_mode = str(options.pop("qa_mode", "integrated")).strip().casefold()
        qa_non_blocking = options.pop("qa_non_blocking", True)
        translation_style_preset = str(
            options.pop(
                "translation_style_preset",
                snapshot.translation_preferences.preset,
            )
        ).strip().casefold()
        translation_custom_rules = str(
            options.pop(
                "translation_custom_rules",
                snapshot.translation_preferences.custom_rules,
            )
        )
        # Multi video passes an explicit QA policy in its immutable snapshot.
        # Keep these fields in the job contract instead of treating them as
        # provider options; otherwise a valid Multi launch fails preflight with
        # an "unknown provider option" error before translation starts.
        qa_policy = str(options.pop("qa_policy", "legacy")).strip().casefold()
        cjk_is_blocker = options.pop("cjk_is_blocker", qa_policy == "multi_auto")
        auto_compact_overlong_once = options.pop(
            "auto_compact_overlong_once", qa_policy == "multi_auto"
        )
        max_overlong_compaction_passes = options.pop("max_overlong_compaction_passes", 0)
        if type(max_chunk_cues) is not int or not 1 <= max_chunk_cues <= 150:
            raise ValueError("Translation chunk cue limit must be between 1 and 150")
        if type(max_chunk_source_chars) is not int or not 200 <= max_chunk_source_chars <= 8_000:
            raise ValueError(
                "Translation chunk source limit must be between 200 and 8000 characters"
            )
        if (
            isinstance(provider_timeout_seconds, bool)
            or not isinstance(provider_timeout_seconds, (int, float))
            or not 30 <= float(provider_timeout_seconds) <= 3_600
        ):
            raise ValueError("Translation provider timeout must be between 30 and 3600 seconds")
        if not isinstance(banned_latin_terms, list) or not all(
            isinstance(value, str) and value.strip() for value in banned_latin_terms
        ):
            raise ValueError("Translation banned Latin terms must be a list of strings")
        if qa_mode not in {"integrated", "independent"}:
            raise ValueError("Translation QA mode must be integrated or independent")
        if type(qa_non_blocking) is not bool:
            raise ValueError("Translation non-blocking QA option must be boolean")
        if translation_style_preset not in TRANSLATION_STYLE_PRESETS:
            raise ValueError("Translation style preset is invalid")
        if len(translation_custom_rules) > 4_000:
            raise ValueError("Translation custom rules cannot exceed 4000 characters")
        if qa_policy not in {"legacy", "multi_auto"}:
            raise ValueError("Translation QA policy must be legacy or multi_auto")
        if type(cjk_is_blocker) is not bool:
            raise ValueError("Translation CJK blocker option must be boolean")
        if type(auto_compact_overlong_once) is not bool:
            raise ValueError("Translation overlong compaction option must be boolean")
        if (
            type(max_overlong_compaction_passes) is not int
            or not 0 <= max_overlong_compaction_passes <= 1
        ):
            raise ValueError("Translation overlong compaction passes must be 0 or 1")
        if qa_policy == "multi_auto" and not cjk_is_blocker:
            raise ValueError("Multi QA policy must keep CJK as a blocker")
        if (
            qa_policy == "multi_auto"
            and auto_compact_overlong_once
            and not max_overlong_compaction_passes
        ):
            max_overlong_compaction_passes = 1
        common_config: dict[str, Any] = {
            "source_path": str(source_path),
            "output_path": str(output_path),
            "title": snapshot.name,
            "source_cues": source_cues,
            "preserved_manual_translations": preserved_manual_translations,
            "max_chunk_cues": max_chunk_cues,
            "max_chunk_source_chars": max_chunk_source_chars,
            "provider_timeout_seconds": float(provider_timeout_seconds),
            "banned_latin_terms": banned_latin_terms,
            "qa_mode": qa_mode,
            "qa_non_blocking": qa_non_blocking,
            "translation_style_preset": translation_style_preset,
            "translation_custom_rules": translation_custom_rules,
            "qa_policy": qa_policy,
            "cjk_is_blocker": cjk_is_blocker,
            "auto_compact_overlong_once": auto_compact_overlong_once,
            "max_overlong_compaction_passes": max_overlong_compaction_passes,
        }
        if provider_id == "gemini-web":
            profile_path = options.pop("profile_path", None)
            if not isinstance(profile_path, str) or not profile_path.strip():
                raise ValueError("Gemini Web requires an isolated profile path")
            headed = options.pop("headed", False)
            guest = options.pop("guest", False)
            temporary_chat = options.pop("temporary_chat", True)
            web_session_retries = options.pop("web_session_retries", 1)
            first_response_timeout = options.pop("web_first_response_timeout_seconds", 30)
            response_stall_timeout = options.pop("web_response_stall_timeout_seconds", 45)
            max_requests_per_session = options.pop("web_max_requests_per_session", 5)
            codex_fallback_enabled = options.pop("codex_fallback_enabled", False)
            if not all(type(value) is bool for value in (headed, guest, temporary_chat)):
                raise ValueError("Gemini Web mode options must be booleans")
            if type(codex_fallback_enabled) is not bool:
                raise ValueError("Gemini Web Codex fallback flag must be boolean")
            if type(web_session_retries) is not int or not 0 <= web_session_retries <= 4:
                raise ValueError("Gemini Web session retries must be between 0 and 4")
            if (
                isinstance(first_response_timeout, bool)
                or not isinstance(first_response_timeout, (int, float))
                or not 10 <= float(first_response_timeout) <= 300
            ):
                raise ValueError(
                    "Gemini Web first-response timeout must be between 10 and 300 seconds"
                )
            if (
                isinstance(response_stall_timeout, bool)
                or not isinstance(response_stall_timeout, (int, float))
                or not 15 <= float(response_stall_timeout) <= 300
            ):
                raise ValueError(
                    "Gemini Web response-stall timeout must be between 15 and 300 seconds"
                )
            if type(max_requests_per_session) is not int or not 1 <= max_requests_per_session <= 20:
                raise ValueError("Gemini Web session request limit must be between 1 and 20")
            common_config.update(
                {
                    "profile_path": profile_path,
                    "headed": headed,
                    "guest": guest,
                    "temporary_chat": temporary_chat,
                    "browser_executable": str(options.pop("browser_executable", "")),
                    "web_session_retries": web_session_retries,
                    "web_first_response_timeout_seconds": float(first_response_timeout),
                    "web_response_stall_timeout_seconds": float(response_stall_timeout),
                    "web_max_requests_per_session": max_requests_per_session,
                    "codex_fallback_enabled": codex_fallback_enabled,
                }
            )
            provider_version = "playwright-isolated-v5"
        elif provider_id == "gemini-api":
            references = options.pop("credential_references", None)
            if (
                not isinstance(references, list)
                or not references
                or not all(isinstance(value, str) and value.strip() for value in references)
            ):
                raise ValueError("Gemini API requires Credential Manager references")
            common_config["credential_references"] = references
            provider_version = "generate-content-v1beta-2026-06"
        elif provider_id in OPENAI_COMPATIBLE_PROVIDER_IDS:
            preset = preset_for(provider_id)
            references = options.pop("credential_references", None)
            if (
                not isinstance(references, list)
                or not references
                or not all(isinstance(value, str) and value.strip() for value in references)
            ):
                raise ValueError("OpenAI-compatible API requires Credential Manager references")
            supplied_base_url = options.pop("base_url", preset.base_url)
            if not isinstance(supplied_base_url, str):
                raise ValueError("OpenAI-compatible base URL must be a string")
            base_url = validate_base_url(supplied_base_url)
            if provider_id != "openai-compatible" and base_url != preset.base_url:
                raise ValueError("Preset API endpoints cannot be changed; use custom provider")
            json_mode = options.pop("json_mode", preset.json_mode)
            if type(json_mode) is not bool:
                raise ValueError("OpenAI-compatible JSON mode must be boolean")
            extra_body = options.pop("extra_body", dict(preset.extra_body))
            if not isinstance(extra_body, dict):
                raise ValueError("OpenAI-compatible extra body must be an object")
            common_config.update(
                {
                    "credential_references": references,
                    "base_url": base_url,
                    "json_mode": json_mode,
                    "extra_body": extra_body,
                }
            )
            provider_version = "openai-compatible-chat-v1"
        else:
            common_config["cli_executable"] = str(options.pop("cli_executable", "gemini"))
            provider_version = "headless-json-v1"
        if options:
            raise ValueError(f"Unknown translation provider options: {sorted(options)}")
        raw = {
            "kind": "translate",
            "project_id": project_id,
            "input_revision": snapshot.revision,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "model_id": model_id,
            "input_fingerprint": stable_fingerprint(source_cues),
            "settings_fingerprint": stable_fingerprint(common_config),
            "provider_config": common_config,
        }
        return self._create_or_reuse_completed_job(
            raw,
            reuse_completed=reuse_completed,
            stage_output=True,
        )

    def create_tts_job(
        self,
        *,
        project_id: str,
        provider_id: str,
        voice_id: str,
        provider_options: dict[str, Any] | None = None,
        reuse_completed: bool = False,
    ) -> dict[str, Any]:
        provider_id = provider_id.strip().casefold()
        voice_id = voice_id.strip()
        if provider_id == "vieneu-3.3":
            raise ValueError(
                "Tác vụ VieNeu 3.3 cũ không thể tiếp tục bằng engine 3.8.1. "
                "Hãy bấm Tạo giọng để tạo tác vụ mới; profile clone vẫn được giữ nguyên."
            )
        if provider_id not in {"piper", "vieneu-3.8"}:
            raise ValueError("TTS provider must be Piper or VieNeu 3.8")
        if not voice_id:
            raise ValueError("TTS voice id is required")
        self._ensure_no_active_stage(project_id, JobKind.TTS)
        options = dict(provider_options or {})
        purpose = str(options.pop("purpose", "production")).strip().casefold()
        if purpose not in {"production", "preview"}:
            raise ValueError("TTS purpose must be production or preview")
        raw_force_cue_ids = options.pop("force_cue_ids", [])
        if not isinstance(raw_force_cue_ids, list) or not all(
            isinstance(value, str) and value.strip() for value in raw_force_cue_ids
        ):
            raise ValueError("TTS force cue ids must be a list of non-empty strings")
        force_cue_ids = tuple(dict.fromkeys(value.strip() for value in raw_force_cue_ids))
        if purpose == "preview" and force_cue_ids:
            raise ValueError("TTS preview cannot force production cues")
        snapshot = self.projects.load(project_id)
        videos = [item for item in snapshot.media if item.kind is MediaKind.VIDEO]
        if not videos:
            raise ValueError("Project has no video for voice generation")
        video = videos[0]
        data_root = Path(str(options.pop("data_root", discover_data_root()))).expanduser().resolve()
        host = _engine_host_path("tts_engine_host.py")
        if purpose == "preview":
            preview_text = str(
                options.pop(
                    "preview_text",
                    "Đây là bản nghe thử giọng đọc của DUCAI Studio hai.",
                )
            ).strip()
            if not preview_text:
                raise ValueError("TTS preview text cannot be empty")
            cues = [
                {
                    "id": "preview",
                    "index": 1,
                    "start_us": 0,
                    "end_us": 30_000_000,
                    "text": preview_text,
                }
            ]
            preview_key = stable_fingerprint(
                {"provider": provider_id, "voice": voice_id, "text": preview_text}
            )[:20]
            output_path = data_root / "voice_previews" / f"{provider_id}-{preview_key}.wav"
            media_duration_us = 30_000_000
        else:
            cues = self._tts_cues_for_snapshot(snapshot)
            available_cue_ids = {str(cue["id"]) for cue in cues}
            unknown_force_ids = sorted(set(force_cue_ids) - available_cue_ids)
            if unknown_force_ids:
                raise ValueError(
                    "TTS force cue ids are not present in the Vietnamese subtitles: "
                    + ", ".join(unknown_force_ids)
                )
            media_path = Path(video.path).expanduser().resolve()
            output_path = media_path.with_name(media_path.stem + "_voice_vi.wav")
            media_duration_us = video.duration_us
        engine_speed = float(options.pop("engine_speed", 1.0))
        # Missing on legacy jobs by design: resuming them must preserve 1.00x.
        voice_read_speed = float(options.pop("voice_read_speed", 1.0))
        batch_size = options.pop("batch_size", 16 if provider_id == "piper" else 32)
        max_absolute_speed = float(options.pop("max_absolute_speed", max(voice_read_speed, 1.28)))
        adaptive_timing = options.pop("adaptive_timing", False)
        # Timing/semantic QA is advisory for new TTS jobs. Keep this explicit
        # in the job contract; legacy specs that omit it retain strict worker
        # behavior while UI-created jobs can publish D1 with timing warnings.
        qa_non_blocking = options.pop("qa_non_blocking", True)
        defer_timing_conform = options.pop(
            "defer_timing_conform",
            purpose == "production" and not bool(adaptive_timing),
        )
        target_lufs = float(options.pop("target_lufs", -18.0))
        provider_timeout_seconds = float(
            options.pop(
                "provider_timeout_seconds",
                300 if provider_id == "piper" else 1_800,
            )
        )
        if not 0.85 <= engine_speed <= 1.15:
            raise ValueError("TTS engine speed must be between 0.85 and 1.15")
        if not 1.0 <= voice_read_speed <= 3.0:
            raise ValueError("Voice read speed must be between 1.0 and 3.0")
        maximum_batch_size = 32
        if type(batch_size) is not int or not 1 <= batch_size <= maximum_batch_size:
            raise ValueError(f"TTS batch size must be between 1 and {maximum_batch_size}")
        if type(adaptive_timing) is not bool:
            raise ValueError("Adaptive TTS timing option must be boolean")
        if type(qa_non_blocking) is not bool:
            raise ValueError("TTS non-blocking QA option must be boolean")
        if type(defer_timing_conform) is not bool:
            raise ValueError("Deferred voice conform option must be boolean")
        if not -24.0 <= target_lufs <= -14.0:
            raise ValueError("TTS target loudness must be between -24 and -14 LUFS")
        pipeline_settings = VoicePipelineSettings(
            max_absolute_speed=max_absolute_speed,
            voice_read_speed=voice_read_speed,
            adaptive_timing=adaptive_timing,
        )
        reference_audio_path = ""
        reference_note = ""
        reference_denoise_value = False
        if provider_id == "piper":
            catalog = VoiceCatalog(data_root)
            entry = catalog.get(voice_id, require_installed=True)
            runtime_python = (
                Path(
                    str(
                        options.pop(
                            "runtime_python",
                            data_root / "piper_runtime" / ".venv" / "Scripts" / "python.exe",
                        )
                    )
                )
                .expanduser()
                .resolve(strict=True)
            )
            voice_identity = f"{voice_id}:{entry.model_fingerprint}"
            model_id = voice_id
            provider_version = "piper-tts-1.6.0"
            profile_path = ""
        else:
            runtime_root = data_root / "vieneu_3_8_runtime"
            runtime_python = (
                Path(
                    str(
                        options.pop(
                            "runtime_python",
                            runtime_root / ".venv" / "Scripts" / "python.exe",
                        )
                    )
                )
                .expanduser()
                .resolve(strict=True)
            )
            selected_profile = options.pop("profile_path", None)
            reference_audio_raw = str(options.pop("reference_audio_path", "")).strip()
            reference_note = str(options.pop("reference_text", "")).strip()
            reference_denoise_value = options.pop("reference_denoise", False)
            if type(reference_denoise_value) is not bool:
                raise ValueError("VieNeu reference denoise option must be boolean")
            if voice_id.startswith("preset:"):
                preset = next(
                    (
                        entry
                        for entry in VoiceCatalog(data_root).vieneu_presets(installed=True)
                        if entry.voice_id == voice_id
                    ),
                    None,
                )
                if preset is None:
                    raise LookupError(f"Unknown VieNeu preset: {voice_id}")
                if selected_profile is not None or reference_audio_raw:
                    raise ValueError("VieNeu preset cannot use clone reference options")
                voice_identity = f"{voice_id}:{preset.model_fingerprint}"
                model_id = "v3turbo"
                provider_version = "3.8.1"
                profile_path = ""
                reference_audio_path = ""
            else:
                reference_audio: Path | None = None
                if reference_audio_raw:
                    reference_audio = Path(reference_audio_raw).expanduser().resolve(strict=True)
                    if not reference_audio.is_file():
                        raise FileNotFoundError("VieNeu reference audio is missing")
                if selected_profile is None and reference_audio is not None:
                    reference_hash = _file_sha256(reference_audio)
                    profile = data_root / "vieneu_profiles" / f"{reference_hash}.npz"
                elif selected_profile is None:
                    profiles = sorted((data_root / "vieneu_profiles").glob("*.npz"))
                    if not profiles:
                        raise FileNotFoundError("No VieNeu clone profile is installed")
                    profile = profiles[0]
                else:
                    profile = Path(str(selected_profile)).expanduser().resolve(strict=True)
                if profile.is_file() and not profile.with_suffix(".json").is_file():
                    raise FileNotFoundError("VieNeu clone profile metadata is missing")
                if profile.is_file():
                    profile_identity = profile_fingerprint(profile)
                elif reference_audio is not None:
                    profile_identity = _file_sha256(reference_audio)
                else:
                    raise FileNotFoundError("VieNeu clone profile is unavailable")
                voice_identity = f"{profile.stem}:{profile_identity}"
                model_id = "v3turbo"
                provider_version = "3.8.1"
                profile_path = str(profile)
                voice_id = profile.stem
                reference_audio_path = str(reference_audio) if reference_audio is not None else ""
        if options:
            raise ValueError(f"Unknown TTS provider options: {sorted(options)}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        qa_path = output_path.with_name(output_path.stem + "_QA.json")
        common_config: dict[str, Any] = {
            "purpose": purpose,
            "cues": cues,
            "media_duration_us": media_duration_us,
            "output_path": str(output_path),
            "qa_path": str(qa_path),
            "cache_root": str(
                data_root / ("voice_previews/cache" if purpose == "preview" else "voice_cue_cache")
            ),
            "data_root": str(data_root),
            "runtime_python": str(runtime_python),
            "engine_host_path": str(host.resolve()),
            "engine_host_sha256": _file_sha256(host),
            "voice_id": voice_id,
            "voice_identity": voice_identity,
            "profile_path": profile_path,
            "reference_audio_path": reference_audio_path,
            "reference_text": reference_note,
            "reference_denoise": reference_denoise_value,
            "force_cue_ids": list(force_cue_ids),
            "engine_speed": engine_speed,
            "voice_read_speed": voice_read_speed,
            "batch_size": batch_size,
            "target_lufs": target_lufs,
            "provider_timeout_seconds": provider_timeout_seconds,
            "pipeline_settings": _json_value(asdict(pipeline_settings)),
            "qa_non_blocking": qa_non_blocking,
            "defer_timing_conform": defer_timing_conform,
        }
        tts_input_fingerprint = _tts_input_fingerprint(
            cues,
            provider_id=provider_id,
            provider_version=provider_version,
            model_id=model_id,
            provider_config=common_config,
        )
        common_config["tts_input_fingerprint_schema"] = _TTS_INPUT_FINGERPRINT_SCHEMA
        common_config["tts_input_fingerprint"] = tts_input_fingerprint
        raw = {
            "kind": "tts",
            "project_id": project_id,
            "input_revision": snapshot.revision,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "model_id": model_id,
            "input_fingerprint": tts_input_fingerprint,
            "settings_fingerprint": stable_fingerprint(common_config),
            "provider_config": common_config,
        }
        return self._create_or_reuse_completed_job(
            raw,
            reuse_completed=reuse_completed,
            stage_output=purpose == "production",
        )

    @staticmethod
    def _tts_cues_for_snapshot(snapshot: ProjectSnapshot) -> list[dict[str, Any]]:
        if not snapshot.translated_cues:
            raise ValueError("Dự án chưa có phụ đề tiếng Việt để tạo giọng.")
        pairs = _voice_subtitle_pairs(snapshot)
        for source, translated in pairs:
            if source.range != translated.range:
                raise ValueError(
                    f"Cue {source.index}: thời gian bản dịch chưa khớp cue gốc; "
                    "hãy kiểm tra thời gian của cặp phụ đề này."
                )
            if not translated.text.strip():
                raise ValueError(f"Cue {source.index}: bản dịch đang trống; hãy nhập nội dung.")
        dialogue_pairs = tuple(pair for pair in pairs if pair[0].is_dialogue)
        if not dialogue_pairs:
            raise ValueError("Project has no dialogue subtitles for voice generation")
        cues: list[dict[str, Any]] = []
        for _source, cue in dialogue_pairs:
            warning_codes = [
                evidence.removeprefix("translation-warning:")
                for evidence in cue.evidence_ids
                if evidence.startswith("translation-warning:")
            ]
            item: dict[str, Any] = {
                "id": cue.id,
                "index": cue.index,
                "start_us": cue.range.start_us,
                "end_us": cue.range.end_us,
                "text": cue.text,
            }
            if warning_codes:
                item["content_warning"] = True
                item["content_warning_codes"] = warning_codes
            cues.append(item)
        return cues

    def rebase_tts_retry(self, job_id: str) -> dict[str, Any] | None:
        """Create a current-revision replacement when TTS semantic input changed.

        ``None`` means the old job remains semantically valid and can use the
        normal retry transition. The replacement keeps all provider settings;
        per-cue fingerprints let the worker adopt unchanged WAV cache entries.
        """

        job = self.jobs.get(job_id)
        if job.spec.kind is not JobKind.TTS:
            return None
        config = dict(job.spec.provider_config)
        if config.get("purpose") != "production" or config.get("job_mode") == "voice_conform":
            return None
        snapshot = self.projects.load(job.spec.project_id)
        current_cues = self._tts_cues_for_snapshot(snapshot)
        if self._tts_fingerprint_for_job(job, current_cues) == job.spec.input_fingerprint:
            return None
        active = self.jobs.active_id(
            project_id=job.spec.project_id,
            kind=JobKind.TTS,
            exclude_job_id=job.id,
        )
        if active:
            raise ValueError("Dự án đã có tác vụ tạo giọng thay thế đang hoạt động")
        config.pop("staging_output_path", None)
        config.pop("staging_qa_path", None)
        config["cues"] = current_cues
        config["replaces_job_id"] = job.id
        config["tts_input_fingerprint_schema"] = _TTS_INPUT_FINGERPRINT_SCHEMA
        config["tts_input_fingerprint"] = _tts_input_fingerprint(
            current_cues,
            provider_id=job.spec.provider_id,
            provider_version=job.spec.provider_version,
            model_id=job.spec.model_id,
            provider_config=config,
        )
        settings_payload = {
            key: value for key, value in config.items() if key != "replaces_job_id"
        }
        raw = {
            "kind": "tts",
            "project_id": snapshot.id,
            "input_revision": snapshot.revision,
            "provider_id": job.spec.provider_id,
            "provider_version": job.spec.provider_version,
            "model_id": job.spec.model_id,
            "input_fingerprint": config["tts_input_fingerprint"],
            "settings_fingerprint": stable_fingerprint(settings_payload),
            "provider_config": config,
        }
        replacement = self._create_or_reuse_completed_job(
            raw,
            reuse_completed=False,
            stage_output=True,
        )
        replacement_id = str(replacement.get("id", ""))
        if replacement_id:
            self.jobs.append_event(
                event_id=str(uuid4()),
                job_id=job.id,
                kind="superseded",
                payload={
                    "replacement_job_id": replacement_id,
                    "replacement_revision": snapshot.revision,
                },
            )
        return {
            **replacement,
            "replacement": True,
            "replaced_job_id": job.id,
        }

    @staticmethod
    def _tts_fingerprint_for_job(job: Any, cues: list[dict[str, Any]]) -> str:
        config = job.spec.provider_config
        if config.get("tts_input_fingerprint_schema") != _TTS_INPUT_FINGERPRINT_SCHEMA:
            # Jobs created before a121 used the cue-only input hash. Keep them
            # resumable and only rebase when those original semantic cues differ.
            return stable_fingerprint(cues)
        return _tts_input_fingerprint(
            cues,
            provider_id=job.spec.provider_id,
            provider_version=job.spec.provider_version,
            model_id=job.spec.model_id,
            provider_config=config,
        )

    def tts_job_needs_rebase(self, job_id: str) -> bool:
        """Return whether the latest dialogue input differs from a TTS job.

        This deliberately ignores the project revision itself. Visual-only
        edits (font, colour, scene text, effects) remain compatible with the
        running voice job, while dialogue text/timestamp changes request a
        checkpoint-safe replacement.
        """

        job = self.jobs.get(job_id)
        if job.spec.kind is not JobKind.TTS:
            return False
        config = job.spec.provider_config
        if config.get("purpose") != "production" or config.get("job_mode") == "voice_conform":
            return False
        snapshot = self.projects.load(job.spec.project_id)
        current_cues = self._tts_cues_for_snapshot(snapshot)
        return self._tts_fingerprint_for_job(job, current_cues) != job.spec.input_fingerprint

    def estimate_voice_conform(
        self,
        *,
        project_id: str,
        parent_tts_job_id: str = "",
        mode: str = VoiceTimingMode.HYBRID.value,
        max_audio_speed: float = 1.25,
        min_video_speed: float = 0.5,
        allow_duration_growth: bool = True,
        include_plan: bool = True,
        verify_cache: bool = True,
    ) -> dict[str, Any]:
        snapshot = self.projects.load(project_id)
        parent_id = self._resolve_voice_base_job_id(snapshot, parent_tts_job_id)
        cues, _entries = self._voice_conform_base_cues(
            snapshot,
            parent_id,
            verify_cache=verify_cache,
        )
        videos = [item for item in snapshot.media if item.kind is MediaKind.VIDEO]
        if not videos:
            raise ValueError("Project has no video for voice conform")
        settings = VoiceConformSettings(
            mode=VoiceTimingMode(mode),
            max_audio_speed=float(max_audio_speed),
            min_video_speed=float(min_video_speed),
            allow_duration_growth=allow_duration_growth,
        )
        plan = build_voice_conform_plan(
            cues,
            source_duration_us=videos[0].duration_us,
            settings=settings,
        )
        issues = [
            {
                "id": decision.index,
                "cue_id": decision.cue_id,
                "start_us": decision.source_start_us,
                "end_us": decision.source_end_us,
                "code": decision.issue_code,
                "message": decision.issue_message,
                "required_audio_speed": decision.required_audio_speed,
                "required_video_speed": decision.required_video_speed,
            }
            for decision in plan.cues
            if decision.unresolved
        ]
        audio_speeds = [item.audio_speed for item in plan.cues] or [1.0]
        video_speeds = [item.video_speed for item in plan.segments] or [1.0]
        applied = bool(
            snapshot.voice_conform is not None
            and snapshot.voice_conform.plan.fingerprint == plan.fingerprint
        )
        result: dict[str, Any] = {
            "status": "PASS" if plan.applicable else "REVIEW",
            "project_id": project_id,
            "parent_tts_job_id": parent_id,
            "plan_fingerprint": plan.fingerprint,
            "adjusted_cue_count": plan.adjusted_cue_count,
            "added_duration_us": plan.added_duration_us,
            "output_duration_us": plan.output_duration_us,
            "min_audio_speed": min(audio_speeds),
            "max_audio_speed": max(audio_speeds),
            "min_video_speed": min(video_speeds),
            "max_video_speed": max(video_speeds),
            "unresolved_count": len(issues),
            "issues": issues,
            "can_apply": plan.applicable,
            "applied": applied,
        }
        # The GUI only needs summary values and unresolved cue targets.  The
        # complete plan is retained for job creation, but sending it over IPC
        # on every slider movement needlessly blocks QML with multi-megabyte
        # JSON payloads.
        if include_plan:
            result["plan"] = plan.as_json()
        return result

    def create_voice_conform_job(
        self,
        *,
        project_id: str,
        parent_tts_job_id: str = "",
        mode: str = VoiceTimingMode.HYBRID.value,
        max_audio_speed: float = 1.25,
        min_video_speed: float = 0.5,
        allow_duration_growth: bool = True,
    ) -> dict[str, Any]:
        active = self.jobs.active_id(project_id=project_id, kind=JobKind.TTS)
        if active:
            raise ValueError("Dự án đang có một tác vụ voice khác hoạt động.")
        estimate = self.estimate_voice_conform(
            project_id=project_id,
            parent_tts_job_id=parent_tts_job_id,
            mode=mode,
            max_audio_speed=max_audio_speed,
            min_video_speed=min_video_speed,
            allow_duration_growth=allow_duration_growth,
            include_plan=True,
            verify_cache=True,
        )
        if estimate["can_apply"] is not True:
            raise ValueError(
                f"Còn {estimate['unresolved_count']} cue vượt giới hạn đồng bộ; "
                "hãy sửa, rút gọn hoặc chủ động ép riêng cue trước."
            )
        snapshot = self.projects.load(project_id)
        parent_id = str(estimate["parent_tts_job_id"])
        parent = self.jobs.get(parent_id)
        _cues, base_entries = self._voice_conform_base_cues(
            snapshot,
            parent_id,
            verify_cache=False,
        )
        plan = decode_voice_conform_plan(estimate["plan"])
        for candidate in self.jobs.list_recent(limit=10_000):
            config = candidate.spec.provider_config
            if (
                candidate.spec.kind is JobKind.TTS
                and candidate.spec.project_id == project_id
                and config.get("job_mode") == "voice_conform"
                and config.get("parent_tts_job_id") == parent_id
                and config.get("plan_fingerprint") == plan.fingerprint
                and (
                    (
                        candidate.state is JobState.SUCCEEDED
                        and snapshot.voice_conform is not None
                        and snapshot.voice_conform.plan.fingerprint == plan.fingerprint
                    )
                    or candidate.state not in TERMINAL_JOB_STATES
                )
            ):
                return _json_value(asdict(candidate))
        base_output = Path(str(parent.spec.provider_config["output_path"])).expanduser().resolve()
        output_path = base_output.with_name(
            f"{base_output.stem}_conformed_{plan.fingerprint[:12]}.wav"
        )
        qa_path = output_path.with_name(output_path.stem + "_QA.json")
        data_root = (
            Path(str(parent.spec.provider_config.get("data_root", discover_data_root())))
            .expanduser()
            .resolve()
        )
        config = {
            "job_mode": "voice_conform",
            "purpose": "voice_conform",
            "parent_tts_job_id": parent_id,
            "plan_fingerprint": plan.fingerprint,
            "conform_plan": plan.as_json(),
            "cues": base_entries,
            "force_cue_ids": [],
            "sample_rate": 48_000,
            "target_lufs": float(parent.spec.provider_config.get("target_lufs", -18.0)),
            "conform_cache_root": str(data_root / "voice_conform_cache"),
            "output_path": str(output_path),
            "qa_path": str(qa_path),
        }
        raw = {
            "kind": "tts",
            "project_id": project_id,
            "input_revision": snapshot.revision,
            "provider_id": "voice-conform",
            "provider_version": "piecewise-cache-v1",
            "model_id": "cache-postprocess",
            "input_fingerprint": stable_fingerprint(base_entries),
            "settings_fingerprint": stable_fingerprint(config),
            "provider_config": config,
        }
        return self.create_job(raw)

    def reset_voice_conform(self, project_id: str) -> dict[str, Any]:
        active = self.jobs.active_id(project_id=project_id, kind=JobKind.TTS)
        if active:
            raise ValueError("Hãy chờ hoặc hủy tác vụ đồng bộ voice trước khi hoàn tác.")
        snapshot = self.projects.load(project_id)
        state = snapshot.voice_conform
        cue_timing: tuple[Mapping[str, object], ...] | None = None
        if state is not None:
            try:
                parent = self.jobs.get(state.parent_tts_job_id)
            except LookupError:
                parent = None
            if parent is not None:
                coverage = self._load_voice_qa_coverage(parent)
                rows = self._voice_timing_timeline_rows(parent, coverage) if coverage else []
                source_keys = {
                    (cue.id, cue.index) for cue in snapshot.source_cues if cue.is_dialogue
                }
                row_keys = {(str(row.get("cue_id", "")).strip(), row.get("index")) for row in rows}
                if rows and row_keys == source_keys:
                    cue_timing = tuple(
                        {
                            "cue_id": str(row["cue_id"]),
                            "index": int(row["index"]),
                            "timeline_start_us": int(row["voice_start_us"]),
                            "timeline_end_us": int(row["voice_end_us"]),
                            "source_start_us": int(row["voice_start_us"]),
                            "source_end_us": int(row["voice_end_us"]),
                        }
                        for row in rows
                    )
        updated = self.media_imports.reset_voice_conform(
            snapshot,
            cue_timing=cue_timing,
        )
        summary = self.projects.get_summary(project_id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        self._timeline_editors[project_id] = TimelineEditor(updated)
        return _json_value(asdict(updated))

    def _resolve_voice_base_job(self, snapshot: ProjectSnapshot, requested_id: str):
        return self.jobs.get(self._resolve_voice_base_job_id(snapshot, requested_id))

    def _resolve_voice_base_job_id(self, snapshot: ProjectSnapshot, requested_id: str) -> str:
        requested = requested_id.strip()
        if not requested and snapshot.voice_conform is not None:
            requested = snapshot.voice_conform.parent_tts_job_id
        voice_track = next(
            (track for track in snapshot.tracks if track.kind is TrackKind.VOICE),
            None,
        )
        if voice_track is None or not voice_track.clips:
            raise ValueError("Project has no completed D1 voice track")
        base_media_id = (
            snapshot.voice_conform.base_voice_media_id
            if snapshot.voice_conform is not None
            else next(
                (clip.media_id for clip in voice_track.clips if clip.media_id is not None),
                None,
            )
        )
        if base_media_id is None:
            raise ValueError("Project base D1 media is missing")
        base_asset = next((item for item in snapshot.media if item.id == base_media_id), None)
        if base_asset is None:
            raise ValueError("Project base D1 media is missing")
        base_path = Path(base_asset.path).expanduser().resolve()
        query = """
            SELECT id
            FROM jobs
            WHERE project_id = ?
              AND kind = 'tts'
              AND state = 'succeeded'
              AND purpose = 'production'
              AND json_extract(config_json, '$.output_path') = ?
        """
        parameters: tuple[object, ...] = (snapshot.id, str(base_path))
        if requested:
            query += " AND id = ?"
            parameters += (requested,)
        query += " ORDER BY created_at DESC, id DESC LIMIT 1"
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is not None:
            return str(row["id"])
        if requested:
            raise ValueError("Tác vụ TTS nền được chọn không khớp với D1 hiện tại.")
        raise ValueError("Không tìm thấy tác vụ TTS nền khớp với D1 hiện tại.")

    def _voice_conform_base_cues(
        self,
        snapshot: ProjectSnapshot,
        parent_tts_job_id: str,
        *,
        verify_cache: bool = True,
    ) -> tuple[tuple[VoiceConformCueInput, ...], list[dict[str, Any]]]:
        checkpoint = self.load_worker_checkpoint(
            parent_tts_job_id,
            stage_key="tts",
            checkpoint_key="latest",
        )
        artifact = checkpoint.get("artifact") if isinstance(checkpoint, Mapping) else None
        ledger_path = (
            Path(str(artifact["uri"]))
            if isinstance(artifact, Mapping) and isinstance(artifact.get("uri"), str)
            else self.checkpoints.staging_root / parent_tts_job_id / "voice-ledger-working.json"
        )
        try:
            ledger_stat = ledger_path.stat()
        except OSError as exc:
            raise ValueError("Không đọc được cache WAV nền của tác vụ TTS.") from exc
        cached = self._voice_conform_base_cache.get(parent_tts_job_id)
        if (
            cached is not None
            and not verify_cache
            and cached[0] == str(ledger_path)
            and cached[1] == ledger_stat.st_size
            and cached[2] == ledger_stat.st_mtime_ns
            and cached[3] == snapshot.revision
        ):
            return cached[4], [dict(entry) for entry in cached[5]]
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Không đọc được cache WAV nền của tác vụ TTS.") from exc
        raw_entries = ledger.get("entries") if isinstance(ledger, Mapping) else None
        if not isinstance(raw_entries, Mapping):
            raise ValueError("Ledger TTS nền không có danh sách WAV hợp lệ.")
        cues: list[VoiceConformCueInput] = []
        entries: list[dict[str, Any]] = []
        for source_cue, translated_cue in _voice_subtitle_pairs(snapshot):
            if not source_cue.is_dialogue:
                continue
            raw = raw_entries.get(source_cue.id)
            if not isinstance(raw, Mapping):
                raise ValueError(f"Cache WAV nền thiếu cue {source_cue.index}.")
            # Post-conform starts from the D1 variant currently published by
            # TTS (including read speed), not from the immutable 1x synthesis
            # cache. Otherwise applying conform would silently undo/compound
            # the selected read-speed policy.
            qa = raw.get("qa", raw.get("base_qa"))
            cache_path_raw = raw.get("cache_path")
            cache_sha256 = raw.get("cache_sha256")
            if (
                not isinstance(qa, Mapping)
                or type(qa.get("duration_us")) is not int
                or int(qa["duration_us"]) <= 0
                or not isinstance(cache_path_raw, str)
                or not isinstance(cache_sha256, str)
            ):
                raise ValueError(f"Cache WAV nền cue {source_cue.index} thiếu bằng chứng QA.")
            cache_path = Path(cache_path_raw).expanduser().resolve(strict=True)
            if _file_sha256(cache_path) != cache_sha256:
                raise ValueError(f"Cache WAV nền cue {source_cue.index} không còn toàn vẹn.")
            cues.append(
                VoiceConformCueInput(
                    source_cue.id,
                    source_cue.index,
                    source_cue.range.start_us,
                    source_cue.range.end_us,
                    int(qa["duration_us"]),
                )
            )
            entries.append(
                {
                    "id": source_cue.id,
                    "index": source_cue.index,
                    "start_us": source_cue.range.start_us,
                    "end_us": source_cue.range.end_us,
                    "text": translated_cue.text,
                    "base_wav_path": str(cache_path),
                    "base_wav_sha256": cache_sha256,
                    "base_qa": dict(qa),
                }
            )
        if not cues:
            raise ValueError("Project has no Vietnamese voice cues")
        cached_cues = tuple(cues)
        cached_entries = tuple(entries)
        self._voice_conform_base_cache[parent_tts_job_id] = (
            str(ledger_path),
            ledger_stat.st_size,
            ledger_stat.st_mtime_ns,
            snapshot.revision,
            cached_cues,
            cached_entries,
        )
        return cached_cues, [dict(entry) for entry in cached_entries]

    def create_export_job(
        self,
        *,
        project_id: str,
        output_path: str,
        export_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = dict(export_options or {})
        snapshot = self.projects.load(project_id)
        output = Path(output_path).expanduser().resolve()
        if output.suffix.casefold() != ".mp4" or not output.stem.strip():
            raise ValueError("Export output must be a named MP4 file")
        if output.stem.upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }:
            raise ValueError("Export output uses a reserved Windows name")
        if any(character in output.name for character in '<>:"/\\|?*'):
            raise ValueError("Export output contains invalid Windows filename characters")
        overwrite = options.pop("overwrite", False)
        if type(overwrite) is not bool:
            raise ValueError("Export overwrite option must be boolean")
        if output.exists() and not overwrite:
            raise FileExistsError("Export output already exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_raw = options.pop("ffmpeg_path", shutil.which("ffmpeg") or "")
        ffprobe_raw = options.pop("ffprobe_path", shutil.which("ffprobe") or "")
        ffmpeg = Path(str(ffmpeg_raw)).expanduser().resolve(strict=True)
        ffprobe = Path(str(ffprobe_raw)).expanduser().resolve(strict=True)
        preset_id = str(options.pop("preset_id", "source-quality")).strip().casefold()
        presets = {
            "source-quality": (None, None, "8M"),
            "1080p": (1920, 1080, "6M"),
            "720p": (1280, 720, "4M"),
            "480p": (854, 480, "2M"),
        }
        if preset_id not in presets:
            raise ValueError("Unknown export preset")
        output_width, output_height, default_bitrate = presets[preset_id]
        backend = str(options.pop("backend", "auto")).strip().casefold()
        chunk_minutes = int(options.pop("chunk_minutes", 10))
        subtitle_raw = options.pop("subtitle_style", {})
        if not isinstance(subtitle_raw, dict):
            raise ValueError("Export subtitle style must be an object")
        video_filter_raw = options.pop("video_filter", {})
        if not isinstance(video_filter_raw, dict):
            raise ValueError("Export video filter must be an object")
        settings = ExportSettings(
            preset_id=preset_id,
            backend=backend,
            video_bitrate=str(options.pop("video_bitrate", default_bitrate)),
            audio_bitrate=str(options.pop("audio_bitrate", "256k")),
            output_width=output_width,
            output_height=output_height,
            fps=int(options.pop("fps", 30)),
            chunk_duration_us=chunk_minutes * 60 * 1_000_000,
            overwrite=overwrite,
            subtitle_style=SubtitleStyle(**subtitle_raw),
            video_filter=VideoFilterSettings(**video_filter_raw),
        )
        data_root = Path(str(options.pop("data_root", discover_data_root()))).expanduser().resolve()
        if options:
            raise ValueError(f"Unknown export options: {sorted(options)}")
        render_snapshot = build_export_snapshot(
            snapshot,
            settings=settings,
            media_probe=self.media_probe,
        )
        source_paths = {
            render_snapshot.video.path,
            *(clip.path for clip in render_snapshot.audio),
            *(overlay.asset_path for overlay in render_snapshot.overlays if overlay.asset_path),
        }
        if output in {Path(raw_path).resolve() for raw_path in source_paths}:
            raise ValueError("Export output cannot overwrite any source media")
        file_identities = []
        for raw_path in sorted(source_paths):
            path = Path(raw_path).resolve(strict=True)
            stat = path.stat()
            file_identities.append(
                {
                    "path": str(path),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        snapshot_json = export_snapshot_json(render_snapshot)
        provider_config = {
            "snapshot": snapshot_json,
            "output_path": str(output),
            "ffmpeg_path": str(ffmpeg),
            "ffprobe_path": str(ffprobe),
            "cache_root": str(data_root / "export_cache"),
        }
        raw = {
            "kind": "export",
            "project_id": project_id,
            "input_revision": snapshot.revision,
            "provider_id": "ffmpeg-one-pass",
            "provider_version": "1",
            "model_id": backend,
            "input_fingerprint": stable_fingerprint(
                {"snapshot": snapshot_json, "sources": file_identities}
            ),
            "settings_fingerprint": stable_fingerprint(provider_config),
            "provider_config": provider_config,
        }
        return self.create_job(raw)

    def apply_ocr_rescan(self, job_id: str, raw: object) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        config = job.spec.provider_config
        cue_id = config.get("rescan_cue_id")
        if not isinstance(cue_id, str) or not cue_id:
            raise ValueError("OCR job is not a targeted rescan")
        if not isinstance(raw, dict):
            raise ValueError("OCR rescan result must be an object")
        text = raw.get("text")
        confidence = raw.get("confidence")
        evidence_ids = raw.get("evidence_ids")
        source = raw.get("source")
        if (
            not isinstance(text, str)
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not isinstance(evidence_ids, list)
            or not all(isinstance(item, str) and item for item in evidence_ids)
            or not isinstance(source, str)
        ):
            raise ValueError("OCR rescan result is incomplete")
        snapshot = self.projects.load(job.spec.project_id)
        editor = self._subtitle_editors.get(snapshot.id)
        self._validate_ocr_scan_layout(snapshot, config)
        if editor is None or editor.snapshot.revision != snapshot.revision:
            editor = SubtitleEditor(snapshot)
            self._subtitle_editors[snapshot.id] = editor
        updated = editor.apply_ocr_rescan(
            cue_id,
            text=text,
            confidence=float(confidence),
            evidence_ids=tuple(evidence_ids),
            source=source,
        )
        summary = self.projects.get_summary(snapshot.id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        self._timeline_editors[snapshot.id] = TimelineEditor(updated)
        return _json_value(asdict(updated))

    def get_job(self, job_id: str) -> dict[str, Any]:
        return _json_value(asdict(self.jobs.get(job_id)))

    def transition_job(self, job_id: str, target: str) -> dict[str, Any]:
        return _json_value(asdict(self.jobs.transition(job_id, JobState(target))))

    def load_worker_checkpoint(
        self,
        job_id: str,
        *,
        stage_key: str = "runtime",
        checkpoint_key: str = "latest",
    ) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return self._load_worker_checkpoint_for_job(
            job, stage_key=stage_key, checkpoint_key=checkpoint_key,
        )

    def _load_worker_checkpoint_for_job(
        self, job: JobRecord, *, stage_key: str, checkpoint_key: str,
    ) -> dict[str, Any] | None:
        checkpoint = self.checkpoints.load(
            job_id=job.id,
            stage_key=stage_key,
            checkpoint_key=checkpoint_key,
            resume_fingerprint=job.spec.resume_fingerprint,
        )
        if checkpoint is None:
            return None
        payload = _json_value(asdict(checkpoint))
        if checkpoint.artifact_id is not None:
            payload["artifact"] = _json_value(
                asdict(self.checkpoints.resolve_artifact(checkpoint.artifact_id))
            )
        return payload

    def list_events(
        self,
        job_id: str,
        after_sequence: int = 0,
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            _json_value(asdict(event))
            for event in self.jobs.list_events(
                job_id,
                after_sequence=after_sequence,
                kind=kind,
            )
        ]

    def _translation_source_fingerprint(self, project_id: str) -> str:
        snapshot = self.projects.load(project_id)
        source_cues = [_translation_cue_payload(cue) for cue in snapshot.source_cues]
        return stable_fingerprint(source_cues)

    def _load_translation_cache(
        self, job_id: str, *, job: JobRecord | None = None,
    ) -> tuple[Any, Any] | None:
        job = job if job is not None else self.jobs.get(job_id)
        if job.id != job_id:
            raise ValueError("Translation cache identity mismatch")
        checkpoint = self._load_worker_checkpoint_for_job(
            job,
            stage_key="translation",
            checkpoint_key="latest",
        )
        if checkpoint is None:
            return None
        artifact = checkpoint.get("artifact")
        if not isinstance(artifact, Mapping) or not isinstance(artifact.get("uri"), str):
            raise ValueError("Translation cache checkpoint has no immutable ledger")
        path = Path(str(artifact["uri"])).expanduser().resolve(strict=True)
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, Mapping):
            raise ValueError("Translation cache ledger is invalid")
        ledger = ledger_from_dict(raw)
        return job, self._recover_pending_translation_qa(job.id, ledger)

    def _recover_pending_translation_qa(
        self,
        job_id: str,
        ledger: TranslationLedger,
    ) -> TranslationLedger:
        """Recover review details cleared by pre-a48 resume checkpoints.

        a47 reopened QA-review chunks as pending and removed their issue rows.
        Immutable older ledgers still contain those rows.  Reading the newest
        matching review entry is conservative: the cue remains unverified until
        a later checkpoint explicitly marks that chunk QA_PASS.
        """

        unresolved = {
            chunk.chunk_index
            for chunk in ledger.chunks
            if chunk.state is ChunkState.PENDING
            and not chunk.qa_issues
            and all(cue_id in ledger.translations for cue_id in chunk.cue_ids)
        }
        if not unresolved:
            return ledger
        with self.database.connect(read_only=True) as connection:
            artifact_ids = [
                str(row["id"])
                for row in connection.execute(
                    """
                    SELECT id FROM artifacts
                    WHERE job_id = ? AND kind = 'translation_ledger'
                    ORDER BY created_at DESC, rowid DESC
                    """,
                    (job_id,),
                ).fetchall()
            ]
        chunks = list(ledger.chunks)
        for artifact_id in artifact_ids:
            if not unresolved:
                break
            try:
                artifact = self.checkpoints.resolve_artifact(artifact_id)
                raw = json.loads(Path(artifact.uri).read_text(encoding="utf-8-sig"))
                if not isinstance(raw, Mapping):
                    continue
                candidate = ledger_from_dict(raw)
            except (LookupError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
                continue
            if candidate.signature != ledger.signature or len(candidate.chunks) != len(chunks):
                continue
            for chunk_index in tuple(unresolved):
                old = candidate.chunks[chunk_index]
                current = chunks[chunk_index]
                if (
                    old.state is ChunkState.QA_REVIEW
                    and old.cue_ids == current.cue_ids
                    and old.qa_issues
                ):
                    chunks[chunk_index] = replace(current, qa_issues=old.qa_issues)
                    unresolved.remove(chunk_index)
        return replace(ledger, chunks=tuple(chunks))

    def list_translation_caches(self, project_id: str) -> list[dict[str, Any]]:
        snapshot = self.projects.load(project_id)
        source_fingerprint = self._translation_source_fingerprint(project_id)
        cue_total = len(snapshot.source_cues)
        state_labels = {
            JobState.QUEUED: "Đang chờ",
            JobState.START_REQUESTED: "Đang khởi động",
            JobState.RUNNING: "Đang chạy",
            JobState.PAUSE_REQUESTED: "Đang tạm dừng",
            JobState.PAUSED: "Đã tạm dừng",
            JobState.RESUME_REQUESTED: "Đang tiếp tục",
            JobState.CANCEL_REQUESTED: "Đang hủy",
            JobState.CANCELLED: "Đã hủy",
            JobState.SUCCEEDED: "Hoàn tất",
            JobState.FAILED: "Thất bại",
            JobState.INTERRUPTED: "Bị gián đoạn",
        }
        caches: list[dict[str, Any]] = []
        for job in self.jobs.list_project_jobs(
            project_id=project_id,
            kind=JobKind.TRANSLATE,
            input_fingerprint=source_fingerprint,
            compact_translation=True,
        ):
            try:
                loaded = self._load_translation_cache(job.id, job=job)
                if loaded is None:
                    continue
                _record, ledger = loaded
                translated_ids = tuple(sorted(int(index) for index in ledger.translations))
                if any(index < 1 or index > cue_total for index in translated_ids):
                    continue
                review = self._manual_translation_review_state(project_id, job, snapshot=snapshot)
                qa = self._overlay_manual_translation_review(
                    translation_qa_snapshot(ledger, snapshot.source_cues),
                    review,
                )
                blockers = int(qa["blocking_count"])
            except (
                KeyError,
                LookupError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                # One stale/corrupt historical checkpoint must not hide every
                # other compatible cache from the selector.
                continue
            effective_state = (
                "manual_ready"
                if review.get("ready") is True
                else (
                    "review" if job.error_code == "translation_review_required" else job.state.value
                )
            )
            state_label = (
                "Cần rà soát"
                if effective_state == "review"
                else (
                    "Đã sửa tay · sẵn sàng lồng tiếng"
                    if effective_state == "manual_ready"
                    else state_labels.get(job.state, job.state.value)
                )
            )
            completed = len(translated_ids)
            caches.append(
                {
                    "job_id": job.id,
                    "completed_cues": completed,
                    "cue_total": cue_total,
                    "blocking_count": blockers,
                    "state": job.state.value,
                    "display_state": effective_state,
                    "error_code": job.error_code or "",
                    "provider_id": job.spec.provider_id,
                    "model_id": job.spec.model_id,
                    "created_at": job.created_at,
                    "provider_config": self._translation_selection_config(job),
                    "resumable": job.state
                    in {
                        JobState.QUEUED,
                        JobState.PAUSED,
                        JobState.FAILED,
                        JobState.INTERRUPTED,
                        JobState.CANCELLED,
                    },
                    "label": (
                        f"{completed}/{cue_total} cue · {state_label} · "
                        f"{job.spec.provider_id} / {job.spec.model_id}"
                    ),
                }
            )
        caches.sort(key=lambda item: str(item["created_at"]), reverse=True)
        caches.sort(
            key=lambda item: (int(item["completed_cues"]), -int(item["blocking_count"])),
            reverse=True,
        )
        return caches

    def get_translation_preview(
        self,
        project_id: str,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.projects.load(project_id)
        selected_id = job_id.strip() if isinstance(job_id, str) else ""
        if selected_id:
            source_fingerprint = self._translation_source_fingerprint(project_id)
            selected_job = self.jobs.get(selected_id)
            if (
                selected_job.spec.kind is not JobKind.TRANSLATE
                or selected_job.spec.project_id != project_id
                or selected_job.spec.input_fingerprint != source_fingerprint
            ):
                raise ValueError("Cache dịch không tương thích với phụ đề nguồn hiện tại")
            loaded = self._load_translation_cache(selected_id, job=selected_job)
            if loaded is None:
                raise ValueError("Cache dịch chưa có checkpoint để xem hoặc tiếp tục")
            job, ledger = loaded
            if any(index < 1 or index > len(snapshot.source_cues) for index in ledger.translations):
                raise ValueError("Cache dịch không tương thích với phụ đề nguồn hiện tại")
            display_state = (
                "review" if job.error_code == "translation_review_required" else job.state.value
            )
        else:
            caches = self.list_translation_caches(project_id)
            selected = caches[0] if caches else None
            if selected is None:
                return {
                    "project_id": project_id,
                    "job_id": "",
                    "values": [],
                    "qa": {
                        "status": "NONE",
                        "completed_cues": 0,
                        "checked_cues": 0,
                        "cue_total": len(snapshot.source_cues),
                        "blocking_count": 0,
                        "blocking_issue_count": 0,
                        "warning_count": 0,
                        "issues": [],
                        "content_warnings": [],
                    },
                }
            loaded = self._load_translation_cache(str(selected["job_id"]))
            if loaded is None:
                return {"project_id": project_id, "job_id": "", "values": []}
            job, ledger = loaded
            display_state = str(selected["display_state"])
        raw_source = job.spec.provider_config.get("source_cues", ())
        if not isinstance(raw_source, (tuple, list)):
            raise ValueError("Translation preview source cues are invalid")
        source_ids = {
            int(item["index"]): str(item["id"])
            for item in raw_source
            if isinstance(item, Mapping)
            and type(item.get("index")) is int
            and isinstance(item.get("id"), str)
        }
        manual_text_by_index: dict[int, str] = {}
        manual_review = self._manual_translation_review_state(
            job.spec.project_id,
            job,
        )
        if manual_review.get("available") is True:
            manual_text_by_index = {
                int(item["index"]): str(item["text"])
                for item in manual_review.get("values", [])
                if isinstance(item, Mapping) and type(item.get("index")) is int
            }
        values: list[dict[str, object]] = []
        for index, text in sorted(ledger.translations.items()):
            cue_id = source_ids.get(index)
            if not cue_id or not str(text).strip():
                raise ValueError("Translation preview ledger does not match source cue IDs")
            values.append(
                {
                    "id": cue_id,
                    "index": index,
                    "text": manual_text_by_index.get(index, str(text)),
                }
            )
        qa = self._overlay_manual_translation_review(
            translation_qa_snapshot(ledger, snapshot.source_cues),
            manual_review,
        )
        return {
            "project_id": project_id,
            "job_id": job.id,
            "state": job.state.value,
            "display_state": display_state,
            "error_code": job.error_code or "",
            "provider_id": job.spec.provider_id,
            "model_id": job.spec.model_id,
            "completed_cues": len(values),
            "provider_config": self._translation_selection_config(job),
            "qa": qa,
            "values": values,
        }

    @staticmethod
    def _translation_selection_config(job: JobRecord) -> dict[str, Any]:
        """Expose selection identity without transporting the full cue snapshot."""
        return {key: thaw_json(job.spec.provider_config[key]) for key in (
            "base_url", "credential_references", "translation_style_preset",
            "translation_custom_rules",
        ) if key in job.spec.provider_config}

    def get_translation_qa(self, job_id: str) -> dict[str, Any]:
        loaded = self._load_translation_cache(job_id)
        if loaded is None:
            return {
                "status": "NONE",
                "completed_cues": 0,
                "checked_cues": 0,
                "cue_total": 0,
                "blocking_count": 0,
                "blocking_issue_count": 0,
                "warning_count": 0,
                "issues": [],
                "content_warnings": [],
            }
        job, ledger = loaded
        snapshot = self.projects.load(job.spec.project_id)
        review = self._manual_translation_review_state(job.spec.project_id, job)
        return self._overlay_manual_translation_review(
            translation_qa_snapshot(ledger, snapshot.source_cues),
            review,
        )

    def get_manual_translation_review_state(
        self,
        project_id: str,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Return whether a Multi CJK review SRT is ready for TTS.

        ``job_id`` is preferred because it ties the decision to the exact
        failed Multi child.  When a new Multi run has reset the item to its
        first stage, the latest compatible Multi review job is used instead.
        """

        selected = None
        if isinstance(job_id, str) and job_id.strip():
            try:
                candidate = self.jobs.get(job_id.strip())
            except LookupError:
                candidate = None
            if candidate is not None:
                selected = candidate
        if selected is None:
            try:
                source_fingerprint = self._translation_source_fingerprint(project_id)
                candidates = self.jobs.list_project_jobs(
                    project_id=project_id,
                    kind=JobKind.TRANSLATE,
                    input_fingerprint=source_fingerprint,
                )
            except (LookupError, OSError, ValueError):
                candidates = ()
            selected = next(
                (
                    candidate
                    for candidate in reversed(tuple(candidates))
                    if str(candidate.error_code or "") == "translation_review_required"
                    and isinstance(candidate.spec.provider_config, Mapping)
                    and candidate.spec.provider_config.get("qa_policy") == "multi_auto"
                ),
                None,
            )
        if selected is None:
            return {"available": False}
        return self._manual_translation_review_state(project_id, selected)

    def _manual_translation_review_state(
        self,
        project_id: str,
        job: object,
        *,
        snapshot: ProjectSnapshot | None = None,
    ) -> dict[str, Any]:
        """Inspect a translation-review SRT after the user edits it by hand.

        Multi deliberately accepts this only for the immutable review import
        produced by a failed CJK translation job.  A normal translated SRT or
        a partial project must never be mistaken for a completed stage.
        """

        spec = getattr(job, "spec", None)
        provider_config = getattr(spec, "provider_config", {})
        if not isinstance(provider_config, Mapping):
            return {"available": False}
        if str(provider_config.get("qa_policy", "")).strip() != "multi_auto":
            return {"available": False}
        if str(getattr(job, "error_code", "") or "") != "translation_review_required":
            return {"available": False}
        if snapshot is None:
            try:
                snapshot = self.projects.load(project_id)
            except (LookupError, OSError, ValueError):
                return {"available": False}
        source_by_index = {cue.index: cue for cue in snapshot.source_cues}
        target_by_index = {cue.index: cue for cue in snapshot.translated_cues}
        values: list[dict[str, object]] = []
        invalid_ids: list[int] = []
        cjk_ids: list[int] = []
        for index, source in source_by_index.items():
            target = target_by_index.get(index)
            if (
                target is None
                or target.id != source.id
                or target.range != source.range
                or not target.text.strip()
                or str(target.source) != "translation_review"
            ):
                invalid_ids.append(index)
                continue
            text = target.text.strip()
            values.append({"id": target.id, "index": index, "text": text})
            if contains_cjk(text):
                cjk_ids.append(index)
        invalid_ids.extend(index for index in target_by_index if index not in source_by_index)
        return {
            "available": True,
            "ready": bool(source_by_index)
            and not invalid_ids
            and len(values) == len(source_by_index)
            and not cjk_ids,
            "values": values,
            "cjk_cue_ids": sorted(set(cjk_ids)),
            "invalid_cue_ids": sorted(set(invalid_ids)),
        }

    def _live_multi_translation_review(
        self,
        item: Mapping[str, object],
        *,
        snapshot: ProjectSnapshot | None = None,
    ) -> tuple[dict[str, object], dict[str, Any] | None]:
        """Overlay the current manual-review CJK count onto a Multi card.

        The batch item intentionally keeps the terminal worker error so resume
        and retry accounting remain durable.  This method only creates a
        display projection; it never mutates the batch database row.
        """

        if (
            str(item.get("stage", "")) != MultiStage.TRANSLATE.value
            or str(item.get("error_code", "")) != "translation_review_required"
        ):
            return dict(item), None
        project_id = str(item.get("project_id", ""))
        if not project_id:
            return dict(item), None
        job_id = str(item.get("child_job_id", ""))
        job = None
        if job_id:
            try:
                job = self.jobs.get(job_id)
            except LookupError:
                job = None
        review: dict[str, Any]
        if job is not None:
            revision = int(snapshot.revision) if snapshot is not None else -1
            cache_key = (project_id, revision, str(getattr(job, "id", job_id)))
            cached = self._multi_manual_review_cache.get(cache_key)
            if cached is None:
                review = self._manual_translation_review_state(
                    project_id,
                    job,
                    snapshot=snapshot,
                )
                if len(self._multi_manual_review_cache) >= 256:
                    self._multi_manual_review_cache.pop(next(iter(self._multi_manual_review_cache)))
                self._multi_manual_review_cache[cache_key] = dict(review)
            else:
                review = dict(cached)
        else:
            # A deferred item can lose its child id after an older recovery
            # path.  Fall back to the latest compatible Multi review job.
            review = self.get_manual_translation_review_state(project_id)
        if review.get("available") is not True:
            return dict(item), review

        result = dict(item)
        raw_payload = item.get("error_payload", {})
        payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        cjk_ids = sorted(
            int(value) for value in review.get("cjk_cue_ids", []) if type(value) is int
        )
        invalid_ids = sorted(
            int(value) for value in review.get("invalid_cue_ids", []) if type(value) is int
        )
        payload.update(
            {
                "cjk_cue_ids": cjk_ids,
                "cjk_blocker_count": len(cjk_ids),
                "manual_review_remaining_cjk": len(cjk_ids),
                "invalid_cue_ids": invalid_ids,
            }
        )
        result["error_payload"] = payload
        result["manual_review_available"] = True
        result["manual_review_remaining_cjk"] = len(cjk_ids)
        result["manual_review_ready"] = review.get("ready") is True
        if review.get("ready") is True:
            # This is a projection only.  The coordinator still reads the
            # durable failed/translate row and promotes it to TTS on resume.
            result.update(
                {
                    "state": MultiItemState.COMPLETED.value,
                    "progress": 1.0,
                    "error_code": "",
                    "project_status_override": True,
                    "project_status_state": "completed",
                    "project_status_text": ("Đã sửa xong chữ Trung · sẵn sàng lồng tiếng"),
                    "project_status_stage": MultiStage.TTS.value,
                    "project_status_progress": 1.0,
                    "project_error_message": "",
                }
            )
            payload.update(
                {
                    "cjk_cue_ids": [],
                    "cjk_blocker_count": 0,
                    "manual_review_ready": True,
                }
            )
            result["error_payload"] = payload
        return result, review

    @staticmethod
    def _overlay_manual_translation_review(
        qa: dict[str, Any],
        review: Mapping[str, object],
    ) -> dict[str, Any]:
        """Replace stale CJK rows with the current review-SRT text."""

        if review.get("available") is not True:
            return qa
        values_by_index = {
            int(item["index"]): str(item["text"])
            for item in review.get("values", [])
            if isinstance(item, Mapping) and type(item.get("index")) is int
        }
        remaining_cjk = {
            int(value) for value in review.get("cjk_cue_ids", []) if type(value) is int
        }
        rows = []
        for row in qa.get("issues", []):
            if not isinstance(row, dict):
                continue
            index = row.get("id")
            codes = row.get("codes", [])
            has_cjk = isinstance(codes, list) and any(
                "cjk" in str(code).casefold() for code in codes
            )
            if has_cjk and type(index) is int and index not in remaining_cjk:
                continue
            copied = dict(row)
            if type(index) is int and index in values_by_index:
                copied["translated_text"] = values_by_index[index]
            rows.append(copied)
        result = dict(qa)
        result["issues"] = rows
        result["blocking_count"] = len(rows)
        result["blocking_issue_count"] = sum(
            max(1, len(row.get("details", []))) for row in rows if isinstance(row, dict)
        )
        result["manual_review_available"] = True
        result["manual_review_ready"] = review.get("ready") is True
        result["cjk_cue_ids"] = sorted(remaining_cjk)
        if review.get("ready") is True and result.get("status") != "PENDING":
            result["status"] = "PASS" if not rows else "REVIEW"
        return result

    def get_voice_timing_review(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if (
            job.spec.kind is not JobKind.TTS
            or job.spec.provider_config.get("purpose") != "production"
        ):
            raise ValueError("Chỉ tác vụ voice thành phẩm mới có QA timeline.")
        is_review = job.error_code == "tts_review_required"
        if not is_review and job.state is not JobState.SUCCEEDED:
            raise ValueError("Tác vụ voice chưa có bằng chứng timeline hoàn chỉnh.")
        checkpoint = self.load_worker_checkpoint(
            job.id,
            stage_key="tts",
            checkpoint_key="review",
        )
        coverage: dict[str, Any] = {}
        if isinstance(checkpoint, Mapping):
            payload = checkpoint.get("payload")
            raw_coverage = payload.get("coverage") if isinstance(payload, Mapping) else None
            if isinstance(raw_coverage, Mapping):
                coverage = dict(raw_coverage)
        if not coverage:
            qa_raw = job.spec.provider_config.get("qa_path")
            if isinstance(qa_raw, str) and qa_raw:
                try:
                    loaded_qa = json.loads(
                        Path(qa_raw)
                        .expanduser()
                        .resolve(strict=True)
                        .read_text(encoding="utf-8-sig")
                    )
                except (OSError, json.JSONDecodeError):
                    loaded_qa = None
                if isinstance(loaded_qa, Mapping):
                    coverage = dict(loaded_qa)
        raw_issues = coverage.get("timing_issues", [])
        issues = [dict(item) for item in raw_issues if isinstance(item, Mapping)]
        # New workers always publish ``timing_issues`` (including an empty
        # array).  An empty, explicit list is a verified PASS and must not be
        # replaced with findings recovered from an older job.  Recovery is
        # only for legacy artifacts that predate this field entirely.
        if "timing_issues" not in coverage:
            issues = self._recover_voice_timing_issues(job)
        cycle = int(job.spec.provider_config.get("timing_repair_cycle", 0) or 0)
        maximum_cycles = int(job.spec.provider_config.get("max_timing_repair_cycles", 3) or 3)
        timing = coverage.get("timing")
        timing = dict(timing) if isinstance(timing, Mapping) else {}
        overflow_us = max(
            0,
            int(timing.get("final_voice_end_us", 0) or 0)
            - int(timing.get("media_duration_us", 0) or 0),
        )
        cue_timeline = self._voice_timing_timeline_rows(job, coverage)
        qa_non_blocking = job.spec.provider_config.get("qa_non_blocking") is True
        # A succeeded non-blocking TTS job is publishable even when measured
        # cue timing still needs adjustment. Keep those findings visible as
        # warnings instead of relabelling the completed job as blocked review.
        status = "REVIEW" if is_review or (issues and not qa_non_blocking) else "PASS"
        return {
            "status": status,
            "job_id": job.id,
            "project_id": job.spec.project_id,
            "error_code": job.error_code or "",
            "issue_count": len(issues),
            "issues": issues,
            "overflow_us": overflow_us,
            "maximum_delay_us": int(timing.get("maximum_delay_us", 0) or 0),
            "repair_cycle": cycle,
            "max_repair_cycles": maximum_cycles,
            "repairable": bool(issues) and cycle < maximum_cycles,
            "timing_warning_count": len(issues) if qa_non_blocking else 0,
            "qa_non_blocking": qa_non_blocking,
            "voice_read_speed": float(coverage.get("voice_read_speed", 1.0) or 1.0),
            "cue_max_total_speed": float(coverage.get("cue_max_total_speed", 1.0) or 1.0),
            "adjusted_cue_count": int(coverage.get("adjusted_cue_count", 0) or 0),
            "maximum_additional_factor": float(
                coverage.get("maximum_additional_factor", 1.0) or 1.0
            ),
            "unresolved_timing_cue_count": len(issues),
            "cue_timeline": cue_timeline,
            "message": (
                (
                    f"Có {len(issues)} cue dài làm voice vượt timeline "
                    f"{overflow_us / 1_000_000:.2f} giây; đây là cảnh báo, "
                    "có thể tự khớp hoặc chỉnh tại Timeline."
                    if qa_non_blocking
                    else f"Có {len(issues)} cue dài làm voice vượt timeline "
                    f"{overflow_us / 1_000_000:.2f} giây."
                )
                if issues
                else (
                    f"Voice đạt QA theo {len(cue_timeline)} cue timestamp SRT; "
                    "các WAV đã được đo bằng duration thực tế."
                    if cue_timeline
                    else "Voice đạt QA nhưng ledger cue chưa có dữ liệu hiển thị."
                )
            ),
        }

    def _voice_timing_timeline_rows(
        self,
        job: Any,
        coverage: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        config = job.spec.provider_config
        raw_cues = config.get("cues", ())
        if not isinstance(raw_cues, (tuple, list)):
            return []
        try:
            cues = tuple(
                TtsCueRequest(
                    str(raw["id"]),
                    int(raw["index"]),
                    int(raw["start_us"]),
                    int(raw["end_us"]),
                    str(raw["text"]),
                    "timeline-evidence",
                )
                for raw in raw_cues
                if isinstance(raw, Mapping)
            )
        except (KeyError, TypeError, ValueError):
            return []
        ledger: Mapping[str, Any] | None = None
        checkpoint = self.load_worker_checkpoint(
            job.id,
            stage_key="tts",
            checkpoint_key="latest",
        )
        artifact = checkpoint.get("artifact") if isinstance(checkpoint, Mapping) else None
        candidate_paths = []
        if isinstance(artifact, Mapping) and isinstance(artifact.get("uri"), str):
            candidate_paths.append(Path(str(artifact["uri"])))
        candidate_paths.append(self.checkpoints.staging_root / job.id / "voice-ledger-working.json")
        for path in candidate_paths:
            try:
                raw_ledger = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw_ledger, Mapping):
                ledger = raw_ledger
                break
        if ledger is None:
            return []
        entries = ledger.get("entries", {})
        blockers = ledger.get("blockers", {})
        if not isinstance(entries, Mapping) or not isinstance(blockers, Mapping):
            return []
        timing = coverage.get("timing")
        timing = timing if isinstance(timing, Mapping) else {}
        # A worker result is validated and imported before the durable job state
        # moves from RUNNING to SUCCEEDED. Derive serialization from the same
        # no-loss policy that assembled the WAV, never from that later state
        # transition, otherwise adjacent cue intervals can overlap only during
        # the publish window and make a valid result fail import.
        serialize_overlaps = bool(
            int(timing.get("shifted_cues", 0) or 0) > 0
            or timing.get("mode") == "base_no_loss"
            or config.get("defer_timing_conform") is True
            or config.get("qa_non_blocking") is True
            or config.get("allow_timeline_overflow") is True
        )
        default_speed = float(
            coverage.get(
                "voice_read_speed",
                config.get("voice_read_speed", 1.0),
            )
            or 1.0
        )
        global_early_shift_us = max(
            0,
            int(timing.get("global_early_shift_us", 0) or 0),
        )
        rows = [
            dict(item)
            for item in build_voice_timeline_rows(
                cues,
                entries,
                blockers,
                serialize_overlaps=serialize_overlaps,
                default_speed=default_speed,
                global_early_shift_us=global_early_shift_us,
            )
        ]
        warning_by_cue = {
            str(raw.get("id", "")): tuple(
                str(code)
                for code in raw.get("content_warning_codes", [])
                if isinstance(code, str) and code
            )
            for raw in raw_cues
            if isinstance(raw, Mapping) and raw.get("content_warning") is True
        }
        for row in rows:
            codes = warning_by_cue.get(str(row.get("cue_id", "")), ())
            if not codes:
                continue
            row["content_warning"] = True
            row["content_warning_codes"] = list(codes)
            if not row.get("issue_code"):
                row["issue_code"] = codes[0]
        return rows

    @staticmethod
    def _load_voice_qa_coverage(job: Any) -> dict[str, Any]:
        config = job.spec.provider_config
        staging_raw = config.get("staging_qa_path")
        qa_raw = (
            staging_raw
            if isinstance(staging_raw, str)
            and staging_raw.strip()
            and Path(staging_raw).expanduser().is_file()
            else config.get("qa_path")
        )
        if not isinstance(qa_raw, str) or not qa_raw.strip():
            return {}
        try:
            loaded = json.loads(
                Path(qa_raw).expanduser().resolve(strict=True).read_text(encoding="utf-8-sig")
            )
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(loaded) if isinstance(loaded, Mapping) else {}

    def _recover_voice_timing_issues(self, job: Any) -> list[dict[str, Any]]:
        config = job.spec.provider_config
        raw_cues = config.get("cues", ())
        raw_settings = config.get("pipeline_settings", {})
        if not isinstance(raw_cues, (tuple, list)) or not isinstance(raw_settings, Mapping):
            return []
        try:
            settings = VoicePipelineSettings(**dict(raw_settings))
            cues = tuple(
                TtsCueRequest(
                    str(raw["id"]),
                    int(raw["index"]),
                    int(raw["start_us"]),
                    int(raw["end_us"]),
                    str(raw["text"]),
                    "timing-review",
                )
                for raw in raw_cues
                if isinstance(raw, Mapping)
            )
        except (KeyError, TypeError, ValueError):
            return []
        # Never borrow timing blockers from another run.  Two jobs can share
        # project/input/provider/voice while intentionally using different
        # read-speed or cue-speed settings; importing an old review would mark
        # a newer successful job as REVIEW and trigger an unnecessary rewrite.
        # The current job's latest checkpoint is the only durable evidence
        # eligible for legacy recovery.
        candidates = (job,)
        for candidate in candidates:
            checkpoint = self.load_worker_checkpoint(
                candidate.id,
                stage_key="tts",
                checkpoint_key="latest",
            )
            artifact = checkpoint.get("artifact") if isinstance(checkpoint, Mapping) else None
            if not isinstance(artifact, Mapping) or not isinstance(artifact.get("uri"), str):
                continue
            try:
                ledger = json.loads(Path(str(artifact["uri"])).read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(ledger, Mapping):
                continue
            entries = ledger.get("entries", {})
            blockers = ledger.get("blockers", {})
            if not isinstance(entries, Mapping) or not isinstance(blockers, Mapping):
                continue
            issues = measured_voice_timing_issues(
                cues,
                entries,
                blockers,
                max_absolute_speed=settings.max_absolute_speed,
            )
            if issues:
                return [dict(item) for item in issues]
        return []

    def create_voice_timing_repair_job(
        self,
        *,
        project_id: str,
        tts_job_id: str,
        translation_job_id: str | None = None,
    ) -> dict[str, Any]:
        review = self.get_voice_timing_review(tts_job_id)
        if review["project_id"] != project_id:
            raise ValueError("Tác vụ voice không thuộc dự án đang mở.")
        if review["repairable"] is not True:
            raise ValueError("Cue dài đã chạm giới hạn tự sửa; cần chỉnh thủ công.")
        active = self.jobs.active_id(project_id=project_id, kind=JobKind.TRANSLATE)
        if active:
            raise ValueError("Dự án đang có một tác vụ dịch khác hoạt động.")
        for candidate in self.jobs.list_recent(limit=10_000):
            if (
                candidate.spec.kind is JobKind.TRANSLATE
                and candidate.spec.project_id == project_id
                and candidate.spec.provider_config.get("parent_tts_job_id") == tts_job_id
            ):
                return _json_value(asdict(candidate))
        selected_job_id = (translation_job_id or "").strip()
        if not selected_job_id:
            caches = self.list_translation_caches(project_id)
            selected = next(
                (
                    item
                    for item in caches
                    if int(item.get("completed_cues", 0)) == int(item.get("cue_total", 0))
                    and int(item.get("blocking_count", 0)) == 0
                ),
                None,
            )
            if selected is None:
                raise ValueError("Không tìm thấy cache dịch PASS để rút gọn cue dài.")
            selected_job_id = str(selected["job_id"])
        loaded = self._load_translation_cache(selected_job_id)
        if loaded is None:
            raise ValueError("Cache dịch được chọn chưa có ledger để sửa.")
        base_job, base_ledger = loaded
        if (
            base_job.spec.project_id != project_id
            or base_job.spec.input_fingerprint != self._translation_source_fingerprint(project_id)
        ):
            raise ValueError("Cache dịch không còn tương thích với phụ đề nguồn.")
        snapshot = self.projects.load(project_id)
        if len(snapshot.translated_cues) != len(snapshot.source_cues):
            raise ValueError("Bản dịch hiện tại chưa đủ cue để rút gọn tự động.")
        current_translations = {
            cue.index: cue.text for cue in snapshot.translated_cues if cue.text.strip()
        }
        if len(current_translations) != len(snapshot.source_cues):
            raise ValueError("Bản dịch hiện tại có cue rỗng.")
        timing_issues = [dict(item) for item in review["issues"]]
        issue_by_index = {
            int(item["id"]): TranslationQaIssue(
                int(item["id"]),
                "CUE_TOO_LONG",
                TranslationQaSeverity.BLOCKER,
                str(item["message"]),
            )
            for item in timing_issues
        }
        chunks = []
        for entry in base_ledger.chunks:
            chunk_issues = tuple(
                issue_by_index[index] for index in entry.cue_ids if index in issue_by_index
            )
            chunks.append(
                replace(
                    entry,
                    # Timing-repair chunks must remain executable.  QA_REVIEW is
                    # a terminal ledger state and the worker deliberately skips
                    # it during resume, which would publish the unchanged long
                    # text without ever asking the provider to condense it.
                    state=ChunkState.PENDING if chunk_issues else ChunkState.QA_PASS,
                    attempts=0,
                    qa_issues=chunk_issues,
                )
            )
        seed = replace(
            base_ledger,
            chunks=tuple(chunks),
            translations=current_translations,
        )
        repair_id = str(uuid4())
        videos = [item for item in snapshot.media if item.kind is MediaKind.VIDEO]
        if not videos:
            raise ValueError("Dự án không còn video để sửa timeline voice.")
        output_path = (
            self.checkpoints.staging_root
            / repair_id
            / f"{Path(videos[0].path).stem}_timing_{int(review['repair_cycle']) + 1}_codex_vi.srt"
        )
        config = _json_value(dict(base_job.spec.provider_config))
        config.update(
            {
                "source_path": str(Path(videos[0].path).with_suffix(".srt")),
                "output_path": str(output_path),
                "title": snapshot.name,
                "source_cues": [
                    _translation_cue_payload(cue) for cue in snapshot.source_cues
                ],
                "translation_purpose": "voice_timing_repair",
                "parent_tts_job_id": tts_job_id,
                "timing_repair_cycle": int(review["repair_cycle"]) + 1,
                "max_timing_repair_cycles": int(review["max_repair_cycles"]),
                "timing_repair_targets": timing_issues,
                "seed_ledger": ledger_to_dict(seed),
            }
        )
        raw = {
            "kind": "translate",
            "project_id": project_id,
            "input_revision": snapshot.revision,
            "provider_id": base_job.spec.provider_id,
            "provider_version": base_job.spec.provider_version,
            "model_id": base_job.spec.model_id,
            "input_fingerprint": self._translation_source_fingerprint(project_id),
            "settings_fingerprint": stable_fingerprint(config),
            "provider_config": config,
        }
        self.projects.flush_edits(project_id)
        record = self.jobs.create(decode_job_spec(raw), job_id=repair_id)
        return _json_value(asdict(record))

    def create_tts_timing_retry_job(
        self,
        *,
        project_id: str,
        parent_tts_job_id: str,
        repair_job_id: str,
    ) -> dict[str, Any]:
        parent = self.jobs.get(parent_tts_job_id)
        repair = self.jobs.get(repair_job_id)
        if (
            parent.spec.kind is not JobKind.TTS
            or repair.spec.kind is not JobKind.TRANSLATE
            or parent.spec.project_id != project_id
            or repair.spec.project_id != project_id
            or repair.state is not JobState.SUCCEEDED
            or repair.spec.provider_config.get("parent_tts_job_id") != parent_tts_job_id
        ):
            raise ValueError("Chuỗi tự sửa voice không hợp lệ hoặc chưa hoàn tất.")
        active = self.jobs.active_id(project_id=project_id, kind=JobKind.TTS)
        if active:
            raise ValueError("Dự án đang có một tác vụ voice khác hoạt động.")
        snapshot = self.projects.load(project_id)
        dialogue_ids = {cue.id for cue in dialogue_cues(snapshot.source_cues)}
        cues = [
            {
                "id": cue.id,
                "index": cue.index,
                "start_us": cue.range.start_us,
                "end_us": cue.range.end_us,
                "text": cue.text,
            }
            for cue in snapshot.translated_cues
            if cue.id in dialogue_ids
        ]
        input_fingerprint = stable_fingerprint(cues)
        for candidate in self.jobs.list_recent(limit=10_000):
            if (
                candidate.spec.kind is JobKind.TTS
                and candidate.spec.project_id == project_id
                and candidate.spec.provider_config.get("timing_repair_job_id") == repair_job_id
                and candidate.spec.input_fingerprint == input_fingerprint
            ):
                return _json_value(asdict(candidate))
        config = _json_value(dict(parent.spec.provider_config))
        config.update(
            {
                "cues": cues,
                "force_cue_ids": [],
                "parent_tts_job_id": parent_tts_job_id,
                "timing_repair_root_job_id": str(
                    parent.spec.provider_config.get("timing_repair_root_job_id")
                    or parent_tts_job_id
                ),
                "timing_repair_job_id": repair_job_id,
                "timing_repair_cycle": int(
                    repair.spec.provider_config.get("timing_repair_cycle", 1)
                ),
                "max_timing_repair_cycles": int(
                    repair.spec.provider_config.get("max_timing_repair_cycles", 3)
                ),
            }
        )
        raw = {
            "kind": "tts",
            "project_id": project_id,
            "input_revision": snapshot.revision,
            "provider_id": parent.spec.provider_id,
            "provider_version": parent.spec.provider_version,
            "model_id": parent.spec.model_id,
            "input_fingerprint": input_fingerprint,
            "settings_fingerprint": stable_fingerprint(config),
            "provider_config": config,
        }
        return self.create_job(raw)

    def create_tts_timing_override_job(
        self,
        *,
        project_id: str,
        parent_tts_job_id: str,
    ) -> dict[str, Any]:
        review = self.get_voice_timing_review(parent_tts_job_id)
        if review["project_id"] != project_id:
            raise ValueError("Tác vụ voice không thuộc dự án đang mở.")
        active = self.jobs.active_id(project_id=project_id, kind=JobKind.TTS)
        if active:
            raise ValueError("Dự án đang có một tác vụ voice khác hoạt động.")
        parent = self.jobs.get(parent_tts_job_id)
        snapshot = self.projects.load(project_id)
        dialogue_ids = {cue.id for cue in dialogue_cues(snapshot.source_cues)}
        cues = [
            {
                "id": cue.id,
                "index": cue.index,
                "start_us": cue.range.start_us,
                "end_us": cue.range.end_us,
                "text": cue.text,
            }
            for cue in snapshot.translated_cues
            if cue.id in dialogue_ids
        ]
        input_fingerprint = stable_fingerprint(cues)
        for candidate in self.jobs.list_recent(limit=10_000):
            if (
                candidate.spec.kind is JobKind.TTS
                and candidate.spec.project_id == project_id
                and candidate.spec.provider_config.get("timing_override_parent_job_id")
                == parent_tts_job_id
                and candidate.spec.input_fingerprint == input_fingerprint
                and (
                    candidate.state is JobState.SUCCEEDED
                    or candidate.state not in TERMINAL_JOB_STATES
                )
            ):
                return _json_value(asdict(candidate))
        config = _json_value(dict(parent.spec.provider_config))
        config.update(
            {
                "cues": cues,
                "force_cue_ids": [],
                "allow_timeline_overflow": True,
                "defer_timing_conform": True,
                "timing_override_parent_job_id": parent_tts_job_id,
            }
        )
        raw = {
            "kind": "tts",
            "project_id": project_id,
            "input_revision": snapshot.revision,
            "provider_id": parent.spec.provider_id,
            "provider_version": parent.spec.provider_version,
            "model_id": parent.spec.model_id,
            "input_fingerprint": input_fingerprint,
            "settings_fingerprint": stable_fingerprint(config),
            "provider_config": config,
        }
        return self.create_job(raw)

    def list_projects(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.projects.list_summaries(limit=limit))

    def delete_project(self, project_id: str) -> dict[str, Any]:
        summary = self.projects.get_summary(project_id)
        job_ids = self.projects.deletable_job_ids(project_id)
        for job_id in job_ids:
            for root in (self.checkpoints.staging_root, self.checkpoints.immutable_root):
                _remove_internal_artifact_path(root / job_id, root)
        deleted_job_ids = self.projects.delete_internal(project_id)
        if deleted_job_ids != job_ids:
            raise RuntimeError("Project jobs changed during deletion")
        self._timeline_editors.pop(project_id, None)
        self._subtitle_editors.pop(project_id, None)
        self._effects_editors.pop(project_id, None)
        return {
            "project_id": project_id,
            "name": str(summary["name"]),
            "deleted_jobs": len(job_ids),
            "source_files_deleted": 0,
        }

    def inspect_media_usage(self, *, media_path: str) -> dict[str, Any]:
        path = Path(media_path).expanduser().resolve(strict=True)
        identity = _media_path_identity(path)
        usages: list[dict[str, Any]] = []
        for summary in self.projects.list_summaries(limit=None):
            snapshot = self.projects.load(str(summary["id"]))
            matches = [
                item for item in snapshot.media if _media_path_identity(item.path) == identity
            ]
            if not matches:
                continue
            usages.append(
                {
                    "project_id": snapshot.id,
                    "project_name": snapshot.name,
                    "project_path": str(summary["path"]),
                    "updated_at": str(summary["updated_at"]),
                    "media_count": len(matches),
                    "media_kinds": sorted({item.kind.value for item in matches}),
                }
            )
        return {
            "media_path": str(path),
            "media_name": path.name,
            "exists": bool(usages),
            "usages": usages,
        }

    def create_project(
        self,
        *,
        project_path: str,
        name: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = ProjectSnapshot(project_id or str(uuid4()), name, 1)
        self.projects.save(project_path=project_path, snapshot=snapshot)
        return _json_value(asdict(snapshot))

    def create_project_with_media(
        self,
        *,
        project_path: str,
        name: str,
        media_path: str,
        default_logo_path: str | None = None,
        overlay_preset: dict[str, object] | None = None,
        excluded_project_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create one project for a Multi video item and import its media.

        A single-video owner is reused when it is not already assigned to a
        different card in the same import.  Projects containing several video
        assets are not reusable here because Multi membership identifies a
        project, not an asset; reusing one would make a card play ``videos[0]``
        instead of the file the user selected.
        """

        excluded = {
            value.strip()
            for value in (excluded_project_ids or [])
            if isinstance(value, str) and value.strip()
        }
        requested_identity = _media_path_identity(media_path)
        usage = self.inspect_media_usage(media_path=media_path)
        usages = usage.get("usages", [])
        if isinstance(usages, list):
            for candidate in usages:
                if not isinstance(candidate, Mapping):
                    continue
                project_id = str(candidate.get("project_id", ""))
                if not project_id or project_id in excluded:
                    continue
                try:
                    snapshot = self.projects.load(project_id)
                except LookupError:
                    continue
                videos = [asset for asset in snapshot.media if asset.kind is MediaKind.VIDEO]
                if len(videos) != 1 or _media_path_identity(videos[0].path) != requested_identity:
                    continue
                return {
                    "project": self.get_project(project_id),
                    "reused": True,
                }
        project = self.create_project(project_path=project_path, name=name)
        imported = self.import_media(
            project_id=str(project["id"]),
            media_path=media_path,
            default_logo_path=default_logo_path,
            overlay_preset=overlay_preset,
        )
        return {"project": imported, "reused": False}

    def list_multi_videos(self) -> dict[str, Any]:
        eligible_project_ids: list[str] = []
        summaries: dict[str, dict[str, Any]] = {}
        snapshots: dict[str, ProjectSnapshot] = {}
        for summary in self.projects.list_summaries(limit=None):
            project_id = str(summary["id"])
            try:
                snapshot = self.projects.load(project_id)
            except LookupError:
                continue
            if not any(asset.kind is MediaKind.VIDEO for asset in snapshot.media):
                continue
            eligible_project_ids.append(project_id)
            summaries[project_id] = dict(summary)
            snapshots[project_id] = snapshot
        batch = self.multi_videos.get_batch()
        if str(batch.get("state", "idle")) not in {
            MultiBatchState.RUNNING.value,
            MultiBatchState.PAUSE_REQUESTED.value,
        }:
            self.multi_videos.sync_projects(eligible_project_ids)
        items = self.multi_videos.list_items()
        activity = self.jobs.latest_project_activity(
            [str(item.get("project_id", "")) for item in items]
        )
        current_project_id = str(batch.get("current_project_id", ""))
        batch_active = str(batch.get("state", "idle")) in {
            MultiBatchState.RUNNING.value,
            MultiBatchState.PAUSE_REQUESTED.value,
            MultiBatchState.PAUSED.value,
            MultiBatchState.INTERRUPTED.value,
        }
        enriched: list[dict[str, Any]] = []
        for item in items:
            project_id = str(item["project_id"])
            summary = summaries.get(project_id)
            snapshot = snapshots.get(project_id)
            if summary is None or snapshot is None:
                continue
            videos = [asset for asset in snapshot.media if asset.kind is MediaKind.VIDEO]
            video = videos[0] if videos else None
            media_paths = {asset.id: asset.path for asset in snapshot.media}
            original_audio_path = _first_track_media_path(
                snapshot, media_paths, TrackKind.ORIGINAL_AUDIO
            )
            voice_audio_path = _first_track_media_path(snapshot, media_paths, TrackKind.VOICE)
            child_job_id = str(item.get("child_job_id", ""))
            child_runtime = self._multi_child_runtime_fields(
                activity.get(project_id), child_job_id=child_job_id
            )
            display_item, manual_review = self._live_multi_translation_review(
                item,
                snapshot=snapshot,
            )
            if manual_review is not None and manual_review.get("available") is True:
                # The review projection is authoritative for this card.  Do
                # not let a terminal worker activity with an old error payload
                # overwrite the live CJK count or ready state.
                project_status = (
                    {}
                    if display_item.get("manual_review_ready") is True
                    else {"project_status_override": False}
                )
            else:
                project_status = self._multi_project_status(
                    activity.get(project_id),
                    has_source=bool(snapshot.source_cues),
                    has_translation=bool(snapshot.translated_cues),
                    has_voice=bool(voice_audio_path),
                    child_job_id=(
                        child_job_id if batch_active and project_id == current_project_id else ""
                    ),
                    project_revision=int(summary.get("head_revision", 0) or 0),
                )
            artifact_status = self._multi_project_status(
                None,
                has_source=bool(snapshot.source_cues),
                has_translation=bool(snapshot.translated_cues),
                has_voice=bool(voice_audio_path),
            )
            enriched.append(
                {
                    **display_item,
                    **project_status,
                    **child_runtime,
                    "artifact_status_state": artifact_status["project_status_state"],
                    "artifact_status_text": artifact_status["project_status_text"],
                    "artifact_status_stage": artifact_status["project_status_stage"],
                    "artifact_status_progress": artifact_status["project_status_progress"],
                    "is_batch_current": project_id == current_project_id,
                    "name": snapshot.name,
                    "project_path": str(summary["path"]),
                    "video_path": video.path if video is not None else "",
                    "duration_us": video.duration_us if video is not None else 0,
                    "has_source_subtitles": bool(snapshot.source_cues),
                    "has_translated_subtitles": bool(snapshot.translated_cues),
                    "original_audio_path": original_audio_path,
                    "voice_audio_path": voice_audio_path,
                    "has_original_audio": bool(original_audio_path),
                    "has_voice_track": bool(voice_audio_path),
                    "has_video": video is not None,
                }
            )
        return {
            "batch": batch,
            "items": enriched,
            "hidden_count": len(
                set(self.multi_videos.hidden_project_ids()).intersection(eligible_project_ids)
            ),
            "partial": False,
        }

    def list_multi_video_status(self) -> dict[str, Any]:
        """Return the 10 Hz event payload without media/subtitle descriptors."""

        batch = self.multi_videos.get_batch()
        current_project_id = str(batch.get("current_project_id", ""))
        batch_active = str(batch.get("state", "idle")) in {
            MultiBatchState.RUNNING.value,
            MultiBatchState.PAUSE_REQUESTED.value,
            MultiBatchState.PAUSED.value,
            MultiBatchState.INTERRUPTED.value,
        }
        raw_items = self.multi_videos.list_items()
        activity = self.jobs.latest_project_activity(
            [str(item.get("project_id", "")) for item in raw_items]
        )
        items: list[dict[str, object]] = []
        for item in raw_items:
            project_id = str(item.get("project_id", ""))
            display_item = dict(item)
            child_job_id = str(item.get("child_job_id", ""))
            child_runtime = self._multi_child_runtime_fields(
                activity.get(project_id), child_job_id=child_job_id
            )
            manual_review: dict[str, Any] | None = None
            if (
                str(item.get("stage", "")) == MultiStage.TRANSLATE.value
                and str(item.get("error_code", "")) == "translation_review_required"
            ):
                # Only translation-review cards need a project decode.  All
                # ordinary progress cards remain a cheap DB/job status lookup.
                try:
                    review_snapshot = self.projects.load(project_id)
                except (LookupError, OSError, ValueError):
                    review_snapshot = None
                display_item, manual_review = self._live_multi_translation_review(
                    item,
                    snapshot=review_snapshot,
                )
            if manual_review is not None and manual_review.get("available") is True:
                status = (
                    {}
                    if display_item.get("manual_review_ready") is True
                    else {"project_status_override": False}
                )
            else:
                status = self._multi_project_status(
                    activity.get(project_id),
                    child_job_id=(
                        child_job_id if batch_active and project_id == current_project_id else ""
                    ),
                    include_artifacts=False,
                )
            items.append(
                {
                    **display_item,
                    **status,
                    **child_runtime,
                    "is_batch_current": project_id == current_project_id,
                }
            )
        return {"batch": batch, "items": items}

    @staticmethod
    def _multi_child_runtime_fields(
        activity: Mapping[str, Any] | None,
        *,
        child_job_id: str,
    ) -> dict[str, object]:
        """Expose the real child lifecycle without transporting its large job spec."""

        if not child_job_id or not activity or str(activity.get("id", "")) != child_job_id:
            return {}
        progress = activity.get("progress", {})
        progress = progress if isinstance(progress, Mapping) else {}
        try:
            cache_restored_cues = max(0, int(progress.get("cache_restored_cues", 0) or 0))
        except (TypeError, ValueError):
            cache_restored_cues = 0
        return {
            "child_job_state": str(activity.get("state", "")),
            "child_phase": str(progress.get("phase", "")),
            "child_phase_detail": str(progress.get("phase_detail", "")),
            "child_cache_restored_cues": cache_restored_cues,
        }

    @staticmethod
    def _multi_project_status(
        activity: Mapping[str, Any] | None,
        *,
        has_source: bool = False,
        has_translation: bool = False,
        has_voice: bool = False,
        child_job_id: str = "",
        include_artifacts: bool = True,
        project_revision: int = 0,
    ) -> dict[str, Any]:
        active_states = {
            "queued",
            "start_requested",
            "running",
            "pause_requested",
            "paused",
            "resume_requested",
            "cancel_requested",
        }
        if activity and str(activity.get("id", "")) != child_job_id:
            kind = str(activity.get("kind", ""))
            state = str(activity.get("state", ""))
            labels = {
                "ocr": "trích xuất SRT",
                "translate": "dịch",
                "tts": "lồng tiếng",
                "export": "xuất video",
            }
            fraction = worker_progress_fraction(activity.get("progress"))
            percent = round(fraction * 100)
            if state in active_states:
                if state == "paused":
                    text = f"Thủ công · Đã tạm dừng {labels.get(kind, kind)} {percent}%"
                    visual_state = "paused"
                elif state == "pause_requested":
                    text = f"Thủ công · Đang tạm dừng {labels.get(kind, kind)} {percent}%"
                    visual_state = "running"
                else:
                    text = f"Thủ công · Đang {labels.get(kind, kind)} {percent}%"
                    visual_state = "running"
                return {
                    "project_status_override": True,
                    "project_status_state": visual_state,
                    "project_status_text": text,
                    "project_status_stage": kind,
                    "project_status_progress": fraction,
                    "project_error_message": "",
                }
            activity_revision = int(activity.get("input_revision", 0) or 0)
            effective_project_revision = project_revision or int(
                activity.get("project_head_revision", 0) or 0
            )
            if state in {"failed", "interrupted", "cancelled"} and (
                effective_project_revision <= 0 or activity_revision == effective_project_revision
            ):
                payload = activity.get("error_payload", {})
                payload = payload if isinstance(payload, Mapping) else {}
                message = str(payload.get("message") or payload.get("reason") or "")
                label = "Lỗi" if state == "failed" else "Đã gián đoạn"
                return {
                    "project_status_override": True,
                    "project_status_state": "failed" if state == "failed" else "interrupted",
                    "project_status_text": f"{label} {labels.get(kind, kind)} {percent}%",
                    "project_status_stage": kind,
                    "project_status_progress": fraction,
                    "project_error_message": message,
                }
        if not include_artifacts:
            return {"project_status_override": False}
        if has_voice:
            return {
                "project_status_override": False,
                "project_status_state": "completed",
                "project_status_text": "D1 sẵn sàng",
                "project_status_stage": "done",
                "project_status_progress": 1.0,
                "project_error_message": "",
            }
        if has_translation:
            text, stage, progress = "Đã dịch", "tts", 2.0 / 3.0
        elif has_source:
            text, stage, progress = "Đã trích xuất SRT", "translate", 1.0 / 3.0
        else:
            text, stage, progress = "Chưa xử lý", "ocr", 0.0
        return {
            "project_status_override": False,
            "project_status_state": "idle",
            "project_status_text": text,
            "project_status_stage": stage,
            "project_status_progress": progress,
            "project_error_message": "",
        }

    def get_multi_preview_descriptor(self, project_id: str) -> dict[str, Any]:
        """Load preview media and subtitle cues for exactly one card on demand."""

        if not any(
            str(item.get("project_id", "")) == project_id for item in self.multi_videos.list_items()
        ):
            raise LookupError("Video không còn trong danh sách Multi")
        snapshot = self.projects.load(project_id)
        videos = [asset for asset in snapshot.media if asset.kind is MediaKind.VIDEO]
        video = videos[0] if videos else None
        if video is None:
            raise ValueError("Project Multi không có video để phát preview")
        media_paths = {asset.id: asset.path for asset in snapshot.media}
        translated_by_id = {cue.id: cue for cue in snapshot.translated_cues}
        translated_by_index = {cue.index: cue for cue in snapshot.translated_cues}
        subtitle_cues: list[dict[str, object]] = []
        for cue in snapshot.source_cues or snapshot.translated_cues:
            translated = translated_by_id.get(cue.id) or translated_by_index.get(cue.index)
            subtitle_cues.append(
                {
                    "cue_id": cue.id,
                    "index": cue.index,
                    "startUs": cue.range.start_us,
                    "endUs": cue.range.end_us,
                    "sourceText": cue.text if snapshot.source_cues else "",
                    "translatedText": translated.text if translated is not None else "",
                }
            )
        original_audio_path = _first_track_media_path(
            snapshot, media_paths, TrackKind.ORIGINAL_AUDIO
        )
        voice_audio_path = _first_track_media_path(snapshot, media_paths, TrackKind.VOICE)
        return {
            "project_id": project_id,
            "video_path": video.path,
            "original_audio_path": original_audio_path,
            "voice_audio_path": voice_audio_path,
            "subtitle_cues": subtitle_cues,
            "has_voice_track": bool(voice_audio_path),
        }

    def add_multi_projects(self, project_ids: list[str]) -> dict[str, Any]:
        self._require_multi_membership_editable()
        self.multi_videos.add_projects(project_ids)
        return self.list_multi_videos()

    def restore_multi_projects(self) -> dict[str, Any]:
        self._require_multi_membership_editable()
        eligible: list[str] = []
        hidden = set(self.multi_videos.hidden_project_ids())
        for summary in self.projects.list_summaries(limit=None):
            project_id = str(summary["id"])
            if project_id not in hidden:
                continue
            try:
                snapshot = self.projects.load(project_id)
            except LookupError:
                continue
            if any(asset.kind is MediaKind.VIDEO for asset in snapshot.media):
                eligible.append(project_id)
        self.multi_videos.add_projects(eligible, selected=True, restore_hidden=True)
        return self.list_multi_videos()

    def remove_multi_project(self, project_id: str) -> dict[str, Any]:
        self._require_multi_membership_editable()
        self.multi_videos.remove_project(project_id)
        return self.list_multi_videos()

    def reorder_multi_projects(self, project_ids: list[str]) -> dict[str, Any]:
        self._require_multi_membership_editable()
        self.multi_videos.reorder_projects(project_ids)
        return self.list_multi_videos()

    def select_multi_project(self, project_id: str, selected: bool) -> dict[str, Any]:
        self._require_multi_membership_editable()
        self.multi_videos.set_selected(project_id, selected)
        return self.list_multi_video_status()

    def select_all_multi_projects(self, selected: bool) -> dict[str, Any]:
        self._require_multi_membership_editable()
        self.multi_videos.set_all_selected(selected)
        return self.list_multi_video_status()

    def _require_multi_membership_editable(self) -> None:
        state = str(self.multi_videos.get_batch().get("state", "idle"))
        if state in {
            MultiBatchState.RUNNING.value,
            MultiBatchState.PAUSE_REQUESTED.value,
        }:
            raise ValueError(
                "Hãy tạm dừng Multi video và chờ checkpoint an toàn trước khi "
                "thay đổi danh sách hoặc lựa chọn."
            )

    def inspect_legacy_project(self, *, source_path: str) -> dict[str, Any]:
        report = LegacyProjectImporter(self.media_imports).inspect(source_path)
        return report.to_dict()

    def import_legacy_project(
        self,
        *,
        source_path: str,
        project_path: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve(strict=True)
        snapshot = ProjectSnapshot(str(uuid4()), (name or source.stem).strip(), 1)
        imported, report = LegacyProjectImporter(self.media_imports).import_project(
            source,
            snapshot,
            cache_import_root=discover_data_root() / "imported_legacy_cache",
        )
        self.projects.save(project_path=project_path, snapshot=imported)
        self._timeline_editors[imported.id] = TimelineEditor(imported)
        self._subtitle_editors[imported.id] = SubtitleEditor(imported)
        self._effects_editors[imported.id] = EffectsEditor(imported)
        return {
            "project": _json_value(asdict(imported)),
            "report": report.to_dict(),
        }

    def runtime_diagnostics(self, *, data_root: str | None = None) -> dict[str, Any]:
        return run_runtime_diagnostics(data_root=data_root).to_dict()

    def get_project(self, project_id: str) -> dict[str, Any]:
        snapshot = self.projects.load(project_id)
        editor = self._timeline_editors.get(project_id)
        if editor is None or editor.snapshot.revision != snapshot.revision:
            self._timeline_editors[project_id] = TimelineEditor(snapshot)
        subtitle_editor = self._subtitle_editors.get(project_id)
        if subtitle_editor is None or subtitle_editor.snapshot.revision != snapshot.revision:
            self._subtitle_editors[project_id] = SubtitleEditor(snapshot)
        effects_editor = self._effects_editors.get(project_id)
        if effects_editor is None or effects_editor.snapshot.revision != snapshot.revision:
            self._effects_editors[project_id] = EffectsEditor(snapshot)
        return {**_json_value(asdict(snapshot)), **self._edit_persistence_state(project_id)}

    def _edit_persistence_state(
        self, project_id: str, *, include_clean: bool = False,
    ) -> dict[str, Any]:
        if not include_clean and not self.projects.has_pending_edits(project_id):
            return {}
        return {"_project_dirty": self.projects.has_pending_edits(project_id),
                "_saved_revision": self.projects.persisted_head_revision(project_id)}

    def save_project_edits(self, project_id: str) -> dict[str, Any]:
        revision = self.projects.flush_edits(project_id)
        return {"id": project_id, "revision": revision,
                **self._edit_persistence_state(project_id, include_clean=True)}

    def _store_editor_snapshot(self, snapshot: ProjectSnapshot, *, deferred: bool) -> None:
        summary = self.projects.get_summary(snapshot.id)
        if deferred:
            self.projects.stage_edits(project_path=summary["path"], snapshot=snapshot)
        else:
            self.projects.save(project_path=summary["path"], snapshot=snapshot)

    def export_subtitle_srt(
        self,
        *,
        project_id: str,
        language: str,
        output_path: str,
    ) -> dict[str, Any]:
        """Render the latest durable subtitle track without mutating the project."""

        normalized_language = language.strip().casefold()
        if normalized_language not in {"source", "translated"}:
            raise ValueError("Ngôn ngữ xuất SRT phải là phụ đề nguồn hoặc tiếng Việt")
        raw_target = output_path.strip()
        if not raw_target:
            raise ValueError("Hãy chọn vị trí lưu file SRT")
        target = Path(raw_target).expanduser().resolve()
        if target.suffix.casefold() != ".srt":
            raise ValueError("File xuất phụ đề phải có đuôi .srt")

        snapshot = project_for_timeline(self.projects.load(project_id))
        all_cues = (
            snapshot.source_cues
            if normalized_language == "source"
            else snapshot.translated_cues
        )
        cues = dialogue_cues(all_cues)
        if not cues:
            label = "nguồn" if normalized_language == "source" else "tiếng Việt"
            raise ValueError(f"Dự án chưa có phụ đề {label} để xuất")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text("\ufeff" + render_srt(cues), encoding="utf-8", newline="\n")
            os.replace(temporary, target)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        return {
            "project_id": snapshot.id,
            "revision": snapshot.revision,
            "language": normalized_language,
            "path": str(target),
            "cue_count": len(cues),
        }

    def import_media(
        self,
        *,
        project_id: str,
        media_path: str,
        timeline_start_us: int | None = None,
        default_logo_path: str | None = None,
        overlay_preset: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.projects.load(project_id)
        path = Path(media_path).expanduser().resolve()
        if path.suffix.casefold() == ".srt":
            updated = self.media_imports.import_srt(snapshot, path)
        else:
            updated = self.media_imports.import_media(
                snapshot,
                path,
                timeline_start_us=timeline_start_us,
                default_logo_path=default_logo_path,
                overlay_preset=overlay_preset,
            )
        summary = self.projects.get_summary(project_id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        self._timeline_editors[project_id] = TimelineEditor(updated)
        self._subtitle_editors[project_id] = SubtitleEditor(updated)
        self._effects_editors[project_id] = EffectsEditor(updated)
        return _json_value(asdict(updated))

    def import_translation_srt(
        self,
        *,
        project_id: str,
        subtitle_path: str,
    ) -> dict[str, Any]:
        snapshot = self.projects.load(project_id)
        if not snapshot.source_cues:
            raise ValueError("Project has no source subtitles to align a translation")
        active_states = {
            JobState.QUEUED,
            JobState.START_REQUESTED,
            JobState.RUNNING,
            JobState.PAUSE_REQUESTED,
            JobState.PAUSED,
            JobState.RESUME_REQUESTED,
            JobState.CANCEL_REQUESTED,
        }
        if any(
            job.spec.project_id == project_id
            and job.spec.kind.value == "translate"
            and job.state in active_states
            for job in self.jobs.list_recent(limit=1_000)
        ):
            raise ValueError("Cannot import an external SRT while a translation job is active")
        updated = self.media_imports.import_srt(snapshot, subtitle_path, language="vi")
        summary = self.projects.get_summary(project_id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        self._timeline_editors[project_id] = TimelineEditor(updated)
        self._subtitle_editors[project_id] = SubtitleEditor(updated)
        self._effects_editors[project_id] = EffectsEditor(updated)
        return _json_value(asdict(updated))

    def prepare_tts_rerun(self, *, project_id: str) -> dict[str, Any]:
        """Detach the active D1 before a deliberate manual replacement.

        This operation is intentionally separate from ``create_tts_job`` so the
        client can apply the new project snapshot and release all MediaPlayer
        handles before a replacement job is allowed to start.
        """

        self._ensure_no_active_stage(project_id, JobKind.TTS)
        snapshot = self.projects.load(project_id)
        updated = self.media_imports.remove_voice_media(snapshot)
        if updated is snapshot:
            return _json_value(asdict(snapshot))
        summary = self.projects.get_summary(project_id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        self._timeline_editors[project_id] = TimelineEditor(updated)
        self._subtitle_editors[project_id] = SubtitleEditor(updated)
        self._effects_editors[project_id] = EffectsEditor(updated)
        return _json_value(asdict(updated))

    def set_translation_preferences(
        self,
        *,
        project_id: str,
        preset: str,
        custom_rules: str,
        active_style_name: str = "",
    ) -> dict[str, Any]:
        snapshot = self.projects.load(project_id)
        preferences = TranslationPreferences(
            preset=preset.strip().casefold(),
            custom_rules=custom_rules,
            saved_styles=snapshot.translation_preferences.saved_styles,
            active_style_name=active_style_name,
        )
        if snapshot.translation_preferences == preferences:
            return {**_json_value(asdict(snapshot)),
                    **self._edit_persistence_state(project_id, include_clean=True)}
        updated = replace(
            snapshot,
            revision=snapshot.revision + 1,
            translation_preferences=preferences,
        )
        summary = self.projects.get_summary(project_id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        return {**_json_value(asdict(updated)),
                **self._edit_persistence_state(project_id, include_clean=True)}

    def save_translation_style(
        self,
        *,
        project_id: str,
        name: str,
        preset: str,
        custom_rules: str,
    ) -> dict[str, Any]:
        """Persist a named style and make it the active project style.

        Names are intentionally immutable identifiers from the user's point of
        view: silently replacing one would make it too easy to destroy a style
        while experimenting.  The user can choose another name instead.
        """

        style = TranslationStylePreset(
            name=name.strip(),
            preset=preset.strip().casefold(),
            custom_rules=custom_rules,
        )
        snapshot = self.projects.load(project_id)
        normalized_name = style.normalized_name.casefold()
        if any(
            saved.normalized_name.casefold() == normalized_name
            for saved in snapshot.translation_preferences.saved_styles
        ):
            raise ValueError("Tên văn phong đã tồn tại; hãy chọn tên khác")
        preferences = TranslationPreferences(
            preset=style.preset,
            custom_rules=style.custom_rules,
            saved_styles=(*snapshot.translation_preferences.saved_styles, style),
            active_style_name=style.name,
        )
        if snapshot.translation_preferences == preferences:
            return {**_json_value(asdict(snapshot)),
                    **self._edit_persistence_state(project_id, include_clean=True)}
        updated = replace(
            snapshot,
            revision=snapshot.revision + 1,
            translation_preferences=preferences,
        )
        summary = self.projects.get_summary(project_id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        return {**_json_value(asdict(updated)),
                **self._edit_persistence_state(project_id, include_clean=True)}

    def effect_command(
        self,
        *,
        project_id: str,
        command: str,
        overlay_id: str | None = None,
        values: dict[str, Any] | None = None,
        deferred: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.projects.load(project_id)
        previous_overlays = {item.id: item for item in snapshot.overlays}
        previous_media = {item.id: item for item in snapshot.media}
        editor = self._effects_editors.get(project_id)
        if editor is None or editor.snapshot.revision != snapshot.revision:
            editor = EffectsEditor(snapshot)
            self._effects_editors[project_id] = editor
        payload = dict(values or {})
        display_time = payload.pop("display_time", False)
        if type(display_time) is not bool:
            raise ValueError("Chế độ thời gian hiệu ứng phải là bật/tắt")
        if display_time:
            for key, endpoint in (("start_us", "start"), ("end_us", "end")):
                if key in payload and payload[key] is not None:
                    if type(payload[key]) is not int:
                        raise ValueError("Mốc hiệu ứng phải dùng micro-giây nguyên")
                    payload[key] = timeline_to_base_us(snapshot, payload[key], endpoint)
        if command == "update":
            if not overlay_id:
                raise ValueError("Effect update requires an overlay id")
            allowed = {
                "x",
                "y",
                "width",
                "height",
                "start_us",
                "end_us",
                "enabled",
                "opacity",
                "blur_radius",
                "tracking_mode",
                "padding_x",
                "padding_y",
                "concealment_mode",
            }
            unknown = set(payload) - allowed
            if unknown:
                raise ValueError(f"Unknown effect values: {sorted(unknown)}")
            updated = editor.update_overlay(overlay_id, **payload)
        elif command == "set_logo_asset":
            logo_path = payload.get("path")
            if not isinstance(logo_path, str) or not logo_path.strip():
                raise ValueError("Logo asset command requires a file path")
            updated = editor.set_logo_asset(logo_path)
        elif command == "add_blur":
            if not overlay_id:
                raise ValueError("Add blur requires an overlay id")
            updated = editor.add_blur(overlay_id)
        elif command == "delete_overlay":
            if not overlay_id:
                raise ValueError("Delete effect requires an overlay id")
            updated = editor.delete_overlay(overlay_id)
        elif command == "reset_defaults":
            logo_path = payload.get("path")
            if logo_path is not None and not isinstance(logo_path, str):
                raise ValueError("Default logo path must be a string")
            updated = editor.reset_defaults(logo_path=logo_path or None)
        else:
            raise ValueError(f"Unknown effect command: {command}")
        self._store_editor_snapshot(updated, deferred=deferred)
        next_overlays = {item.id: item for item in updated.overlays}
        history = self._timeline_editors.get(project_id)
        if history is None or history.snapshot.revision != snapshot.revision:
            history = TimelineEditor(snapshot)
        history._commit(updated)
        self._sync_edit_history(history)
        next_media = {item.id: item for item in updated.media}
        return _json_value(
            {
                "_effect_patch": True,
                **self._edit_persistence_state(project_id),
                "_timeline_can_undo": history.can_undo,
                "_timeline_can_redo": history.can_redo,
                "_subtitle_can_undo": history.can_undo,
                "_subtitle_can_redo": history.can_redo,
                "id": updated.id,
                "revision": updated.revision,
                "overlay_upserts": [
                    asdict(item)
                    for key, item in next_overlays.items()
                    if previous_overlays.get(key) != item
                ],
                "removed_overlay_ids": sorted(set(previous_overlays) - set(next_overlays)),
                "media_upserts": [
                    asdict(item)
                    for key, item in next_media.items()
                    if previous_media.get(key) != item
                ],
                "removed_media_ids": sorted(set(previous_media) - set(next_media)),
            }
        )

    def _sync_edit_history(self, editor: TimelineEditor | SubtitleEditor) -> None:
        """Keep one ordered session history for clip and subtitle edits."""
        project_id = editor.snapshot.id
        timeline = editor if isinstance(editor, TimelineEditor) else TimelineEditor(editor.snapshot)
        subtitle = editor if isinstance(editor, SubtitleEditor) else SubtitleEditor(editor.snapshot)
        timeline._undo, timeline._redo = list(editor._undo), list(editor._redo)
        subtitle._undo, subtitle._redo = list(editor._undo), list(editor._redo)
        self._timeline_editors[project_id] = timeline
        self._subtitle_editors[project_id] = subtitle

    def subtitle_command(
        self,
        *,
        project_id: str,
        command: str,
        cue_id: str | None = None,
        language: str = "source",
        params: dict[str, Any] | None = None,
        compact: bool = False,
        deferred: bool = False,
    ) -> dict[str, Any]:
        if (params or {}).get("title_mode") is True and command not in {"undo", "redo"}:
            return self.timeline_command(
                project_id=project_id, command="edit_title_text", deferred=deferred,
                value={"command": command, "cue_id": cue_id, "values": dict(params or {})},
            )
        if command in {"undo", "redo"}:
            patch = self.timeline_command(project_id=project_id, command=command, deferred=deferred)
            patch["_subtitle_can_undo"] = patch["_timeline_can_undo"]
            patch["_subtitle_can_redo"] = patch["_timeline_can_redo"]
            if compact:
                return patch
            return {
                **_json_value(asdict(self._timeline_editors[project_id].snapshot)),
                "_subtitle_can_undo": patch["_subtitle_can_undo"],
                "_subtitle_can_redo": patch["_subtitle_can_redo"],
                "_timeline_can_undo": patch["_timeline_can_undo"],
                "_timeline_can_redo": patch["_timeline_can_redo"],
            }
        editor = self._subtitle_editors.get(project_id)
        head_revision = self.projects.head_revision(project_id)
        if editor is not None and editor.snapshot.revision == head_revision:
            snapshot = editor.snapshot
        else:
            snapshot = self.projects.load(project_id, head_revision)
            editor = SubtitleEditor(snapshot)
            self._subtitle_editors[project_id] = editor
        previous_undo, previous_redo = list(editor._undo), list(editor._redo)
        values = dict(params or {})
        display_time = values.pop("display_time", False)
        if type(display_time) is not bool:
            raise ValueError("Chế độ thời gian hiển thị phải là bật/tắt")
        if display_time:
            for key, endpoint in (("start_us", "start"), ("end_us", "end"),
                                  ("split_us", "start")):
                if key in values and values[key] is not None:
                    if type(values[key]) is not int:
                        raise ValueError("Mốc phụ đề phải dùng micro-giây nguyên")
                    values[key] = timeline_to_base_us(snapshot, values[key], endpoint)

        def selected_cue_ids() -> tuple[str, ...]:
            raw = values.get("cue_ids")
            if isinstance(raw, list):
                result = tuple(
                    dict.fromkeys(
                        str(item).strip()
                        for item in raw
                        if isinstance(item, str) and item.strip()
                    )
                )
                if result:
                    return result
            return (cue_id,) if cue_id else ()

        try:
            if command == "add":
                updated = editor.add(
                    language=language,
                    after_cue_id=cue_id,
                    text=str(values.get("text", "Cue mới")),
                )
            elif command == "update_text":
                if cue_id is None:
                    raise ValueError("update_text requires cue_id")
                text = values.get("text")
                if not isinstance(text, str):
                    raise ValueError("Subtitle text must be a string")
                updated = editor.update_text(cue_id, text, language=language)
            elif command == "accept_scene_text":
                if cue_id is None:
                    raise ValueError("accept_scene_text requires cue_id")
                updated = editor.accept_scene_text(cue_id)
            elif command == "set_scene_text_font_scale":
                if cue_id is None:
                    raise ValueError("Chưa chọn cue chữ trong cảnh cần chỉnh kích thước")
                scale = values.get("scale")
                if isinstance(scale, bool) or not isinstance(scale, (int, float)):
                    raise ValueError("Kích thước chữ trong cảnh phải là một số")
                updated = editor.set_scene_text_font_scale(cue_id, float(scale))
            elif command == "set_scene_text_timing":
                if cue_id is None:
                    raise ValueError("Chưa chọn cue chữ trong cảnh cần chỉnh thời gian")
                start_us = values.get("start_us")
                end_us = values.get("end_us")
                if type(start_us) is not int or type(end_us) is not int:
                    raise ValueError(
                        "Thời gian chữ trong cảnh phải dùng micro-giây nguyên"
                    )
                updated = editor.set_scene_text_timing(
                    cue_id,
                    start_us=start_us,
                    end_us=end_us,
                )
            elif command == "set_cue_role":
                raw_ids = values.get("cue_ids")
                if raw_ids is not None and (
                    not isinstance(raw_ids, list) or not raw_ids
                    or any(not isinstance(item, str) or not item.strip() for item in raw_ids)
                ):
                    raise ValueError("Hãy chọn các cue hợp lệ để chuyển loại")
                role = values.get("role")
                if not isinstance(role, str):
                    raise ValueError("Loại cue phải là thoại S1 hoặc chữ trong cảnh S2")
                updated = editor.set_cue_role_many(selected_cue_ids(), role=role)
            elif command == "set_text_format":
                cue_ids = selected_cue_ids()
                if not cue_ids:
                    raise ValueError("Chưa chọn cue chữ cần định dạng")
                font_size = values.get("font_size")
                orientation = values.get("orientation")
                background_color = values.get("background_color", "")
                text_color = values.get("text_color", "")
                style_hint = values.get("style_hint", "standard")
                background_padding_x = values.get("background_padding_x", 20)
                background_padding_y = values.get("background_padding_y", 10)
                background_style = values.get("background_style", "solid")
                letter_spacing_percent = values.get("letter_spacing_percent", 0)
                font_bold = values.get("font_bold", False)
                font_italic = values.get("font_italic", False)
                text_opacity_percent = values.get("text_opacity_percent", 100)
                background_opacity_percent = values.get(
                    "background_opacity_percent", 90
                )
                if type(font_size) is not int:
                    raise ValueError("Kích thước chữ phải là số nguyên dương")
                if (
                    type(background_padding_x) is not int
                    or type(background_padding_y) is not int
                    or type(letter_spacing_percent) is not int
                    or type(text_opacity_percent) is not int
                    or type(background_opacity_percent) is not int
                ):
                    raise ValueError("Khoảng nền, khoảng cách chữ và độ đục phải là số nguyên")
                if not all(
                    isinstance(item, str)
                    for item in (
                        orientation,
                        background_color,
                        text_color,
                        style_hint,
                        background_style,
                    )
                ):
                    raise ValueError("Định dạng chữ không hợp lệ")
                if type(font_bold) is not bool or type(font_italic) is not bool:
                    raise ValueError("Định dạng đậm/nghiêng không hợp lệ")
                updated = editor.set_text_format_many(
                    cue_ids,
                    font_size=font_size,
                    orientation=orientation,
                    background_color=background_color,
                    text_color=text_color,
                    style_hint=style_hint,
                    background_padding_x=background_padding_x,
                    background_padding_y=background_padding_y,
                    background_style=background_style,
                    letter_spacing_percent=letter_spacing_percent,
                    font_bold=font_bold,
                    font_italic=font_italic,
                    text_opacity_percent=text_opacity_percent,
                    background_opacity_percent=background_opacity_percent,
                )
            elif command == "transform_cue_geometry":
                cue_ids = selected_cue_ids()
                if not cue_ids:
                    raise ValueError("Chưa chọn cue chữ cần di chuyển")
                geometry_values = tuple(
                    values.get(key, fallback)
                    for key, fallback in (
                        ("delta_x", 0.0),
                        ("delta_y", 0.0),
                        ("scale_x", 1.0),
                        ("scale_y", 1.0),
                    )
                )
                if not all(
                    isinstance(item, (int, float)) and not isinstance(item, bool)
                    for item in geometry_values
                ):
                    raise ValueError("Tọa độ chữ không hợp lệ")
                updated = editor.transform_cue_geometry(
                    cue_ids,
                    delta_x=float(geometry_values[0]),
                    delta_y=float(geometry_values[1]),
                    scale_x=float(geometry_values[2]),
                    scale_y=float(geometry_values[3]),
                    panel_seeds=values.get("panel_seeds"),
                )
            elif command == "move_scene_texts":
                cue_ids = selected_cue_ids()
                delta_us = values.get("delta_us", 0)
                lane_delta = values.get("lane_delta", 0)
                if type(delta_us) is not int or type(lane_delta) is not int:
                    raise ValueError("Dịch chuyển cue phải dùng số nguyên")
                updated = editor.move_scene_texts(
                    cue_ids,
                    delta_us=delta_us,
                    lane_delta=lane_delta,
                    range_overrides=self._display_cue_time_overrides(
                        snapshot, cue_ids, delta_us, "move") if display_time else None,
                )
            elif command == "resize_scene_texts":
                cue_ids = selected_cue_ids()
                edge = values.get("edge", "")
                delta_us = values.get("delta_us", 0)
                if not isinstance(edge, str) or type(delta_us) is not int:
                    raise ValueError("Cạnh và độ dịch thời gian cue không hợp lệ")
                updated = editor.resize_scene_texts(
                    cue_ids,
                    edge=edge,
                    delta_us=delta_us,
                    range_overrides=self._display_cue_time_overrides(
                        snapshot, cue_ids, delta_us, edge) if display_time else None,
                )
            elif command == "set_scene_text_fade":
                cue_ids = selected_cue_ids()
                enabled = values.get("enabled", False)
                duration_ms = values.get("duration_ms", 300)
                if type(enabled) is not bool or type(duration_ms) is not int:
                    raise ValueError("Chế độ mờ dần không hợp lệ")
                updated = editor.set_scene_fade_many(
                    cue_ids,
                    enabled=enabled,
                    duration_ms=duration_ms,
                )
            elif command in {"add_scene_text", "add_subtitle_cue"}:
                text = values.get("text")
                start_us = values.get("start_us")
                end_us = values.get("end_us")
                raw_box = values.get("visual_box")
                cue_role = values.get("cue_role", "scene_text")
                scene_text = cue_role == "scene_text"
                font_size = values.get("font_size", 12)
                orientation = values.get("orientation", "horizontal")
                background_color = values.get(
                    "background_color",
                    SCENE_TEXT_DEFAULT_BACKGROUND_COLOR if scene_text else "",
                )
                text_color = values.get(
                    "text_color", SCENE_TEXT_DEFAULT_TEXT_COLOR if scene_text else ""
                )
                style_hint = values.get(
                    "style_hint",
                    SCENE_TEXT_DEFAULT_STYLE_HINT if scene_text else "standard",
                )
                track_id = values.get("track_id", "")
                background_padding_x = values.get("background_padding_x", 20)
                background_padding_y = values.get("background_padding_y", 10)
                background_style = values.get(
                    "background_style",
                    SCENE_TEXT_DEFAULT_BACKGROUND_STYLE if scene_text else "solid",
                )
                letter_spacing_percent = values.get("letter_spacing_percent", 0)
                font_bold = values.get("font_bold", False)
                font_italic = values.get("font_italic", False)
                text_opacity_percent = values.get("text_opacity_percent", 100)
                background_opacity_percent = values.get(
                    "background_opacity_percent",
                    SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT
                    if scene_text
                    else 90,
                )
                fade_enabled = values.get(
                    "fade_enabled",
                    SCENE_TEXT_DEFAULT_FADE_ENABLED if scene_text else False,
                )
                fade_duration_ms = values.get(
                    "fade_duration_ms", SCENE_TEXT_DEFAULT_FADE_DURATION_MS
                )
                if not isinstance(text, str):
                    raise ValueError("Nội dung chữ thủ công phải là chuỗi")
                if type(start_us) is not int or type(end_us) is not int:
                    raise ValueError("Thời gian chữ thủ công phải dùng micro-giây nguyên")
                if (
                    not isinstance(raw_box, list)
                    or len(raw_box) != 4
                    or not all(
                        isinstance(item, (int, float)) and not isinstance(item, bool)
                        for item in raw_box
                    )
                ):
                    raise ValueError("Vùng chữ thủ công không hợp lệ")
                if type(font_size) is not int:
                    raise ValueError("Kích thước chữ phải là số nguyên từ 1 đến 100")
                if not all(
                    isinstance(item, str)
                    for item in (
                        orientation,
                        background_color,
                        text_color,
                        style_hint,
                        track_id,
                        cue_role,
                        background_style,
                    )
                ):
                    raise ValueError("Định dạng chữ thủ công không hợp lệ")
                if (
                    type(background_padding_x) is not int
                    or type(background_padding_y) is not int
                    or type(letter_spacing_percent) is not int
                    or type(text_opacity_percent) is not int
                    or type(background_opacity_percent) is not int
                    or type(fade_duration_ms) is not int
                    or type(font_bold) is not bool
                    or type(font_italic) is not bool
                    or type(fade_enabled) is not bool
                ):
                    raise ValueError("Thông số định dạng chữ thủ công không hợp lệ")
                updated = editor.add_subtitle_cue(
                    text=text,
                    start_us=start_us,
                    end_us=end_us,
                    visual_box=tuple(float(item) for item in raw_box),
                    font_size=font_size,
                    orientation=orientation,
                    background_color=background_color,
                    text_color=text_color,
                    style_hint=style_hint,
                    track_id=track_id,
                    cue_role=cue_role,
                    background_padding_x=background_padding_x,
                    background_padding_y=background_padding_y,
                    background_style=background_style,
                    letter_spacing_percent=letter_spacing_percent,
                    font_bold=font_bold,
                    font_italic=font_italic,
                    text_opacity_percent=text_opacity_percent,
                    background_opacity_percent=background_opacity_percent,
                    fade_enabled=fade_enabled,
                    fade_duration_ms=fade_duration_ms,
                )
            elif command == "set_ocr_regions":
                raw_dialogue = values.get("dialogue_region")
                raw_exclusions = values.get("exclusion_regions", [])

                def normalized_region(raw: object) -> tuple[float, float, float, float] | None:
                    if raw is None:
                        return None
                    if (
                        not isinstance(raw, list)
                        or len(raw) != 4
                        or not all(
                            isinstance(item, (int, float)) and not isinstance(item, bool)
                            for item in raw
                        )
                    ):
                        raise ValueError("Vùng OCR cần đủ x, y, rộng và cao")
                    return tuple(float(item) for item in raw)

                if not isinstance(raw_exclusions, list):
                    raise ValueError("Danh sách vùng không OCR không hợp lệ")
                updated = editor.set_ocr_regions(
                    dialogue_region=normalized_region(raw_dialogue),
                    exclusion_regions=tuple(
                        region
                        for item in raw_exclusions
                        for region in (normalized_region(item),)
                        if region is not None
                    ),
                )
            elif command == "split":
                if cue_id is None:
                    raise ValueError("split requires cue_id")
                split_us = values.get("split_us")
                if split_us is not None and type(split_us) is not int:
                    raise ValueError("Subtitle split time must be integer microseconds")
                updated = editor.split(cue_id, split_us=split_us, language=language)
            elif command == "merge_next":
                if cue_id is None:
                    raise ValueError("merge_next requires cue_id")
                updated = editor.merge_with_next(cue_id, language=language)
            elif command == "adjust_time":
                if cue_id is None:
                    raise ValueError("adjust_time requires cue_id")
                start_us = values.get("start_us")
                end_us = values.get("end_us")
                if type(start_us) is not int or type(end_us) is not int:
                    raise ValueError("Subtitle time must use integer microseconds")
                updated = editor.adjust_time(
                    cue_id,
                    start_us=start_us,
                    end_us=end_us,
                    language=language,
                )
            elif command == "shift":
                if cue_id is None:
                    raise ValueError("shift requires cue_id")
                delta_us = values.get("delta_us")
                if type(delta_us) is not int:
                    raise ValueError("Subtitle shift must use integer microseconds")
                updated = editor.shift(cue_id, delta_us, language=language)
            elif command == "auto_break":
                max_chars = values.get("max_chars")
                if max_chars is not None and type(max_chars) is not int:
                    raise ValueError("Subtitle line length must be an integer")
                updated = editor.auto_break(
                    language=language,
                    cue_id=cue_id,
                    max_chars=max_chars,
                )
            elif command == "find_replace":
                find = values.get("find")
                replacement = values.get("replacement", "")
                if not isinstance(find, str) or not isinstance(replacement, str):
                    raise ValueError("Find and replacement must be strings")
                expected_count = values.get("expected_count")
                if expected_count is not None and type(expected_count) is not int:
                    raise ValueError("Expected replacement count must be an integer")
                updated = editor.find_replace(
                    find,
                    replacement,
                    language=language,
                    expected_count=expected_count,
                )
            elif command == "delete":
                if cue_id is None:
                    raise ValueError("delete requires cue_id")
                updated = editor.delete(cue_id)
            elif command == "delete_cues":
                ids = values.get("cue_ids")
                if not isinstance(ids, list) or not ids or any(
                    not isinstance(item, str) or not item.strip() for item in ids
                ):
                    raise ValueError("Hãy chọn cue trước khi xóa")
                updated = editor.delete_many(tuple(ids))
            elif command == "undo":
                updated = editor.undo()
            elif command == "redo":
                updated = editor.redo()
            else:
                raise ValueError(f"Unknown subtitle command: {command}")
            if updated is not snapshot:
                self._store_editor_snapshot(updated, deferred=deferred)
        except Exception:
            # A concurrent writer may have occupied the immutable revision
            # while this editor was preparing its candidate. Never reuse the
            # mutated in-memory history after that failure. Restore it when the
            # head is unchanged, otherwise force a fresh load of both editors.
            editor.snapshot = snapshot
            editor._undo, editor._redo = previous_undo, previous_redo
            if self.projects.head_revision(project_id) != snapshot.revision:
                self._subtitle_editors.pop(project_id, None)
                self._timeline_editors.pop(project_id, None)
            raise
        self._sync_edit_history(editor)
        if compact:
            previous_source = {cue.id: cue for cue in snapshot.source_cues}
            next_source = {cue.id: cue for cue in updated.source_cues}
            previous_translated = {cue.id: cue for cue in snapshot.translated_cues}
            next_translated = {cue.id: cue for cue in updated.translated_cues}
            previous_clips = {
                clip.id: clip
                for track in snapshot.tracks
                if track.kind is TrackKind.SUBTITLE
                for clip in track.clips
            }
            next_clips = {
                clip.id: clip
                for track in updated.tracks
                if track.kind is TrackKind.SUBTITLE
                for clip in track.clips
            }
            previous_tracks = {
                track.id: track
                for track in snapshot.tracks
                if track.kind is TrackKind.SUBTITLE
            }
            next_tracks = {
                track.id: track
                for track in updated.tracks
                if track.kind is TrackKind.SUBTITLE
            }
            index_only_clips = {
                clip_id: {"cue_index": clip.cue_index, "payload_revision": clip.payload_revision}
                for clip_id, clip in next_clips.items()
                if clip_id in previous_clips and previous_clips[clip_id] != clip
                and replace(previous_clips[clip_id], cue_index=clip.cue_index,
                            payload_revision=clip.payload_revision) == clip
            }
            payload = {
                "_subtitle_patch": True,
                "id": updated.id,
                "revision": updated.revision,
                "source_cue_upserts": _json_value(
                    [
                        asdict(cue)
                        for cue_id, cue in next_source.items()
                        if _cue_content_changed(previous_source.get(cue_id), cue)
                    ]
                ),
                "removed_source_cue_ids": sorted(set(previous_source) - set(next_source)),
                "translated_cue_upserts": _json_value(
                    [
                        asdict(cue)
                        for cue_id, cue in next_translated.items()
                        if _cue_content_changed(previous_translated.get(cue_id), cue)
                    ]
                ),
                "removed_translated_cue_ids": sorted(
                    set(previous_translated) - set(next_translated)
                ),
                "source_cue_indices": {
                    key: cue.index for key, cue in next_source.items()
                    if key in previous_source and previous_source[key].index != cue.index
                },
                "translated_cue_indices": {
                    key: cue.index for key, cue in next_translated.items()
                    if key in previous_translated and previous_translated[key].index != cue.index
                },
                "subtitle_clip_upserts": _json_value(
                    [
                        asdict(clip)
                        for clip_id, clip in next_clips.items()
                        if previous_clips.get(clip_id) != clip and clip_id not in index_only_clips
                    ]
                ),
                "removed_subtitle_clip_ids": sorted(set(previous_clips) - set(next_clips)),
                "subtitle_clip_metadata": index_only_clips,
                "subtitle_track_upserts": _json_value(
                    [
                        {
                            "id": track.id,
                            "kind": track.kind.value,
                            "name": track.name,
                            "locked": track.locked,
                            "visible": track.visible,
                            "muted": track.muted,
                        }
                        for track_id, track in next_tracks.items()
                        if track_id not in previous_tracks
                        or replace(track, clips=()) != replace(
                            previous_tracks[track_id], clips=()
                        )
                    ]
                ),
                "removed_subtitle_track_ids": sorted(
                    set(previous_tracks) - set(next_tracks)
                ),
                "ocr_dialogue_region": _json_value(updated.ocr_dialogue_region),
                "ocr_exclusion_regions": _json_value(updated.ocr_exclusion_regions),
            }
        else:
            payload = _json_value(asdict(updated))
        payload["_subtitle_can_undo"] = editor.can_undo
        payload.update(self._edit_persistence_state(project_id))
        payload["_subtitle_can_redo"] = editor.can_redo
        payload["_timeline_can_undo"] = editor.can_undo
        payload["_timeline_can_redo"] = editor.can_redo
        return payload

    def copy_video_clip(self, project_id: str, clip_id: str) -> dict[str, Any]:
        snapshot = self.projects.load(project_id)
        view = self._materialized_video_view(snapshot)
        self._video_clipboards[project_id] = TimelineEditor(view).copy_video(
            clip_id, include_fragments=(snapshot.voice_conform is not None
                                        and snapshot.timeline_edit is None))
        return {"project_id": project_id, "clip_id": clip_id, "copied": True}

    @staticmethod
    def _materialized_video_view(snapshot: ProjectSnapshot) -> ProjectSnapshot:
        if snapshot.voice_conform is not None and snapshot.timeline_edit is None:
            from ducai_studio.domain.timeline_edit import cut_state

            duration = build_scan_plan(snapshot)["duration_us"]
            snapshot = replace(snapshot, timeline_edit=cut_state(
                snapshot, "keep_range", 0, duration))
        return project_for_timeline(snapshot)

    def timeline_command(
        self,
        *,
        project_id: str,
        command: str,
        clip_id: str | None = None,
        track_id: str | None = None,
        timeline_us: int | None = None,
        timeline_start_us: int | None = None,
        timeline_end_us: int | None = None,
        track_ids: tuple[str, ...] = (),
        value: str | int | float | dict | None = None,
        apply_linked: bool = True,
        operation: str | None = None,
        range_start_us: int | None = None,
        range_end_us: int | None = None,
        base_revision: int | None = None,
        request_id: str | None = None,
        deferred: bool = False,
    ) -> dict[str, Any]:
        editor = self._timeline_editors.get(project_id)
        head_revision = self.projects.head_revision(project_id)
        if editor is not None and editor.snapshot.revision == head_revision:
            snapshot = editor.snapshot
        else:
            snapshot = self.projects.load(project_id, head_revision)
            editor = TimelineEditor(snapshot)
            self._timeline_editors[project_id] = editor
        if base_revision is not None and base_revision != snapshot.revision:
            raise ValueError("Dự án đã thay đổi; hãy tải lại trước khi cắt")
        previous_undo, previous_redo = list(editor._undo), list(editor._redo)
        original_editor = editor
        if command in {"cut", "split_video", "delete_video_segment",
                       "paste_video", "duplicate_video"}:
            for kind in (JobKind.OCR, JobKind.TRANSLATE, JobKind.TTS, JobKind.EXPORT):
                if self.jobs.active_id(project_id=project_id, kind=kind):
                    raise ValueError(
                        "Không thể cắt khi dự án còn tác vụ OCR/dịch/lồng tiếng/xuất video "
                        "chưa kết thúc. Hãy hoàn tất hoặc hủy tác vụ trước (tạm dừng chưa đủ)."
                    )
        if snapshot.timeline_edit is not None and command in {
            "split", "split_video", "trim_head", "trim_tail", "speed", "resize", "move",
            "delete", "volume", "clip_mute", "paste_video", "duplicate_video",
        }:
            # A free media edit changes the source layout itself. Materialize the
            # retained view in the same transaction, so display coordinates and
            # fragment identities stay exact and Undo restores the original cut.
            editor = TimelineEditor(project_for_timeline(snapshot))
        if command in {"paste_video", "duplicate_video", "clip_mute"} and (
            snapshot.voice_conform is not None and editor is original_editor
        ):
            editor = TimelineEditor(self._materialized_video_view(snapshot))
        if command == "split_video":
            if timeline_us is None:
                raise ValueError("Hãy đặt playhead trong đoạn video cần tách")
            if snapshot.voice_conform is not None and editor is original_editor:
                from ducai_studio.domain.timeline_edit import cut_state

                duration = build_scan_plan(snapshot)["duration_us"]
                materialized = replace(snapshot, timeline_edit=cut_state(
                    snapshot, "keep_range", 0, duration))
                editor = TimelineEditor(project_for_timeline(materialized))
            videos = [c for t in editor.snapshot.tracks if t.kind is TrackKind.VIDEO
                      and t.visible for c in t.clips
                      if c.timeline_range.start_us < timeline_us < c.timeline_range.end_us]
            selected_video = next((c for c in videos if c.id == clip_id), None)
            if selected_video is None and videos:
                ranks = {t.id: rank for rank, t in enumerate(editor.snapshot.tracks)}
                selected_video = max(videos, key=lambda c: (
                    ranks[c.track_id], c.timeline_range.start_us))
            if selected_video is None:
                raise ValueError("Hãy đặt playhead bên trong một đoạn video, không ở mép nối")
            updated = editor.split(selected_video.id, timeline_us, apply_linked=True)
        elif command == "delete_video_segment":
            view = project_for_timeline(snapshot)
            selected = next((c for t in view.tracks if t.kind is TrackKind.VIDEO
                             for c in t.clips if c.id == clip_id), None)
            if selected is None:
                raise ValueError("Hãy chọn đoạn video cần xóa trên V1")
            interval = selected.timeline_range
            if snapshot.voice_conform is not None and snapshot.timeline_edit is None:
                spans = [s for s in build_scan_plan(snapshot)["segments"]
                         if s["clip_id"] == clip_id]
                if not spans:
                    raise ValueError("Đoạn video không còn trong thành phẩm")
                interval = TimeRange(spans[0]["timeline_start_us"],
                                     spans[-1]["timeline_end_us"])
            first_video_track = next(t.id for t in view.tracks if t.kind is TrackKind.VIDEO)
            if selected.track_id != first_video_track:
                if editor is original_editor and snapshot.timeline_edit is not None:
                    editor = TimelineEditor(view)
                updated = editor.delete(selected.id, apply_linked=True)
            else:
                updated = editor.cut("remove_range", interval.start_us, interval.end_us)
        elif command == "paste_video":
            bundle = self._video_clipboards.get(project_id)
            if bundle is None:
                raise ValueError("Hãy sao chép đoạn video trước khi dán")
            updated = editor.paste_video(bundle, timeline_us if timeline_us is not None else -1)
        elif command == "duplicate_video":
            updated = editor.duplicate_video(clip_id or "", include_fragments=(
                snapshot.voice_conform is not None and snapshot.timeline_edit is None))
        elif command == "clip_mute":
            if type(value) is not bool:
                raise ValueError("Trạng thái tắt tiếng phải là boolean")
            updated = editor.set_clip_muted(clip_id or "", value, include_fragments=(
                snapshot.voice_conform is not None and snapshot.timeline_edit is None))
        elif command == "cut":
            if range_start_us is None or range_end_us is None or operation is None:
                raise ValueError("Cắt video cần thao tác và đủ hai mốc A/B")
            updated = editor.cut(operation, range_start_us, range_end_us)
        elif command == "set_markers":
            if not isinstance(value, dict):
                raise ValueError("Danh sách mark Timeline không hợp lệ")
            updated = editor.set_markers(value.get("markers"))
        elif command == "split":
            updated = editor.split(
                clip_id or "",
                timeline_us if timeline_us is not None else -1,
                apply_linked=apply_linked,
            )
        elif command == "trim_head":
            updated = editor.trim_head(
                clip_id or "",
                timeline_us if timeline_us is not None else -1,
                apply_linked=apply_linked,
            )
        elif command == "trim_tail":
            updated = editor.trim_tail(
                clip_id or "",
                timeline_us if timeline_us is not None else -1,
                apply_linked=apply_linked,
            )
        elif command == "speed":
            if value is None:
                raise ValueError("Speed command requires a percent value")
            updated = editor.set_speed_percent(clip_id or "", value, apply_linked=apply_linked)
        elif command == "resize":
            if timeline_start_us is None or timeline_end_us is None:
                raise ValueError("Resize command requires a timeline range")
            updated = editor.resize(
                clip_id or "",
                timeline_start_us,
                timeline_end_us,
                apply_linked=apply_linked,
            )
        elif command == "move":
            if timeline_start_us is None:
                raise ValueError("Move command requires a timeline start")
            updated = editor.move(
                clip_id or "",
                timeline_start_us,
                apply_linked=apply_linked,
            )
        elif command == "volume":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Volume command requires a numeric value")
            updated = editor.set_volume(clip_id or "", float(value))
        elif command == "delete":
            updated = editor.delete(clip_id or "", apply_linked=apply_linked)
        elif command == "track_mute":
            if type(value) is not bool:
                raise ValueError("Track mute command requires a boolean value")
            updated = editor.set_track_muted(track_id or "", value)
        elif command == "track_volume":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Track volume command requires a numeric value")
            updated = editor.set_track_volume(track_id or "", float(value))
        elif command == "track_lock":
            if type(value) is not bool:
                raise ValueError("Track lock command requires a boolean value")
            updated = editor.set_track_locked(track_id or "", value)
        elif command == "auto_fit_voice_timing":
            updated = editor.auto_fit_voice_timing()
        elif command == "delete_tracks":
            updated = editor.delete_tracks(tuple(track_ids))
        elif command == "edit_title_text":
            from ducai_studio.application.title_text_commands import edit_title_text

            if not isinstance(value, dict) or not isinstance(value.get("values"), dict):
                raise ValueError("Thông số Text không hợp lệ")
            title_values = dict(value["values"])
            title_command = str(value.get("command", ""))
            if title_values.pop("display_time", False):
                for key, endpoint in (("start_us", "start"), ("end_us", "end")):
                    if key in title_values:
                        title_values[key] = timeline_to_base_us(
                            snapshot, title_values[key], endpoint)
                if title_command in {"move_scene_texts", "resize_scene_texts"}:
                    title_values["range_overrides"] = self._display_cue_time_overrides(
                        snapshot, tuple(title_values.get("cue_ids", [])),
                        title_values.get("delta_us", 0),
                        "move" if title_command == "move_scene_texts" else title_values["edge"],
                    )
            updated = editor._commit(edit_title_text(snapshot, title_command,
                                                    value.get("cue_id"), title_values))
        elif command == "undo":
            updated = editor.undo()
        elif command == "redo":
            updated = editor.redo()
        else:
            raise ValueError(f"Unknown timeline command: {command}")
        if editor is not original_editor:
            updated = original_editor._commit(updated)
            editor = original_editor
        try:
            self._store_editor_snapshot(updated, deferred=deferred)
        except Exception:
            editor.snapshot = snapshot
            editor._undo, editor._redo = previous_undo, previous_redo
            raise
        self._sync_edit_history(editor)
        previous_tracks = {track.id: track for track in snapshot.tracks}
        next_tracks = {track.id: track for track in updated.tracks}
        next_clip_tracks = {
            clip.id: track.id for track in updated.tracks for clip in track.clips
        }
        previous_clips = {
            clip.id: clip for track in snapshot.tracks for clip in track.clips
        }
        next_clips = {clip.id: clip for track in updated.tracks for clip in track.clips}
        payload = {
            "_timeline_patch": True,
            **self._edit_persistence_state(project_id),
            "id": updated.id,
            "revision": updated.revision,
            "clip_upserts": _json_value(
                [
                    {**asdict(clip), "track_id": next_clip_tracks[clip_id]}
                    for clip_id, clip in next_clips.items()
                    if previous_clips.get(clip_id) != clip
                ]
            ),
            "removed_clip_ids": sorted(set(previous_clips) - set(next_clips)),
            "track_upserts": _json_value(
                [
                    {
                        "id": track.id,
                        "kind": track.kind.value,
                        "name": track.name,
                        "locked": track.locked,
                        "visible": track.visible,
                        "muted": track.muted,
                    }
                    for track_id, track in next_tracks.items()
                    if track_id not in previous_tracks
                    or replace(track, clips=()) != replace(previous_tracks[track_id], clips=())
                ]
            ),
            "removed_track_ids": sorted(set(previous_tracks) - set(next_tracks)),
            "track_order": [track.id for track in updated.tracks],
        }
        payload["_timeline_can_undo"] = editor.can_undo
        payload["_timeline_can_redo"] = editor.can_redo
        payload["_subtitle_can_undo"] = editor.can_undo
        payload["_subtitle_can_redo"] = editor.can_redo
        previous_media = {item.id: item for item in snapshot.media}
        current_media = {item.id: item for item in updated.media}
        payload["media_upserts"] = _json_value([
            asdict(item) for key, item in current_media.items()
            if previous_media.get(key) != item
        ])
        payload["removed_media_ids"] = sorted(set(previous_media) - set(current_media))
        payload["timeline_edit"] = (
            _json_value(asdict(updated.timeline_edit)) if updated.timeline_edit else None
        )
        if updated.timeline_markers != snapshot.timeline_markers:
            payload["timeline_markers"] = list(updated.timeline_markers)
        payload["request_id"] = request_id
        payload["base_revision"] = snapshot.revision
        if updated.voice_conform != snapshot.voice_conform:
            payload["voice_conform"] = (
                _json_value(asdict(updated.voice_conform)) if updated.voice_conform else None
            )
        if updated.overlays != snapshot.overlays:
            previous_overlays = {overlay.id: overlay for overlay in snapshot.overlays}
            current_overlays = {overlay.id: overlay for overlay in updated.overlays}
            payload["overlay_upserts"] = _json_value([
                asdict(overlay) for key, overlay in current_overlays.items()
                if previous_overlays.get(key) != overlay
            ])
            payload["removed_overlay_ids"] = sorted(
                set(previous_overlays) - set(current_overlays)
            )
        for field, prefix in (("source_cues", "source_cue"),
                              ("translated_cues", "translated_cue"),
                              ("title_cues", "title_cue")):
            previous_cues = getattr(snapshot, field)
            current_cues = getattr(updated, field)
            # Independent edits retain immutable collections by identity; do
            # not scan or resend thousands of untouched subtitle indices.
            if current_cues is previous_cues:
                payload[prefix + "_upserts"] = []
                payload["removed_" + prefix + "_ids"] = []
                payload[prefix + "_indices"] = {}
                continue
            previous = {cue.id: cue for cue in previous_cues}
            current = {cue.id: cue for cue in current_cues}
            payload[prefix + "_upserts"] = _json_value([
                asdict(cue) for key, cue in current.items()
                if key not in previous or replace(cue, index=1) != replace(previous[key], index=1)
            ])
            payload["removed_" + prefix + "_ids"] = sorted(set(previous) - set(current))
            payload[prefix + "_indices"] = {key: cue.index for key, cue in current.items()}
        return payload

    @staticmethod
    def _display_cue_time_overrides(snapshot: ProjectSnapshot, cue_ids: tuple[str, ...],
                                    delta_us: int, edge: str) -> dict[str, TimeRange]:
        selected = set(cue_ids)
        result = {}
        plan = build_scan_plan(snapshot)
        for cue in (*snapshot.source_cues, *snapshot.title_cues):
            if cue.id not in selected:
                continue
            interval = (range_on_scan_timeline(plan, cue.range)
                        if snapshot.timeline_edit or snapshot.voice_conform else cue.range)
            if interval is None:
                raise ValueError("Cue không còn nằm trong phần video giữ lại")
            start = interval.start_us + (delta_us if edge in {"move", "left"} else 0)
            end = interval.end_us + (delta_us if edge in {"move", "right"} else 0)
            result[cue.id] = (TimeRange(scan_time_to_base_us(plan, start, "start"),
                                       scan_time_to_base_us(plan, end, "end"))
                              if snapshot.timeline_edit or snapshot.voice_conform
                              else TimeRange(start, end))
        return result

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for record, progress, final_result in self.jobs.list_recent_activity(limit=limit):
            item = _json_value(asdict(record))
            if progress is not None:
                item["progress"] = _json_value(progress)
            if final_result is not None:
                item["result"] = _json_value(final_result)
            result.append(item)
        return result

    def delete_job_history(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.state not in TERMINAL_JOB_STATES:
            raise ValueError("Chỉ có thể xóa lịch sử của tác vụ đã kết thúc.")
        self._cleanup_external_job_staging(job, remove_files=True)
        for root in (self.checkpoints.staging_root, self.checkpoints.immutable_root):
            _remove_internal_artifact_path(root / job_id, root)
        self.jobs.delete_terminal(job_id)
        return {
            "job_id": job_id,
            "project_id": job.spec.project_id,
            "kind": job.spec.kind.value,
        }

    def clear_project_job_history(self, project_id: str, kinds: tuple[str, ...]) -> dict[str, Any]:
        allowed = {JobKind.OCR.value, JobKind.TRANSLATE.value, JobKind.TTS.value}
        normalized = tuple(dict.fromkeys(value.strip() for value in kinds if value.strip()))
        if not normalized or any(value not in allowed for value in normalized):
            raise ValueError("Lịch sử chỉ hỗ trợ OCR, dịch thuật và tạo giọng.")
        job_kinds = tuple(JobKind(value) for value in normalized)
        job_ids = self.jobs.terminal_ids(project_id=project_id, kinds=job_kinds)
        for job_id in job_ids:
            self._cleanup_external_job_staging(
                self.jobs.get(job_id),
                remove_files=True,
            )
            for root in (self.checkpoints.staging_root, self.checkpoints.immutable_root):
                _remove_internal_artifact_path(root / job_id, root)
        for job_id in job_ids:
            self.jobs.delete_terminal(job_id)
        return {
            "project_id": project_id,
            "kinds": list(normalized),
            "deleted_jobs": len(job_ids),
            "job_ids": list(job_ids),
        }

    @staticmethod
    def _cleanup_external_job_staging(job: Any, *, remove_files: bool) -> None:
        """Clean only a verified per-job candidate directory beside media."""

        config = job.spec.provider_config
        output_raw = config.get("output_path")
        staging_raw = config.get("staging_output_path")
        if not isinstance(output_raw, str) or not isinstance(staging_raw, str):
            return
        active = Path(output_raw).expanduser().resolve()
        staging = Path(staging_raw).expanduser().resolve()
        external_root = active.parent / ".ducai-staging"
        job_root = external_root / job.id
        if staging.parent != job_root or staging.name != active.name:
            return
        candidates = [staging]
        active_qa_raw = config.get("qa_path")
        staging_qa_raw = config.get("staging_qa_path")
        if isinstance(active_qa_raw, str) and isinstance(staging_qa_raw, str):
            active_qa = Path(active_qa_raw).expanduser().resolve()
            staging_qa = Path(staging_qa_raw).expanduser().resolve()
            if staging_qa.parent == job_root and staging_qa.name == active_qa.name:
                candidates.append(staging_qa)
        if remove_files:
            for candidate in candidates:
                candidate.unlink(missing_ok=True)
            if job_root.is_dir():
                for child in job_root.iterdir():
                    resolved_child = child.resolve()
                    if resolved_child.parent == job_root and resolved_child.is_file():
                        resolved_child.unlink(missing_ok=True)
        with suppress(OSError):
            job_root.rmdir()
        with suppress(OSError):
            external_root.rmdir()

    def list_project_job_history_ids(
        self, project_id: str, kinds: tuple[str, ...]
    ) -> tuple[str, ...]:
        allowed = {JobKind.OCR.value, JobKind.TRANSLATE.value, JobKind.TTS.value}
        normalized = tuple(dict.fromkeys(value.strip() for value in kinds if value.strip()))
        if not normalized or any(value not in allowed for value in normalized):
            raise ValueError("Lịch sử chỉ hỗ trợ OCR, dịch thuật và tạo giọng.")
        return self.jobs.project_ids(
            project_id=project_id,
            kinds=tuple(JobKind(value) for value in normalized),
        )

    def import_final_voice_wav(self, job_id: str, output_path: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.spec.kind.value != "tts":
            raise ValueError("Only TTS jobs can import a voice result")
        config = job.spec.provider_config
        if config.get("purpose") != "production":
            raise ValueError("Preview TTS output cannot be imported into the timeline")
        expected_raw = config.get("output_path")
        if not isinstance(expected_raw, str):
            raise ValueError("TTS job has no expected output path")
        active = Path(expected_raw).expanduser().resolve()
        candidate_raw = config.get("staging_output_path", expected_raw)
        if not isinstance(candidate_raw, str):
            raise ValueError("TTS job has no candidate output path")
        target = Path(output_path).expanduser().resolve(strict=True)
        staged = Path(candidate_raw).expanduser().resolve()
        expected = staged if staged.is_file() or target == staged else active
        if target != expected or not target.name.endswith("_voice_vi.wav"):
            raise ValueError("TTS result path does not match the guarded final output")
        qa_raw = config.get("staging_qa_path", config.get("qa_path"))
        active_qa_raw = config.get("qa_path")
        if not isinstance(qa_raw, str) or not isinstance(active_qa_raw, str):
            raise ValueError("TTS job has no QA output path")
        qa_path = Path(qa_raw).expanduser().resolve()
        active_qa = Path(active_qa_raw).expanduser().resolve()
        if target == staged and staged != active:
            try:
                qa = json.loads(qa_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("TTS result has no readable QA report") from exc
            if (
                not isinstance(qa, Mapping)
                or qa.get("status") != "PASS"
                or qa.get("job_id") != job.id
                or qa.get("input_fingerprint") != job.spec.input_fingerprint
                or qa.get("settings_fingerprint") != job.spec.settings_fingerprint
                or qa.get("output_path") != str(target)
                or qa.get("output_sha256") != _file_sha256(target)
            ):
                raise ValueError("TTS QA report does not match the candidate output")
        snapshot = self.projects.load(job.spec.project_id)
        raw_cues = config.get("cues", ())
        try:
            job_cues = tuple(
                (
                    str(item["id"]),
                    int(item["index"]),
                    int(item["start_us"]),
                    int(item["end_us"]),
                    str(item["text"]),
                )
                for item in raw_cues
                if isinstance(item, Mapping)
            )
        except (KeyError, TypeError, ValueError):
            job_cues = ()
        current_cues = tuple(
            (
                cue["id"],
                cue["index"],
                cue["start_us"],
                cue["end_us"],
                cue["text"],
            )
            for cue in self._tts_cues_for_snapshot(snapshot)
        )
        if not job_cues or job_cues != current_cues:
            raise ValueError(
                "Phụ đề hoặc nội dung dịch đã thay đổi; project revision đã thay đổi "
                "phần thoại hoặc timestamp sau khi tạo tác vụ voice. "
                "hãy bấm Tiếp tục từ cache để tool tạo tác vụ thay thế trên bản sửa mới nhất."
            )
        cue_timing: tuple[Mapping[str, object], ...] | None = None
        coverage = self._load_voice_qa_coverage(job)
        # The published master is assembled from the same cue WAVs recorded in
        # the durable ledger. Import the measured intervals when available so
        # D1 becomes one clip per SRT cue; old jobs without that evidence keep
        # the legacy single-clip import.
        rows = self._voice_timing_timeline_rows(job, coverage) if coverage else []
        source_keys = {
            (cue.id, cue.index) for cue in snapshot.source_cues if cue.is_dialogue
        }
        row_keys = {(str(row.get("cue_id", "")).strip(), row.get("index")) for row in rows}
        if rows and row_keys == source_keys:
            cue_timing = tuple(
                {
                    "cue_id": str(row["cue_id"]),
                    "index": int(row["index"]),
                    "timeline_start_us": int(row["voice_start_us"]),
                    "timeline_end_us": int(row["voice_end_us"]),
                    "source_start_us": int(row["voice_start_us"]),
                    "source_end_us": int(row["voice_end_us"]),
                    "timing_slot_start_us": int(row.get("source_start_us", row["voice_start_us"])),
                    "timing_slot_end_us": int(row.get("source_end_us", row["voice_end_us"])),
                    "cue_text": next(
                        (
                            cue.text
                            for cue in snapshot.translated_cues
                            if cue.id == str(row["cue_id"]) and cue.index == int(row["index"])
                        ),
                        "",
                    ),
                    "content_warning": row.get("content_warning") is True,
                    "issue_code": str(row.get("issue_code", "")),
                }
                for row in rows
            )
        promotion_pairs = ((target, active),)
        if qa_path.is_file():
            promotion_pairs = (*promotion_pairs, (qa_path, active_qa))
        with _promote_output_files(promotion_pairs):
            updated = self.media_imports.import_voice_media(
                snapshot,
                active,
                cue_timing=cue_timing,
            )
            summary = self.projects.get_summary(snapshot.id)
            self.projects.save(project_path=summary["path"], snapshot=updated)
        self._cleanup_external_job_staging(job, remove_files=False)
        self._timeline_editors[snapshot.id] = TimelineEditor(updated)
        return _json_value(asdict(updated))

    def import_final_voice_conform(self, job_id: str, output_path: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        config = job.spec.provider_config
        if job.spec.kind is not JobKind.TTS or config.get("job_mode") != "voice_conform":
            raise ValueError("Only voice conform jobs can publish a conformed D1")
        expected_raw = config.get("output_path")
        qa_raw = config.get("qa_path")
        if not isinstance(expected_raw, str) or not isinstance(qa_raw, str):
            raise ValueError("Voice conform job has no guarded output")
        target = Path(output_path).expanduser().resolve(strict=True)
        expected = Path(expected_raw).expanduser().resolve()
        qa_path = Path(qa_raw).expanduser().resolve(strict=True)
        if target != expected or target.suffix.casefold() != ".wav":
            raise ValueError("Voice conform result path does not match the job")
        try:
            qa = json.loads(qa_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Voice conform QA is unreadable") from exc
        if (
            not isinstance(qa, Mapping)
            or qa.get("status") != "PASS"
            or qa.get("kind") != "voice_conform"
            or qa.get("output_path") != str(target)
            or qa.get("output_sha256") != _file_sha256(target)
        ):
            raise ValueError("Voice conform QA does not match its output")
        plan = decode_voice_conform_plan(qa.get("plan"))
        if plan.fingerprint != config.get("plan_fingerprint"):
            raise ValueError("Voice conform plan fingerprint changed during processing")
        snapshot = self.projects.load(job.spec.project_id)
        if snapshot.revision != job.spec.input_revision:
            raise ValueError("Project changed while voice conform was running")
        parent_tts_job_id = str(config.get("parent_tts_job_id", ""))
        updated = self.media_imports.apply_voice_conform_media(
            snapshot,
            target,
            plan=plan,
            parent_tts_job_id=parent_tts_job_id,
        )
        summary = self.projects.get_summary(snapshot.id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        self._timeline_editors[snapshot.id] = TimelineEditor(updated)
        return _json_value(asdict(updated))

    def validate_final_export(self, job_id: str, output_path: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.spec.kind.value != "export":
            raise ValueError("Only export jobs can publish a rendered output")
        expected_raw = job.spec.provider_config.get("output_path")
        if not isinstance(expected_raw, str):
            raise ValueError("Export job has no expected output path")
        target = Path(output_path).expanduser().resolve(strict=True)
        expected = Path(expected_raw).expanduser().resolve()
        qa_path = target.with_name(target.stem + "_QA.json")
        if target != expected or target.suffix.casefold() != ".mp4":
            raise ValueError("Export result path does not match the immutable snapshot")
        if not qa_path.is_file():
            raise ValueError("Export result has no final QA report")
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        if (
            not isinstance(qa, dict)
            or qa.get("status") != "PASS"
            or qa.get("output_path") != str(target)
            or qa.get("output_sha256") != _file_sha256(target)
        ):
            raise ValueError("Export QA report does not match the finalized output")
        return {"output_path": str(target), "qa_path": str(qa_path), "status": "PASS"}

    def import_final_ocr_srt(self, job_id: str, output_path: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.spec.kind.value != "ocr":
            raise ValueError("Only OCR jobs can import an OCR result")
        config = job.spec.provider_config
        expected_raw = config.get("output_path")
        if not isinstance(expected_raw, str):
            raise ValueError("OCR job has no expected output path")
        active = Path(expected_raw).expanduser().resolve()
        candidate_raw = config.get("staging_output_path", expected_raw)
        if not isinstance(candidate_raw, str):
            raise ValueError("OCR job has no candidate output path")
        target = Path(output_path).expanduser().resolve(strict=True)
        staged = Path(candidate_raw).expanduser().resolve()
        expected = staged if staged.is_file() or target == staged else active
        if target != expected or not target.name.endswith("_ZH_FINAL.srt"):
            raise ValueError("OCR result path does not match the guarded final output")
        qa_path = target.with_name(target.stem + "_QA.json")
        if not qa_path.is_file():
            raise ValueError("OCR result has no matching coverage QA report")
        try:
            qa = json.loads(qa_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("OCR coverage QA report is unreadable") from exc
        method = str(config.get("extraction_method", "frame_ocr"))
        common_qa_valid = bool(
            isinstance(qa, dict)
            and qa.get("status") == "PASS"
            and qa.get("job_id") == job_id
            and qa.get("input_fingerprint") == job.spec.input_fingerprint
            and qa.get("settings_fingerprint") == job.spec.settings_fingerprint
            and qa.get("output_path") == str(target)
            and qa.get("output_sha256") == _file_sha256(target)
        )
        if method == "whisper_audio":
            if (
                not common_qa_valid
                or qa.get("schema_version") != 1
                or qa.get("kind") != "whisper_asr"
                or qa.get("extraction_method") != "whisper_audio"
                or type(qa.get("cue_count")) is not int
                or int(qa["cue_count"]) <= 0
            ):
                raise ValueError("Whisper QA report does not match the guarded final output")
        else:
            total = qa.get("subtitle_positive_timestamps") if isinstance(qa, dict) else None
            covered = qa.get("covered_timestamps") if isinstance(qa, dict) else None
            threshold = qa.get("coverage_threshold") if isinstance(qa, dict) else None
            ratio = qa.get("coverage_ratio") if isinstance(qa, dict) else None
            continuity_limit = qa.get("continuity_gap_limit_us") if isinstance(qa, dict) else None
            maximum_gap = qa.get("maximum_positive_gap_us") if isinstance(qa, dict) else None
            valid_counts = bool(
                type(total) is int and type(covered) is int and total > 0 and 0 <= covered <= total
            )
            valid_ratio = bool(
                valid_counts
                and not isinstance(threshold, bool)
                and isinstance(threshold, (int, float))
                and float(threshold) >= 0.95
                and not isinstance(ratio, bool)
                and isinstance(ratio, (int, float))
                and abs(float(ratio) - covered / total) <= 1e-9
                and float(ratio) >= float(threshold)
            )
            valid_continuity = bool(
                type(continuity_limit) is int
                and continuity_limit > 0
                and type(maximum_gap) is int
                and 0 <= maximum_gap <= continuity_limit
                and qa.get("timeline_continuity_passes") is True
            )
            if (
                not common_qa_valid
                or qa.get("schema_version") not in {2, 3}
                or qa.get("kind") != "ocr_hard_sub_coverage"
                or not valid_ratio
                or not valid_continuity
            ):
                raise ValueError("OCR coverage QA report does not match the guarded final output")
        snapshot = self.projects.load(job.spec.project_id)
        self._validate_ocr_scan_layout(snapshot, config)
        videos = [item for item in snapshot.media if item.kind is MediaKind.VIDEO]
        if not videos:
            raise ValueError("OCR project no longer has a video")
        candidate_snapshot = self.media_imports.import_srt(snapshot, target, language="zh")
        if method != "whisper_audio":
            candidate_snapshot = _apply_ocr_cue_regions(candidate_snapshot, qa)
            candidate_snapshot = self._canonicalize_ocr_result(
                candidate_snapshot, config,
                watermarks_in_timeline=isinstance(qa.get("watermark_regions"), list),
            )
        if method == "whisper_audio" and len(candidate_snapshot.source_cues) != int(
            qa["cue_count"]
        ):
            raise ValueError("Whisper cue count does not match the guarded final output")
        maximum_end = max((segment["base_end_us"]
                           for segment in config.get("scan_plan", {}).get("segments", [])),
                          default=videos[0].duration_us)
        if candidate_snapshot.source_cues[-1].range.end_us > maximum_end + 1_000:
            raise ValueError("OCR subtitle timeline exceeds the source video")
        active_qa = active.with_name(active.stem + "_QA.json")
        with _promote_output_files(((target, active), (qa_path, active_qa))):
            updated = self.media_imports.import_srt(snapshot, active, language="zh")
            if method == "whisper_audio":
                updated = replace(
                    updated,
                    source_cues=tuple(
                        replace(cue, source="whisper-large-v3") for cue in updated.source_cues
                    ),
                )
            else:
                updated = _apply_ocr_cue_regions(updated, qa)
                updated = self._canonicalize_ocr_result(
                    updated, config,
                    watermarks_in_timeline=isinstance(qa.get("watermark_regions"), list),
                )
            updated = _merge_preserved_manual_subtitle_cues(snapshot, updated)
            summary = self.projects.get_summary(snapshot.id)
            self.projects.save(project_path=summary["path"], snapshot=updated)
        self._cleanup_external_job_staging(job, remove_files=False)
        self._subtitle_editors[snapshot.id] = SubtitleEditor(updated)
        self._timeline_editors[snapshot.id] = TimelineEditor(updated)
        return _json_value(asdict(updated))

    @staticmethod
    def _validate_ocr_scan_layout(snapshot: ProjectSnapshot, config: Mapping) -> None:
        plan = config.get("scan_plan")
        if isinstance(plan, Mapping) and thaw_json(plan) != build_scan_plan(snapshot):
            raise ValueError(
                "Timeline đã thay đổi sau khi bắt đầu OCR; không nhập kết quả lệch mốc"
            )

    @staticmethod
    def _canonicalize_ocr_result(
        snapshot: ProjectSnapshot, config: Mapping, *, watermarks_in_timeline: bool = False,
    ) -> ProjectSnapshot:
        plan = config.get("scan_plan")
        if not isinstance(plan, Mapping):
            return snapshot
        # Repository job settings are recursively frozen mappings/tuples.
        # Review imports use those settings directly, unlike final JSON IPC.
        plan = thaw_json(plan)
        source = tuple(replace(cue, range=TimeRange(
            scan_time_to_base_us(plan, cue.range.start_us, "start"),
            scan_time_to_base_us(plan, cue.range.end_us, "end"),
        )) for cue in snapshot.source_cues)
        tracks = SubtitleEditor(replace(snapshot, source_cues=source))._rebuild_subtitle_tracks(
            source, snapshot.translated_cues)
        overlays = tuple(replace(overlay, range=TimeRange(
            scan_time_to_base_us(plan, overlay.range.start_us, "start"),
            scan_time_to_base_us(plan, overlay.range.end_us, "end"),
        )) if watermarks_in_timeline and overlay.tracking_mode == "ocr_watermark" else overlay
            for overlay in snapshot.overlays)
        return replace(snapshot, source_cues=source, tracks=tracks, overlays=overlays)

    def import_final_translation_srt(self, job_id: str, output_path: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.spec.kind.value != "translate":
            raise ValueError("Only translation jobs can import a translated result")
        expected_raw = job.spec.provider_config.get("output_path")
        if not isinstance(expected_raw, str):
            raise ValueError("Translation job has no expected output path")
        active = Path(expected_raw).expanduser().resolve()
        candidate_raw = job.spec.provider_config.get("staging_output_path", expected_raw)
        if not isinstance(candidate_raw, str):
            raise ValueError("Translation job has no candidate output path")
        target = Path(output_path).expanduser().resolve(strict=True)
        staged = Path(candidate_raw).expanduser().resolve()
        expected = staged if staged.is_file() or target == staged else active
        if target != expected or not target.name.endswith("_codex_vi.srt"):
            raise ValueError("Translation result path does not match the guarded final output")
        qa_path = target.with_name(target.stem + "_QA.json")
        active_qa = active.with_name(active.stem + "_QA.json")
        if target == staged and staged != active:
            try:
                qa = json.loads(qa_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("Translation result has no readable QA report") from exc
            if (
                not isinstance(qa, Mapping)
                or qa.get("status") != "PASS"
                or qa.get("job_id") != job.id
                or qa.get("input_fingerprint") != job.spec.input_fingerprint
                or qa.get("settings_fingerprint") != job.spec.settings_fingerprint
                or qa.get("output_path") != str(target)
                or qa.get("output_sha256") != _file_sha256(target)
            ):
                raise ValueError("Translation QA report does not match the candidate output")
        snapshot = self.projects.load(job.spec.project_id)
        candidate_snapshot = self.media_imports.import_srt(snapshot, target, language="vi")
        if len(candidate_snapshot.translated_cues) != len(snapshot.source_cues):
            raise ValueError("Translated result cue count does not match source")
        for source, translated in zip(
            snapshot.source_cues,
            candidate_snapshot.translated_cues,
            strict=True,
        ):
            if (
                translated.id != source.id
                or translated.index != source.index
                or translated.range != source.range
                or not translated.text.strip()
            ):
                raise ValueError(f"Translated result violates source cue {source.index} integrity")
        promotion_pairs = ((target, active),)
        if qa_path.is_file():
            promotion_pairs = (*promotion_pairs, (qa_path, active_qa))
        with _promote_output_files(promotion_pairs):
            updated = self.media_imports.import_srt(snapshot, active, language="vi")
            loaded_cache = self._load_translation_cache(job_id)
            if loaded_cache is not None:
                _cache_job, ledger = loaded_cache
                qa = translation_qa_snapshot(ledger, snapshot.source_cues)
                warning_codes = {
                    int(item["id"]): tuple(str(code) for code in item.get("codes", []))
                    for item in qa.get("content_warnings", [])
                    if isinstance(item, Mapping) and type(item.get("id")) is int
                }
                updated = replace(
                    updated,
                    translated_cues=tuple(
                        replace(
                            cue,
                            evidence_ids=tuple(
                                dict.fromkeys(
                                    (
                                        *cue.evidence_ids,
                                        *(
                                            f"translation-warning:{code}"
                                            for code in warning_codes.get(cue.index, ())
                                        ),
                                    )
                                )
                            ),
                        )
                        for cue in updated.translated_cues
                    ),
                )
            summary = self.projects.get_summary(snapshot.id)
            self.projects.save(project_path=summary["path"], snapshot=updated)
        self._cleanup_external_job_staging(job, remove_files=False)
        self._timeline_editors[snapshot.id] = TimelineEditor(updated)
        self._subtitle_editors[snapshot.id] = SubtitleEditor(updated)
        return _json_value(asdict(updated))

    def import_review_translation_srt(self, job_id: str, checkpoint: object) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.spec.kind.value != "translate" or not isinstance(checkpoint, dict):
            raise ValueError("Translation review checkpoint is invalid")
        payload = checkpoint.get("payload")
        artifact = checkpoint.get("artifact")
        if (
            not isinstance(payload, dict)
            or payload.get("stage") != "review"
            or not isinstance(artifact, dict)
            or artifact.get("kind") != "translation_review_srt"
            or not isinstance(artifact.get("uri"), str)
        ):
            raise ValueError("Translation review checkpoint is not a review SRT")
        snapshot = self.projects.load(job.spec.project_id)
        updated = self.media_imports.import_srt(snapshot, str(artifact["uri"]), language="vi")
        updated = replace(
            updated,
            translated_cues=tuple(
                replace(cue, source="translation_review") for cue in updated.translated_cues
            ),
        )
        summary = self.projects.get_summary(snapshot.id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        self._cleanup_external_job_staging(job, remove_files=True)
        self._timeline_editors[snapshot.id] = TimelineEditor(updated)
        self._subtitle_editors[snapshot.id] = SubtitleEditor(updated)
        return _json_value(asdict(updated))

    def import_review_ocr_srt(
        self,
        job_id: str,
        checkpoint: object,
        qa_checkpoint: object,
    ) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if (
            job.spec.kind.value != "ocr"
            or not isinstance(checkpoint, dict)
            or not isinstance(qa_checkpoint, dict)
        ):
            raise ValueError("OCR review checkpoint is invalid")
        payload = checkpoint.get("payload")
        artifact = checkpoint.get("artifact")
        qa_payload = qa_checkpoint.get("payload")
        qa_artifact = qa_checkpoint.get("artifact")
        if (
            not isinstance(payload, dict)
            or payload.get("stage") != "review"
            or not isinstance(artifact, dict)
            or artifact.get("kind") != "ocr_review_srt"
            or not isinstance(artifact.get("uri"), str)
            or not isinstance(artifact.get("sha256"), str)
            or not isinstance(qa_payload, dict)
            or qa_payload.get("stage") != "review"
            or not isinstance(qa_artifact, dict)
            or qa_artifact.get("kind") != "ocr_qa_json"
            or not isinstance(qa_artifact.get("uri"), str)
        ):
            raise ValueError("OCR review checkpoint is not a review SRT")
        try:
            qa = json.loads(Path(str(qa_artifact["uri"])).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("OCR review QA report is unreadable") from exc
        if (
            not isinstance(qa, dict)
            or qa.get("schema_version") not in {2, 3}
            or qa.get("kind") != "ocr_hard_sub_coverage"
            or qa.get("status") != "REVIEW"
            or qa.get("job_id") != job_id
            or qa.get("input_fingerprint") != job.spec.input_fingerprint
            or qa.get("settings_fingerprint") != job.spec.settings_fingerprint
            or qa.get("output_sha256") != artifact.get("sha256")
            or not isinstance(qa.get("blockers"), list)
            or not qa["blockers"]
        ):
            raise ValueError("OCR review QA report does not match the immutable review SRT")
        snapshot = self.projects.load(job.spec.project_id)
        updated = self.media_imports.import_srt(snapshot, str(artifact["uri"]), language="zh")
        self._validate_ocr_scan_layout(snapshot, job.spec.provider_config)
        updated = _apply_ocr_cue_regions(updated, qa)
        updated = self._canonicalize_ocr_result(
            updated, job.spec.provider_config,
            watermarks_in_timeline=isinstance(qa.get("watermark_regions"), list),
        )
        updated = replace(
            updated,
            source_cues=tuple(replace(cue, source="ocr_review") for cue in updated.source_cues),
        )
        from ducai_studio.application.subtitle_finalizer import (
            collapse_partial_ocr_cues,
            collapse_proven_ocr_cues,
        )

        raw_proofs = qa.get("suppressed_readings", [])
        proofs = {
            item["evidence_id"]: item["winner_evidence_id"]
            for item in raw_proofs if isinstance(item, dict)
            and isinstance(item.get("evidence_id"), str)
            and isinstance(item.get("winner_evidence_id"), str)
            and item.get("reason") == "same_frame_crop_cut"
        } if isinstance(raw_proofs, list) else {}
        collapse_cues = (
            collapse_proven_ocr_cues
            if qa.get("proof_only_collapse") is True else collapse_partial_ocr_cues
        )
        cleaned_cues = collapse_cues(updated.source_cues, suppressed_evidence=proofs)
        if cleaned_cues != updated.source_cues:
            by_id = {cue.id: cue for cue in cleaned_cues}
            tracks = tuple(replace(track, clips=tuple(replace(clip,
                timeline_range=by_id[clip.cue_id].range,
                source_range=None, cue_index=by_id[clip.cue_id].index,
                cue_text=by_id[clip.cue_id].text) for clip in track.clips
                if clip.cue_id in by_id)) if track.kind is TrackKind.SUBTITLE else track
                for track in updated.tracks)
            updated = replace(updated, source_cues=cleaned_cues, tracks=tracks)
        updated = _merge_preserved_manual_subtitle_cues(snapshot, updated)
        summary = self.projects.get_summary(snapshot.id)
        self.projects.save(project_path=summary["path"], snapshot=updated)
        self._timeline_editors[snapshot.id] = TimelineEditor(updated)
        self._subtitle_editors[snapshot.id] = SubtitleEditor(updated)
        return _json_value(asdict(updated))

    def commit_worker_checkpoint(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        event_id: str,
    ) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        command = CommitCheckpointCommand.from_payload(payload)
        checkpoint = self.checkpoints.commit(
            job_id=job_id,
            stage_key=command.stage_key,
            checkpoint_key=command.checkpoint_key,
            schema_version=command.schema_version,
            resume_fingerprint=job.spec.resume_fingerprint,
            cursor_us=command.cursor_us,
            payload=dict(command.payload),
            artifact_path=command.artifact_path,
            artifact_kind=command.artifact_kind,
            artifact_state=ArtifactState(command.artifact_state),
            artifact_id=command.artifact_id,
            expected_sha256=command.expected_sha256,
            event_id=event_id,
            event_kind="checkpoint",
            event_payload=payload,
        )
        return _json_value(asdict(checkpoint))
