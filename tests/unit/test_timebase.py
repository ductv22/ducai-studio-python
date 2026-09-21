from decimal import Decimal

import pytest

from ducai_studio.domain.timebase import (
    SpeedRatio,
    TimeRange,
    format_srt_timestamp,
    parse_srt_timestamp,
    seconds_to_us,
    us_to_seconds_text,
)


def test_srt_timestamp_round_trip_for_multi_hour_value() -> None:
    value = parse_srt_timestamp("07:50:12,345")
    assert format_srt_timestamp(value) == "07:50:12,345"


def test_seconds_conversion_is_decimal_and_deterministic() -> None:
    assert seconds_to_us("0.0000005") == 1
    assert seconds_to_us(Decimal("12.345678")) == 12_345_678
    assert us_to_seconds_text(12_345_678) == "12.345678"


@pytest.mark.parametrize("source_us", [0, 1, 99, 1_000_000, 28_200_000_000, 33_333_333_333])
def test_85_percent_speed_round_trips_exactly(source_us: int) -> None:
    speed = SpeedRatio.from_percent(85)
    timeline_us = speed.source_to_timeline_us(source_us)
    assert speed.timeline_to_source_us(timeline_us) == source_us


def test_time_range_uses_half_open_intersection() -> None:
    left = TimeRange(0, 1_000_000)
    touching = TimeRange(1_000_000, 2_000_000)
    overlap = TimeRange(999_999, 2_000_000)
    assert not left.intersects(touching)
    assert left.intersects(overlap)
    assert left.contains(999_999)
    assert not left.contains(1_000_000)


def test_invalid_time_values_fail_early() -> None:
    with pytest.raises(ValueError):
        TimeRange(5, 5)
    with pytest.raises(ValueError):
        seconds_to_us(float("nan"))
    with pytest.raises(ValueError):
        parse_srt_timestamp("00:99:00,000")

