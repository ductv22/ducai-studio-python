from __future__ import annotations

import importlib.util
import io
import struct
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from types import SimpleNamespace

import pytest

HOST_PATH = Path(__file__).parents[2] / "src/ducai_studio/engine_hosts/ocr_engine_host.py"


@pytest.fixture
def host_module(monkeypatch):
    spec = importlib.util.spec_from_file_location("ocr_parallel_host_test", HOST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_available_ram_mb", lambda: 8192)
    monkeypatch.setattr(module, "_available_gpu_mb", lambda _device: 8192)
    return module


def _reading(frame, band):
    return {"text": str(frame), "confidence": .99, "box": [0., 0., 1., 1.], "band": band}


class RecordingChild:
    def __init__(self, *_args):
        self.requests = []
        self.close_calls = []

    def ready(self):
        return None

    def recognize(self, frames, bands):
        self.requests.append((list(frames), list(bands)))
        return [_reading(frame, band) for frame, band in zip(frames, bands, strict=True)]

    def close(self, *, force=False):
        self.close_calls.append(force)


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 17])
def test_three_predictors_restore_every_input_in_original_order(host_module, count):
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12,
                                               child_factory=RecordingChild)
    children = list(runtime.children)
    frames = list(range(count))
    bands = ["dialogue" if i % 2 else "scene" for i in frames]
    try:
        assert runtime.recognize_batched(frames, bands) == [
            _reading(frame, band) for frame, band in zip(frames, bands, strict=True)
        ]
        sent = [frame for child in children for request, _ in child.requests for frame in request]
        assert sorted(sent) == frames
        assert runtime.worker_count == 3
        assert runtime.provider_id == "paddle-fast"
        assert runtime.model_id == host_module.PADDLE_MODELS["fast"][1]
    finally:
        runtime.close()
    assert all(child.close_calls == [False] for child in children)
    runtime.close()
    assert all(child.close_calls == [False] for child in children)
    with pytest.raises(RuntimeError, match="engine_not_open"):
        runtime.recognize([1])


def test_input_mismatch_is_not_silently_replayed(host_module):
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12,
                                               child_factory=RecordingChild)
    try:
        with pytest.raises(ValueError, match="paddle_band_count_mismatch"):
            runtime.recognize_batched([1, 2], ["scene"])
        assert runtime.worker_count == 3
        assert all(not child.requests for child in runtime.children)
    finally:
        runtime.close()


@pytest.mark.parametrize("failure", ["exception", "short", "invalid"])
def test_failed_partition_replays_entire_request_once_then_stays_serial(
    host_module, monkeypatch, failure,
):
    children = []
    serial_requests = []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        if len(children) == 2:
            if failure == "exception":
                def fail(*_args):
                    raise RuntimeError("gpu_oom")
                child.recognize = fail
            else:
                child.recognize = lambda *_args: [] if failure == "short" else [None] * 3
        return child

    def serial_factory(*args):
        assert args == ("quality", "gpu:0", 12)

        def recognize(frames, bands):
            serial_requests.append((list(frames), list(bands)))
            return [_reading(frame, band) for frame, band in zip(frames, bands, strict=True)]
        return SimpleNamespace(recognize_batched=recognize)

    monkeypatch.setattr(host_module, "PaddleRuntime", serial_factory)
    runtime = host_module.ParallelPaddleRuntime("quality", "gpu:0", 12, child_factory=factory)
    try:
        frames, bands = list(range(9)), ["dialogue"] * 9
        assert runtime.recognize_batched(frames, bands) == [_reading(i, "dialogue") for i in frames]
        if failure == "exception":
            assert serial_requests == []
            assert children[3].requests == [(frames, bands)]
            assert children[3].close_calls == []
        else:
            assert serial_requests == [(frames, bands)]
            assert runtime.children == [] and runtime.executor is None
        assert all(child.close_calls == [True] for child in children[:3])
        assert runtime.worker_count == 1
        assert runtime.provider_id == "paddle-quality"
        assert runtime.recognize([99], "scene") == [_reading(99, "scene")]
        requests = children[3].requests if failure == "exception" else serial_requests
        assert requests[-1] == ([99], ["scene"])
        assert all(child.close_calls == [True] for child in children[:3])
    finally:
        runtime.close()


