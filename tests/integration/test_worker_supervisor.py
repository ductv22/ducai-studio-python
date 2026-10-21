import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from ducai_studio.job_manager import worker_supervisor as supervisor_module
from ducai_studio.job_manager.worker_supervisor import WorkerSupervisor
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def test_progress_burst_coalesces_without_reordering_checkpoints_or_terminal():
    _application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    events = []
    supervisor.eventReceived.connect(lambda job, message: events.append((job, message)))
    for i in range(538):
        supervisor._deliver_event(
            "large-job", WorkerMessage(PROTOCOL_VERSION, "large-job", str(i), "progress",
                                       {"current": i * 16, "total": 8587})
        )
    assert events == []
    for kind in ("checkpoint", "paused", "resumed", "succeeded"):
        supervisor._deliver_event(
            "large-job", WorkerMessage(PROTOCOL_VERSION, "large-job", kind, kind, {})
        )
    assert [message.kind for _, message in events] == [
        "progress", "checkpoint", "paused", "resumed", "succeeded"
    ]
    assert events[0][1].payload["current"] == 537 * 16
    supervisor._flush_progress()
    assert len(events) == 5
    assert not supervisor._progress_timer.isActive()


def test_progress_is_flushed_within_timer_and_isolated_per_job():
    application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    events = []
    supervisor.eventReceived.connect(lambda job, message: events.append((job, message)))
    for job in ("one", "two"):
        supervisor._deliver_event(
            job, WorkerMessage(PROTOCOL_VERSION, job, job, "progress", {"current": 32})
        )
    assert pump_until(application, lambda: len(events) == 2, timeout=1)
    assert {job for job, _ in events} == {"one", "two"}
    assert not supervisor._progress_timer.isActive()


