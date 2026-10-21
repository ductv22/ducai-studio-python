from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

import pytest


def _module():
    return runpy.run_path(str(Path(__file__).parents[1] / "fixtures" / "ocr-io-benchmark.py"))


def test_io_instrumentation_preserves_rows_return_values_and_exceptions():
    module = _module()
    stats = module["IoTimings"]()
    appended = []

    class Worker:
        def _append_evidence(self, rows):
            if not isinstance(rows, list):
                raise ValueError("invalid_rows")
            appended.extend(rows)
            return "appended"

        def _checkpoint(self, **kwargs):
            return kwargs

    worker = module["instrument_worker"](Worker, stats)()
    rows = [{"text": "娘"}, {"text": "场景文字"}]
    assert worker._append_evidence(rows) == "appended"
    assert worker._append_evidence([]) == "appended"
    assert appended == rows and appended[0] is rows[0]
    with pytest.raises(ValueError, match="invalid_rows"):
        worker._append_evidence(None)
    assert worker._checkpoint(checkpoint_key="latest") == {"checkpoint_key": "latest"}
    assert stats.values["append_calls"] == 3
    assert stats.values["empty_append_calls"] == 1
    assert stats.values["append_rows"] == 2
    assert stats.values["checkpoint_calls"] == 1
    assert stats.values["append_seconds"] >= 0


def test_io_probe_executes_real_sync_and_propagates_failure():
    stats = _module()["IoTimings"]()
    descriptors = []

    def sync(descriptor):
        descriptors.append(descriptor)
        if descriptor < 0:
            raise OSError("disk_error")
        return "synced"

    assert stats.sync(sync, 8) == "synced"
    with pytest.raises(OSError, match="disk_error"):
        stats.sync(sync, -1)
    assert descriptors == [8, -1]
    assert stats.values["fsync_calls"] == 2
    assert stats.values["fsync_seconds"] >= 0


@pytest.mark.parametrize("raises", [False, True])
def test_io_probe_restores_owned_environment_and_globals(monkeypatch, tmp_path, raises):
    module = _module()
    keys = module["BENCHMARK_ENV_KEYS"]
    for index, key in enumerate(keys):
        if index % 2:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, "original-value")
    environment = {key: os.environ.get(key) for key in keys}
    monkeypatch.setattr(sys, "argv", ["probe", "unused.json", str(tmp_path / "run")])
    original_argv, original_sync = sys.argv, os.fsync

    class Worker:
        pass

    def benchmark():
        for index, key in enumerate(keys):
            if index % 2:
                os.environ[key] = "benchmark-value"
            else:
                os.environ.pop(key, None)
        if raises:
            raise RuntimeError("benchmark-failed")
        return 17

    monkeypatch.setattr(runpy, "run_path", lambda _path: {
        "OcrWorker": Worker, "main": benchmark,
    })
    if raises:
        with pytest.raises(RuntimeError, match="benchmark-failed"):
            module["main"]()
    else:
        assert module["main"]() == 17
    assert {key: os.environ.get(key) for key in keys} == environment
    assert sys.argv is original_argv and os.fsync is original_sync
