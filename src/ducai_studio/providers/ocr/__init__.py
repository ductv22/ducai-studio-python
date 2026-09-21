"""OCR provider contracts and adapters."""

from .contracts import (
    DEFAULT_SUBTITLE_BANDS,
    OcrBandProfile,
    OcrMode,
    OcrProvider,
    OcrRequest,
)
from .paddle import PaddleOcrProvider
from .rapid import RapidOcrProvider

__all__ = [
    "DEFAULT_SUBTITLE_BANDS",
    "OcrBandProfile",
    "OcrMode",
    "OcrProvider",
    "OcrRequest",
    "PaddleOcrProvider",
    "RapidOcrProvider",
]
