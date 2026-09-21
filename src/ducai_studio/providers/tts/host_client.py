from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any
from uuid import uuid4

from ducai_studio.external_runtime import (
    isolated_python_environment,
    isolated_python_working_directory,
)


class EngineHostError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class EngineHostClient:
    def __init__(
        self,
        *,
        python_executable: Path,
        host_script: Path,
        engine: str,
        timeout_seconds: float = 600,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.python_executable = python_executable.expanduser().resolve()
        self.host_script = host_script.expanduser().resolve()
        if not self.python_executable.is_file() or not self.host_script.is_file():
            raise FileNotFoundError("TTS runtime Python or engine host is missing")
        if engine not in {"piper", "vieneu"}:
            raise ValueError("Unsupported TTS engine host")
        self.engine = engine
        self.timeout_seconds = float(timeout_seconds)
        self.environment = dict(environment or {})
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=80)
        self._lock = threading.RLock()

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = isolated_python_environment(self.environment)
        self._process = subprocess.Popen(
            [str(self.python_executable), str(self.host_script), self.engine],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
            env=env,
            cwd=isolated_python_working_directory(self.python_executable),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def request(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self.start()
            process = self._process
            if process is None or process.stdin is None or process.poll() is not None:
                raise EngineHostError("tts_host_not_running", self.diagnostic_tail())
            request_id = uuid4().hex
            message = {
                "protocol_version": 1,
                "id": request_id,
                "action": action,
                "payload": payload or {},
            }
            try:
                process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise EngineHostError("tts_host_write_failed", self.diagnostic_tail()) from exc
            try:
                response = self._responses.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                self.close(force=True)
                raise EngineHostError("tts_host_timeout", self.diagnostic_tail()) from exc
            if isinstance(response, BaseException):
                raise EngineHostError("tts_host_protocol_failed", str(response)) from response
            if response.get("id") != request_id:
                raise EngineHostError("tts_host_response_id_mismatch")
            if response.get("ok") is not True:
                error = response.get("error")
                code = (
                    str(error.get("code", "tts_host_failed"))
                    if isinstance(error, dict)
                    else "tts_host_failed"
                )
                raise EngineHostError(code, self.diagnostic_tail())
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise EngineHostError("tts_host_result_invalid")
            return result

    def diagnostic_tail(self) -> str:
        return "\n".join(self._stderr)[-4_000:]

    def close(self, *, force: bool = False) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None and not force:
            try:
                if process.stdin is not None:
                    message = {
                        "protocol_version": 1,
                        "id": uuid4().hex,
                        "action": "shutdown",
                        "payload": {},
                    }
                    process.stdin.write(json.dumps(message) + "\n")
                    process.stdin.flush()
                    process.wait(timeout=5)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                force = True
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                raw = json.loads(line)
                if not isinstance(raw, dict) or raw.get("protocol_version") != 1:
                    raise ValueError("Invalid TTS host response")
                self._responses.put(raw)
        except BaseException as exc:
            self._responses.put(exc)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            value = line.rstrip()
            if value:
                self._stderr.append(value[:1_000])

    def __enter__(self) -> EngineHostClient:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
