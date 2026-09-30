from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
    VoiceConformState,
    dialogue_cues,
)
from ducai_studio.domain.timebase import TimeRange, parse_srt_timestamp
from ducai_studio.domain.timeline import (
    VOICE_TIMING_OVERFLOW,
    classify_voice_timing,
    project_range,
)
from ducai_studio.domain.voice_conform import VoiceConformPlan
from ducai_studio.providers.media_probe import MediaProbeResult

from .effects_commands import ensure_default_overlays


class MediaProbe(Protocol):
    def probe(self, media_path: str | Path) -> MediaProbeResult: ...


_SRT_TIMING = re.compile(r"^\s*(\d+:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{1,3})")


def parse_srt(
    text: str,
    *,
    language: str,
    id_factory: Callable[[], str],
) -> tuple[SubtitleCue, ...]:
    normalized = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[SubtitleCue] = []
    for block in re.split(r"\n{2,}", normalized.strip()):
        lines = [line.strip() for line in block.split("\n")]
        if len(lines) < 2:
            continue
        timing_index = 1 if lines[0].isdigit() else 0
        if timing_index >= len(lines):
            continue
        match = _SRT_TIMING.match(lines[timing_index])
        if match is None:
            raise ValueError(f"Invalid SRT timing: {lines[timing_index]!r}")
        start_us = parse_srt_timestamp(match.group(1))
        end_us = parse_srt_timestamp(match.group(2))
        content = "\n".join(line for line in lines[timing_index + 1 :] if line).strip()
        cues.append(
            SubtitleCue(
                id=f"cue-{id_factory()}",
                index=len(cues) + 1,
                range=TimeRange(start_us, end_us),
                text=content,
                language=language,
                source="imported_srt",
            )
        )
    if not cues:
        raise ValueError("SRT contains no valid cues")
    return tuple(cues)


