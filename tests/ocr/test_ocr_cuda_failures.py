from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def host():
    path = Path(__file__).parents[2] / "src/ducai_studio/engine_hosts/ocr_engine_host.py"
    spec = importlib.util.spec_from_file_location("cuda_failure_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("message,code", [
    ("CUDA error(35), CUDA driver version is insufficient for CUDA runtime version",
     "ocr_cuda_driver_incompatible"),
    ("CUDA error(209), no kernel image is available for execution on the device",
     "ocr_cuda_kernel_unsupported"),
    ("CUDA error(719), unspecified launch failure", "ocr_cuda_launch_failed"),
    ("CUDA error(700), an illegal memory access was encountered", "ocr_cuda_launch_failed"),
    ("Out of memory error on GPU 0", "ocr_cuda_out_of_memory"),
    ("Could not load cudnn_ops64_9.dll or one of its dependencies",
     "ocr_cuda_library_missing"),
    ("[WinError 126] Error loading cublas64_12.dll", "ocr_cuda_library_missing"),
    ("ocr_cuda_blackwell_runtime_incompatible", "ocr_cuda_blackwell_runtime_incompatible"),
    ("paddle_result_count_mismatch", "paddle_result_count_mismatch"),
    ("unknown error with api_key=DO_NOT_DISCLOSE at C:/private/project.mp4", "RuntimeError"),
    ("private_token_with_no_spaces", "RuntimeError"),
])
def test_known_cuda_cause_survives_without_persisting_raw_messages(host, message, code):
    assert host._error_code(RuntimeError(message)) == code


def test_cuda_error_category_survives_predictor_and_engine_boundaries(host):
    class FailedHost:
        def open(self, _payload):
            child_error = host._error_code(RuntimeError(
                "CUDA error(209), no kernel image is available; api_key=DO_NOT_DISCLOSE"
            ))
            raise RuntimeError(child_error)

        def close(self):
            pass

    output = io.StringIO()
    host.serve(io.StringIO(json.dumps({"protocol_version": 1, "id": "failure",
                                     "action": "open", "payload": {}})), output, FailedHost())
    assert json.loads(output.getvalue())["error"] == {"code": "ocr_cuda_kernel_unsupported"}
    assert "DO_NOT_DISCLOSE" not in output.getvalue()


@pytest.mark.parametrize("capability,cuda,device,error", [
    ((12, 0), "12.6", "gpu:0", "ocr_cuda_blackwell_runtime_incompatible"),
    ((12, 0), "12.8", "gpu:0", "ocr_cuda_blackwell_runtime_incompatible"),
    ((12, 0), "12.9", "gpu:0", None),
    ((12, 0), "13.0", "gpu:0", None),
    ((8, 6), "12.6", "gpu:0", None),
    ((12, 0), "12.6", "cpu", None),
])
def test_blackwell_guard_does_not_change_other_gpus_or_cpu(host, capability, cuda, device, error):
    paddle = SimpleNamespace(is_compiled_with_cuda=lambda: True,
        version=SimpleNamespace(cuda=lambda: cuda), device=SimpleNamespace(cuda=SimpleNamespace(
            device_count=lambda: 1, get_device_capability=lambda _index: capability)))
    if error:
        with pytest.raises(RuntimeError, match=error):
            host._validate_paddle_gpu(paddle, device)
    else:
        host._validate_paddle_gpu(paddle, device)


def test_requested_gpu_rejects_cpu_only_runtime_and_missing_device(host):
    paddle = SimpleNamespace(is_compiled_with_cuda=lambda: False)
    with pytest.raises(RuntimeError, match="ocr_cuda_cpu_only"):
        host._validate_paddle_gpu(paddle, "gpu:0")
    paddle.is_compiled_with_cuda = lambda: True
    paddle.device = SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: 0))
    with pytest.raises(RuntimeError, match="ocr_cuda_device_unavailable"):
        host._validate_paddle_gpu(paddle, "gpu:0")
