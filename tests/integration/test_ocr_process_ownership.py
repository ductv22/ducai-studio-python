from __future__ import annotations

import ctypes
import importlib.util
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from ducai_studio.workers.ocr_worker import EngineHostClient


def _active(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows launch and process-tree contract")
@pytest.mark.parametrize("stall", [False, True])
def test_host_guard_stops_descendants_after_graceful_or_forced_shutdown(monkeypatch, stall):
    monkeypatch.setenv("DUCAI_LIFECYCLE_STALL", "1" if stall else "0")
    probe = Path(__file__).parents[1] / "fixtures/ocr-host-lifecycle-probe.py"
    client = EngineHostClient(Path(sys.executable), probe)
    pids = []
    try:
        result = client.request("health", {})
        pids = [result["pid"], result["child_pid"], client.process.pid]
        assert result["console"] == 0
        assert all(_active(pid) for pid in pids)
        started = time.monotonic()
        client.close()
        assert time.monotonic() - started < 10
        deadline = time.monotonic() + 3
        while any(_active(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not any(_active(pid) for pid in pids)
        assert client._process_tree is None and client.process is None
        client.close()
    finally:
        client.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows venv launcher tree contract")
def test_predictor_force_close_stops_actual_venv_launcher_descendants():
    source = Path(__file__).parents[2] / "src/ducai_studio/engine_hosts/ocr_engine_host.py"
    spec = importlib.util.spec_from_file_location("ocr_owned_predictor", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    child = object.__new__(module.ChildPredictor)
    child.closed = False
    child.process = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import os,time; print(os.getpid(),flush=True); time.sleep(60)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        actual_pid = int(child.process.stdout.readline())
        assert _active(actual_pid)
        child.close(force=True)
        assert child.process.poll() is not None
        assert not _active(actual_pid)
        assert child.process.stdin.closed and child.process.stdout.closed
        child.close(force=True)
    finally:
        child.close(force=True)
        child.process.stderr.close()
