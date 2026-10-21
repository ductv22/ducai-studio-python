"""Measure byte-exact cache reuse using the unchanged serial production host.

The optional larger limit exists only in this diagnostic adapter. Shadow caches
store keys, not fabricated readings; all actual results come from real engines.
"""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import time
from collections import OrderedDict
from pathlib import Path

_module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "src"
                            / "ducai_studio" / "engine_hosts" / "ocr_engine_host.py"))
PaddleRuntime = _module["PaddleRuntime"]
RapidRuntime = _module["RapidRuntime"]
_combine_reading = _module["_combine_reading"]
serve = _module["serve"]


class ShadowCache:
    """Mirror the host's request-level insertion and bounded eviction order."""

    def __init__(self, limit):
        self.limit = limit
        self.entries = OrderedDict()

    def access(self, keys):
        pending = {}
        for index, key in enumerate(keys):
            if key is not None and key in self.entries:
                self.entries.move_to_end(key)
            else:
                pending.setdefault(key if key is not None else index, index)
        for index in pending.values():
            if keys[index] is not None:
                self.entries[keys[index]] = None
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        return len(pending)


class OcrEngineHost(_module["OcrEngineHost"]):
    def __init__(self, runtime_factory=None, video_factory=None):
        super().__init__(runtime_factory, video_factory)
        raw_limit = os.environ.get("DUCAI_OCR_CACHE_LIMIT")
        limit = self.READING_CACHE_LIMIT if raw_limit is None else int(raw_limit)
        if limit not in (256, 4096):
            raise ValueError("diagnostic_cache_limit_must_be_256_or_4096")
        self.READING_CACHE_LIMIT = limit
        self.shadows = {value: ShadowCache(value) for value in (256, 4096)}

    def close(self):
        super().close()
        for shadow in getattr(self, "shadows", {}).values():
            shadow.entries.clear()

    def _recognize_cached(self, crops, crop_bands, recognize_batched):
        started = time.perf_counter()
        keys = []
        for crop, band in zip(crops, crop_bands, strict=True):
            key = None
            if callable(getattr(crop, "tobytes", None)):
                key = (band, tuple(crop.shape), str(crop.dtype),
                       hashlib.sha256(crop.tobytes()).digest())
            keys.append(key)
        key_seconds = time.perf_counter() - started
        misses = {str(limit): shadow.access(keys) for limit, shadow in self.shadows.items()}
        fresh = 0
        inference_seconds = 0.0

        def measured(frames, bands):
            nonlocal fresh, inference_seconds
            fresh += len(frames)
            began = time.perf_counter()
            try:
                return recognize_batched(frames, bands)
            finally:
                inference_seconds += time.perf_counter() - began

        readings = super()._recognize_cached(crops, crop_bands, measured)
        if fresh != misses[str(self.READING_CACHE_LIMIT)]:
            raise RuntimeError("diagnostic_shadow_cache_accounting_mismatch")
        path = os.environ.get("DUCAI_OCR_CACHE_PROBE_PATH")
        if path:
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "mode": self.mode,
                    "requested_crops": len(crops),
                    "fresh_crops": fresh,
                    "cache_limit": self.READING_CACHE_LIMIT,
                    "cache_entries": len(self._reading_cache),
                    "shadow_fresh_crops": misses,
                    "inference_seconds": inference_seconds,
                    "key_seconds": key_seconds,
                }) + "\n")
        return readings


if __name__ == "__main__":
    raise SystemExit(serve(host=OcrEngineHost()))
