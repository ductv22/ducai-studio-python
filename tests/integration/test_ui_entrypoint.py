from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QImageReader
from PySide6.QtNetwork import QLocalSocket

from ducai_studio.infrastructure.database import Database
from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
    encode_frame,
)
from ducai_studio.infrastructure.job_repository import JobRepository
from ducai_studio.infrastructure.project_repository import ProjectRepository

PROJECT_ROOT = Path(__file__).parents[2]
SMOKE_BOOTSTRAP = PROJECT_ROOT / "tests" / "fixtures" / "ui-smoke-bootstrap"
BUNDLED_FONT_ROOT = PROJECT_ROOT / "src" / "ducai_studio" / "ui" / "assets" / "fonts"


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.fail("PowerShell is required for the Windows process-tree smoke test")
    return executable


def _job_manager_child(parent_pid: int, timeout: float = 5) -> tuple[int, int, str]:
    script = f"""
$deadline = [DateTime]::UtcNow.AddSeconds({timeout})
do {{
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $known = @({parent_pid})
    do {{
        $descendants = @(
            $processes |
                Where-Object {{
                    $known -contains [int]$_.ParentProcessId -and
                    $known -notcontains [int]$_.ProcessId
                }}
        )
        $newIds = @($descendants | ForEach-Object {{ [int]$_.ProcessId }})
        $previousCount = $known.Count
        $known = @($known + $newIds | Select-Object -Unique)
    }} while ($known.Count -gt $previousCount)
    $child = $processes |
        Where-Object {{
            $known -contains [int]$_.ProcessId -and
            $_.CommandLine -match 'ducai_studio\\.job_manager'
        }} |
        Select-Object -First 1 ProcessId, ParentProcessId, CommandLine
    if ($null -ne $child) {{
        $child | ConvertTo-Json -Compress
        exit 0
    }}
    Start-Sleep -Milliseconds 50
}} while ([DateTime]::UtcNow -lt $deadline)
exit 1
"""
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout + 2,
        check=False,
    )
    assert result.returncode == 0, (
        "The UI did not start its Job Manager child process\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    child = json.loads(result.stdout)
    return (
        int(child["ProcessId"]),
        int(child["ParentProcessId"]),
        str(child["CommandLine"]),
    )


def _server_name(command_line: str) -> str:
    match = re.search(r'--server-name\s+(?:"([^"]+)"|(\S+))', command_line)
    assert match is not None, f"Missing --server-name in Job Manager command: {command_line}"
    return match.group(1) or match.group(2)


def _ping_job_manager(server_name: str) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    socket = QLocalSocket()
    decoder = FrameDecoder()
    try:
        connect_deadline = time.monotonic() + 5
        while (
            time.monotonic() < connect_deadline
            and socket.state() != QLocalSocket.LocalSocketState.ConnectedState
        ):
            socket.abort()
            socket.connectToServer(server_name)
            socket.waitForConnected(250)
        assert (
            socket.state() == QLocalSocket.LocalSocketState.ConnectedState
        ), socket.errorString()
        request = IpcMessage(
            protocol_version=IPC_PROTOCOL_VERSION,
            message_id="ui-smoke-ping",
            kind="request",
            payload={"action": "ping", "params": {}},
        )
        socket.write(encode_frame(request))
        assert socket.waitForBytesWritten(2_000), socket.errorString()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if socket.bytesAvailable() or socket.waitForReadyRead(250):
                responses = decoder.feed(bytes(socket.readAll()))
                if responses:
                    response = responses[0]
                    assert response.payload == {
                        "request_id": "ui-smoke-ping",
                        "ok": True,
                        "result": {"status": "ready"},
                    }
                    return
        raise AssertionError("Timed out waiting for the Job Manager ready response")
    finally:
        socket.abort()


def _request_job_manager(
    server_name: str, message_id: str, action: str, params: dict
) -> IpcMessage:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    socket = QLocalSocket()
    decoder = FrameDecoder()
    try:
        socket.connectToServer(server_name)
        assert socket.waitForConnected(5_000), socket.errorString()
        request = IpcMessage(
            protocol_version=IPC_PROTOCOL_VERSION,
            message_id=message_id,
            kind="request",
            payload={"action": action, "params": params},
        )
        socket.write(encode_frame(request))
        assert socket.waitForBytesWritten(2_000), socket.errorString()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if socket.bytesAvailable() or socket.waitForReadyRead(250):
                responses = decoder.feed(bytes(socket.readAll()))
                if responses:
                    return responses[0]
        raise AssertionError(f"Timed out waiting for Job Manager action: {action}")
    finally:
        socket.abort()


def _wait_job_manager_state(
    server_name: str, job_id: str, expected: str, timeout: float = 5
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _request_job_manager(
            server_name,
            f"wait-{time.monotonic_ns()}",
            "get_job",
            {"job_id": job_id},
        )
        if response.payload["result"]["state"] == expected:
            return
    raise AssertionError(f"Job {job_id} did not reach {expected}")


if sys.platform == "win32":
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PROCESS_TERMINATE = 0x0001

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


def _process_parent_id(process_id: int) -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            if entry.th32ProcessID == process_id:
                return int(entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return None
    finally:
        kernel32.CloseHandle(snapshot)


def _wait_for_process_exit(process_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_parent_id(process_id) is None:
            return True
        time.sleep(0.05)
    return _process_parent_id(process_id) is None


def _terminate_owned_child(process_id: int, parent_pid: int) -> None:
    if _process_parent_id(process_id) != parent_pid:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, process_id)
    if handle:
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)


def _ui_environment(data_root: Path, *, exit_ms: int) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DUCAI_SMOKE_EXIT_MS": str(exit_ms),
            "DUCAI_TEST_DATA_ROOT": str(data_root),
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (str(SMOKE_BOOTSTRAP), environment.get("PYTHONPATH"))
                if part
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QPA_FONTDIR": str(BUNDLED_FONT_ROOT),
            "QT_QUICK_BACKEND": "software",
        }
    )
    return environment


