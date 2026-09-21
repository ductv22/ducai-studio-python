from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from ducai_studio.domain.subtitles import NormalizedBox, SubtitleEvidence
from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)

from .contracts import OcrBandProfile, OcrMode

_MODELS = {
    OcrMode.FAST: ("PP-OCRv6_tiny_det", "PP-OCRv6_small_rec"),
    OcrMode.QUALITY: ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
}


class PaddleOcrProvider:
    kind = ProviderKind.OCR

    def __init__(
        self,
        mode: OcrMode = OcrMode.FAST,
        *,
        device: str = "gpu:0",
        batch_size: int = 12,
        engine_factory: Callable[..., object] | None = None,
    ) -> None:
        if mode not in _MODELS:
            raise ValueError("Paddle provider supports only Fast and Quality modes")
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("Paddle OCR batch size must be positive")
        self.mode = mode
        self.provider_id = f"paddle-{mode.value}"
        detection_model, recognition_model = _MODELS[mode]
        self.detection_model = detection_model
        self.model_id = recognition_model
        self.device = device
        self.batch_size = batch_size
        self._engine_factory = engine_factory
        self._engine: object | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {
                "batch": True,
                "device": self.device,
                "mode": self.mode.value,
                "detection_model": self.detection_model,
                "recognition_model": self.model_id,
                "languages": ["zh"],
            }
        )

    def health(self) -> ProviderHealth:
        available = (
            self._engine_factory is not None
            or importlib.util.find_spec("paddleocr") is not None
        )
        return ProviderHealth(
            self.provider_id,
            HealthState.READY if available else HealthState.UNAVAILABLE,
            datetime.now(UTC).isoformat(),
            "PaddleOCR sẵn sàng" if available else "Runtime PaddleOCR chưa được cấu hình",
            {"model_id": self.model_id, "device": self.device},
        )

    def _load_engine(self) -> object:
        if self._engine is not None:
            return self._engine
        factory = self._engine_factory
        if factory is None:
            from paddleocr import PaddleOCR  # type: ignore[import-not-found]

            factory = PaddleOCR
        self._engine = factory(
            lang="ch",
            device=self.device,
            text_detection_model_name=self.detection_model,
            text_recognition_model_name=self.model_id,
            text_recognition_batch_size=self.batch_size,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        return self._engine

    def recognize_batch(
        self,
        frames: Sequence[object],
        *,
        frame_ids: Sequence[str],
        timestamps_us: Sequence[int],
        band: OcrBandProfile,
    ) -> tuple[SubtitleEvidence, ...]:
        if not len(frames) == len(frame_ids) == len(timestamps_us):
            raise ValueError("Paddle OCR batch metadata length mismatch")
        if not frames:
            return ()
        engine = self._load_engine()
        results = list(engine.predict(list(frames)))
        if len(results) != len(frames):
            raise RuntimeError("Paddle OCR returned a different result count than the batch")
        evidence: list[SubtitleEvidence] = []
        for frame, frame_id, timestamp_us, result in zip(
            frames, frame_ids, timestamps_us, results, strict=True
        ):
            payload = result.json
            if callable(payload):
                payload = payload()
            data = payload.get("res", payload)
            texts = _values(data.get("rec_texts"))
            scores = _values(data.get("rec_scores"))
            boxes = _values(data.get("rec_boxes"))
            height, width = frame.shape[:2]
            for index, raw_text in enumerate(texts):
                text = str(raw_text or "").strip()
                if not text:
                    continue
                confidence = float(scores[index]) if index < len(scores) else 0.0
                box = _normalized_box(boxes[index], width, height) if index < len(boxes) else None
                identity = _evidence_id(
                    self.provider_id, self.model_id, frame_id, band.name, index, text
                )
                evidence.append(
                    SubtitleEvidence(
                        identity,
                        frame_id,
                        int(timestamp_us),
                        band.name,
                        text,
                        confidence,
                        self.provider_id,
                        self.model_id,
                        box,
                    )
                )
        return tuple(evidence)


def _normalized_box(raw: Any, width: int, height: int) -> NormalizedBox | None:
    try:
        values = list(raw)
        if len(values) == 4 and all(isinstance(value, (int, float)) for value in values):
            x1, y1, x2, y2 = (float(value) for value in values)
        else:
            points = [tuple(float(coordinate) for coordinate in point) for point in values]
            x1 = min(point[0] for point in points)
            y1 = min(point[1] for point in points)
            x2 = max(point[0] for point in points)
            y2 = max(point[1] for point in points)
        left = max(0.0, x1 / width)
        top = max(0.0, y1 / height)
        right = min(1.0, x2 / width)
        bottom = min(1.0, y2 / height)
        if right <= left or bottom <= top:
            return None
        return NormalizedBox(
            left,
            top,
            right - left,
            bottom - top,
        )
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return None


def _values(value: Any) -> list[Any]:
    return list(value) if value is not None else []


def _evidence_id(*parts: object) -> str:
    raw = "|".join(str(value) for value in parts)
    return "ocr-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