@pytest.mark.parametrize("failure", [TimeoutError(), EOFError("ocr_predictor_exited"),
                                    BrokenPipeError("private details")])
def test_transient_failure_restarts_once_and_replays_whole_request_in_order(
    host_module, monkeypatch, failure,
):
    children = []

    def factory(*args):
        if len(children) == 3:
            assert all(child.close_calls == [True] for child in children)
        child = RecordingChild(*args)
        children.append(child)
        if len(children) == 2:
            child.recognize = lambda *_args: (_ for _ in ()).throw(failure)
        return child

    def unexpected_serial(*_args):
        pytest.fail("A recoverable fault must not permanently load a serial model")

    monkeypatch.setattr(host_module, "PaddleRuntime", unexpected_serial)
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12, child_factory=factory)
    try:
        frames, bands = list(range(17)), ["dialogue", "scene"] * 8 + ["dialogue"]
        assert runtime.recognize_batched(frames, bands) == [
            _reading(frame, band) for frame, band in zip(frames, bands, strict=True)
        ]
        replayed = [frame for child in children[3:] for values, _ in child.requests
                    for frame in values]
        assert sorted(replayed) == frames  # Never publish a partially successful first attempt.
        assert runtime.worker_count == 3 and runtime.execution_fallback_reason == ""
        assert runtime.execution_recovery_attempts == 1
        assert runtime.execution_recovery_reason in {
            "ocr_predictor_timeout", "ocr_predictor_exited", "BrokenPipeError",
        }
        assert runtime.recognize([99], "scene") == [_reading(99, "scene")]
        assert len(children) == 6
    finally:
        runtime.close()
    assert all(child.close_calls == [True] for child in children[:3])
    assert all(child.close_calls == [False] for child in children[3:])


@pytest.mark.parametrize("second_failure", ["request", "ready", "factory"])
def test_repeated_or_restart_failure_is_bounded_and_replays_serial_once(
    host_module, monkeypatch, second_failure,
):
    children, serial_requests = [], []

    def factory(*args):
        if len(children) == 3 and second_failure == "factory":
            raise RuntimeError("model_startup_failed")
        child = RecordingChild(*args)
        children.append(child)
        if len(children) == 2 or (len(children) == 5 and second_failure == "request"):
            child.recognize = lambda *_args: (_ for _ in ()).throw(TimeoutError())
        if len(children) == 5 and second_failure == "ready":
            child.ready = lambda: (_ for _ in ()).throw(TimeoutError())
        return child

    def serial_factory(*_args):
        assert all(child.close_calls == [True] for child in children)

        def recognize(frames, bands):
            serial_requests.append(list(frames))
            return [_reading(frame, band) for frame, band in zip(frames, bands, strict=True)]
        return SimpleNamespace(recognize_batched=recognize)

    monkeypatch.setattr(host_module, "PaddleRuntime", serial_factory)
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12, child_factory=factory)
    try:
        assert runtime.recognize([1, 2, 3]) == [_reading(i, "") for i in [1, 2, 3]]
        assert serial_requests == [[1, 2, 3]]
        assert runtime.worker_count == 1 and runtime.execution_recovery_attempts == 1
        assert runtime.execution_fallback_reason in {
            "ocr_predictor_timeout", "model_startup_failed",
        }
        assert runtime.recognize([4]) == [_reading(4, "")]
        assert serial_requests == [[1, 2, 3], [4]]
        assert len(children) <= 6 and runtime.executor is None
    finally:
        runtime.close()


@pytest.mark.parametrize("ram,gpu", [(4095, 8192), (8192, 4095), (None, 8192), (8192, None)])
def test_recovery_frees_old_pool_and_obeys_memory_guard(host_module, monkeypatch, ram, gpu):
    children = []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        if len(children) <= 3:
            child.recognize = lambda *_args: (_ for _ in ()).throw(TimeoutError())
        return child

    def probe():
        assert all(child.close_calls == [True] for child in children[:3])
        return ram

    monkeypatch.setattr(host_module, "_available_ram_mb", probe)
    monkeypatch.setattr(host_module, "_available_gpu_mb", lambda _device: gpu)
    monkeypatch.setattr(host_module, "PaddleRuntime", lambda *_args: SimpleNamespace(
        recognize_batched=lambda frames, bands: [
            _reading(f, b) for f, b in zip(frames, bands, strict=True)]))
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12, child_factory=factory)
    try:
        assert runtime.recognize([1]) == [_reading(1, "")]
        assert len(children) == 4 and runtime.worker_count == 1
        assert runtime.children == children[3:] and children[3].close_calls == []
        expected = ("insufficient_free_memory" if ram is not None and gpu is not None
                    else "memory_probe_unavailable")
        assert runtime.execution_fallback_reason == expected
    finally:
        runtime.close()


