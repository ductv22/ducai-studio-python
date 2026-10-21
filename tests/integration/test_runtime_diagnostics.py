from __future__ import annotations

import ctypes
import io
import json
import os
import subprocess
import sys
import time
import venv
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

import ducai_studio.infrastructure.runtime_diagnostics as runtime_diagnostics
from ducai_studio.infrastructure.runtime_diagnostics import run_runtime_diagnostics


def mock_probe(monkeypatch, response, calls, *, assignment_error=None):
    class Guard:
        def __enter__(self):
            return self

        def assign_pid(self, pid):
            calls.append(("assign", pid))
            if assignment_error is not None:
                raise assignment_error

        def close(self):
            calls.append(("close", None))

        def __exit__(self, *_args):
            self.close()

    class Process:
        pid = 12345
        instances = []

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
            self.returncode = None
            self.instances.append(self)

        def communicate(self, gate, timeout):
            calls.append(("communicate", (gate, timeout)))
            item = response.pop(0) if isinstance(response, list) else response
            if isinstance(item, Exception):
                raise item
            self.returncode = item.returncode
            return item.stdout.encode(), b""

        def poll(self):
            return self.returncode

        def kill(self):
            calls.append(("kill", None))
            self.returncode = -1

        def wait(self, timeout):
            calls.append(("wait", timeout))
            return self.returncode

    monkeypatch.setattr(runtime_diagnostics, "ProcessTreeGuard", Guard)
    monkeypatch.setattr(runtime_diagnostics.subprocess, "Popen", Process)
    return Process


def test_runtime_diagnostics_reports_every_managed_boundary_without_secrets(
    tmp_path: Path,
) -> None:
    report = run_runtime_diagnostics(
        data_root=tmp_path,
        executable_resolver=lambda _name: None,
    )
    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    components = {item.component for item in report.checks}

    assert report.schema == "ducai-runtime-diagnostics-v1"
    assert components == {
        "gui-python",
        "ffmpeg",
        "paddle-ocr",
        "whisper",
        "gemini-web",
        "gemini-cli",
        "piper",
        "vieneu-3.8",
    }
    assert report.overall_status == "blocked"
    assert "api_key" not in serialized.casefold()
    assert "token=" not in serialized.casefold()


def test_whisper_diagnostics_rejects_wrong_ctranslate2_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "whisper_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    model = tmp_path / "model_cache" / "whisper" / "large-v3"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "whisper_runtime" / "ducai_runtime.json").write_text(
        json.dumps({"model_path": "model_cache/whisper/large-v3"}),
        encoding="utf-8",
    )

    versions = {"faster-whisper": "1.2.1", "ctranslate2": "4.7.0"}
    monkeypatch.setattr(
        runtime_diagnostics,
        "_python_package_version",
        lambda _runtime, package: versions.get(package, ""),
    )

    result = runtime_diagnostics._check_whisper_runtime(tmp_path)

    assert result.status == "blocked"
    assert "CTranslate2 4.8.1" in result.summary


@pytest.mark.parametrize("state,status,fragment", [
    ("cuda_ready", "ready", "Paddle CUDA sẵn sàng"),
    ("cpu_only", "degraded", "chỉ có CPU"),
    ("cuda_unavailable", "degraded", "chưa thấy GPU CUDA"),
    ("cuda_failed", "degraded", "phép thử CUDA lỗi"),
    ("probe_unavailable", "degraded", "chưa xác minh được CUDA"),
])
def test_paddle_presence_is_not_misreported_as_working_cuda(
    tmp_path, monkeypatch, state, status, fragment,
):
    runtime = tmp_path / "paddle_runtime/.venv/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    monkeypatch.delenv("DUCAI_PADDLE_PYTHON", raising=False)
    monkeypatch.setattr(runtime_diagnostics, "_python_package_version", lambda *_: "3.2.2")
    monkeypatch.setattr(runtime_diagnostics, "_paddle_cuda_probe",
                        lambda _runtime: {"state": state, "gpu_name": "RTX 5060 Ti"})
    result = runtime_diagnostics._check_paddle_runtime(tmp_path)
    assert result.status == status and fragment in result.summary
    assert result.details["state"] == state


