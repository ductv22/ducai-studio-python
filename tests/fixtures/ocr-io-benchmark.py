"""Attribute real OCR evidence durability I/O; do not alter recognition or syncs."""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import time
from contextlib import contextmanager
from pathlib import Path

BENCHMARK_ENV_KEYS = (
    "DUCAI_ARTIFACT_STAGING_ROOT",
    "DUCAI_OCR_PROFILE_PATH",
    "DUCAI_OCR_RAW_PROFILE_PATH",
    "DUCAI_OCR_TRACE_SERIAL_PADDLE",
    "DUCAI_OCR_TRACE_DETECTOR_BATCH_SIZE",
    "DUCAI_OCR_TRACE_RUNTIME_PATH",
)


class IoTimings:
    def __init__(self):
        self.values = {key: 0 for key in ("append_calls", "empty_append_calls", "append_rows",
                                        "append_seconds", "fsync_calls", "fsync_seconds",
                                        "checkpoint_calls", "checkpoint_seconds")}

    @contextmanager
    def measure(self, phase):
        self.values[phase + "_calls"] += 1
        started = time.perf_counter()
        try:
            yield
        finally:
            self.values[phase + "_seconds"] += time.perf_counter() - started

    def sync(self, original, descriptor):
        with self.measure("fsync"):
            return original(descriptor)


def instrument_worker(worker_type, timings):
    class TimedWorker(worker_type):
        def _append_evidence(self, rows):
            if isinstance(rows, list):
                timings.values["append_rows"] += len(rows)
                timings.values["empty_append_calls"] += not rows
            with timings.measure("append"):
                return super()._append_evidence(rows)

        def _checkpoint(self, **kwargs):
            with timings.measure("checkpoint"):
                return super()._checkpoint(**kwargs)

    return TimedWorker


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker-module", type=Path)
    options, rest = parser.parse_known_args()
    benchmark = runpy.run_path(str(Path(__file__).with_name("ocr-speed-benchmark.py")))
    worker_type = (runpy.run_path(str(options.worker_module.resolve()))["OcrWorker"]
                   if options.worker_module else benchmark["OcrWorker"])
    timings = IoTimings()
    benchmark["main"].__globals__["OcrWorker"] = instrument_worker(worker_type, timings)
    original_sync, original_argv = os.fsync, sys.argv
    original_environment = {key: os.environ.get(key) for key in BENCHMARK_ENV_KEYS}
    os.fsync = lambda descriptor: timings.sync(original_sync, descriptor)
    sys.argv = [__file__, *rest]
    try:
        result = benchmark["main"]()
    finally:
        os.fsync, sys.argv = original_sync, original_argv
        for key, value in original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        print(json.dumps({"io_timing": timings.values}), flush=True)
    # The existing benchmark owns creation of the fresh output directory.
    output = Path(rest[1]).resolve()
    report_path = output / "report.json"
    if report_path.is_file() and timings.values["checkpoint_calls"]:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["io_timing"] = timings.values
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