def test_later_transient_failure_cannot_restart_forever(host_module, monkeypatch):
    children = []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        if len(children) == 1:
            child.recognize = lambda *_args: (_ for _ in ()).throw(TimeoutError())
        return child

    monkeypatch.setattr(host_module, "PaddleRuntime", lambda *_args: SimpleNamespace(
        recognize_batched=lambda frames, bands: [
            _reading(f, b) for f, b in zip(frames, bands, strict=True)]))
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12, child_factory=factory)
    try:
        assert runtime.recognize([1]) == [_reading(1, "")]
        children[3].recognize = lambda *_args: (_ for _ in ()).throw(TimeoutError())
        assert runtime.recognize([2]) == [_reading(2, "")]
        assert len(children) == 6 and runtime.worker_count == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("failure_at", [0, 1, 2])
def test_partial_startup_failure_closes_created_children_before_serial(
    host_module, monkeypatch, failure_at,
):
    children, loaded = [], []

    def factory(*args):
        if len(children) == failure_at:
            raise RuntimeError("model_startup_failed")
        child = RecordingChild(*args)
        children.append(child)
        return child

    def serial_factory(*_args):
        assert all(child.close_calls == [True] for child in children)
        loaded.append(True)
        return SimpleNamespace(recognize_batched=lambda *_args: [])

    monkeypatch.setattr(host_module, "PaddleRuntime", serial_factory)
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12, child_factory=factory)
    assert runtime.worker_count == 1 and loaded == [True]
    assert runtime.execution_fallback_reason == "model_startup_failed"
    runtime.close()


def test_handshake_timeout_cleans_all_started_workers(host_module, monkeypatch):
    children = []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        child.ready = lambda: (_ for _ in ()).throw(TimeoutError())
        return child

    monkeypatch.setattr(host_module, "PaddleRuntime", lambda *_args: object())
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12, child_factory=factory)
    assert all(child.close_calls == [True] for child in children)
    assert runtime.worker_count == 1
    runtime.close()


def test_one_absolute_deadline_and_submitted_order(host_module, monkeypatch):
    ready, pending = Future(), Future()
    ready.set_result("first")
    clock = iter([10., 10.25, 11.25])
    monkeypatch.setattr(host_module.time, "monotonic", lambda: next(clock))
    results = host_module._ordered_results([ready, pending], 1.)
    assert next(results) == "first"
    with pytest.raises(TimeoutError):
        next(results)


def test_private_packets_roundtrip_keep_types_and_order(host_module):
    values = [([bytes(range(256)), (1, 2, 3)], ["dialogue", "scene"]),
              (True, [_reading(3, "scene")]), None]
    stream = io.BytesIO()
    for value in values:
        host_module._send_packet(stream, value)
    stream.seek(0)
    assert [host_module._receive_packet(stream) for _ in values] == values
    assert stream.read() == b""


@pytest.mark.parametrize("packet", [b"", b"\x01" * 7, struct.pack("<Q", 50) + b"short"])
def test_private_packet_truncation_is_not_empty_ocr(host_module, packet):
    with pytest.raises(EOFError, match="ocr_predictor_exited"):
        host_module._receive_packet(io.BytesIO(packet))


def test_packet_limit_rejected_before_reading_payload(host_module, monkeypatch):
    monkeypatch.setattr(host_module, "MAX_PREDICTOR_PACKET_BYTES", 4)
    with pytest.raises(ValueError, match="ocr_predictor_packet_too_large"):
        host_module._send_packet(io.BytesIO(), "long value")
    with pytest.raises(ValueError, match="ocr_predictor_packet_too_large"):
        host_module._receive_packet(io.BytesIO(struct.pack("<Q", 5)))


