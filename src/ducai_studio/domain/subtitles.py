from __future__ import annotations

from dataclasses import dataclass

from .timebase import TimeRange, TimeUs


@dataclass(frozen=True, slots=True)
class NormalizedBox:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise TypeError("OCR box values must be numeric")
        if not 0 <= self.x < 1 or not 0 <= self.y < 1:
            raise ValueError("OCR box origin must be normalized")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("OCR box dimensions must be positive")
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("OCR box must stay inside the normalized frame")


@dataclass(frozen=True, slots=True)
class SubtitleEvidence:
    id: str
    frame_id: str
    timestamp_us: TimeUs
    band: str
    text: str
    confidence: float
    provider_id: str
    model_id: str
    box: NormalizedBox | None = None

    def __post_init__(self) -> None:
        required = (
            self.id,
            self.frame_id,
            self.band,
            self.text,
            self.provider_id,
            self.model_id,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Subtitle evidence identifiers and text are required")
        if type(self.timestamp_us) is not int or self.timestamp_us < 0:
            raise ValueError("Evidence timestamp must be a non-negative integer")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise TypeError("Evidence confidence must be numeric")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Evidence confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class SubtitleCandidate:
    id: str
    range: TimeRange
    text: str
    confidence: float
    evidence_ids: tuple[str, ...]
    source: str
    review_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.text.strip() or not self.source.strip():
            raise ValueError("Subtitle candidate identity, text and source are required")
        if not self.evidence_ids or len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("Subtitle candidate requires unique evidence ids")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Candidate confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class RescueWindow:
    range: TimeRange
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("A rescue window requires at least one reason")
