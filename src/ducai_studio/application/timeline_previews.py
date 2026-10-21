"""Bounded, stable source-time tiles. No media I/O runs on the editor path."""

from math import ceil, log2


def visible_media_tiles(
    segments: list[dict],
    visible_start_us: int,
    visible_end_us: int,
    pixels_per_us: float,
    *,
    waveform: bool,
    limit: int = 32,
) -> list[dict]:
    """Split affine source/display intervals into reusable power-of-two buckets.

    Source intervals come from the same trim/speed/conform map as playback.
    Partial buckets are cropped, not re-decoded at each pan pixel. A waveform
    request spans one bucket, never the unbounded duration of a source movie.
    """
    if not segments or visible_end_us <= visible_start_us or pixels_per_us <= 0:
        return []
    if not waveform:
        target_seconds = max(
            0.125,
            96 / pixels_per_us / 1_000_000,
            (visible_end_us - visible_start_us) / max(1, limit - 2) / 1_000_000,
        )
        bucket_us = max(125_000, round(2 ** ceil(log2(target_seconds)) * 1_000_000))
        result = []
        bucket = visible_start_us // bucket_us * bucket_us
        while bucket < visible_end_us and len(result) < limit:
            left, right = max(bucket, visible_start_us), min(bucket + bucket_us, visible_end_us)
            # Sample the middle of the available bucket, not end_us - 1.
            # A seek after the last frame's presentation time can return no
            # frame at all, especially for short or low-frame-rate sources.
            sample_start = max(bucket, min(s["start_us"] for s in segments))
            sample_end = min(bucket + bucket_us, max(s["end_us"] for s in segments))
            midpoint = (sample_start + sample_end) // 2
            segment = next((s for s in segments if s["start_us"] <= midpoint < s["end_us"]), None)
            if segment:
                source_time = segment["source_start_us"] + (midpoint - segment["start_us"]) * (
                    segment["source_end_us"] - segment["source_start_us"]
                ) // (segment["end_us"] - segment["start_us"])
                result.append(
                    {
                        "x": (left - visible_start_us) * pixels_per_us,
                        "width": (right - left) * pixels_per_us,
                        "start_us": int(source_time),
                        "duration_us": 1,
                        "crop_start": 0.0,
                        "crop_end": 1.0,
                        "waveform": False,
                    }
                )
            bucket += bucket_us
        return result
    tiles = []
    source_floor = min((s["source_start_us"] for s in segments), default=0)
    source_ceiling = max((s["source_end_us"] for s in segments), default=0)
    visible_segments = [
        s for s in segments if s["end_us"] > visible_start_us and s["start_us"] < visible_end_us
    ]
    if not visible_segments:
        return []
    # Conform may have hundreds of tiny affine spans. Group all paint regions
    # referencing the same source bucket into one request/Canvas, not one
    # decoder or delegate per spoken cue.
    source_extent = max(s["source_end_us"] for s in visible_segments) - min(
        s["source_start_us"] for s in visible_segments
    )
    rate = max(
        (s["source_end_us"] - s["source_start_us"]) / (s["end_us"] - s["start_us"])
        for s in visible_segments
    )
    target_seconds = max(96 / pixels_per_us * rate, source_extent / max(1, limit - 2)) / 1_000_000
    bucket_us = max(125_000, round(2 ** ceil(log2(max(0.125, target_seconds))) * 1_000_000))
    grouped = {}
    for segment in segments:
        start, end = segment["start_us"], segment["end_us"]
        source_start, source_end = segment["source_start_us"], segment["source_end_us"]
        left, right = max(start, visible_start_us), min(end, visible_end_us)
        if right <= left or source_end <= source_start or end <= start:
            continue
        rate = (source_end - source_start) / (end - start)
        source_left = source_start + (left - start) * rate
        source_right = source_start + (right - start) * rate
        bucket = int(source_left // bucket_us) * bucket_us
        while bucket < source_right:
            request_start = max(source_floor, bucket)
            request_end = min(source_ceiling, bucket + bucket_us)
            shown_start = max(source_left, request_start)
            shown_end = min(source_right, request_end)
            if shown_end > shown_start and request_end > request_start:
                tile_start = start + (shown_start - source_start) / rate
                tile_end = start + (shown_end - source_start) / rate
                group = grouped.setdefault(
                    bucket,
                    {
                        "start_us": request_start,
                        "duration_us": request_end - request_start,
                        "waveform": True,
                        "regions": [],
                    },
                )
                group["regions"].append(
                    {
                        "x": (tile_start - visible_start_us) * pixels_per_us,
                        "width": (tile_end - tile_start) * pixels_per_us,
                        "crop_start": (shown_start - request_start) / (request_end - request_start),
                        "crop_end": (shown_end - request_start) / (request_end - request_start),
                    }
                )
            bucket += bucket_us
    tiles.extend(grouped.values())
    return tiles
