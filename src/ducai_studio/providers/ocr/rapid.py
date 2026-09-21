from __future__ import annotations

import importlib.util
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from ducai_studio.domain.subtitles import SubtitleEvidence
from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)

from .contracts import OcrBandProfile, OcrMode
from .paddle import _evidence_id, _normalized_box


class RapidOcrProvider:
    provider_id = "rapid-compatibility"
    kind = ProviderKind.OCR
    mode = OcrMode.COMPATIBILITY
    model_id = "PP-OCRv6-tiny-small"

    def __init__(self, *, engine_factory: Callable[[], object] | None = None) -> None:
        self._engine_factory = engine_factory
        self._engine: object | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {"batch": False, "device": "cpu", "mode": self.mode.value, "languages": ["zh"]}
        )

    def health(self) -> ProviderHealth:
        available = (
            self._engine_factory is not None
            or importlib.util.find_spec("rapidocr") is not None
        )
        return ProviderHealth(
            self.provider_id,
            HealthState.READY if available else HealthState.UNAVAILABLE,
            datetime.now(UTC).isoformat(),
            "RapidOCR sẵn sàng" if available else "Runtime RapidOCR chưa được cấu hình",
        )

    def _load_engine(self) -> object:
        if self._engine is not None:
            return self._engine
        factory = self._engine_factory
        if factory is None:
            from rapidocr import RapidOCR  # type: ignore[import-not-found]

            factory = RapidOCR
        self._engine = factory()
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
            raise ValueError("RapidOCR batch metadata length mismatch")
        engine = self._load_engine()
        evidence: list[SubtitleEvidence] = []
        for frame, frame_id, timestamp_us in zip(
            frames, frame_ids, timestamps_us, strict=True
        ):
            output = engine(frame)
            raw_boxes = getattr(output, "boxes", None)
            raw_texts = getattr(output, "txts", None)
            raw_scores = getattr(output, "scores", None)
            boxes = list(raw_boxes) if raw_boxes is not None else []
            texts = list(raw_texts) if raw_texts is not None else []
            scores = list(raw_scores) if raw_scores is not None else []
            height, width = frame.shape[:2]
            for index, raw_text in enumerate(texts):
                text = str(raw_text or "").strip()
                if not text:
                    continue
                confidence = float(scores[index]) if index < len(scores) else 0.0
                box = _normalized_box(boxes[index], width, height) if index < len(boxes) else None
                evidence.append(
                    SubtitleEvidence(
                        _evidence_id(
                            self.provider_id,
                            self.model_id,
                            frame_id,
                            band.name,
                            index,
                            text,
                        ),
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
