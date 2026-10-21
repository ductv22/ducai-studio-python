"""Non-destructive cuts in canonical project time, composed after voice conform.

Source/cue data remains canonical. Only the presentation/export view is packed;
an absent mapping is never clamped across removed footage.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, replace
from typing import Any

from .project import ProjectSnapshot, TimelineEditState
from .timebase import TimeRange


def _raw(snapshot: ProjectSnapshot | dict) -> dict:
    if not isinstance(snapshot, ProjectSnapshot):
        return snapshot
    # Time-map lookups must not copy thousands of cue/style records.
    return {
        "tracks": [
            {
                "kind": track.kind,
                "visible": track.visible,
                "clips": [
                    {
                        "id": clip.id,
                        "media_id": clip.media_id,
                        "timeline_range": asdict(clip.timeline_range),
                        "source_range": asdict(clip.source_range) if clip.source_range else None,
                    }
                    for clip in track.clips
                ],
            }
            for track in snapshot.tracks
            if track.kind.value == "video"
        ],
        "media": [{"id": item.id, "path": item.path} for item in snapshot.media],
        "timeline_edit": asdict(snapshot.timeline_edit) if snapshot.timeline_edit else None,
        "voice_conform": {
            "plan": {
                "segments": [asdict(segment) for segment in snapshot.voice_conform.plan.segments]
            }
        }
        if snapshot.voice_conform
        else None,
    }


def _kind(value: Any) -> str:
    return str(getattr(value, "value", value))


def _lerp(value: int, start: int, end: int, target_start: int, target_end: int) -> int:
    return target_start + (value - start) * (target_end - target_start) // (end - start)


def timeline_spans(snapshot: ProjectSnapshot | dict) -> list[dict]:
    raw = _raw(snapshot)
    from .video_layers import resolved_video_rows

    # A later overlay track must not duplicate time in the retained-range map.
    # The underlying sequence is the clock; visible layer priority is resolved
    # independently by preview, scan and export.
    clips = resolved_video_rows(raw, last_track_wins=False, include_hidden=True)
    edit = raw.get("timeline_edit")
    retained = edit.get("retained_ranges", []) if edit else None
    conform = raw.get("voice_conform")
    conform_segments = conform.get("plan", {}).get("segments", []) if conform else []
    media_paths = {item["id"]: item["path"] for item in raw.get("media", [])}
    result: list[dict] = []
    cursor = 0
    for clip in clips:
        base = clip["timeline_range"]
        source = clip["source_range"]
        windows = retained if retained is not None else [base]
        for window in windows:
            start = max(base["start_us"], window["start_us"])
            end = min(base["end_us"], window["end_us"])
            if end <= start:
                continue
            pieces = conform_segments or [
                {
                    "source_start_us": base["start_us"],
                    "source_end_us": base["end_us"],
                    "output_start_us": base["start_us"],
                    "output_end_us": base["end_us"],
                }
            ]
            for piece in pieces:
                a = max(start, piece["source_start_us"])
                b = min(end, piece["source_end_us"])
                if b <= a:
                    continue
                old_a = _lerp(
                    a,
                    piece["source_start_us"],
                    piece["source_end_us"],
                    piece["output_start_us"],
                    piece["output_end_us"],
                )
                old_b = _lerp(
                    b,
                    piece["source_start_us"],
                    piece["source_end_us"],
                    piece["output_start_us"],
                    piece["output_end_us"],
                )
                source_a = _lerp(
                    a, base["start_us"], base["end_us"], source["start_us"], source["end_us"]
                )
                source_b = _lerp(
                    b, base["start_us"], base["end_us"], source["start_us"], source["end_us"]
                )
                output_a = cursor if edit else old_a
                output_b = output_a + old_b - old_a
                if output_b <= output_a or source_b <= source_a:
                    raise ValueError("Đoạn cắt quá ngắn để giữ khung hình hợp lệ")
                result.append(
                    {
                        "clip_id": clip["id"],
                        "media_id": clip.get("media_id"),
                        "media_path": media_paths.get(clip.get("media_id"), ""),
                        "source_start_us": source_a,
                        "source_end_us": source_b,
                        "base_start_us": a,
                        "base_end_us": b,
                        "uncut_start_us": old_a,
                        "uncut_end_us": old_b,
                        "timeline_start_us": output_a,
                        "timeline_end_us": output_b,
                    }
                )
                cursor = output_b
    return result


def timeline_to_base_us(
    snapshot: ProjectSnapshot | dict, time_us: int, endpoint: str = "start"
) -> int:
    raw = _raw(snapshot)
    if not raw.get("timeline_edit") and not raw.get("voice_conform"):
        return int(time_us)
    spans = timeline_spans(raw)
    index = (
        bisect_right(spans, time_us, key=lambda s: s["timeline_start_us"]) - 1
        if endpoint == "start"
        else bisect_left(spans, time_us, key=lambda s: s["timeline_end_us"])
    )
    for span in spans[max(0, index) : max(0, index) + 1]:
        a, b = span["timeline_start_us"], span["timeline_end_us"]
        if a <= time_us <= b:
            return _lerp(int(time_us), a, b, span["base_start_us"], span["base_end_us"])
    raise ValueError("Mốc thời gian không nằm trong phần video còn lại")


def base_to_timeline_us(
    snapshot: ProjectSnapshot | dict, time_us: int, endpoint: str = "start"
) -> int | None:
    raw = _raw(snapshot)
    if not raw.get("timeline_edit") and not raw.get("voice_conform"):
        return int(time_us)
    spans = timeline_spans(raw)
    index = (
        bisect_right(spans, time_us, key=lambda s: s["base_start_us"]) - 1
        if endpoint == "start"
        else bisect_left(spans, time_us, key=lambda s: s["base_end_us"])
    )
    for span in spans[max(0, index) : max(0, index) + 1]:
        a, b = span["base_start_us"], span["base_end_us"]
        if a <= time_us <= b:
            return _lerp(int(time_us), a, b, span["timeline_start_us"], span["timeline_end_us"])
    return None


def _project_interval(
    interval: dict, spans: list[dict], *, output_time: bool = False
) -> list[tuple[int, int, int, int]]:
    result = []
    prefix = "uncut" if output_time else "base"
    first = bisect_right(spans, interval["start_us"], key=lambda s: s[f"{prefix}_end_us"])
    last = bisect_left(spans, interval["end_us"], key=lambda s: s[f"{prefix}_start_us"])
    for span in spans[first:last]:
        a = max(interval["start_us"], span[f"{prefix}_start_us"])
        b = min(interval["end_us"], span[f"{prefix}_end_us"])
        if b > a:
            start = _lerp(
                a,
                span[f"{prefix}_start_us"],
                span[f"{prefix}_end_us"],
                span["timeline_start_us"],
                span["timeline_end_us"],
            )
            end = _lerp(
                b,
                span[f"{prefix}_start_us"],
                span[f"{prefix}_end_us"],
                span["timeline_start_us"],
                span["timeline_end_us"],
            )
            result.append((a, b, start, end))
    return result


def project_json_for_timeline(raw_snapshot: dict) -> dict:
    if not raw_snapshot.get("timeline_edit"):
        return raw_snapshot
    spans = timeline_spans(raw_snapshot)
    result = dict(raw_snapshot)
    tracks = []
    reserved_ids = {
        clip["id"] for track in raw_snapshot.get("tracks", []) for clip in track.get("clips", [])
    }
    for track in raw_snapshot.get("tracks", []):
        kind = _kind(track.get("kind"))
        clips = []
        for clip in track.get("clips", []):
            interval = clip["timeline_range"]
            output_time = kind in {"voice", "background_music"}
            fragments = _project_interval(interval, spans, output_time=output_time)
            for index, (a, b, start, end) in enumerate(fragments):
                changed = dict(clip)
                fragment_id = clip["id"]
                if index:
                    suffix = index
                    fragment_id = f"{clip['id']}::cut::{suffix}"
                    while fragment_id in reserved_ids:
                        suffix += 1
                        fragment_id = f"{clip['id']}::cut::{suffix}"
                    reserved_ids.add(fragment_id)
                changed["id"] = fragment_id
                changed["track_id"] = track["id"]
                changed["timeline_range"] = {"start_us": start, "end_us": end}
                if clip.get("link_group_id"):
                    # Only matching A/V fragments stay linked after a ripple cut.
                    changed["link_group_id"] = f"{clip['link_group_id']}::cut::{start}:{end}"
                source = clip.get("source_range")
                if source:
                    source_a = _lerp(
                        a,
                        interval["start_us"],
                        interval["end_us"],
                        source["start_us"],
                        source["end_us"],
                    )
                    source_b = _lerp(
                        b,
                        interval["start_us"],
                        interval["end_us"],
                        source["start_us"],
                        source["end_us"],
                    )
                    changed["source_range"] = {"start_us": source_a, "end_us": source_b}
                    changed["speed"] = {
                        "numerator": source_b - source_a,
                        "denominator": end - start,
                    }
                slot = clip.get("timing_slot")
                slot_pieces = (
                    _project_interval(slot, spans, output_time=output_time) if slot else []
                )
                if slot_pieces:
                    changed["timing_slot"] = {
                        "start_us": slot_pieces[0][2],
                        "end_us": slot_pieces[-1][3],
                    }
                clips.append(changed)
        tracks.append({**track, "clips": clips})
    result["tracks"] = tracks
    for key in ("source_cues", "translated_cues", "title_cues", "overlays"):
        values = []
        for item in raw_snapshot.get(key, []):
            pieces = _project_interval(item["range"], spans)
            if pieces:
                values.append(
                    {**item, "range": {"start_us": pieces[0][2], "end_us": pieces[-1][3]}}
                )
        result[key] = values
    result["voice_conform"] = None
    result["timeline_edit"] = None
    cue_indices = {cue["id"]: cue["index"] for cue in result.get("source_cues", [])}
    for track in result["tracks"]:
        for clip in track["clips"]:
            if clip.get("cue_id") in cue_indices:
                clip["cue_index"] = cue_indices[clip["cue_id"]]
    return result


def project_for_timeline(snapshot: ProjectSnapshot) -> ProjectSnapshot:
    if snapshot.timeline_edit is None:
        return snapshot
    # Decoder is deliberately not imported here: domain stays infrastructure-free.
    raw = project_json_for_timeline(asdict(snapshot))
    from .project import TimelineClip
    from .timebase import SpeedRatio

    tracks = []
    for original, projected in zip(snapshot.tracks, raw["tracks"], strict=True):
        clips = []
        for item in projected["clips"]:
            item = dict(item)
            for field in ("timeline_range", "source_range", "timing_slot"):
                if item.get(field) is not None:
                    item[field] = TimeRange(**item[field])
            item["speed"] = SpeedRatio(**item["speed"])
            clips.append(TimelineClip(**item))
        tracks.append(replace(original, clips=tuple(clips)))
    updates: dict[str, Any] = {
        "tracks": tuple(tracks),
        "voice_conform": None,
        "timeline_edit": None,
    }
    for key in ("source_cues", "translated_cues", "title_cues", "overlays"):
        by_id = {item.id: item for item in getattr(snapshot, key)}
        updates[key] = tuple(
            replace(by_id[item["id"]], range=TimeRange(**item["range"])) for item in raw[key]
        )
    return replace(snapshot, **updates)


def cut_state(
    snapshot: ProjectSnapshot, operation: str, start_us: int, end_us: int
) -> TimelineEditState:
    spans = timeline_spans(snapshot)
    if not spans:
        raise ValueError("Dự án không có video để cắt")
    if any(
        left["base_end_us"] > right["base_start_us"]
        for left, right in zip(spans, spans[1:], strict=False)
    ):
        raise ValueError("Hãy xử lý các clip video chồng nhau trước khi cắt đồng bộ")
    duration = max(span["timeline_end_us"] for span in spans)
    if operation == "keep_from":
        end_us = duration
    elif operation == "keep_until":
        start_us = 0
    if (
        type(start_us) is not int
        or type(end_us) is not int
        or not 0 <= start_us < end_us <= duration
    ):
        raise ValueError("Mốc A/B không hợp lệ: cần 0 ≤ A < B ≤ thời lượng video")
    if operation not in {"keep_from", "keep_until", "keep_range", "remove_range"}:
        raise ValueError("Thao tác cắt video không hợp lệ")
    windows = (
        [(start_us, end_us)] if operation != "remove_range" else [(0, start_us), (end_us, duration)]
    )
    retained = []
    for a, b in windows:
        if b <= a:
            continue
        for span in spans:
            left, right = max(a, span["timeline_start_us"]), min(b, span["timeline_end_us"])
            if right <= left:
                continue
            base_a = _lerp(
                left,
                span["timeline_start_us"],
                span["timeline_end_us"],
                span["base_start_us"],
                span["base_end_us"],
            )
            base_b = _lerp(
                right,
                span["timeline_start_us"],
                span["timeline_end_us"],
                span["base_start_us"],
                span["base_end_us"],
            )
            if retained and retained[-1].end_us == base_a:
                retained[-1] = TimeRange(retained[-1].start_us, base_b)
            else:
                retained.append(TimeRange(base_a, base_b))
    if not retained:
        raise ValueError("Không thể bỏ toàn bộ video")
    return TimelineEditState(tuple(retained))


def build_scan_plan(snapshot: ProjectSnapshot | dict) -> dict:
    segments = timeline_spans(snapshot)
    raw = _raw(snapshot)
    from .video_layers import resolved_video_rows

    # Keep the monotonic underlying clock but scan the visible winning media,
    # not a hidden V1 beneath pasted video. Never map across a removed interval.
    winners = resolved_video_rows(raw)
    paths = {item["id"]: item["path"] for item in raw.get("media", [])}
    visible_segments = []
    for span in segments:
        for row in winners:
            interval, source = row["timeline_range"], row["source_range"]
            start = max(span["base_start_us"], interval["start_us"])
            end = min(span["base_end_us"], interval["end_us"])
            if end <= start:
                continue
            visible_segments.append(
                {
                    **span,
                    "clip_id": row["id"],
                    "media_id": row.get("media_id"),
                    "media_path": paths.get(row.get("media_id"), ""),
                    "base_start_us": start,
                    "base_end_us": end,
                    "source_start_us": _lerp(
                        start,
                        interval["start_us"],
                        interval["end_us"],
                        source["start_us"],
                        source["end_us"],
                    ),
                    "source_end_us": _lerp(
                        end,
                        interval["start_us"],
                        interval["end_us"],
                        source["start_us"],
                        source["end_us"],
                    ),
                    **{
                        prefix + "_" + edge + "_us": _lerp(
                            value,
                            span["base_start_us"],
                            span["base_end_us"],
                            span[prefix + "_start_us"],
                            span[prefix + "_end_us"],
                        )
                        for prefix in ("uncut", "timeline")
                        for edge, value in (("start", start), ("end", end))
                    },
                }
            )
    segments = visible_segments
    return {
        "schema_version": 1,
        "duration_us": max((segment["timeline_end_us"] for segment in segments), default=0),
        "segments": segments,
    }


def retained_cues(snapshot: ProjectSnapshot, cues: tuple) -> tuple:
    if snapshot.timeline_edit is None:
        return cues
    spans = timeline_spans(snapshot)
    return tuple(cue for cue in cues if _project_interval(asdict(cue.range), spans))


def range_on_timeline(snapshot: ProjectSnapshot | dict, interval: TimeRange) -> TimeRange | None:
    return range_on_scan_timeline(build_scan_plan(snapshot), interval)


def range_on_scan_timeline(plan: dict, interval: TimeRange) -> TimeRange | None:
    pieces = _project_interval(asdict(interval), plan["segments"])
    return TimeRange(pieces[0][2], pieces[-1][3]) if pieces else None


def scan_time_to_base_us(plan: dict, value: int, endpoint: str = "start") -> int:
    segments = plan["segments"]
    # SRT/JSON subtitle timestamps have millisecond precision while media
    # durations are measured in microseconds.  A final cue ending at the
    # rounded SRT boundary can therefore be a few hundred microseconds beyond
    # the retained video even though it is the same real endpoint.  Accept only
    # that bounded serialization error; a larger excursion remains an error so
    # a genuinely stale/out-of-scope OCR result cannot be imported.
    if segments:
        first_start = segments[0]["timeline_start_us"]
        last_end = segments[-1]["timeline_end_us"]
        rounding_tolerance_us = 1_000
        if first_start - rounding_tolerance_us <= value < first_start:
            value = first_start
        elif endpoint == "end" and last_end < value <= last_end + rounding_tolerance_us:
            # TimeRange uses a half-open interval for media/cue playback.  An
            # SRT end timestamp rounded up to the next millisecond can land a
            # few hundred microseconds past the last frame, so keep the
            # canonicalized end strictly inside the retained span.  Mapping
            # to ``last_end`` would be numerically valid for interpolation but
            # would make an end boundary look like an in-range frame to later
            # endpoint lookups.
            value = max(first_start, last_end - 1)
    index = (
        bisect_right(segments, value, key=lambda s: s["timeline_start_us"]) - 1
        if endpoint == "start"
        else bisect_left(segments, value, key=lambda s: s["timeline_end_us"])
    )
    for span in segments[max(0, index) : max(0, index) + 1]:
        if span["timeline_start_us"] <= value <= span["timeline_end_us"]:
            return _lerp(
                value,
                span["timeline_start_us"],
                span["timeline_end_us"],
                span["base_start_us"],
                span["base_end_us"],
            )
    raise ValueError("OCR trả mốc thời gian ngoài phần video giữ lại")