class MediaImportService:
    def __init__(
        self,
        probe: MediaProbe,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.probe = probe
        self._id_factory = id_factory or (lambda: uuid4().hex)

    def _id(self, prefix: str) -> str:
        value = self._id_factory().strip()
        if not value:
            raise RuntimeError("Media id factory returned a blank value")
        return f"{prefix}-{value}"

    @staticmethod
    def _upsert_track(
        tracks: list[TimelineTrack],
        *,
        track_id: str,
        kind: TrackKind,
        name: str,
        clips: tuple[TimelineClip, ...],
    ) -> None:
        for index, track in enumerate(tracks):
            if track.id == track_id:
                tracks[index] = replace(track, clips=(*track.clips, *clips))
                return
        tracks.append(TimelineTrack(track_id, kind, name, clips))

    def import_media(
        self,
        snapshot: ProjectSnapshot,
        media_path: str | Path,
        *,
        timeline_start_us: int | None = None,
        default_logo_path: str | Path | None = None,
        overlay_preset: dict[str, object] | None = None,
    ) -> ProjectSnapshot:
        path = Path(media_path).expanduser().resolve()
        result = self.probe.probe(path)
        media_kind = MediaKind.VIDEO if result.has_video else MediaKind.AUDIO
        media_id = self._id("media")
        asset = MediaAsset(media_id, str(path), media_kind, result.duration_us)
        project_end = project_range(snapshot)
        start_us = (
            timeline_start_us
            if timeline_start_us is not None
            else (project_end.end_us if project_end else 0)
        )
        if start_us < 0:
            raise ValueError("Timeline import position must be non-negative")
        source_range = TimeRange(0, result.duration_us)
        timeline_range = TimeRange(start_us, start_us + result.duration_us)
        tracks = list(snapshot.tracks)

        if result.has_video:
            link_id = self._id("link") if result.has_audio else None
            video = TimelineClip(
                self._id("clip"),
                "V1",
                timeline_range,
                source_range,
                media_id,
                link_id,
                volume=0.0 if result.has_audio else 1.0,
            )
            self._upsert_track(
                tracks,
                track_id="V1",
                kind=TrackKind.VIDEO,
                name="Video 1",
                clips=(video,),
            )
            if result.has_audio:
                audio = TimelineClip(
                    self._id("clip"),
                    "A1",
                    timeline_range,
                    source_range,
                    media_id,
                    link_id,
                )
                self._upsert_track(
                    tracks,
                    track_id="A1",
                    kind=TrackKind.ORIGINAL_AUDIO,
                    name="Âm thanh gốc",
                    clips=(audio,),
                )
        else:
            audio = TimelineClip(
                self._id("clip"),
                "A2",
                timeline_range,
                source_range,
                media_id,
            )
            self._upsert_track(
                tracks,
                track_id="A2",
                kind=TrackKind.BACKGROUND_MUSIC,
                name="Âm thanh nhập",
                clips=(audio,),
            )
        updated = replace(
            snapshot,
            revision=snapshot.revision + 1,
            media=(*snapshot.media, asset),
            tracks=tuple(tracks),
        )
        if result.has_video:
            updated = ensure_default_overlays(
                updated,
                logo_path=default_logo_path,
                preset=overlay_preset,
            )
        return updated

    def import_srt(
        self,
        snapshot: ProjectSnapshot,
        subtitle_path: str | Path,
        *,
        language: str = "zh",
    ) -> ProjectSnapshot:
        path = Path(subtitle_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        cues = parse_srt(
            path.read_text(encoding="utf-8-sig"),
            language=language,
            id_factory=self._id_factory,
        )
        if language.casefold() == "vi":
            if snapshot.source_cues and len(cues) != len(snapshot.source_cues):
                raise ValueError("Translated SRT cue count does not match source subtitles")
            aligned: list[SubtitleCue] = []
            for index, cue in enumerate(cues):
                if not snapshot.source_cues:
                    aligned.append(cue)
                    continue
                source = snapshot.source_cues[index]
                if cue.index != source.index or cue.range != source.range:
                    raise ValueError(
                        f"Translated SRT timing does not match source cue {source.index}"
                    )
                aligned.append(
                    replace(
                        cue,
                        id=source.id,
                        index=source.index,
                        range=source.range,
                        source="translated_srt",
                        confidence=source.confidence,
                        evidence_ids=source.evidence_ids,
                        visual_box=source.visual_box,
                        visual_box_source=source.visual_box_source,
                        role=source.role,
                        orientation=source.orientation,
                        style_hint=source.style_hint,
                        panel_box=source.panel_box,
                        background_color=source.background_color,
                        text_color=source.text_color,
                        review_state=source.review_state,
                        scene_font_scale=source.scene_font_scale,
                        text_font_size=source.text_font_size,
                        subtitle_track_id=source.subtitle_track_id,
                        background_padding_x=source.background_padding_x,
                        background_padding_y=source.background_padding_y,
                        background_style=source.background_style,
                        letter_spacing_percent=source.letter_spacing_percent,
                        font_bold=source.font_bold,
                        font_italic=source.font_italic,
                        text_opacity_percent=source.text_opacity_percent,
                        background_opacity_percent=source.background_opacity_percent,
                    )
                )
            without_voice = (
                self._without_voice(snapshot)
                if snapshot.translated_cues
                else snapshot
            )
            return replace(
                without_voice,
                revision=snapshot.revision + 1,
                translated_cues=tuple(aligned),
            )
        duration_us = max(cue.range.end_us for cue in cues)
        media_id = self._id("media")
        asset = MediaAsset(media_id, str(path), MediaKind.SUBTITLE, duration_us)
        clips = tuple(
            TimelineClip(
                self._id("clip"),
                "S1",
                cue.range,
                cue.range,
                media_id,
            )
            for cue in cues
        )
        without_voice = (
            self._without_voice(snapshot)
            if snapshot.source_cues
            else snapshot
        )
        tracks = [track for track in without_voice.tracks if track.id != "S1"]
        tracks.append(TimelineTrack("S1", TrackKind.SUBTITLE, "Phụ đề nguồn", clips))
        media = tuple(
            item for item in without_voice.media if item.kind is not MediaKind.SUBTITLE
        )
        return replace(
            without_voice,
            revision=snapshot.revision + 1,
            media=(*media, asset),
            tracks=tuple(tracks),
            source_cues=cues,
            translated_cues=(),
        )

    @staticmethod
    def _without_voice(snapshot: ProjectSnapshot) -> ProjectSnapshot:
        """Drop the active D1 projection without deleting its file or cache."""

        voice_track = next(
            (track for track in snapshot.tracks if track.id == "D1"),
            None,
        )
        voice_media_ids = (
            {clip.media_id for clip in voice_track.clips if clip.media_id is not None}
            if voice_track is not None
            else set()
        )
        if snapshot.voice_conform is not None:
            voice_media_ids.update(
                {
                    snapshot.voice_conform.base_voice_media_id,
                    snapshot.voice_conform.conformed_voice_media_id,
                }
            )
        return replace(
            snapshot,
            media=tuple(item for item in snapshot.media if item.id not in voice_media_ids),
            tracks=tuple(track for track in snapshot.tracks if track.id != "D1"),
            voice_conform=None,
        )

    def remove_voice_media(self, snapshot: ProjectSnapshot) -> ProjectSnapshot:
        """Remove the active D1 projection while preserving every backing file.

        A manual replacement must make Qt release the stable WAV before Windows
        can promote the newly mastered candidate.  The project mutation removes
        only the D1 track and its media references; cache, checkpoints and the
        old WAV stay outside this project snapshot and are deliberately kept.
        """

        if (
            snapshot.voice_conform is None
            and all(track.id != "D1" for track in snapshot.tracks)
        ):
            return snapshot
        updated = self._without_voice(snapshot)
        return replace(updated, revision=snapshot.revision + 1)

    def import_voice_media(
        self,
        snapshot: ProjectSnapshot,
        media_path: str | Path,
        *,
        cue_timing: tuple[Mapping[str, object], ...] | None = None,
    ) -> ProjectSnapshot:
        path = Path(media_path).expanduser().resolve()
        result = self.probe.probe(path)
        if result.has_video or not result.has_audio:
            raise ValueError("Voice result must be an audio-only media file")
        old_voice_track = next((track for track in snapshot.tracks if track.id == "D1"), None)
        old_media_ids = (
            {
                clip.media_id
                for clip in old_voice_track.clips
                if old_voice_track is not None and clip.media_id is not None
            }
            if old_voice_track is not None
            else set()
        )
        if snapshot.voice_conform is not None:
            old_media_ids.update(
                {
                    snapshot.voice_conform.base_voice_media_id,
                    snapshot.voice_conform.conformed_voice_media_id,
                }
            )
        media = tuple(item for item in snapshot.media if item.id not in old_media_ids)
        tracks = [track for track in snapshot.tracks if track.id != "D1"]
        media_id = self._id("media")
        asset = MediaAsset(media_id, str(path), MediaKind.AUDIO, result.duration_us)
        source_range = TimeRange(0, result.duration_us)
        clips = self._voice_clips_for_master(
            media_id,
            result.duration_us,
            cue_timing,
            fallback_range=source_range,
            expected_cues=dialogue_cues(snapshot.source_cues),
        )
        tracks.append(TimelineTrack("D1", TrackKind.VOICE, "Thuyết minh", clips))
        return replace(
            snapshot,
            revision=snapshot.revision + 1,
            media=(*media, asset),
            tracks=tuple(tracks),
            voice_conform=None,
        )

    def _voice_clips_for_master(
        self,
        media_id: str,
        media_duration_us: int,
        cue_timing: tuple[Mapping[str, object], ...] | None,
        *,
        fallback_range: TimeRange,
        expected_cues: tuple[SubtitleCue, ...] | None = None,
    ) -> tuple[TimelineClip, ...]:
        """Represent a mastered D1 WAV as cue-scoped timeline clips.

        The media asset intentionally remains the single mastered WAV. Each
        clip points at the exact interval occupied by one cue, so preview and
        export can address cue timing without playing both a master and its
        component files. A missing timing ledger is a legacy import and keeps
        the old one-clip representation.
        """

        if not cue_timing:
            return (
                TimelineClip(
                    self._id("clip"),
                    "D1",
                    fallback_range,
                    fallback_range,
                    media_id,
                ),
            )

        ordered = sorted(
            cue_timing,
            key=lambda item: (
                int(item.get("index", 0)) if isinstance(item.get("index"), int) else 0,
                str(item.get("cue_id", "")),
            ),
        )
        clips: list[TimelineClip] = []
        previous_end = -1
        seen_ids: set[str] = set()
        expected_by_key = {
            (cue.id, cue.index): cue
            for cue in (expected_cues or ())
        }
        for item in ordered:
            cue_id = str(item.get("cue_id", "")).strip()
            index = item.get("index")
            timeline_start = item.get("timeline_start_us")
            timeline_end = item.get("timeline_end_us")
            source_start = item.get("source_start_us", timeline_start)
            source_end = item.get("source_end_us", timeline_end)
            expected_cue = expected_by_key.get((cue_id, index))
            if not cue_id:
                raise ValueError("Không thể nhập D1, một cue không có mã nhận dạng.")
            if cue_id in seen_ids:
                raise ValueError(f"Không thể nhập D1, cue {index!r} bị lặp mã nhận dạng.")
            if type(index) is not int or index <= 0:
                raise ValueError(f"Không thể nhập D1, số thứ tự cue {index!r} không hợp lệ.")
            if any(
                type(value) is not int
                for value in (timeline_start, timeline_end, source_start, source_end)
            ):
                raise ValueError(f"Không thể nhập D1, timestamp của cue {index} không hợp lệ.")
            if timeline_start < 0 or timeline_end <= timeline_start:
                raise ValueError(
                    f"Không thể nhập D1, khoảng timeline của cue {index} không hợp lệ: "
                    f"{timeline_start}..{timeline_end} µs."
                )
            if source_start < 0 or source_end <= source_start:
                raise ValueError(
                    f"Không thể nhập D1, khoảng WAV nguồn của cue {index} không hợp lệ: "
                    f"{source_start}..{source_end} µs."
                )
            if source_end > media_duration_us or timeline_end > media_duration_us:
                raise ValueError(
                    f"Không thể nhập D1, cue {index} vượt quá thời lượng WAV master "
                    f"{media_duration_us / 1_000_000:.3f} giây."
                )
            if timeline_start < previous_end:
                overlap_us = previous_end - timeline_start
                raise ValueError(
                    f"Không thể nhập D1, cue {index} chồng cue trước {overlap_us / 1_000:.1f} ms. "
                    "Hãy bấm Tiếp tục từ cache để tool xếp lại timestamp rồi nhập lại."
                )
            if timeline_end - timeline_start != source_end - source_start:
                raise ValueError(
                    f"Không thể nhập D1, cue {index} có thời lượng timeline và WAV nguồn "
                    "không khớp. Hãy bấm Tiếp tục từ cache để ráp lại D1."
                )
            seen_ids.add(cue_id)
            previous_end = timeline_end
            timing_range = TimeRange(timeline_start, timeline_end)
            source_interval = TimeRange(source_start, source_end)
            # The SRT interval is the authoritative timing slot.  Keep it
            # alongside the measured master-WAV interval so Timeline can
            # distinguish a timing mismatch from a content warning without
            # creating one WAV asset per cue.
            raw_slot_start = item.get("timing_slot_start_us")
            raw_slot_end = item.get("timing_slot_end_us")
            if (
                type(raw_slot_start) is int
                and type(raw_slot_end) is int
                and raw_slot_end > raw_slot_start
            ):
                timing_slot = TimeRange(raw_slot_start, raw_slot_end)
            else:
                timing_slot = expected_cue.range if expected_cue is not None else None
            timing_state = classify_voice_timing(timing_range, timing_slot)
            timing_unresolved = timing_state == VOICE_TIMING_OVERFLOW
            content_warning = bool(item.get("content_warning", False))
            issue_code = str(item.get("issue_code", ""))
            if timing_unresolved and not issue_code:
                issue_code = "CUE_SYNC_LIMIT_EXCEEDED"
            clips.append(
                TimelineClip(
                    f"voice-{index:06d}-{cue_id[-12:]}",
                    "D1",
                    timing_range,
                    source_interval,
                    media_id,
                    cue_id=cue_id,
                    cue_index=index,
                    cue_text=str(
                        item.get(
                            "cue_text",
                            item.get("text", expected_cue.text if expected_cue else ""),
                        )
                    ),
                    timing_slot=timing_slot,
                    timing_unresolved=timing_unresolved,
                    content_warning=content_warning,
                    issue_code=issue_code if (timing_unresolved or content_warning) else "",
                )
            )
        if not clips:
            raise ValueError(
                "Không thể nhập D1, ledger không chứa khoảng thời gian cue. "
                "Hãy bấm Tiếp tục từ cache để tool dựng lại timing."
            )
        if expected_cues is not None:
            expected = {
                (cue.id, cue.index)
                for cue in expected_cues
            }
            actual = {
                (str(item.get("cue_id", "")).strip(), item.get("index"))
                for item in ordered
            }
            if actual != expected:
                missing = sorted(index for _cue_id, index in expected - actual)
                unexpected = sorted(
                    index for _cue_id, index in actual - expected if type(index) is int
                )
                details = []
                if missing:
                    details.append("thiếu cue " + ", ".join(map(str, missing[:8])))
                if unexpected:
                    details.append("thừa cue " + ", ".join(map(str, unexpected[:8])))
                raise ValueError(
                    "Không thể nhập D1, ledger không khớp phụ đề nguồn"
                    + (f" ({'; '.join(details)})" if details else "")
                    + ". Hãy chọn đúng cache của dự án rồi bấm Tiếp tục từ cache."
                )
        return tuple(clips)

    def apply_voice_conform_media(
        self,
        snapshot: ProjectSnapshot,
        media_path: str | Path,
        *,
        plan: VoiceConformPlan,
        parent_tts_job_id: str,
    ) -> ProjectSnapshot:
        if not plan.applicable:
            raise ValueError("Voice conform plan still has unresolved cues")
        path = Path(media_path).expanduser().resolve()
        result = self.probe.probe(path)
        if result.has_video or not result.has_audio:
            raise ValueError("Conformed voice result must be an audio-only media file")
        if abs(result.duration_us - plan.output_duration_us) > 100_000:
            raise ValueError("Conformed voice duration does not match its time map")
        voice_track = next((track for track in snapshot.tracks if track.id == "D1"), None)
        if voice_track is None or not voice_track.clips:
            raise ValueError("Project has no base D1 track to conform")
        active_media_id = next(
            (clip.media_id for clip in voice_track.clips if clip.media_id is not None),
            None,
        )
        if active_media_id is None:
            raise ValueError("Base D1 clip has no media")
        if snapshot.voice_conform is None:
            base_media_id = active_media_id
            stale_conformed_id = ""
        else:
            if snapshot.voice_conform.parent_tts_job_id != parent_tts_job_id:
                raise ValueError("Conform parent does not match the active base D1")
            base_media_id = snapshot.voice_conform.base_voice_media_id
            stale_conformed_id = snapshot.voice_conform.conformed_voice_media_id
        if not any(item.id == base_media_id for item in snapshot.media):
            raise ValueError("Base D1 media is missing")
        conformed_media_id = self._id("media")
        asset = MediaAsset(
            conformed_media_id,
            str(path),
            MediaKind.AUDIO,
            result.duration_us,
        )
        output_range = TimeRange(0, result.duration_us)
        cue_timing = tuple(
            {
                "cue_id": decision.cue_id,
                "index": decision.index,
                "timeline_start_us": decision.voice_start_us,
                "timeline_end_us": decision.voice_end_us,
                "source_start_us": decision.voice_start_us,
                "source_end_us": decision.voice_end_us,
                "timing_slot_start_us": decision.output_start_us,
                "timing_slot_end_us": decision.output_end_us,
                "cue_text": next(
                    (
                        cue.text
                        for cue in snapshot.translated_cues
                        if cue.id == decision.cue_id
                        and cue.index == decision.index
                    ),
                    "",
                ),
                "content_warning": any(
                    evidence.startswith("translation-warning:")
                    for cue in snapshot.translated_cues
                    if cue.id == decision.cue_id
                    and cue.index == decision.index
                    for evidence in cue.evidence_ids
                ),
                "issue_code": next(
                    (
                        evidence.removeprefix("translation-warning:")
                        for cue in snapshot.translated_cues
                        if cue.id == decision.cue_id
                        and cue.index == decision.index
                        for evidence in cue.evidence_ids
                        if evidence.startswith("translation-warning:")
                    ),
                    "",
                ),
            }
            for decision in plan.cues
        )
        clips = tuple(
            replace(clip, volume=voice_track.clips[0].volume)
            for clip in self._voice_clips_for_master(
                conformed_media_id,
                result.duration_us,
                cue_timing,
                fallback_range=output_range,
                expected_cues=dialogue_cues(snapshot.source_cues),
            )
        )
        tracks = [track for track in snapshot.tracks if track.id != "D1"]
        tracks.append(
            TimelineTrack(
                "D1",
                TrackKind.VOICE,
                voice_track.name,
                clips,
                voice_track.locked,
                voice_track.visible,
                voice_track.muted,
            )
        )
        media = tuple(
            item
            for item in snapshot.media
            if not stale_conformed_id or item.id != stale_conformed_id
        )
        return replace(
            snapshot,
            revision=snapshot.revision + 1,
            media=(*media, asset),
            tracks=tuple(tracks),
            voice_conform=VoiceConformState(
                plan,
                base_media_id,
                conformed_media_id,
                parent_tts_job_id,
            ),
        )

    def reset_voice_conform(
        self,
        snapshot: ProjectSnapshot,
        *,
        cue_timing: tuple[Mapping[str, object], ...] | None = None,
    ) -> ProjectSnapshot:
        state = snapshot.voice_conform
        if state is None:
            raise ValueError("Project has no applied voice conform plan")
        base = next(
            (item for item in snapshot.media if item.id == state.base_voice_media_id),
            None,
        )
        if base is None:
            raise ValueError("Base D1 media is missing")
        voice_track = next((track for track in snapshot.tracks if track.id == "D1"), None)
        if voice_track is None or not voice_track.clips:
            raise ValueError("Project has no active D1 track")
        warning_by_key = {
            (clip.cue_id, clip.cue_index): (clip.content_warning, clip.issue_code)
            for clip in voice_track.clips
            if clip.cue_id is not None and clip.cue_index is not None
        }
        if cue_timing:
            cue_timing = tuple(
                {
                    **dict(item),
                    "content_warning": item.get(
                        "content_warning",
                        warning_by_key.get(
                            (str(item.get("cue_id", "")), item.get("index")),
                            (False, ""),
                        )[0],
                    ),
                    "issue_code": item.get(
                        "issue_code",
                        warning_by_key.get(
                            (str(item.get("cue_id", "")), item.get("index")),
                            (False, ""),
                        )[1],
                    ),
                }
                for item in cue_timing
            )
        base_range = TimeRange(0, base.duration_us)
        clips = self._voice_clips_for_master(
            base.id,
            base.duration_us,
            cue_timing,
            fallback_range=base_range,
            expected_cues=dialogue_cues(snapshot.source_cues),
        )
        clips = tuple(
            replace(clip, volume=voice_track.clips[0].volume)
            for clip in clips
        )
        tracks = [track for track in snapshot.tracks if track.id != "D1"]
        tracks.append(
            TimelineTrack(
                "D1",
                TrackKind.VOICE,
                voice_track.name,
                clips,
                voice_track.locked,
                voice_track.visible,
                voice_track.muted,
            )
        )
        return replace(
            snapshot,
            revision=snapshot.revision + 1,
            media=tuple(
                item for item in snapshot.media if item.id != state.conformed_voice_media_id
            ),
            tracks=tuple(tracks),
            voice_conform=None,
        )
