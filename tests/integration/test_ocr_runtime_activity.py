from __future__ import annotations

from types import SimpleNamespace

import pytest

from ducai_studio.workers.ocr_worker import OcrWorker


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    return OcrWorker("runtime-activity")


@pytest.mark.parametrize("phase", ["fast", "adaptive_ocr", "quality", "scene_quality"])
def test_gpu_execution_details_stay_visible_on_each_scanning_progress(worker, capsys, phase):
    worker._engine_mode = "fast"
    worker._remember_ocr_execution({"execution_device": "gpu:0", "worker_count": 3})
    original = {"phase": phase, "current": 10, "total": 100}
    message = worker.send("progress", original)
    assert message.payload["execution_device"] == "gpu:0"
    assert message.payload["execution_workers"] == 3
    assert message.payload["phase_detail"] == "GPU · 3 tiến trình nhận chữ"
    assert "execution_device" not in original
    capsys.readouterr()


def test_compatibility_cpu_warning_does_not_disappear_after_one_event(worker, capsys):
    worker._engine_mode = "compatibility"
    worker._ocr_fallback_reason = "ocr_engine_failed"
    worker._remember_ocr_execution({"execution_device": "cpu", "worker_count": 1})
    for current in (0, 12, 24):
        message = worker.send("progress", {"phase": "fast", "current": current, "total": 100})
        assert message.payload["execution_device"] == "cpu"
        assert message.payload["phase_detail"] == (
            "CPU · OCR tương thích · Paddle lỗi: ocr_engine_failed"
        )
    capsys.readouterr()


def test_explicit_cpu_and_single_gpu_are_not_labelled_as_three_gpu_workers(worker, capsys):
    for device, label in (("cpu", "CPU"), ("gpu:0", "GPU")):
        worker._engine_mode = "fast"
        worker._remember_ocr_execution({"execution_device": device, "worker_count": 1})
        message = worker.send("progress", {"phase": "fast", "current": 1, "total": 2})
        assert message.payload["phase_detail"] == label + " · 1 tiến trình nhận chữ"
    capsys.readouterr()


@pytest.mark.parametrize("metadata", [{}, {"execution_device": "gpu:0", "worker_count": 7},
                                      {"execution_device": 0}, {"execution_device": ""}])
def test_legacy_or_invalid_metadata_does_not_invent_execution_telemetry(worker, capsys, metadata):
    worker._remember_ocr_execution(metadata)
    message = worker.send("progress", {"phase": "fast", "current": 1, "total": 2})
    assert "phase_detail" not in message.payload and "execution_device" not in message.payload
    capsys.readouterr()


def test_finalization_whisper_and_explicit_phase_details_are_preserved(worker, capsys):
    worker._engine_mode = "fast"
    worker._remember_ocr_execution({"execution_device": "gpu:0", "worker_count": 3})
    for phase in ("finalize", "transcribe"):
        payload = {"phase": phase, "phase_detail": "Đối chiếu bằng chứng"}
        assert dict(worker.send("progress", payload).payload) == payload
    message = worker.send("progress", {"phase": "quality", "phase_detail": "Đọc lại vùng chữ"})
    assert message.payload["phase_detail"] == "Đọc lại vùng chữ"
    capsys.readouterr()


def test_recognition_refreshes_actual_worker_count_after_serial_fallback(worker, capsys):
    worker._engine_mode = "fast"
    worker._remember_ocr_execution({"execution_device": "gpu:0", "worker_count": 3})
    result = {"evidence": [], "execution_device": "gpu:0", "worker_count": 1}
    worker.engine = SimpleNamespace(request=lambda *_args: result)
    assert worker._recognize({"timestamps_us": [0]}, mode="fast") == result
    message = worker.send("progress", {"phase": "fast", "current": 1, "total": 2})
    assert message.payload["execution_workers"] == 1
    assert message.payload["phase_detail"] == "GPU · 1 tiến trình nhận chữ"
    capsys.readouterr()


def test_parallel_fallback_reason_survives_recognition_and_repeated_progress(worker, capsys):
    worker._engine_mode = "fast"
    worker._remember_ocr_execution({"execution_device": "gpu:0", "worker_count": 3})
    result = {"evidence": [], "execution_device": "gpu:0", "worker_count": 1,
              "execution_fallback_reason": "ocr_predictor_timeout",
              "execution_recovery_reason": "ocr_predictor_exited",
              "execution_recovery_attempts": 1}
    worker.engine = SimpleNamespace(request=lambda *_args: result)
    worker._recognize({"timestamps_us": [0]}, mode="fast")
    for phase in ("fast", "adaptive_ocr", "quality"):
        message = worker.send("progress", {"phase": phase, "current": 1, "total": 2})
        assert message.payload["execution_fallback_reason"] == "ocr_predictor_timeout"
        assert message.payload["execution_recovery_reason"] == "ocr_predictor_exited"
        assert "quá thời gian chờ" in message.payload["phase_detail"]
        assert message.payload["execution_recovery_attempts"] == 1
    capsys.readouterr()


