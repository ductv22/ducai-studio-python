from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ducai_studio.domain.subtitles import SubtitleEvidence
from ducai_studio.providers.contracts import Provider


class OcrMode(StrEnum):
    FAST = "fast"
    QUALITY = "quality"
    COMPATIBILITY = "compatibility"


@dataclass(frozen=True, slots=True)
class OcrBandProfile:
    name: str
    x1: float
    y1: float
    x2: float
    y2: float
    scale: float
    reading_mode: str = "dialogue"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("OCR band name is required")
        if not 0 <= self.x1 < self.x2 <= 1 or not 0 <= self.y1 < self.y2 <= 1:
            raise ValueError("OCR band crop must use ordered normalized coordinates")
        if not 0.5 <= self.scale <= 2:
            raise ValueError("OCR band scale must be between 0.5 and 2")


DEFAULT_SUBTITLE_BANDS = (
    OcrBandProfile("upper", 0.06, 0.765, 0.94, 0.982, 1.0),
    OcrBandProfile("lower", 0.08, 0.885, 0.92, 0.982, 0.75),
)

# Full-frame dialogue routing/placement box; not a limit on subtitle artwork.
DEFAULT_DIALOGUE_REGION = (0.06, 0.765, 0.88, 0.217)


@dataclass(frozen=True, slots=True)
class OcrRequest:
    media_path: Path
    duration_us: int
    sample_interval_us: int = 800_000
    mode: OcrMode = OcrMode.FAST
    bands: tuple[OcrBandProfile, ...] = DEFAULT_SUBTITLE_BANDS

    def __post_init__(self) -> None:
        path = self.media_path.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("OCR duration must be a positive integer")
        if (
            type(self.sample_interval_us) is not int
            or not 400_000 <= self.sample_interval_us <= 1_500_000
        ):
            raise ValueError("OCR sample interval must be between 0.4 and 1.5 seconds")
        if not self.bands or len({band.name for band in self.bands}) != len(self.bands):
            raise ValueError("OCR request requires uniquely named bands")
        object.__setattr__(self, "media_path", path)


@runtime_checkable
class OcrProvider(Provider, Protocol):
    mode: OcrMode
    model_id: str

    def recognize_batch(
        self,
        frames: Sequence[object],
        *,
        frame_ids: Sequence[str],
        timestamps_us: Sequence[int],
        band: OcrBandProfile,
    ) -> tuple[SubtitleEvidence, ...]: ...