@pytest.mark.parametrize("device,workers,ram,gpu,parallel,reason", [
    ("cpu", 3, 8000, 8000, False, ""),
    ("gpu:0", 1, 8000, 8000, False, ""),
    ("gpu:0", 3, 8000, 8000, True, ""),
    ("gpu:0", 3, 4096, 4096, True, ""),
    ("gpu:0", 3, 4095, 8000, False, "insufficient_free_memory"),
    ("gpu:1", 3, 8000, 4095, False, "insufficient_free_memory"),
    ("gpu:0", 3, None, 8000, False, "memory_probe_unavailable"),
    ("gpu:0", 3, 8000, None, False, "memory_probe_unavailable"),
])
def test_resource_guard_chooses_three_or_unchanged_serial(
    host_module, monkeypatch, device, workers, ram, gpu, parallel, reason,
):
    monkeypatch.setattr(host_module, "_available_ram_mb", lambda: ram)
    monkeypatch.setattr(host_module, "_available_gpu_mb", lambda value: gpu)
    monkeypatch.setattr(host_module, "PaddleRuntime",
                        lambda *_args: SimpleNamespace(parallel=False))

    def adaptive_factory(*args, defer_reason):
        assert args == ("fast", device, 12)
        assert device.startswith("gpu") and workers == 3
        return SimpleNamespace(parallel=not bool(defer_reason),
                               worker_count=1 if defer_reason else 3,
                               execution_fallback_reason=defer_reason)

    monkeypatch.setattr(host_module, "ParallelPaddleRuntime", adaptive_factory)
    result = host_module._paddle_runtime("fast", device, 12, workers)
    assert result.parallel == parallel
    if not parallel:
        assert result.worker_count == 1 and result.execution_fallback_reason == reason


@pytest.mark.parametrize("workers", [0, 2, 4, True, "3"])
def test_worker_limit_validated(host_module, workers):
    with pytest.raises(ValueError, match="invalid_ocr_worker_count"):
        host_module._paddle_runtime("fast", "gpu:0", 12, workers)


def test_host_mode_switch_closes_previous_predictors_and_failed_open_video(host_module):
    closed = []

    def video_factory(path):
        return SimpleNamespace(duration_us=1000, fps=25., close=lambda: closed.append(path))

    def runtime_factory(mode, device, batch_size):
        if mode == "quality":
            raise RuntimeError("model_missing")
        return SimpleNamespace(provider_id="paddle-fast", model_id="original",
                               close=lambda: closed.append("predictors"))

    host = host_module.OcrEngineHost(runtime_factory=runtime_factory, video_factory=video_factory)
    result = host.open({"mode": "fast", "media_path": "first"})
    assert result["worker_count"] == 1
    with pytest.raises(RuntimeError, match="model_missing"):
        host.open({"mode": "quality", "media_path": "second"})
    assert closed == ["predictors", "first", "second"]
    assert host.runtime is None and host.video is None
    host.close()


def test_low_memory_start_promotes_at_next_due_request_without_replaying_outputs(
    host_module, monkeypatch,
):
    clock, free = [0.0], [8192, 1000]
    children, probes = [], []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        return child

    monkeypatch.setattr(host_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host_module, "_available_ram_mb", lambda: free[0])
    monkeypatch.setattr(host_module, "_available_gpu_mb",
                        lambda device: probes.append(device) or free[1])
    monkeypatch.setattr(host_module, "ChildPredictor", factory)
    monkeypatch.setattr(host_module, "PaddleRuntime", lambda *_args: pytest.fail(
        "Resource fallback must not retain an in-process CUDA allocator"))
    runtime = host_module._paddle_runtime("fast", "gpu:0", 12)
    try:
        original = children[0]
        assert runtime.worker_count == 1
        assert runtime.recognize([1, 2], "dialogue") == [_reading(i, "dialogue") for i in (1, 2)]
        assert len(probes) == 1
        free[1] = 8192
        clock[0] = 5.0
        assert runtime.recognize([3, 4, 5], "scene") == [_reading(i, "scene") for i in (3, 4, 5)]
        assert runtime.worker_count == 3 and runtime.execution_fallback_reason == ""
        assert len(children) == 3 and runtime.children[0] is original
        assert original.close_calls == []
        assert runtime.execution_recovery_attempts == 0
        sent = [frame for child in children for request, _ in child.requests for frame in request]
        assert sorted(sent) == [1, 2, 3, 4, 5]
        clock[0] = 1000.0
        runtime.recognize([6])
        assert len(children) == 3 and len(probes) == 2
    finally:
        runtime.close()
    assert all(child.close_calls == [False] for child in children)