def test_restored_parallel_pool_is_visible_without_stale_serial_warning(worker, capsys):
    worker._engine_mode = "fast"
    for metadata in ({"execution_device": "gpu:0", "worker_count": 1,
                      "execution_fallback_reason": "insufficient_free_memory"},
                     {"execution_device": "gpu:0", "worker_count": 3,
                      "execution_fallback_reason": "", "execution_recovery_attempts": 1,
                      "execution_recovery_reason": "ocr_predictor_timeout"}):
        worker._remember_ocr_execution(metadata)
    message = worker.send("progress", {"phase": "fast", "current": 1, "total": 2})
    assert "execution_fallback_reason" not in message.payload
    assert "đã khôi phục chạy song song" in message.payload["phase_detail"]
    capsys.readouterr()


def test_resource_promotion_updates_activity_without_inventing_fault_recovery(worker, capsys):
    worker._engine_mode = "fast"
    worker._remember_ocr_execution({"execution_device": "gpu:0", "worker_count": 1,
                                  "execution_fallback_reason": "insufficient_free_memory"})
    result = {"evidence": [], "execution_device": "gpu:0", "worker_count": 3,
              "execution_fallback_reason": "", "execution_recovery_attempts": 0,
              "execution_recovery_reason": ""}
    worker.engine = SimpleNamespace(request=lambda *_args: result)
    worker._recognize({"timestamps_us": [0]}, mode="fast")
    message = worker.send("progress", {"phase": "fast", "current": 1, "total": 2})
    assert message.payload["execution_workers"] == 3
    assert message.payload["phase_detail"] == "GPU · 3 tiến trình nhận chữ"
    assert "execution_fallback_reason" not in message.payload
    assert message.payload["execution_recovery_attempts"] == 0
    capsys.readouterr()


@pytest.mark.parametrize("reason", ["secret-api-key", "ocr_predictor_timeout /private/path", "",
                                  "C:\\Users\\private", None, ["ocr_predictor_timeout"]])
def test_parallel_metadata_never_persists_raw_framework_error(worker, capsys, reason):
    worker._engine_mode = "fast"
    worker._remember_ocr_execution({"execution_device": "gpu:0", "worker_count": 1,
        "execution_fallback_reason": reason, "execution_recovery_reason": reason,
        "execution_recovery_attempts": True})
    message = worker.send("progress", {"phase": "fast", "current": 1, "total": 2})
    for key in ("execution_fallback_reason", "execution_recovery_reason",
                "execution_recovery_attempts"):
        assert key not in message.payload
    capsys.readouterr()


def test_legacy_recognition_response_keeps_verified_open_metadata(worker, capsys):
    worker._engine_mode = "fast"
    worker._remember_ocr_execution({"execution_device": "gpu:0", "worker_count": 3})
    worker.engine = SimpleNamespace(request=lambda *_args: {"evidence": []})
    worker._recognize({"timestamps_us": [0]}, mode="fast")
    message = worker.send("progress", {"phase": "fast", "current": 1, "total": 2})
    assert message.payload["execution_workers"] == 3
    capsys.readouterr()


def test_legacy_compatibility_host_cannot_leave_a_gpu_label_visible(worker, capsys):
    worker._engine_mode = "compatibility"
    worker._remember_ocr_execution({})
    message = worker.send("progress", {"phase": "fast", "current": 1, "total": 2})
    assert message.payload["execution_device"] == "cpu"
    assert message.payload["phase_detail"] == "CPU · OCR tương thích"
    capsys.readouterr()


def test_compatibility_explains_driver_failure_without_leaking_raw_details(worker, capsys):
    worker._engine_mode = "compatibility"
    worker._ocr_fallback_reason = "ocr_cuda_driver_incompatible"
    worker._remember_ocr_execution({"execution_device": "cpu", "worker_count": 1})
    message = worker.send("progress", {"phase": "quality", "current": 1, "total": 2})
    assert message.payload["ocr_fallback_reason"] == "ocr_cuda_driver_incompatible"
    assert "driver NVIDIA" in message.payload["phase_detail"]
    capsys.readouterr()
