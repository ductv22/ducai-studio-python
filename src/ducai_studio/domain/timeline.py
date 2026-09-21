from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace

from .project import ProjectSnapshot, TimelineClip, TimelineTrack
from .timebase import SpeedRatio, TimeRange, TimeUs


class TimelineEditError(ValueError):
    pass


VOICE_TIMING_TOLERANCE_US = 50_000
VOICE_TIMING_SNAP_TOLERANCE_US = 80_000
VOICE_TIMING_UNKNOWN = "unknown"
VOICE_TIMING_MATCHED = "matched"
VOICE_TIMING_UNDERFLOW = "underflow"
VOICE_TIMING_OVERFLOW = "overflow"
VOICE_TIMING_POSITION_MISMATCH = "position_mismatch"


def classify_voice_timing(
    timeline_range: TimeRange,
    timing_slot: TimeRange | None,
    *,
    tolerance_us: TimeUs = VOICE_TIMING_TOLERANCE_US,
) -> str:
    """Classify a D1 clip against its authoritative SRT slot.

    Duration overflow is the only red/error state. A short clip or a clip whose
    position moved away from the SRT slot remains a warning so users can still
    distinguish it from speech that would run into the following cue.
    """

    if tolerance_us < 0:
        raise ValueError("Voice timing tolerance must be non-negative")
    if timing_slot is None:
        return VOICE_TIMING_UNKNOWN
    duration_delta_us = timeline_range.duration_us - timing_slot.duration_us
    if duration_delta_us > tolerance_us:
        return VOICE_TIMING_OVERFLOW
    if duration_delta_us < -tolerance_us:
        return VOICE_TIMING_UNDERFLOW
    if (
        abs(timeline_range.start_us - timing_slot.start_us) > tolerance_us
        or abs(timeline_range.end_us - timing_slot.end_us) > tolerance_us
    ):
        return VOICE_TIMING_POSITION_MISMATCH
    return VOICE_TIMING_MATCHED


def _timing_edit_fields(
    clip: TimelineClip,
    timeline_range: TimeRange,
) -> tuple[bool, str]:
    state = classify_voice_timing(timeline_range, clip.timing_slot)
    unresolved = state == VOICE_TIMING_OVERFLOW
    if clip.content_warning:
        issue_code = clip.issue_code
    else:
        issue_code = "CUE_SYNC_LIMIT_EXCEEDED" if unresolved else ""
    return unresolved, issue_code


def source_at_timeline(clip: TimelineClip, timeline_us: TimeUs) -> TimeUs:
    if clip.source_range is None:
        raise TimelineEditError("Clip has no source range")
    if not clip.timeline_range.start_us <= timeline_us <= clip.timeline_range.end_us:
        raise TimelineEditError("Timeline position is outside the clip")
    offset = timeline_us - clip.timeline_range.start_us
    return min(
        clip.source_range.end_us,
        clip.source_range.start_us + clip.speed.timeline_to_source_us(offset),
    )


def timeline_at_source(clip: TimelineClip, source_us: TimeUs) -> TimeUs:
    if clip.source_range is None:
        raise TimelineEditError("Clip has no source range")
    if not clip.source_range.start_us <= source_us <= clip.source_range.end_us:
        raise TimelineEditError("Source position is outside the clip")
    offset = source_us - clip.source_range.start_us
    return min(
        clip.timeline_range.end_us,
        clip.timeline_range.start_us + clip.speed.source_to_timeline_us(offset),
    )


def with_speed(clip: TimelineClip, speed: SpeedRatio) -> TimelineClip:
    if clip.source_range is None:
        raise TimelineEditError("Cannot change speed without a source range")
    duration_us = speed.source_to_timeline_us(clip.source_range.duration_us)
    timeline_range = TimeRange(
        clip.timeline_range.start_us,
        clip.timeline_range.start_us + duration_us,
    )
    timing_unresolved, issue_code = _timing_edit_fields(clip, timeline_range)
    return replace(
        clip,
        timeline_range=timeline_range,
        speed=speed,
        timing_unresolved=timing_unresolved,
        # A timing edit clears only a resolved timing finding. Content
        # warnings remain independently visible on the same cue.
        issue_code=issue_code,
        payload_revision=clip.payload_revision + 1,
    )


def with_volume(clip: TimelineClip, volume: float) -> TimelineClip:
    return replace(
        clip,
        volume=volume,
        payload_revision=clip.payload_revision + 1,
    )


def resize_clip(
    clip: TimelineClip,
    timeline_range: TimeRange,
    *,
    min_speed: float = 0.5,
    max_speed: float = 3.0,
) -> TimelineClip:
    """Resize a clip without trimming its source interval.

    The source range remains intact; the exact rational speed is recomputed so
    the entire source audio is rendered into the requested timeline duration.
    """

    if clip.source_range is None:
        raise TimelineEditError("Cannot resize a clip without a source range")
    if not 0 < min_speed <= max_speed:
        raise ValueError("Clip resize speed bounds are invalid")
    speed = SpeedRatio(clip.source_range.duration_us, timeline_range.duration_us)
    speed_value = speed.numerator / speed.denominator
    if speed_value < min_speed - 1e-9 or speed_value > max_speed + 1e-9:
        raise TimelineEditError(
            f"Thời lượng clip yêu cầu tốc độ {speed_value:.3f}×, nằm ngoài "
            f"giới hạn {min_speed:.2f}×-{max_speed:.2f}×"
        )
    timing_unresolved, issue_code = _timing_edit_fields(clip, timeline_range)
    return replace(
        clip,
        timeline_range=timeline_range,
        speed=speed,
        timing_unresolved=timing_unresolved,
        issue_code=issue_code,
        payload_revision=clip.payload_revision + 1,
    )