@pytest.mark.parametrize("ram,gpu", [(4095, 8192), (8192, 4095), (None, 8192), (8192, None)])
def test_deferred_promotion_requires_both_known_memory_budgets(
    host_module, monkeypatch, ram, gpu,
):
    clock, free = [0.0], [1000, 1000]
    monkeypatch.setattr(host_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host_module, "_available_ram_mb", lambda: free[0])
    monkeypatch.setattr(host_module, "_available_gpu_mb", lambda _device: free[1])
    monkeypatch.setattr(host_module, "ChildPredictor", RecordingChild)
    runtime = host_module._paddle_runtime("quality", "gpu:1", 12)
    try:
        free[:] = ram, gpu
        clock[0] = 5.0
        assert runtime.recognize([1]) == [_reading(1, "")]
        assert runtime.worker_count == 1 and len(runtime.children) == 1
        expected = ("memory_probe_unavailable" if ram is None or gpu is None
                    else "insufficient_free_memory")
        assert runtime.execution_fallback_reason == expected
        free[:] = 4096, 4096
        clock[0] = 10.0
        assert runtime.recognize([2]) == [_reading(2, "")]
        assert runtime.worker_count == 3 and len(runtime.children) == 3
    finally:
        runtime.close()


@pytest.mark.parametrize("failure,reason", [(RuntimeError("gpu_oom"), "gpu_oom"),
                                          (MemoryError(), "insufficient_free_memory")])
def test_failed_expansion_keeps_healthy_predictor_and_throttles_retry(
    host_module, monkeypatch, failure, reason,
):
    clock, free, children = [0.0], [1000], []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        if len(children) == 3:
            child.ready = lambda: (_ for _ in ()).throw(failure)
        return child

    monkeypatch.setattr(host_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host_module, "_available_gpu_mb", lambda _device: free[0])
    monkeypatch.setattr(host_module, "ChildPredictor", factory)
    runtime = host_module._paddle_runtime("fast", "gpu:0", 12)
    try:
        original = children[0]
        free[0], clock[0] = 8192, 5.0
        assert runtime.recognize([1, 2]) == [_reading(i, "") for i in (1, 2)]
        assert original.close_calls == [] and runtime.children == [original]
        assert all(child.close_calls == [True] for child in children[1:])
        assert runtime.worker_count == 1 and runtime.execution_fallback_reason == reason
        clock[0] = 64.9
        runtime.recognize([3])
        assert len(children) == 3
        clock[0] = 65.0
        runtime.recognize([4])
        assert runtime.worker_count == 3 and len(children) == 5
        assert runtime.execution_fallback_reason == ""
        assert original.requests == [([1, 2], ["", ""]), ([3], [""]), ([4], [""])]
    finally:
        runtime.close()


def test_semantic_expansion_failure_does_not_loop_or_lose_work(host_module, monkeypatch):
    clock, free, children = [0.0], [1000], []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        if len(children) == 2:
            child.ready = lambda: (_ for _ in ()).throw(RuntimeError("model_missing"))
        return child

    monkeypatch.setattr(host_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host_module, "_available_gpu_mb", lambda _device: free[0])
    monkeypatch.setattr(host_module, "ChildPredictor", factory)
    runtime = host_module._paddle_runtime("fast", "gpu:0", 12)
    try:
        free[0], clock[0] = 8192, 5.0
        assert runtime.recognize([1]) == [_reading(1, "")]
        clock[0] = 1000.0
        assert runtime.recognize([2]) == [_reading(2, "")]
        assert len(children) == 3 and runtime.worker_count == 1
        assert runtime.execution_fallback_reason == "model_missing"
        assert children[0].close_calls == []
    finally:
        runtime.close()