def test_cuda_probe_is_hidden_isolated_and_returns_only_diagnostic_fields(
    tmp_path, monkeypatch,
):
    runtime = tmp_path / "Scripts/python.exe"
    monkeypatch.setenv("PYTHONHOME", "host-python")
    calls = []

    response = SimpleNamespace(returncode=0, stdout='unrelated startup output\n'
            'DUCAI_CUDA_PROBE={"state":"cuda_ready","gpu_name":"RTX 5060 Ti",'
            '"compute_capability":[12,0],"unknown":"discard"}\n')
    mock_probe(monkeypatch, response, calls)
    result = runtime_diagnostics._paddle_cuda_probe(runtime)
    assert result == {"state": "cuda_ready", "gpu_name": "RTX 5060 Ti",
                      "compute_capability": [12, 0]}
    command, kwargs = calls[0]
    assert command[1:] == ["-m", "ducai_studio.infrastructure.runtime_diagnostics", str(runtime)]
    assert "conv2d" in runtime_diagnostics._PADDLE_CUDA_PROBE
    assert "matmul" in runtime_diagnostics._PADDLE_CUDA_PROBE
    assert "PYTHONHOME" not in kwargs["env"]
    assert kwargs["cwd"] == runtime.parent.resolve()
    assert calls[1:3] == [("assign", 12345), ("communicate", (b"DUCAI_LAUNCH\n", 45))]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize("response,reason,details", [
    (SimpleNamespace(returncode=1, stdout='DUCAI_CUDA_PROBE={"state":"cuda_ready"}'),
     "probe_exited", {"return_code": 1}),
    (SimpleNamespace(returncode=0, stdout="not json"), "probe_invalid_output", {}),
    (SimpleNamespace(returncode=0, stdout="DUCAI_CUDA_PROBE=[]"), "probe_invalid_output", {}),
    (SimpleNamespace(returncode=0, stdout='DUCAI_CUDA_PROBE={"state":"fake"}'),
     "probe_invalid_output", {}),
    (subprocess.TimeoutExpired("private-runtime-path", 45), "probe_timeout",
     {"timeout_seconds": 45}),
    (OSError("private-token-do-not-disclose"), "probe_process_failed", {}),
])
def test_failed_or_unreadable_cuda_probe_does_not_claim_gpu_is_ready(
    tmp_path, monkeypatch, response, reason, details,
):
    calls = []
    mock_probe(monkeypatch, response, calls)
    assert runtime_diagnostics._paddle_cuda_probe(tmp_path / "python.exe") == {
        "state": "probe_unavailable", "probe_reason": reason, **details,
    }
    if isinstance(response, Exception):
        assert calls.index(("close", None)) < calls.index(("kill", None))
        assert ("wait", 5) in calls


@pytest.mark.skipif(os.name != "nt", reason="Windows venv launcher timeout contract")
def test_timed_out_cuda_probe_stops_real_interpreter_descendants(tmp_path, monkeypatch):
    receipt = tmp_path / "probe-pid.txt"
    root = tmp_path / "slow-runtime"
    venv.EnvBuilder(with_pip=False).create(root)
    code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(receipt)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    # Only this disposable runtime's import stalls. The shipped probe stays
    # fixed and cannot be replaced by an environment-supplied Python command.
    (root / "Lib/site-packages/paddle.py").write_text(code, encoding="utf-8")
    monkeypatch.setattr(runtime_diagnostics, "_PADDLE_CUDA_PROBE_TIMEOUT_SECONDS", 2)
    assert runtime_diagnostics._paddle_cuda_probe(root / "Scripts/python.exe") == {
        "state": "probe_unavailable", "probe_reason": "probe_timeout", "timeout_seconds": 2,
    }
    assert receipt.is_file(), "The actual runtime must start before testing its cleanup"
    pid = int(receipt.read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 2000) == 0
        finally:
            kernel.CloseHandle(handle)


@pytest.mark.parametrize("arguments", [[], ["missing.exe"], ["python.exe", "-c", "custom"]])
def test_diagnostic_launcher_rejects_missing_runtime_or_custom_commands(arguments):
    assert runtime_diagnostics.main(arguments) == 64