def trim_head(clip: TimelineClip, timeline_us: TimeUs, *, unlink: bool = False) -> TimelineClip:
    if not clip.timeline_range.start_us < timeline_us < clip.timeline_range.end_us:
        raise TimelineEditError("Head trim must be strictly inside the clip")
    source_us = source_at_timeline(clip, timeline_us)
    if clip.source_range is None or source_us >= clip.source_range.end_us:
        raise TimelineEditError("Head trim would create an empty clip")
    return replace(
        clip,
        timeline_range=TimeRange(timeline_us, clip.timeline_range.end_us),
        source_range=TimeRange(source_us, clip.source_range.end_us),
        link_group_id=None if unlink else clip.link_group_id,
        payload_revision=clip.payload_revision + 1,
    )


def trim_tail(clip: TimelineClip, timeline_us: TimeUs, *, unlink: bool = False) -> TimelineClip:
    if not clip.timeline_range.start_us < timeline_us < clip.timeline_range.end_us:
        raise TimelineEditError("Tail trim must be strictly inside the clip")
    source_us = source_at_timeline(clip, timeline_us)
    if clip.source_range is None or source_us <= clip.source_range.start_us:
        raise TimelineEditError("Tail trim would create an empty clip")
    return replace(
        clip,
        timeline_range=TimeRange(clip.timeline_range.start_us, timeline_us),
        source_range=TimeRange(clip.source_range.start_us, source_us),
        link_group_id=None if unlink else clip.link_group_id,
        payload_revision=clip.payload_revision + 1,
    )


def split_clip(
    clip: TimelineClip,
    timeline_us: TimeUs,
    *,
    left_id: str,
    right_id: str,
    left_link_group_id: str | None,
    right_link_group_id: str | None,
) -> tuple[TimelineClip, TimelineClip]:
    if not clip.timeline_range.start_us < timeline_us < clip.timeline_range.end_us:
        raise TimelineEditError("Split must be strictly inside the clip")
    if clip.source_range is None:
        raise TimelineEditError("Cannot split a clip without a source range")
    source_us = source_at_timeline(clip, timeline_us)
    common = {
        "track_id": clip.track_id,
        "media_id": clip.media_id,
        "speed": clip.speed,
        "volume": clip.volume,
        "payload_revision": clip.payload_revision + 1,
        "cue_id": clip.cue_id,
        "cue_index": clip.cue_index,
        "cue_text": clip.cue_text,
        "timing_slot": clip.timing_slot,
        "timing_unresolved": clip.timing_unresolved,
        "content_warning": clip.content_warning,
        "issue_code": clip.issue_code,
    }
    return (
        TimelineClip(
            id=left_id,
            timeline_range=TimeRange(clip.timeline_range.start_us, timeline_us),
            source_range=TimeRange(clip.source_range.start_us, source_us),
            link_group_id=left_link_group_id,
            **common,
        ),
        TimelineClip(
            id=right_id,
            timeline_range=TimeRange(timeline_us, clip.timeline_range.end_us),
            source_range=TimeRange(source_us, clip.source_range.end_us),
            link_group_id=right_link_group_id,
            **common,
        ),
    )


def project_range(snapshot: ProjectSnapshot) -> TimeRange | None:
    clips = [clip for track in snapshot.tracks for clip in track.clips]
    if not clips:
        return None
    return TimeRange(
        min(clip.timeline_range.start_us for clip in clips),
        max(clip.timeline_range.end_us for clip in clips),
    )


@dataclass(frozen=True, slots=True)
class TimelineViewport:
    range: TimeRange
    prefetch_us: TimeUs = 0

    def __post_init__(self) -> None:
        if self.prefetch_us < 0:
            raise ValueError("Viewport prefetch must be non-negative")

    @property
    def query_range(self) -> TimeRange:
        return TimeRange(
            max(0, self.range.start_us - self.prefetch_us),
            self.range.end_us + self.prefetch_us,
        )


class ClipIntervalIndex:
    def __init__(self, tracks: tuple[TimelineTrack, ...]) -> None:
        self._tracks: dict[
            str, tuple[tuple[TimelineClip, ...], tuple[int, ...], tuple[int, ...]]
        ] = {}
        for track in tracks:
            clips = tuple(sorted(track.clips, key=lambda clip: clip.timeline_range.start_us))
            starts = tuple(clip.timeline_range.start_us for clip in clips)
            prefix_max: list[int] = []
            maximum = -1
            for clip in clips:
                maximum = max(maximum, clip.timeline_range.end_us)
                prefix_max.append(maximum)
            self._tracks[track.id] = (clips, starts, tuple(prefix_max))

    def query(self, track_id: str, viewport: TimelineViewport) -> tuple[TimelineClip, ...]:
        clips, starts, prefix_max = self._tracks.get(track_id, ((), (), ()))
        if not clips:
            return ()
        query = viewport.query_range
        lower = bisect_right(prefix_max, query.start_us)
        upper = bisect_left(starts, query.end_us)
        return tuple(
            clip for clip in clips[lower:upper] if clip.timeline_range.intersects(query)
        )
