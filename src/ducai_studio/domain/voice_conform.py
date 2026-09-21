from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any

from .fingerprint import stable_fingerprint


class VoiceTimingMode(StrEnum):
    STRETCH_VIDEO = "stretch_video"
    SPEED_AUDIO = "speed_audio"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class VoiceConformSettings:
    mode: VoiceTimingMode = VoiceTimingMode.HYBRID
    max_audio_speed: float = 1.25
    min_video_speed: float = 0.5
    allow_duration_growth: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", VoiceTimingMode(self.mode))
        if not math.isfinite(self.max_audio_speed) or not 1.0 <= self.max_audio_speed <= 2.0:
            raise ValueError("Conform audio speed must be between 1.0 and 2.0")
        if not math.isfinite(self.min_video_speed) or not 0.5 <= self.min_video_speed <= 1.0:
            raise ValueError("Conform video speed must be between 0.5 and 1.0")
        if type(self.allow_duration_growth) is not bool:
            raise ValueError("Conform duration-growth option must be boolean")

    def as_json(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "max_audio_speed": self.max_audio_speed,
            "min_video_speed": self.min_video_speed,
            "allow_duration_growth": self.allow_duration_growth,
        }


@dataclass(frozen=True, slots=True)
class VoiceConformCueInput:
    cue_id: str
    index: int
    start_us: int
    end_us: int
    audio_duration_us: int

    def __post_init__(self) -> None:
        if not self.cue_id.strip() or self.index <= 0:
            raise ValueError("Conform cue identity is invalid")
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise ValueError("Conform cue source range is invalid")
        if self.audio_duration_us <= 0:
            raise ValueError("Conform cue audio duration must be positive")

    @property
    def slot_duration_us(self) -> int:
        return self.end_us - self.start_us


@dataclass(frozen=True, slots=True)
class VoiceConformSegment:
    source_start_us: int
    source_end_us: int
    output_start_us: int
    output_end_us: int
    video_speed: float

    def __post_init__(self) -> None:
        if self.source_start_us < 0 or self.source_end_us <= self.source_start_us:
            raise ValueError("Conform segment source range is invalid")
        if self.output_start_us < 0 or self.output_end_us <= self.output_start_us:
            raise ValueError("Conform segment output range is invalid")
        if not math.isfinite(self.video_speed) or self.video_speed <= 0:
            raise ValueError("Conform segment video speed must be positive")

    @property
    def source_duration_us(self) -> int:
        return self.source_end_us - self.source_start_us

    @property
    def output_duration_us(self) -> int:
        return self.output_end_us - self.output_start_us


@dataclass(frozen=True, slots=True)
class VoiceConformCueDecision:
    cue_id: str
    index: int
    source_start_us: int
    source_end_us: int
    output_start_us: int
    output_end_us: int
    voice_start_us: int
    voice_end_us: int
    audio_duration_us: int
    rendered_audio_duration_us: int
    audio_speed: float
    video_speed: float
    required_audio_speed: float
    required_video_speed: float
    issue_code: str = ""
    issue_message: str = ""

    @property
    def adjusted(self) -> bool:
        return abs(self.audio_speed - 1.0) > 1e-9 or abs(self.video_speed - 1.0) > 1e-9

    @property
    def unresolved(self) -> bool:
        return bool(self.issue_code)


