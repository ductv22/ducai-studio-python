from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

type TimeUs = int
MICROSECONDS_PER_SECOND = 1_000_000
_SRT_TIMESTAMP = re.compile(
    r"^(?P<hours>\d+):(?P<minutes>\d{2}):"
    r"(?P<seconds>\d{2})[,.](?P<millis>\d{1,3})$"
)


def _round_fraction(value: Fraction) -> int:
    """Round a non-negative fraction to the nearest integer, ties upward."""
    if value < 0:
        raise ValueError("Timeline values cannot be negative")
    quotient, remainder = divmod(value.numerator, value.denominator)
    return quotient + int(remainder * 2 >= value.denominator)


def seconds_to_us(value: str | int | float | Decimal) -> TimeUs:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid seconds value: {value!r}") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError("Seconds must be finite and non-negative")
    return _round_fraction(Fraction(decimal_value) * MICROSECONDS_PER_SECOND)


def us_to_seconds_text(value: TimeUs, *, decimal_places: int = 6) -> str:
    if value < 0:
        raise ValueError("Timeline values cannot be negative")
    whole, micros = divmod(int(value), MICROSECONDS_PER_SECOND)
    if decimal_places <= 0:
        return str(whole + int(micros >= 500_000))
    digits = f"{micros:06d}"[: min(6, decimal_places)]
    return f"{whole}.{digits}"


def parse_srt_timestamp(value: str) -> TimeUs:
    match = _SRT_TIMESTAMP.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    millis = int(match.group("millis").ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * MICROSECONDS_PER_SECOND + millis * 1_000


def format_srt_timestamp(value: TimeUs) -> str:
    if value < 0:
        raise ValueError("Timeline values cannot be negative")
    total_millis = _round_fraction(Fraction(value, 1_000))
    total_seconds, millis = divmod(total_millis, 1_000)
    total_minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


@dataclass(frozen=True, slots=True)
class TimeRange:
    start_us: TimeUs
    end_us: TimeUs

    def __post_init__(self) -> None:
        if self.start_us < 0:
            raise ValueError("start_us must be non-negative")
        if self.end_us <= self.start_us:
            raise ValueError("end_us must be greater than start_us")

    @property
    def duration_us(self) -> TimeUs:
        return self.end_us - self.start_us

    def intersects(self, other: TimeRange) -> bool:
        return self.start_us < other.end_us and other.start_us < self.end_us

    def contains(self, position_us: TimeUs) -> bool:
        return self.start_us <= position_us < self.end_us


@dataclass(frozen=True, slots=True)
class SpeedRatio:
    """Exact playback speed stored as a reduced positive rational."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("Speed ratio must be positive")
        reduced = Fraction(self.numerator, self.denominator)
        object.__setattr__(self, "numerator", reduced.numerator)
        object.__setattr__(self, "denominator", reduced.denominator)

    @classmethod
    def from_percent(cls, percent: str | int | float | Decimal) -> SpeedRatio:
        try:
            decimal_percent = Decimal(str(percent))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid speed percent: {percent!r}") from exc
        if not decimal_percent.is_finite() or decimal_percent <= 0:
            raise ValueError("Speed percent must be finite and positive")
        ratio = Fraction(decimal_percent) / 100
        return cls(ratio.numerator, ratio.denominator)

    @property
    def percent(self) -> Decimal:
        return Decimal(self.numerator * 100) / Decimal(self.denominator)

    def source_to_timeline_us(self, source_us: TimeUs) -> TimeUs:
        if source_us < 0:
            raise ValueError("source_us must be non-negative")
        return _round_fraction(Fraction(source_us * self.denominator, self.numerator))

    def timeline_to_source_us(self, timeline_us: TimeUs) -> TimeUs:
        if timeline_us < 0:
            raise ValueError("timeline_us must be non-negative")
        return _round_fraction(Fraction(timeline_us * self.numerator, self.denominator))
