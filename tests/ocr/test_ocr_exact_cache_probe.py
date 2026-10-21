from __future__ import annotations

import hashlib
import runpy
from pathlib import Path

import pytest


def _module():
    return runpy.run_path(str(Path(__file__).parents[1] / "fixtures"
                             / "ocr-exact-cache-host.py"))


class ByteCrop:
    shape = (1, 1, 1)
    dtype = "uint8"

    def __init__(self, data):
        self.data = data

    def tobytes(self):
        return self.data


@pytest.mark.parametrize("limit", [1, 2, 256, 4096])
def test_shadow_cache_matches_actual_host_request_eviction_and_duplicate_order(limit):
    module = _module()
    host = module["_module"]["OcrEngineHost"]()
    host.READING_CACHE_LIMIT = limit
    shadow = module["ShadowCache"](limit)
    for values in ([0, 0, 1, 2], [0, 2, 3], [3, 0, 1], [1, 1], [], [0, 3, 2, 0]):
        crops = [ByteCrop(bytes([value])) for value in values]
        keys = [("upper", crop.shape, crop.dtype, hashlib.sha256(crop.tobytes()).digest())
                for crop in crops]
        calls = []

        def recognize(frames, _bands, calls=calls):
            calls.extend(frames)
            return [{"text": str(frame.data[0])} for frame in frames]

        readings = host._recognize_cached(crops, ["upper"] * len(crops), recognize)
        assert shadow.access(keys) == len(calls)
        assert list(shadow.entries) == list(host._reading_cache)
        assert [reading["text"] for reading in readings] == [str(value) for value in values]


def test_shadow_does_not_reuse_non_array_observations():
    shadow = _module()["ShadowCache"](2)
    assert shadow.access([None, None]) == 2
    assert shadow.access([None, None]) == 2
    assert not shadow.entries


def test_probe_requires_explicit_diagnostic_override_and_resets_with_media(monkeypatch):
    module = _module()
    monkeypatch.delenv("DUCAI_OCR_CACHE_LIMIT", raising=False)
    baseline = module["OcrEngineHost"]()
    assert baseline.READING_CACHE_LIMIT == 256
    monkeypatch.setenv("DUCAI_OCR_CACHE_LIMIT", "4096")
    candidate = module["OcrEngineHost"]()
    assert candidate.READING_CACHE_LIMIT == 4096
    for shadow in candidate.shadows.values():
        shadow.access([("upper", (1, 1), "uint8", b"digest")])
    candidate.close()
    assert not candidate._reading_cache
    assert all(not shadow.entries for shadow in candidate.shadows.values())
    assert module["_module"]["OcrEngineHost"].READING_CACHE_LIMIT == 256


@pytest.mark.parametrize("limit", ["1", "1000000", "invalid"])
def test_probe_rejects_unbounded_or_invalid_cache_override(monkeypatch, limit):
    monkeypatch.setenv("DUCAI_OCR_CACHE_LIMIT", limit)
    with pytest.raises(ValueError):
        _module()["OcrEngineHost"]()