@dataclass(frozen=True, slots=True)
class VoiceConformPlan:
    schema_version: int
    settings: VoiceConformSettings
    source_duration_us: int
    output_duration_us: int
    segments: tuple[VoiceConformSegment, ...]
    cues: tuple[VoiceConformCueDecision, ...]
    unresolved_cue_ids: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Unsupported voice conform plan schema")
        if self.source_duration_us <= 0 or self.output_duration_us <= 0:
            raise ValueError("Voice conform plan duration is invalid")
        if not self.segments:
            raise ValueError("Voice conform plan requires covering segments")
        previous_source = 0
        previous_output = 0
        for segment in self.segments:
            if (
                segment.source_start_us != previous_source
                or segment.output_start_us != previous_output
            ):
                raise ValueError("Voice conform segments must be contiguous")
            previous_source = segment.source_end_us
            previous_output = segment.output_end_us
        if previous_source != self.source_duration_us:
            raise ValueError("Voice conform segments do not cover the source duration")
        if previous_output != self.output_duration_us:
            raise ValueError("Voice conform segments do not cover the output duration")
        if not self.fingerprint.strip():
            raise ValueError("Voice conform plan fingerprint is required")

    @property
    def applicable(self) -> bool:
        return not self.unresolved_cue_ids

    @property
    def added_duration_us(self) -> int:
        return self.output_duration_us - self.source_duration_us

    @property
    def adjusted_cue_count(self) -> int:
        return sum(decision.adjusted for decision in self.cues)

    def source_to_output_us(self, source_us: int) -> int:
        position = max(0, min(self.source_duration_us, int(source_us)))
        if position == self.source_duration_us:
            return self.output_duration_us
        segment = next(
            item
            for item in self.segments
            if item.source_start_us <= position < item.source_end_us
        )
        return segment.output_start_us + _map_duration(
            position - segment.source_start_us,
            segment.source_duration_us,
            segment.output_duration_us,
        )

    def output_to_source_us(self, output_us: int) -> int:
        position = max(0, min(self.output_duration_us, int(output_us)))
        if position == self.output_duration_us:
            return self.source_duration_us
        segment = next(
            item
            for item in self.segments
            if item.output_start_us <= position < item.output_end_us
        )
        return segment.source_start_us + _map_duration(
            position - segment.output_start_us,
            segment.output_duration_us,
            segment.source_duration_us,
        )

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "settings": self.settings.as_json(),
            "source_duration_us": self.source_duration_us,
            "output_duration_us": self.output_duration_us,
            "segments": [asdict(item) for item in self.segments],
            "cues": [asdict(item) for item in self.cues],
            "unresolved_cue_ids": list(self.unresolved_cue_ids),
            "fingerprint": self.fingerprint,
        }


@dataclass(slots=True)
class _CueDraft:
    cue: VoiceConformCueInput
    audio_speed: float
    rendered_audio_duration_us: int
    required_audio_speed: float
    required_video_speed: float
    issue_code: str = ""
    issue_message: str = ""


def build_voice_conform_plan(
    cues: tuple[VoiceConformCueInput, ...],
    *,
    source_duration_us: int,
    settings: VoiceConformSettings,
) -> VoiceConformPlan:
    if source_duration_us <= 0:
        raise ValueError("Voice conform source duration must be positive")
    ordered = tuple(sorted(cues, key=lambda item: (item.start_us, item.end_us, item.index)))
    if len({item.cue_id for item in ordered}) != len(ordered):
        raise ValueError("Voice conform cue ids must be unique")
    if len({item.index for item in ordered}) != len(ordered):
        raise ValueError("Voice conform cue indexes must be unique")
    if any(item.end_us > source_duration_us for item in ordered):
        raise ValueError("Voice conform cue exceeds the source duration")

    drafts = tuple(_draft_cue(item, settings) for item in ordered)
    groups = _overlap_groups(drafts)
    segments: list[VoiceConformSegment] = []
    decisions: list[VoiceConformCueDecision] = []
    output_cursor = 0
    source_cursor = 0
    for group in groups:
        group_start = min(item.cue.start_us for item in group)
        group_end = max(item.cue.end_us for item in group)
        if group_start > source_cursor:
            gap_duration = group_start - source_cursor
            segments.append(
                VoiceConformSegment(
                    source_cursor,
                    group_start,
                    output_cursor,
                    output_cursor + gap_duration,
                    1.0,
                )
            )
            source_cursor = group_start
            output_cursor += gap_duration

        scale, relative_schedule = _schedule_group(group, settings)
        required_group_video_speed = 1.0 / scale
        group_issue = ""
        group_message = ""
        if not settings.allow_duration_growth and scale > 1.0 + 1e-9:
            group_issue = "CUE_SYNC_LIMIT_EXCEEDED"
            group_message = (
                "Block cần tăng thời lượng video nhưng tùy chọn cho phép tăng "
                "tổng duration đang tắt."
            )
        elif settings.mode is VoiceTimingMode.SPEED_AUDIO and relative_schedule:
            last_voice_end = max(end for _start, end in relative_schedule.values())
            if last_voice_end > group_end - group_start:
                group_issue = "CUE_SYNC_LIMIT_EXCEEDED"
                group_message = (
                    "Các cue chồng thời gian không thể xếp tuần tự khi giữ nguyên video; "
                    "hãy dùng chế độ kết hợp/giãn video hoặc rút gọn câu."
                )
        elif required_group_video_speed < settings.min_video_speed - 1e-9:
            group_issue = "CUE_SYNC_LIMIT_EXCEEDED"
            group_message = (
                f"Block cần video {required_group_video_speed:.2f}×, thấp hơn giới hạn "
                f"{settings.min_video_speed:.2f}×."
            )

        group_output_duration = max(1, int(round((group_end - group_start) * scale)))
        segment = VoiceConformSegment(
            group_start,
            group_end,
            output_cursor,
            output_cursor + group_output_duration,
            required_group_video_speed,
        )
        segments.append(segment)
        for draft in group:
            cue = draft.cue
            mapped_start = segment.output_start_us + _map_duration(
                cue.start_us - group_start,
                group_end - group_start,
                group_output_duration,
            )
            mapped_end = segment.output_start_us + _map_duration(
                cue.end_us - group_start,
                group_end - group_start,
                group_output_duration,
            )
            voice_start_rel, voice_end_rel = relative_schedule[cue.cue_id]
            issue_code = draft.issue_code or group_issue
            issue_message = draft.issue_message or group_message
            decisions.append(
                VoiceConformCueDecision(
                    cue.cue_id,
                    cue.index,
                    cue.start_us,
                    cue.end_us,
                    mapped_start,
                    mapped_end,
                    segment.output_start_us + voice_start_rel,
                    segment.output_start_us + voice_end_rel,
                    cue.audio_duration_us,
                    draft.rendered_audio_duration_us,
                    draft.audio_speed,
                    segment.video_speed,
                    draft.required_audio_speed,
                    draft.required_video_speed,
                    issue_code,
                    issue_message,
                )
            )
        source_cursor = group_end
        output_cursor = segment.output_end_us

    if source_cursor < source_duration_us:
        tail = source_duration_us - source_cursor
        segments.append(
            VoiceConformSegment(
                source_cursor,
                source_duration_us,
                output_cursor,
                output_cursor + tail,
                1.0,
            )
        )
        output_cursor += tail
    if not segments:
        segments.append(
            VoiceConformSegment(0, source_duration_us, 0, source_duration_us, 1.0)
        )
        output_cursor = source_duration_us

    merged_segments = _merge_segments(tuple(segments))
    ordered_decisions = tuple(sorted(decisions, key=lambda item: item.index))
    unresolved = tuple(item.cue_id for item in ordered_decisions if item.unresolved)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "settings": settings.as_json(),
        "source_duration_us": source_duration_us,
        "output_duration_us": output_cursor,
        "segments": [asdict(item) for item in merged_segments],
        "cues": [asdict(item) for item in ordered_decisions],
        "unresolved_cue_ids": list(unresolved),
    }
    return VoiceConformPlan(
        1,
        settings,
        source_duration_us,
        output_cursor,
        merged_segments,
        ordered_decisions,
        unresolved,
        stable_fingerprint(payload),
    )


