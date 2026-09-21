from __future__ import annotations

import json
import os
import sys
import time


def _write(request_id: str, event: str, result: dict | None = None, error: str = "") -> None:
    body = {
        "protocol_version": 1,
        "id": request_id,
        "ok": not error,
        "event": event,
    }
    if error:
        body["error"] = {"code": error}
    else:
        body["result"] = result or {}
    sys.stdout.write(json.dumps(body, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        request = json.loads(line)
        request_id = str(request.get("id", "unknown"))
        action = request.get("action")
        if action == "shutdown":
            _write(request_id, "complete", {"stopped": True})
            return 0
        if action != "transcribe":
            _write(request_id, "error", error="unknown_action")
            continue
        payload = request.get("payload", {})
        device = str(payload.get("device", "cuda"))
        if os.environ.get("DUCAI_FAKE_WHISPER_CUDA_FAIL") == "1" and device == "cuda":
            _write(request_id, "error", error="whisper_cuda_failed")
            continue
        start_us = int(payload.get("start_us", 0))
        duration_us = int(payload.get("duration_us", 0))
        _write(
            request_id,
            "metadata",
            {
                "device": device,
                "compute_type": "int8" if device == "cpu" else "float16",
                "language": "zh",
                "language_probability": 0.99,
                "duration_us": duration_us,
                "start_us": start_us,
            },
        )
        rows = (
            (0, 3_000_000, "第一句话。"),
            (3_000_000, 6_000_000, "第二句话。"),
            (6_000_000, 9_000_000, "第三句话。"),
        )
        delay = int(os.environ.get("DUCAI_FAKE_WHISPER_DELAY_MS", "0")) / 1_000
        count = 0
        for segment_start, segment_end, text in rows:
            if segment_end <= start_us or segment_start >= duration_us:
                continue
            if delay:
                time.sleep(delay)
            count += 1
            _write(
                request_id,
                "segment",
                {
                    "index": count,
                    "start_us": max(start_us, segment_start),
                    "end_us": min(duration_us, segment_end),
                    "text": text,
                    "avg_logprob": -0.1,
                    "no_speech_prob": 0.01,
                    "compression_ratio": 1.0,
                    "avg_word_probability": 0.95,
                    "words": [],
                },
            )
        _write(
            request_id,
            "complete",
            {
                "segment_count": count,
                "device": device,
                "compute_type": "int8" if device == "cpu" else "float16",
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