@pytest.mark.parametrize("device,workers", [("cpu", 3), ("gpu:0", 1)])
def test_explicit_serial_settings_never_probe_or_expand(host_module, monkeypatch, device, workers):
    def unexpected(*_args):
        pytest.fail("Explicit serial/CPU settings must not probe or expand")

    monkeypatch.setattr(host_module, "_available_ram_mb", unexpected)
    monkeypatch.setattr(host_module, "_available_gpu_mb", unexpected)
    monkeypatch.setattr(host_module, "ChildPredictor", unexpected)
    serial = SimpleNamespace(recognize_batched=lambda *_args: [])
    monkeypatch.setattr(host_module, "PaddleRuntime", lambda *_args: serial)
    assert host_module._paddle_runtime("fast", device, 12, workers) is serial
    assert serial.worker_count == 1 and serial.execution_fallback_reason == ""


def test_memory_recovery_does_not_reset_transport_recovery_budget(host_module, monkeypatch):
    clock, free, children = [0.0], [1000], []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        if len(children) == 1:
            child.recognize = lambda *_args: (_ for _ in ()).throw(TimeoutError())
        return child

    monkeypatch.setattr(host_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host_module, "ChildPredictor", factory)
    monkeypatch.setattr(host_module, "_available_gpu_mb", lambda _device: free[0])
    serial = SimpleNamespace(recognize_batched=lambda frames, bands: [
        _reading(f, b) for f, b in zip(frames, bands, strict=True)])
    monkeypatch.setattr(host_module, "PaddleRuntime", lambda *_args: serial)
    runtime = host_module.ParallelPaddleRuntime("fast", "gpu:0", 12, child_factory=factory)
    try:
        assert runtime.recognize([1, 2]) == [_reading(i, "") for i in (1, 2)]
        assert runtime.worker_count == 1 and runtime.execution_recovery_attempts == 1
        free[0], clock[0] = 8192, 5.0
        assert runtime.recognize([3]) == [_reading(3, "")]
        assert runtime.worker_count == 3 and len(children) == 6
        assert runtime.execution_recovery_attempts == 1
        children[3].recognize = lambda *_args: (_ for _ in ()).throw(TimeoutError())
        assert runtime.recognize([4]) == [_reading(4, "")]
        assert runtime._serial_runtime is serial and len(children) == 6
        clock[0] = 1000.0
        assert runtime.recognize([5]) == [_reading(5, "")]
        assert len(children) == 6
    finally:
        runtime.close()


def test_expansion_retry_backoff_is_bounded_and_never_keeps_more_than_three_models(
    host_module, monkeypatch,
):
    clock, free, children = [0.0], [1000], []

    def factory(*args):
        assert sum(not child.close_calls for child in children) < 3
        child = RecordingChild(*args)
        children.append(child)
        if len(children) > 1:
            child.ready = lambda: (_ for _ in ()).throw(TimeoutError())
        return child

    monkeypatch.setattr(host_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host_module, "ChildPredictor", factory)
    monkeypatch.setattr(host_module, "_available_gpu_mb", lambda _device: free[0])
    runtime = host_module._paddle_runtime("fast", "gpu:0", 12)
    try:
        free[0], clock[0] = 8192, 5.0
        for attempt, delay in enumerate((60, 120, 240, 300, 300), 1):
            assert runtime.recognize([attempt]) == [_reading(attempt, "")]
            assert runtime.worker_count == 1 and len(children) == 1 + 2 * attempt
            assert runtime._next_resource_probe == clock[0] + delay
            clock[0] += delay
        assert all(child.close_calls == [True] for child in children[1:])
        assert children[0].close_calls == [] and len(children[0].requests) == 5
    finally:
        runtime.close()


def test_empty_invalid_and_closed_requests_cannot_trigger_model_expansion(host_module, monkeypatch):
    clock, free, children = [0.0], [1000], []

    def factory(*args):
        child = RecordingChild(*args)
        children.append(child)
        return child

    monkeypatch.setattr(host_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host_module, "ChildPredictor", factory)
    monkeypatch.setattr(host_module, "_available_gpu_mb", lambda _device: free[0])
    runtime = host_module._paddle_runtime("fast", "gpu:0", 12)
    free[0], clock[0] = 8192, 1000.0
    assert runtime.recognize([]) == []
    with pytest.raises(ValueError, match="paddle_band_count_mismatch"):
        runtime.recognize_batched([1], [])
    runtime.close()
    with pytest.raises(RuntimeError, match="engine_not_open"):
        runtime.recognize([1])
    assert len(children) == 1 and children[0].requests == []