def decode_voice_conform_plan(value: object) -> VoiceConformPlan:
    if not isinstance(value, dict):
        raise TypeError("Voice conform plan must be an object")
    raw_settings = value.get("settings")
    if not isinstance(raw_settings, dict):
        raise TypeError("Voice conform settings must be an object")
    settings = VoiceConformSettings(
        mode=VoiceTimingMode(str(raw_settings.get("mode", VoiceTimingMode.HYBRID.value))),
        max_audio_speed=float(raw_settings.get("max_audio_speed", 1.25)),
        min_video_speed=float(raw_settings.get("min_video_speed", 0.5)),
        allow_duration_growth=raw_settings.get("allow_duration_growth", True),
    )
    segments_raw = value.get("segments")
    cues_raw = value.get("cues")
    unresolved_raw = value.get("unresolved_cue_ids", [])
    if not isinstance(segments_raw, list) or not isinstance(cues_raw, list):
        raise TypeError("Voice conform segments and cues must be arrays")
    if not isinstance(unresolved_raw, list):
        raise TypeError("Voice conform unresolved cue ids must be an array")
    return VoiceConformPlan(
        schema_version=int(value.get("schema_version", 1)),
        settings=settings,
        source_duration_us=int(value["source_duration_us"]),
        output_duration_us=int(value["output_duration_us"]),
        segments=tuple(VoiceConformSegment(**item) for item in segments_raw),
        cues=tuple(VoiceConformCueDecision(**item) for item in cues_raw),
        unresolved_cue_ids=tuple(str(item) for item in unresolved_raw),
        fingerprint=str(value["fingerprint"]),
    )


