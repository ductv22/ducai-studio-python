from __future__ import annotations

from dataclasses import dataclass

import pytest

from ducai_studio.providers.contracts import HealthState, PreviewProvider, ProviderKind
from ducai_studio.providers.ocr import (
    DEFAULT_SUBTITLE_BANDS,
    OcrMode,
    OcrProvider,
    PaddleOcrProvider,
    RapidOcrProvider,
)


class FakeFrame:
    shape = (100, 500, 3)


@dataclass
class FakePaddleResult:
    json: dict


class FakePaddleEngine:
    def predict(self, frames):
        return [
            FakePaddleResult(
                {
                    "res": {
                        "rec_texts": ["你好世界"],
                        "rec_scores": [0.97],
                        "rec_boxes": [[100, 20, 400, 80]],
                    }
                }
            )
            for _frame in frames
        ]


class FakeRapidOutput:
    boxes = [[100, 20, 400, 80]]
    txts = ["下排台词"]
    scores = [0.93]


class FakeRapidEngine:
    def __call__(self, _frame):
        return FakeRapidOutput()


def test_paddle_fast_adapter_returns_typed_deterministic_evidence() -> None:
    provider = PaddleOcrProvider(
        OcrMode.FAST,
        engine_factory=lambda **_kwargs: FakePaddleEngine(),
    )

    first = provider.recognize_batch(
        (FakeFrame(),),
        frame_ids=("frame-1",),
        timestamps_us=(800_000,),
        band=DEFAULT_SUBTITLE_BANDS[0],
    )
    second = provider.recognize_batch(
        (FakeFrame(),),
        frame_ids=("frame-1",),
        timestamps_us=(800_000,),
        band=DEFAULT_SUBTITLE_BANDS[0],
    )

    assert isinstance(provider, OcrProvider)
    assert provider.kind is ProviderKind.OCR
    assert provider.health().state is HealthState.READY
    assert first == second
    assert first[0].text == "你好世界"
    assert first[0].box is not None
    assert first[0].box.x == 0.2
    assert first[0].box.width == pytest.approx(0.6)


def test_rapid_adapter_is_explicit_compatibility_fallback() -> None:
    provider = RapidOcrProvider(engine_factory=FakeRapidEngine)

    evidence = provider.recognize_batch(
        (FakeFrame(),),
        frame_ids=("frame-2",),
        timestamps_us=(1_600_000,),
        band=DEFAULT_SUBTITLE_BANDS[1],
    )

    assert isinstance(provider, OcrProvider)
    assert not isinstance(provider, PreviewProvider)
    assert provider.mode is OcrMode.COMPATIBILITY
    assert provider.capabilities().values["batch"] is False
    assert evidence[0].band == "lower"
    assert evidence[0].text == "下排台词"