def test_diagnostic_launcher_accepts_only_the_fixed_probe_after_owned_gate(tmp_path, monkeypatch):
    runtime = tmp_path / "python.exe"
    runtime.touch()
    calls = []
    gate = iter(runtime_diagnostics._PROBE_LAUNCH_GATE)
    monkeypatch.setattr(runtime_diagnostics.sys, "stdin",
                        SimpleNamespace(fileno=lambda: 0, buffer=object()))
    monkeypatch.setattr(runtime_diagnostics.os, "read", lambda *_args: bytes([next(gate)]))
    monkeypatch.setenv("DUCAI_WORKER_ARGUMENTS_JSON", '["-c", "custom"]')

    def launch(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(wait=lambda: 17)

    monkeypatch.setattr(runtime_diagnostics.subprocess, "Popen", launch)
    assert runtime_diagnostics.main([str(runtime)]) == 17
    command, kwargs = calls[0]
    assert command[:3] == [str(runtime), "-c", runtime_diagnostics._PADDLE_CUDA_PROBE]
    assert Path(command[3]).name == "ocr_engine_host.py"
    assert "DUCAI_WORKER_ARGUMENTS_JSON" not in command[3]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_diagnostic_launcher_does_not_start_runtime_without_exact_owned_gate(tmp_path, monkeypatch):
    runtime = tmp_path / "python.exe"
    runtime.touch()
    monkeypatch.setattr(runtime_diagnostics.sys, "stdin", SimpleNamespace(fileno=lambda: 0))
    monkeypatch.setattr(runtime_diagnostics.os, "read", lambda *_args: b"")
    assert runtime_diagnostics.main([str(runtime)]) == 65


def test_diagnostics_explains_known_blackwell_runtime_mismatch(tmp_path, monkeypatch):
    runtime = tmp_path / "paddle_runtime/.venv/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    monkeypatch.delenv("DUCAI_PADDLE_PYTHON", raising=False)
    monkeypatch.setattr(runtime_diagnostics, "_python_package_version", lambda *_: "3.2.2")
    monkeypatch.setattr(runtime_diagnostics, "_paddle_cuda_probe", lambda _runtime: {
        "state": "cuda_failed", "gpu_name": "RTX 5060 Ti", "compute_capability": [12, 0],
        "cuda_version": "12.6", "error_code": "ocr_cuda_blackwell_runtime_incompatible",
    })
    result = runtime_diagnostics._check_paddle_runtime(tmp_path)
    assert result.status == "degraded"
    assert "CUDA 12.9" in result.summary
    assert result.details["error_code"] == "ocr_cuda_blackwell_runtime_incompatible"


@pytest.mark.parametrize("probe,fragment", [
    ({"probe_reason": "probe_timeout", "timeout_seconds": 45}, "quá 45 giây"),
    ({"probe_reason": "probe_process_failed"}, "Không chạy được"),
    ({"probe_reason": "probe_exited", "return_code": -1073741515}, "0xC0000135"),
    ({"probe_reason": "probe_invalid_output"}, "Không đọc được"),
])
def test_probe_failure_summary_is_actionable_without_guessing_driver(
    tmp_path, monkeypatch, probe, fragment,
):
    runtime = tmp_path / "paddle_runtime/.venv/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    monkeypatch.delenv("DUCAI_PADDLE_PYTHON", raising=False)
    monkeypatch.setattr(runtime_diagnostics, "_python_package_version", lambda *_: "3.2.2")
    monkeypatch.setattr(runtime_diagnostics, "_paddle_cuda_probe", lambda _runtime: {
        "state": "probe_unavailable", **probe,
    })
    result = runtime_diagnostics._check_paddle_runtime(tmp_path)
    assert result.status == "degraded" and fragment in result.summary
    assert result.details["probe_reason"] == probe["probe_reason"]
    assert "driver" not in result.summary.casefold()


def test_isolated_diagnostics_round_trip_uses_fixed_report_in_real_child(tmp_path, monkeypatch):
    monkeypatch.delenv("DUCAI_PADDLE_PYTHON", raising=False)
    monkeypatch.setenv("PATH", "")
    result = runtime_diagnostics.isolated_runtime_diagnostics(
        data_root=str(tmp_path), cancelled=lambda: False)
    assert result["schema"] == "ducai-runtime-diagnostics-v1"
    assert {item["component"] for item in result["checks"]} == {
        "gui-python", "ffmpeg", "paddle-ocr", "whisper", "gemini-web",
        "gemini-cli", "piper", "vieneu-3.8"}


def test_isolated_diagnostics_cancel_closes_owned_tree_and_pipes(tmp_path, monkeypatch):
    calls = []
    mock_probe(monkeypatch, subprocess.TimeoutExpired("probe", 0.2), calls)
    cancelled = iter((False, False, True))
    result = runtime_diagnostics.isolated_runtime_diagnostics(
        data_root=str(tmp_path), cancelled=lambda: next(cancelled))
    assert result is None
    command, kwargs = calls[0]
    assert command[1:] == ["-m", "ducai_studio.infrastructure.runtime_diagnostics",
                           "--report", str(tmp_path)]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert calls[1:3] == [("assign", 12345), ("communicate", (b"DUCAI_LAUNCH\n", 0.2))]
    assert ("close", None) in calls and ("kill", None) in calls and ("wait", 5) in calls


def test_isolated_diagnostics_deadline_closes_tree_without_claiming_cuda_failed(
    tmp_path, monkeypatch,
):
    calls = []
    process_type = mock_probe(monkeypatch, subprocess.TimeoutExpired("probe", 0.2), calls)
    clock = iter((10.0, 10.0, 310.0))
    monkeypatch.setattr(runtime_diagnostics, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="Chẩn đoán quá thời gian"):
        runtime_diagnostics.isolated_runtime_diagnostics(
            data_root=str(tmp_path), cancelled=lambda: False)
    assert ("kill", None) in calls and ("wait", 5) in calls
    assert all(stream.closed for stream in vars(process_type.instances[0]).values()
               if isinstance(stream, io.BytesIO))


def test_isolated_diagnostics_sends_launch_gate_only_once_across_communicate_retries(
    tmp_path, monkeypatch,
):
    report = {"schema": "ducai-runtime-diagnostics-v1", "checks": []}
    calls = []
    mock_probe(monkeypatch, [subprocess.TimeoutExpired("probe", 0.2),
        SimpleNamespace(returncode=0, stdout="startup\nDUCAI_DIAGNOSTICS=" + json.dumps(report))],
        calls)
    assert runtime_diagnostics.isolated_runtime_diagnostics(
        data_root=str(tmp_path), cancelled=lambda: False) == report
    assert [value for name, value in calls if name == "communicate"] == [
        (b"DUCAI_LAUNCH\n", 0.2), (None, 0.2)]


@pytest.mark.parametrize("returncode,output,error", [
    (1, 'DUCAI_DIAGNOSTICS={"schema":"ducai-runtime-diagnostics-v1"}', RuntimeError),
    (0, "private runtime error", ValueError),
    (0, "DUCAI_DIAGNOSTICS={", ValueError),
    (0, "DUCAI_DIAGNOSTICS=[]", ValueError),
    (0, 'DUCAI_DIAGNOSTICS={"schema":"other"}', ValueError),
])
def test_isolated_diagnostics_rejects_failed_or_invalid_reports_and_closes_pipes(
    tmp_path, monkeypatch, returncode, output, error,
):
    calls = []
    process_type = mock_probe(monkeypatch, SimpleNamespace(returncode=returncode, stdout=output),
                              calls)
    with pytest.raises(error):
        runtime_diagnostics.isolated_runtime_diagnostics(
            data_root=str(tmp_path), cancelled=lambda: False)
    assert ("close", None) in calls
    assert all(stream.closed for stream in vars(process_type.instances[0]).values()
               if isinstance(stream, io.BytesIO))


def test_isolated_diagnostics_assignment_failure_never_releases_child(tmp_path, monkeypatch):
    calls = []
    process_type = mock_probe(monkeypatch, None, calls, assignment_error=OSError("assign"))
    with pytest.raises(OSError, match="assign"):
        runtime_diagnostics.isolated_runtime_diagnostics(
            data_root=str(tmp_path), cancelled=lambda: False)
    assert not any(name == "communicate" for name, _ in calls)
    assert ("kill", None) in calls and ("wait", 5) in calls
    assert all(stream.closed for stream in vars(process_type.instances[0]).values()
               if isinstance(stream, io.BytesIO))


@pytest.mark.skipif(os.name != "nt", reason="Windows process tree ownership")
def test_cancelled_report_stops_real_probe_descendants_but_not_unrelated_process(
    tmp_path, monkeypatch,
):
    runtime = tmp_path / "paddle_runtime/.venv"
    venv.EnvBuilder(with_pip=False).create(runtime)
    packages = runtime / "Lib/site-packages"
    for name in ("paddleocr", "paddlepaddle"):
        metadata = packages / f"{name}-1.0.dist-info/METADATA"
        metadata.parent.mkdir()
        metadata.write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    receipt = tmp_path / "probe.pid"
    descendant = tmp_path / "descendant.pid"
    child_code = ("import os,time; from pathlib import Path; "
                  f"Path({str(descendant)!r}).write_text(str(os.getpid())); time.sleep(60)")
    (packages / "paddle.py").write_text(
        "import os,sys,time,subprocess\nfrom pathlib import Path\n"
        f"Path({str(receipt)!r}).write_text(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], creationflags=0x08000000)\n"
        "time.sleep(60)\n", encoding="utf-8")
    monkeypatch.delenv("DUCAI_PADDLE_PYTHON", raising=False)
    monkeypatch.setenv("PATH", "")
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 15
        result = runtime_diagnostics.isolated_runtime_diagnostics(data_root=str(tmp_path),
            cancelled=lambda: (receipt.is_file() and descendant.is_file())
                              or time.monotonic() > deadline)
        assert result is None and receipt.is_file() and descendant.is_file()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for path in (receipt, descendant):
            handle = kernel.OpenProcess(0x100000, False, int(path.read_text()))
            if handle:
                try:
                    assert kernel.WaitForSingleObject(handle, 2000) == 0
                finally:
                    kernel.CloseHandle(handle)
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)