@pytest.mark.skipif(sys.platform != "win32", reason="DUCAI STUDIO is a Windows desktop app")
def test_offscreen_entrypoint_reaches_ready_and_stops_without_warnings(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "app-data"

    environment = _ui_environment(data_root, exit_ms=5000)
    screenshot = tmp_path / "ui-shell.png"
    environment.update(
        {
            "DUCAI_SMOKE_NAVIGATION_CYCLES": "3",
            "DUCAI_SMOKE_SCREENSHOT": str(screenshot),
            "DUCAI_SMOKE_SIZE": "1180x720",
            "QT_SCALE_FACTOR": "1.25",
        }
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio"],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    child_pid: int | None = None
    child_parent_pid: int | None = None
    try:
        child_pid, child_parent_pid, command_line = _job_manager_child(process.pid)
        _ping_job_manager(_server_name(command_line))
        _stdout, stderr = process.communicate(timeout=12)

        assert process.returncode == 0, f"UI exited with {process.returncode}\n{stderr}"
        assert stderr == "", f"QML or teardown warning:\n{stderr}"
        assert (data_root / "state.db").is_file()
        image_size = QImageReader(str(screenshot)).size()
        assert image_size.width() >= 1180
        assert image_size.height() >= 720
        assert _wait_for_process_exit(child_pid, 2), (
            f"Job Manager process {child_pid} survived UI process {process.pid}"
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if child_pid is not None and child_parent_pid is not None:
            _terminate_owned_child(child_pid, child_parent_pid)


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="The Windows media tools are required",
)
def test_ui_import_command_creates_project_and_auto_timeline_tracks(tmp_path: Path) -> None:
    media = tmp_path / "video nhập thử.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "0.3",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-y",
            str(media),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    data_root = tmp_path / "import-data"
    environment = _ui_environment(data_root, exit_ms=5000)
    environment["DUCAI_SMOKE_IMPORT_PATH"] = QUrl.fromLocalFile(str(media)).toString()
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio"],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    child_pid: int | None = None
    child_parent_pid: int | None = None
    try:
        child_pid, child_parent_pid, _command_line = _job_manager_child(process.pid)
        _stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 0, stderr
        assert stderr == ""
        projects = ProjectRepository(Database(data_root / "state.db"))
        summaries = projects.list_summaries()
        assert len(summaries) == 1
        snapshot = projects.load(summaries[0]["id"])
        assert snapshot.name == media.stem
        assert [track.id for track in snapshot.tracks] == ["V1", "A1"]
        assert snapshot.tracks[0].clips[0].volume == 0.0
        assert snapshot.tracks[0].clips[0].link_group_id == (
            snapshot.tracks[1].clips[0].link_group_id
        )
        assert _wait_for_process_exit(child_pid, 2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child_pid is not None and child_parent_pid is not None:
            _terminate_owned_child(child_pid, child_parent_pid)


@pytest.mark.skipif(sys.platform != "win32", reason="DUCAI STUDIO is a Windows desktop app")
def test_repeated_window_close_waits_for_cooperative_shutdown(tmp_path: Path) -> None:
    data_root = tmp_path / "double-close-data"
    environment = _ui_environment(data_root, exit_ms=60_000)
    environment["DUCAI_SMOKE_DOUBLE_CLOSE_MS"] = "3000"
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio"],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    child_pid: int | None = None
    child_parent_pid: int | None = None
    try:
        child_pid, child_parent_pid, command_line = _job_manager_child(process.pid)
        server_name = _server_name(command_line)
        _ping_job_manager(server_name)
        created = _request_job_manager(
            server_name,
            "double-close-create",
            "create_job",
            {
                "kind": "ocr",
                "project_id": "double-close-project",
                "input_revision": 1,
                "provider_id": "fixture",
                "provider_version": "1",
                "model_id": "fixture",
                "input_fingerprint": "fixture",
                "settings_fingerprint": "fixture",
                "provider_config": {},
            },
        )
        assert created.payload["ok"] is True
        job_id = str(created.payload["result"]["id"])
        worker = PROJECT_ROOT / "tests" / "fixtures" / "checkpoint_worker.py"
        started = _request_job_manager(
            server_name,
            "double-close-start",
            "start_job",
            {
                "job_id": job_id,
                "program": sys.executable,
                "arguments": [str(worker), job_id],
            },
        )
        assert started.payload["result"]["state"] == "start_requested"
        _wait_job_manager_state(server_name, job_id, "running")
        _stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 0, f"UI exited with {process.returncode}\n{stderr}"
        assert stderr == "", f"QML or teardown warning:\n{stderr}"
        assert _wait_for_process_exit(child_pid, 2)
        jobs = JobRepository(Database(data_root / "state.db"))
        assert jobs.get(job_id).state.value == "interrupted"
        event_kinds = {event.kind for event in jobs.list_events(job_id)}
        assert {"checkpoint", "stopped"} <= event_kinds
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child_pid is not None and child_parent_pid is not None:
            _terminate_owned_child(child_pid, child_parent_pid)


@pytest.mark.skipif(sys.platform != "win32", reason="DUCAI STUDIO is a Windows desktop app")
def test_second_application_instance_is_rejected(tmp_path: Path) -> None:
    environment = _ui_environment(tmp_path / "single-instance-data", exit_ms=5000)
    first = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio"],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    child_pid: int | None = None
    child_parent_pid: int | None = None
    try:
        child_pid, child_parent_pid, _command_line = _job_manager_child(first.pid)
        second = subprocess.run(
            [sys.executable, "-m", "ducai_studio"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=8,
            check=False,
        )
        assert second.returncode == 3
        assert second.stderr == ""
        first.communicate(timeout=12)
        assert first.returncode == 0
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
        if child_pid is not None and child_parent_pid is not None:
            _terminate_owned_child(child_pid, child_parent_pid)


@pytest.mark.skipif(sys.platform != "win32", reason="DUCAI STUDIO is a Windows desktop app")
def test_job_manager_dies_when_gui_is_killed_abnormally(tmp_path: Path) -> None:
    environment = _ui_environment(tmp_path / "parent-death-data", exit_ms=60_000)
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio"],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    child_pid: int | None = None
    child_parent_pid: int | None = None
    try:
        child_pid, child_parent_pid, _command_line = _job_manager_child(process.pid)
        process.kill()
        process.wait(timeout=5)
        assert _wait_for_process_exit(child_pid, 3), (
            f"Job Manager {child_pid} survived abnormal GUI exit {process.pid}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child_pid is not None and child_parent_pid is not None:
            _terminate_owned_child(child_pid, child_parent_pid)
