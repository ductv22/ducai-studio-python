"""Diagnostic adapter around the real OCR host; never supplies synthetic readings."""
import json
import os
import runpy
import sys
import time
import traceback
from pathlib import Path

module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "src" / "ducai_studio"
                            / "engine_hosts" / "ocr_engine_host.py")
                       if not os.environ.get("DUCAI_OCR_TRACE_RUNTIME_PATH")
                       else os.environ["DUCAI_OCR_TRACE_RUNTIME_PATH"])
original_combine = module["_combine_reading"]


def traced_combine(texts, scores, boxes, width, height, *, band=""):
    result = original_combine(texts, scores, boxes, width, height, band=band)
    path = os.environ.get("DUCAI_OCR_RAW_PROFILE_PATH")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"texts": texts, "scores": scores, "boxes": boxes,
                "width": width, "height": height, "band": band, "result": result},
                ensure_ascii=False) + "\n")
    return result


module["PaddleRuntime"]._recognize_frames.__globals__["_combine_reading"] = traced_combine


class TraceHost(module["OcrEngineHost"]):
    def recognize(self, payload):
        timings = {"decode": 0.0, "inference": 0.0}
        frame_size = []
        originals = []
        for obj, method, phase in ((self.video, "read_many", "decode"),
                                    (self.runtime, "recognize_batched", "inference")):
            original = getattr(obj, method)
            originals.append((obj, method, original))

            def measured(*args, original=original, phase=phase, **kwargs):
                started = time.perf_counter()
                try:
                    result = original(*args, **kwargs)
                    if phase == "decode" and result:
                        frame_size[:] = list(result[0].shape[:2][::-1])
                    return result
                finally:
                    timings[phase] += time.perf_counter() - started

            setattr(obj, method, measured)
        began = time.perf_counter()
        try:
            result = super().recognize(payload)
            result["diagnostic_timing"] = {**timings, "total": time.perf_counter() - began}
            profile_path = os.environ.get("DUCAI_OCR_PROFILE_PATH")
            if profile_path:
                with Path(profile_path).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"timing": result["diagnostic_timing"],
                        "mode": self.mode, "frames": len(payload["timestamps_us"]),
                        "worker_count": getattr(self.runtime, "worker_count", 1),
                        "execution_fallback_reason": getattr(
                            self.runtime, "execution_fallback_reason", ""),
                        "bands": [b["name"] for b in payload["bands"]],
                        "frame_size": frame_size}) + "\n")
            return result
        except Exception:
            traceback.print_exc(file=sys.stderr)
            raise
        finally:
            for obj, method, original in originals:
                setattr(obj, method, original)


if __name__ == "__main__":
    def runtime_factory(mode, device, batch_size):
        if mode == "compatibility":
            return module["RapidRuntime"](mode, device, batch_size)
        limit = (1 if os.environ.get("DUCAI_OCR_TRACE_SERIAL_PADDLE") == "1"
                 else os.environ.get("DUCAI_OCR_TRACE_DETECTOR_BATCH_SIZE"))
        # Rejected experiments can be replayed only with an archived candidate
        # host. The product runtime retains its original serial constructor.
        options = ({"detector_batch_size": int(limit)}
                   if limit is not None and int(limit) > 1 else {})
        if limit is None and "_paddle_runtime" in module:
            return module["_paddle_runtime"](mode, device, batch_size)
        return module["PaddleRuntime"](mode, device, batch_size, **options)

    raise SystemExit(module["serve"](host=TraceHost(runtime_factory=runtime_factory)))
