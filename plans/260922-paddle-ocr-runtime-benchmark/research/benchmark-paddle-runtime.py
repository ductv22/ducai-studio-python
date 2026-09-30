"""Measure one DUCAI Paddle runtime against representative subtitle video crops."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import statistics
import sys
import time
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--mode", choices=("fast", "quality"), default="fast")
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def main() -> int:
    args = _parse_args()
    host_root = args.repo / "src" / "ducai_studio" / "engine_hosts"
    sys.path.insert(0, str(host_root))
    import ocr_engine_host as host
    import paddle
    import paddleocr
    import paddlex

    engine = host.OcrEngineHost()
    opening_started = time.perf_counter()
    descriptor = engine.open(
        {
            "mode": args.mode,
            "media_path": str(args.video.resolve()),
            "device": "gpu:0",
            "batch_size": 12,
        }
    )
    initialized = time.perf_counter()
    duration_us = int(descriptor["duration_us"])
    lower = 1_000_000
    upper = max(lower, duration_us - 1_000_000)
    if args.frames == 1:
        timestamps = [min(lower, upper)]
    else:
        span = max(0, upper - lower)
        timestamps = [
            lower + round(span * index / (args.frames - 1))
            for index in range(args.frames)
        ]
    timestamps = sorted(set(min(max(0, value), max(0, duration_us - 1)) for value in timestamps))
    payload = {
        "timestamps_us": timestamps,
        "bands": [
            {"name": "upper", "x1": 0.05, "y1": 0.56, "x2": 0.95, "y2": 0.80, "scale": 1.0},
            {"name": "lower", "x1": 0.05, "y1": 0.76, "x2": 0.95, "y2": 0.98, "scale": 1.0},
        ],
    }

    warmup_seconds: list[float] = []
    for _ in range(args.warmup):
        before = time.perf_counter()
        engine.recognize(payload)
        paddle.device.synchronize()
        warmup_seconds.append(time.perf_counter() - before)

    run_seconds: list[float] = []
    last_result: dict[str, object] = {}
    for _ in range(args.runs):
        before = time.perf_counter()
        last_result = engine.recognize(payload)
        paddle.device.synchronize()
        run_seconds.append(time.perf_counter() - before)

    evidence = list(last_result.get("evidence", []))
    normalized = [
        {
            "timestamp_us": item.get("timestamp_us"),
            "band": item.get("band"),
            "text": item.get("text"),
            "segment_count": item.get("segment_count"),
        }
        for item in evidence
    ]
    digest = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    memory_bytes = None
    max_memory = getattr(paddle.device.cuda, "max_memory_allocated", None)
    if callable(max_memory):
        try:
            memory_bytes = int(max_memory())
        except (OSError, RuntimeError, TypeError, ValueError):
            memory_bytes = None
    engine.close()

    report = {
        "schema": "ducai-paddle-benchmark-v1",
        "python": sys.version.split()[0],
        "paddle": paddle.__version__,
        "paddleocr": getattr(paddleocr, "__version__", ""),
        "paddlex": getattr(paddlex, "__version__", ""),
        "device": paddle.device.get_device(),
        "cuda": paddle.version.cuda(),
        "cudnn": _distribution_version("nvidia-cudnn-cu12"),
        "mode": args.mode,
        # Do not persist local project names or paths in a benchmark report that
        # may later be committed.  The duration and evidence digest identify the
        # workload strongly enough for this comparison.
        "video": "<representative-project-media>",
        "video_duration_us": duration_us,
        "sample_frames": len(timestamps),
        "crop_count_per_run": len(timestamps) * 2,
        "engine_init_seconds": round(initialized - opening_started, 6),
        "engine_init_scope": "media-open-only; lazy model init occurs in warmup",
        "measurement_scope": "host-call seek/decode/crop/OCR/synchronize",
        "warmup_seconds": [round(value, 6) for value in warmup_seconds],
        "run_seconds": [round(value, 6) for value in run_seconds],
        "median_seconds": round(statistics.median(run_seconds), 6),
        "p95_seconds": round(_percentile(run_seconds, 0.95), 6),
        "crops_per_second": round((len(timestamps) * 2) / statistics.median(run_seconds), 3),
        "max_gpu_memory_bytes": memory_bytes,
        "evidence_count": len(evidence),
        "evidence_sha256": digest,
        "evidence_hash_scope": "normalized text and segment count from final measured pass",
        "evidence_redacted": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
