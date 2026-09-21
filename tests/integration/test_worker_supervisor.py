import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from ducai_studio.job_manager.worker_supervisor import WorkerSupervisor


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
