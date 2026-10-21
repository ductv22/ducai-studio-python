from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from ducai_studio.infrastructure.process_tree import ProcessTreeGuard
from ducai_studio.infrastructure.runtime_program import runtime_program_path
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


@dataclass(slots=True)
class _WorkerHandle:
    process: QProcess
    output_buffer: bytearray
    tree: ProcessTreeGuard
    stop_requested: bool = False
    terminal_received: bool = False


class WorkerSupervisor(QObject):
    eventReceived = Signal(str, object)
    workerLog = Signal(str, str)
    workerFailed = Signal(str, str)
    workerFinished = Signal(str, int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._workers: dict[str, _WorkerHandle] = {}
        self._pending_progress: dict[str, WorkerMessage] = {}
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(200)
        self._progress_timer.timeout.connect(self._flush_progress)

    def _flush_progress(self, job_id: str | None = None) -> None:
        job_ids = (job_id,) if job_id is not None else tuple(self._pending_progress)
        for current in job_ids:
            message = self._pending_progress.pop(current, None)
            if message is not None:
                self.eventReceived.emit(current, message)
        if not self._pending_progress:
            self._progress_timer.stop()

    def _deliver_event(self, job_id: str, message: WorkerMessage) -> None:
        # Progress is an absolute snapshot, not a durable operation. Superseded
        # snapshots must not queue hundreds of synchronous database/UI updates.
        if message.kind == "progress":
            self._pending_progress[job_id] = message
            if not self._progress_timer.isActive():
                self._progress_timer.start()
            return
        # Preserve checkpoint/control/terminal ordering and the last real count.
        self._flush_progress(job_id)
        self.eventReceived.emit(job_id, message)

    def start(
        self,
        *,
        job_id: str,
        program: str,
        arguments: list[str],
        environment: Mapping[str, str] | None = None,
        startup_timeout_ms: int = 10_000,
    ) -> None:
        if job_id in self._workers:
            raise ValueError(f"Worker already exists for job: {job_id}")
        process = QProcess(self)
        process.setProgram(str(runtime_program_path()))
        process.setArguments(["-m", "ducai_studio.workers.preowned_launcher"])
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process_environment = QProcessEnvironment.systemEnvironment()
        if environment is not None:
            if not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()
            ):
                raise ValueError("Worker environment keys and values must be strings")
            for key, value in environment.items():
                process_environment.insert(key, value)
        process_environment.insert("DUCAI_WORKER_PROGRAM", program)
        process_environment.insert(
            "DUCAI_WORKER_ARGUMENTS_JSON",
            json.dumps(arguments, ensure_ascii=False),
        )
        process.setProcessEnvironment(process_environment)
        handle = _WorkerHandle(
            process=process,
            output_buffer=bytearray(),
            tree=ProcessTreeGuard(),
        )
        self._workers[job_id] = handle
        process.readyReadStandardOutput.connect(lambda: self._read_output(job_id))
        process.readyReadStandardError.connect(lambda: self._read_error(job_id))
        process.finished.connect(lambda code, _status: self._finished(job_id, int(code)))
        process.start()
        if not process.waitForStarted(startup_timeout_ms):
            self._workers.pop(job_id, None)
            error = process.errorString()
            handle.tree.close()
            process.deleteLater()
            raise RuntimeError(f"Worker did not start: {error}")
        try:
            handle.tree.assign_pid(int(process.processId()))
        except OSError as exc:
            process.kill()
            process.waitForFinished(5_000)
            self._workers.pop(job_id, None)
            handle.tree.close()
            process.deleteLater()
            raise RuntimeError(f"Cannot own worker process tree: {exc}") from exc
        if process.write(b"DUCAI_LAUNCH\n") < 0 or not process.waitForBytesWritten(1_000):
            process.kill()
            process.waitForFinished(5_000)
            self._workers.pop(job_id, None)
            handle.tree.close()
            process.deleteLater()
            raise RuntimeError("Cannot release the pre-owned worker launcher")

    def send(self, job_id: str, *, kind: str, payload: dict) -> None:
        handle = self._workers.get(job_id)
        if handle is None:
            raise LookupError(f"Unknown worker: {job_id}")
        if handle.process.state() is not QProcess.ProcessState.Running:
            raise RuntimeError(f"Worker is not running: {job_id}")
        message = WorkerMessage(
            protocol_version=PROTOCOL_VERSION,
            job_id=job_id,
            event_id=str(uuid4()),
            kind=kind,
            payload=payload,
        )
        if handle.process.write(message.to_json_line()) < 0:
            raise RuntimeError(f"Cannot write worker command: {job_id}")

    def stop(self, job_id: str, *, grace_ms: int = 5_000) -> None:
        self.request_stop(job_id, grace_ms=grace_ms)

    def request_stop(
        self,
        job_id: str,
        *,
        action: str = "stop",
        command_id: str | None = None,
        grace_ms: int = 5_000,
    ) -> None:
        handle = self._workers.get(job_id)
        if handle is None or handle.stop_requested:
            return
        handle.stop_requested = True
        try:
            payload = {"action": action}
            if command_id is not None:
                payload["command_id"] = command_id
            self.send(job_id, kind="control", payload=payload)
        except Exception:
            self._terminate(job_id, handle)
            return
        # QProcess flushes its queued write through the event loop. Blocking
        # that loop delays controls for every worker and GUI client.
        QTimer.singleShot(grace_ms, lambda: self._terminate(job_id, handle))

    def shutdown(self) -> None:
        for job_id in tuple(self._workers):
            self.request_stop(job_id)

    def force_shutdown(self) -> None:
        for job_id in tuple(self._workers):
            handle = self._workers.get(job_id)
            if handle is None:
                continue
            handle.process.kill()
            handle.process.waitForFinished(1_000)
            if job_id in self._workers:
                self._finished(job_id, int(handle.process.exitCode()))

    def active_job_ids(self) -> tuple[str, ...]:
        return tuple(self._workers)

    def has_worker(self, job_id: str) -> bool:
        return job_id in self._workers

    def _read_output(self, job_id: str) -> None:
        handle = self._workers.get(job_id)
        if handle is None:
            return
        handle.output_buffer.extend(bytes(handle.process.readAllStandardOutput()))
        while b"\n" in handle.output_buffer:
            line, _, remainder = handle.output_buffer.partition(b"\n")
            handle.output_buffer = bytearray(remainder)
            if not line.strip():
                continue
            try:
                message = WorkerMessage.from_json_line(line)
            except Exception as exc:
                if handle.terminal_received:
                    self.workerLog.emit(
                        job_id,
                        "Ignored post-terminal non-protocol stdout "
                        f"({len(line)} bytes).",
                    )
                    continue
                self.workerFailed.emit(job_id, f"Invalid worker protocol: {exc}")
                self.request_stop(job_id)
                return
            if message.job_id != job_id:
                self.workerFailed.emit(job_id, "Worker message job id does not match owner")
                self.request_stop(job_id)
                return
            if handle.terminal_received and message.kind == "progress":
                continue
            if message.kind in {"succeeded", "failed", "stopped"}:
                handle.terminal_received = True
            self._deliver_event(job_id, message)

    def _read_error(self, job_id: str) -> None:
        handle = self._workers.get(job_id)
        if handle is None:
            return
        raw = bytes(handle.process.readAllStandardError()).decode("utf-8", "replace").strip()
        if raw:
            if len(raw) > 16_384:
                raw = raw[:16_384] + "…[truncated]"
            self.workerLog.emit(job_id, raw)

    def _finished(self, job_id: str, exit_code: int) -> None:
        self._read_output(job_id)
        self._flush_progress(job_id)
        handle = self._workers.pop(job_id, None)
        if handle is None:
            return
        handle.tree.close()
        handle.process.deleteLater()
        self.workerFinished.emit(job_id, exit_code)

    def _terminate(self, job_id: str, expected: _WorkerHandle | None = None) -> None:
        handle = self._workers.get(job_id)
        if (handle is None or (expected is not None and handle is not expected)
                or handle.process.state() is QProcess.ProcessState.NotRunning):
            return
        handle.process.terminate()
        # A cached job can be retried with the same id before this timer fires.
        # Its previous process must never own the new process's stop deadline.
        QTimer.singleShot(2_000, lambda: self._kill(job_id, handle))

    def _kill(self, job_id: str, expected: _WorkerHandle | None = None) -> None:
        handle = self._workers.get(job_id)
        if (handle is None or (expected is not None and handle is not expected)
                or handle.process.state() is QProcess.ProcessState.NotRunning):
            return
        handle.process.kill()