def _draft_cue(
    cue: VoiceConformCueInput,
    settings: VoiceConformSettings,
) -> _CueDraft:
    required_audio_speed = max(1.0, cue.audio_duration_us / cue.slot_duration_us)
    if required_audio_speed <= 1.0 + 1e-9:
        return _CueDraft(cue, 1.0, cue.audio_duration_us, 1.0, 1.0)

    issue_code = ""
    issue_message = ""
    if settings.mode is VoiceTimingMode.STRETCH_VIDEO:
        audio_speed = 1.0
    elif settings.mode is VoiceTimingMode.SPEED_AUDIO:
        audio_speed = min(required_audio_speed, settings.max_audio_speed)
        if required_audio_speed > settings.max_audio_speed + 1e-9:
            issue_code = "CUE_SYNC_LIMIT_EXCEEDED"
            issue_message = (
                f"Cue cần audio {required_audio_speed:.2f}×, vượt mức thanh kéo "
                f"{settings.max_audio_speed:.2f}×."
            )
    else:
        audio_speed = min(required_audio_speed, settings.max_audio_speed)
    rendered_duration = max(1, int(math.ceil(cue.audio_duration_us / audio_speed)))
    required_video_speed = min(1.0, cue.slot_duration_us / rendered_duration)
    if (
        settings.mode is not VoiceTimingMode.SPEED_AUDIO
        and required_video_speed < settings.min_video_speed - 1e-9
    ):
        issue_code = "CUE_SYNC_LIMIT_EXCEEDED"
        issue_message = (
            f"Cue cần video {required_video_speed:.2f}× sau khi audio chạy "
            f"{audio_speed:.2f}×, thấp hơn giới hạn {settings.min_video_speed:.2f}×."
        )
    return _CueDraft(
        cue,
        audio_speed,
        rendered_duration,
        required_audio_speed,
        required_video_speed,
        issue_code,
        issue_message,
    )


def _overlap_groups(drafts: tuple[_CueDraft, ...]) -> tuple[tuple[_CueDraft, ...], ...]:
    groups: list[list[_CueDraft]] = []
    group_end = -1
    for draft in drafts:
        if not groups or draft.cue.start_us >= group_end:
            groups.append([draft])
            group_end = draft.cue.end_us
            continue
        groups[-1].append(draft)
        group_end = max(group_end, draft.cue.end_us)
    # OCR timestamps can overlap or occasionally arrive with the same start.
    # The accepted dubbing contract gives cue index precedence inside a block,
    # so speech order stays deterministic even when timestamp ordering is noisy.
    return tuple(tuple(sorted(group, key=lambda item: item.cue.index)) for group in groups)


def _schedule_group(
    group: tuple[_CueDraft, ...],
    settings: VoiceConformSettings,
) -> tuple[float, dict[str, tuple[int, int]]]:
    group_start = min(item.cue.start_us for item in group)
    group_end = max(item.cue.end_us for item in group)
    source_duration = group_end - group_start
    if settings.mode is VoiceTimingMode.SPEED_AUDIO:
        scale = 1.0
    else:
        scale = max(1.0, *(1.0 / item.required_video_speed for item in group))

    schedule: dict[str, tuple[int, int]] = {}
    for _iteration in range(16):
        occupied_until = 0
        schedule = {}
        for draft in group:
            desired_start = int(round((draft.cue.start_us - group_start) * scale))
            voice_start = max(desired_start, occupied_until)
            voice_end = voice_start + draft.rendered_audio_duration_us
            schedule[draft.cue.cue_id] = (voice_start, voice_end)
            occupied_until = voice_end
        if settings.mode is VoiceTimingMode.SPEED_AUDIO:
            break
        required_scale = max(scale, occupied_until / source_duration)
        if abs(required_scale - scale) < 1e-9:
            break
        scale = required_scale
    return scale, schedule


def _merge_segments(
    segments: tuple[VoiceConformSegment, ...],
) -> tuple[VoiceConformSegment, ...]:
    merged: list[VoiceConformSegment] = []
    for segment in segments:
        if (
            merged
            and merged[-1].source_end_us == segment.source_start_us
            and merged[-1].output_end_us == segment.output_start_us
            and abs(merged[-1].video_speed - segment.video_speed) < 1e-9
        ):
            previous = merged[-1]
            merged[-1] = VoiceConformSegment(
                previous.source_start_us,
                segment.source_end_us,
                previous.output_start_us,
                segment.output_end_us,
                previous.video_speed,
            )
        else:
            merged.append(segment)
    return tuple(merged)


def _map_duration(value: int, source_duration: int, target_duration: int) -> int:
    if value <= 0:
        return 0
    if value >= source_duration:
        return target_duration
    scaled = Fraction(value * target_duration, source_duration)
    quotient, remainder = divmod(scaled.numerator, scaled.denominator)
    return quotient + int(remainder * 2 >= scaled.denominator)