def process_is_active(process_id: int) -> bool:
    if sys.platform != "win32":
        return False
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, process_id)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def pump_until(application: QCoreApplication, predicate, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_supervisor_receives_protocol_and_stops_worker() -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    events: list[str] = []
    failures: list[str] = []
    finished: list[int] = []
    supervisor.eventReceived.connect(lambda _job, message: events.append(message.kind))
    supervisor.workerFailed.connect(lambda _job, error: failures.append(error))
    supervisor.workerFinished.connect(lambda _job, code: finished.append(code))
    fixture = Path(__file__).parents[1] / "fixtures" / "echo_worker.py"
    try:
        supervisor.start(job_id="worker-test", program=sys.executable, arguments=[str(fixture)])
        assert pump_until(application, lambda: "ready" in events)
        assert not failures
        supervisor.stop("worker-test")
        assert pump_until(application, lambda: bool(finished))
        assert "stopped" in events
    finally:
        supervisor.shutdown()


def test_supervisor_waits_for_cooperative_checkpoint_before_termination() -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    events: list[str] = []
    finished: list[int] = []
    supervisor.eventReceived.connect(lambda _job, message: events.append(message.kind))
    supervisor.workerFinished.connect(lambda _job, code: finished.append(code))
    fixture = Path(__file__).parents[1] / "fixtures" / "checkpoint_worker.py"
    try:
        supervisor.start(
            job_id="checkpoint-test",
            program=sys.executable,
            arguments=[str(fixture), "checkpoint-test"],
        )
        assert pump_until(application, lambda: "ready" in events)
        supervisor.stop("checkpoint-test")
        assert pump_until(application, lambda: bool(finished))
        assert events[-2:] == ["checkpoint", "stopped"]
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize("phase", ["grace", "kill"])
def test_stop_timer_cannot_terminate_a_retried_worker_with_same_job_id(monkeypatch, phase):
    application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    timers, events = [], []
    monkeypatch.setattr(supervisor_module.QTimer, "singleShot",
                        lambda delay, callback: timers.append(callback))
    supervisor.eventReceived.connect(lambda _job, message: events.append(message.kind))
    fixture = Path(__file__).parents[1] / "fixtures" / "control_worker.py"
    job_id = "reused-control-job"
    try:
        supervisor.start(job_id=job_id, program=sys.executable,
                         arguments=[str(fixture), job_id])
        assert pump_until(application, lambda: "ready" in events)
        if phase == "kill":
            supervisor._terminate(job_id)
            assert timers
            supervisor.force_shutdown()
        else:
            supervisor.request_stop(job_id)
            assert pump_until(application, lambda: not supervisor.has_worker(job_id))
        old_callback = timers[-1]
        events.clear()
        supervisor.start(job_id=job_id, program=sys.executable,
                         arguments=[str(fixture), job_id])
        assert pump_until(application, lambda: "ready" in events)
        timer_count = len(timers)
        old_callback()
        assert len(timers) == timer_count, "Old stop scheduled termination of the new worker"
        assert not pump_until(application, lambda: not supervisor.has_worker(job_id), timeout=0.2)
        supervisor.send(job_id, kind="control", payload={"action": "start", "command_id": "new"})
        assert pump_until(application, lambda: "started" in events, timeout=1)
        assert supervisor.has_worker(job_id)
    finally:
        supervisor.force_shutdown()


def test_control_send_returns_to_event_loop_without_blocking_pipe_wait(monkeypatch):
    application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    fixture = Path(__file__).parents[1] / "fixtures" / "control_worker.py"
    events, waits = [], []
    supervisor.eventReceived.connect(lambda _job, message: events.append(message.kind))
    try:
        supervisor.start(job_id="nonblocking-stop", program=sys.executable,
                         arguments=[str(fixture), "nonblocking-stop"])
        assert pump_until(application, lambda: "ready" in events)
        monkeypatch.setattr(supervisor._workers["nonblocking-stop"].process,
                            "waitForBytesWritten", lambda timeout: waits.append(timeout) or True)
        supervisor.request_stop("nonblocking-stop")
        assert waits == []
        assert pump_until(application, lambda: not supervisor.has_worker("nonblocking-stop"))
        assert "stopped" in events
    finally:
        supervisor.force_shutdown()


def test_supervisor_treats_stderr_as_log_not_lifecycle_failure() -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    events: list[str] = []
    logs: list[str] = []
    failures: list[str] = []
    supervisor.eventReceived.connect(lambda _job, message: events.append(message.kind))
    supervisor.workerLog.connect(lambda _job, message: logs.append(message))
    supervisor.workerFailed.connect(lambda _job, error: failures.append(error))
    fixture = Path(__file__).parents[1] / "fixtures" / "warning_worker.py"
    try:
        supervisor.start(
            job_id="warning-test",
            program=sys.executable,
            arguments=[str(fixture)],
        )
        assert pump_until(application, lambda: "ready" in events and bool(logs))
        assert "fallback path" in logs[0]
        assert failures == []
        assert supervisor.has_worker("warning-test")
        supervisor.stop("warning-test")
        assert pump_until(application, lambda: not supervisor.has_worker("warning-test"))
    finally:
        supervisor.shutdown()


def test_supervisor_does_not_replace_a_terminal_result_with_trailing_stdout_noise() -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    events: list[str] = []
    logs: list[str] = []
    failures: list[str] = []
    finished: list[int] = []
    supervisor.eventReceived.connect(lambda _job, message: events.append(message.kind))
    supervisor.workerLog.connect(lambda _job, message: logs.append(message))
    supervisor.workerFailed.connect(lambda _job, error: failures.append(error))
    supervisor.workerFinished.connect(lambda _job, code: finished.append(code))
    fixture = Path(__file__).parents[1] / "fixtures" / "terminal_then_garbage_worker.py"
    try:
        supervisor.start(
            job_id="terminal-garbage-test",
            program=sys.executable,
            arguments=[str(fixture)],
        )
        assert pump_until(application, lambda: bool(finished))

        assert events == ["ready", "failed"]
        assert failures == []
        assert len(logs) == 1
        assert "post-terminal" in logs[0]
        for _ in range(3):
            application.processEvents()
            time.sleep(0.01)
    finally:
        supervisor.shutdown()


def test_preowned_launcher_captures_worker_child_before_execution(tmp_path: Path) -> None:
    if sys.platform != "win32":
        return
    application = QCoreApplication.instance() or QCoreApplication([])
    supervisor = WorkerSupervisor()
    events: list[str] = []
    supervisor.eventReceived.connect(lambda _job, message: events.append(message.kind))
    fixture = Path(__file__).parents[1] / "fixtures" / "immediate_child_worker.py"
    pid_file = tmp_path / "child.pid"
    child_pid: int | None = None
    try:
        supervisor.start(
            job_id="escape-test",
            program=sys.executable,
            arguments=[str(fixture), "escape-test", str(pid_file)],
        )
        assert pump_until(application, lambda: "ready" in events and pid_file.is_file())
        child_pid = int(pid_file.read_text(encoding="ascii"))
        assert process_is_active(child_pid)
        supervisor.force_shutdown()
        assert pump_until(application, lambda: not process_is_active(child_pid))
    finally:
        supervisor.force_shutdown()
        if child_pid is not None and process_is_active(child_pid):
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console creation contract")
@pytest.mark.parametrize("launcher_kind", ["console", "gui", "hidden-console"])
@pytest.mark.parametrize("program_kind", ["python", "cmd", "powershell"])
def test_preowned_launcher_hides_console_and_preserves_child_streams(
    program_kind: str, launcher_kind: str,
) -> None:
    launcher = Path(sys.executable)
    if launcher_kind == "gui":
        launcher = launcher.with_name("pythonw.exe")
        if not launcher.is_file():
            pytest.skip("Windows GUI-subsystem Python is unavailable")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    flags = (
        subprocess.CREATE_NEW_CONSOLE
        if launcher_kind == "hidden-console" else subprocess.CREATE_NO_WINDOW
    )
    fixture = Path(__file__).parents[1] / "fixtures" / "hidden-child-probe.py"
    python_arguments = [str(fixture)]
    if program_kind in {"python", "cmd"}:
        if program_kind == "python":
            program = sys.executable
            arguments = python_arguments
        else:
            program = shutil.which("cmd.exe")
            if program is None:
                pytest.skip("Windows CMD is unavailable")
            arguments = [
                "/d", "/c", sys.executable, *python_arguments,
            ]
    else:
        program = shutil.which("powershell.exe")
        if program is None:
            pytest.skip("Windows PowerShell is unavailable")
        arguments = [
            "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
            "public static class ConsoleProbe { [DllImport(\"kernel32.dll\")] "
            "public static extern IntPtr GetConsoleWindow(); }'; "
            "$report = @{console=[ConsoleProbe]::GetConsoleWindow().ToInt64(); "
            "input=[Console]::In.ReadLine()}; "
            "[Console]::Out.WriteLine(($report | ConvertTo-Json -Compress)); "
            "[Console]::Error.WriteLine('child diagnostic'); exit 17",
        ]
    environment = os.environ.copy()
    environment["DUCAI_WORKER_PROGRAM"] = program
    environment["DUCAI_WORKER_ARGUMENTS_JSON"] = json.dumps(arguments)
    completed = subprocess.run(
        [str(launcher), "-m", "ducai_studio.workers.preowned_launcher"],
        input=b"DUCAI_LAUNCH\nafter-gate\n",
        capture_output=True,
        timeout=20,
        creationflags=flags,
        startupinfo=startup,
        env=environment,
    )

    assert completed.returncode == 17
    assert completed.stderr.strip() == b"child diagnostic"
    report = json.loads(completed.stdout)
    assert report["input"] == "after-gate"
    assert report["console"] == 0
