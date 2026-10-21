"""Resolve full-frame video layers without changing the project's time map."""

from __future__ import annotations

import heapq
from dataclasses import asdict
from fractions import Fraction

from .project import ProjectSnapshot


def resolved_video_rows(
    snapshot: ProjectSnapshot | dict, *, last_track_wins: bool = True, include_hidden: bool = False
) -> list[dict]:
    raw = (
        {"tracks": [asdict(track) for track in snapshot.tracks if track.kind.value == "video"]}
        if isinstance(snapshot, ProjectSnapshot)
        else snapshot
    )
    events: dict[int, list[tuple[bool, int]]] = {}
    rows: list[dict] = []
    for rank, track in enumerate(raw.get("tracks", [])):
        kind = str(getattr(track.get("kind"), "value", track.get("kind")))
        if kind != "video" or (not include_hidden and not track.get("visible", True)):
            continue
        for clip in track.get("clips", []):
            if not clip.get("source_range"):
                continue
            row = {
                **clip,
                "track_id": track.get("id", clip.get("track_id", "V1")),
                "track_muted": track.get("muted", False),
                "track_rank": rank,
            }
            index = len(rows)
            rows.append(row)
            interval = clip["timeline_range"]
            events.setdefault(interval["start_us"], []).append((True, index))
            events.setdefault(interval["end_us"], []).append((False, index))
    boundaries = sorted(events)
    active: set[int] = set()
    heap: list[tuple[int, int, int]] = []
    result: list[dict] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        for added, index in events[start]:
            if added:
                active.add(index)
                row = rows[index]
                rank = row["track_rank"]
                heapq.heappush(
                    heap,
                    (-rank if last_track_wins else rank, -row["timeline_range"]["start_us"], index),
                )
            else:
                active.discard(index)
        while heap and heap[0][2] not in active:
            heapq.heappop(heap)
        if not heap:
            continue
        row = rows[heap[0][2]]
        interval, source = row["timeline_range"], row["source_range"]
        duration = interval["end_us"] - interval["start_us"]
        source_duration = source["end_us"] - source["start_us"]
        source_start = (
            source["start_us"] + (start - interval["start_us"]) * source_duration // duration
        )
        source_end = source["start_us"] + (end - interval["start_us"]) * source_duration // duration
        if source_end <= source_start:
            raise ValueError("Đoạn video quá ngắn để giữ frame hợp lệ")
        piece = {
            **row,
            "timeline_range": {"start_us": start, "end_us": end},
            "source_range": {"start_us": source_start, "end_us": source_end},
        }
        if (
            result
            and result[-1]["id"] == piece["id"]
            and result[-1]["track_id"] == piece["track_id"]
            and result[-1]["timeline_range"]["end_us"] == start
            and result[-1]["source_range"]["end_us"] == source_start
        ):
            result[-1]["timeline_range"]["end_us"] = end
            result[-1]["source_range"]["end_us"] = source_end
        else:
            result.append(piece)
    for row in result:
        speed = Fraction(
            row["source_range"]["end_us"] - row["source_range"]["start_us"],
            row["timeline_range"]["end_us"] - row["timeline_range"]["start_us"],
        )
        original = row.get("speed")
        if original:
            duration = row["timeline_range"]["end_us"] - row["timeline_range"]["start_us"]
            source_duration = row["source_range"]["end_us"] - row["source_range"]["start_us"]
            expected = round(source_duration * original["denominator"] / original["numerator"])
            if abs(expected - duration) <= 1:
                continue
        row["speed"] = {"numerator": speed.numerator, "denominator": speed.denominator}
    return result
