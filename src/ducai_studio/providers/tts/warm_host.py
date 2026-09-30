from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ducai_studio.infrastructure.process_tree import ProcessTreeGuard

from .host_client import EngineHostClient, EngineHostError

_PROTOCOL_VERSION = 1
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_MAX_TOKEN_BYTES = 512
_ALLOWED_ACTIONS = frozenset(
    {
        "load",
        "inspect_profile",
        "bind_profile",
        "bind_preset",
        "enroll_profile",
        "synthesize_batch",
    }
)
_SAFE_CODE = re.compile(r"^[a-zA-Z0-9_]{1,80}$")


def _file_identity(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return str(path), 0, 0
    return str(path), stat.st_size, stat.st_mtime_ns


@dataclass(frozen=True, slots=True)
class WarmTtsHostConfig:
    """Manager-trusted launch configuration for one VieNeu runtime identity."""

    runtime_python: Path
    host_script: Path
    data_root: Path
    timeout_seconds: float = 1_800
    batch_size: int = 32

    def __post_init__(self) -> None:
        runtime_python = self.runtime_python.expanduser().resolve()
        host_script = self.host_script.expanduser().resolve()
        data_root = self.data_root.expanduser().resolve()
        if not runtime_python.is_file() or not host_script.is_file():
            raise FileNotFoundError("TTS runtime Python or engine host is missing")
        if not 1 <= int(self.batch_size) <= 32:
            raise ValueError("VieNeu warm-host batch size must be between 1 and 32")
        if not 1 <= float(self.timeout_seconds) <= 3_600:
            raise ValueError("VieNeu warm-host timeout must be between 1 and 3600 seconds")
        object.__setattr__(self, "runtime_python", runtime_python)
        object.__setattr__(self, "host_script", host_script)
        object.__setattr__(self, "data_root", data_root)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        object.__setattr__(self, "batch_size", int(self.batch_size))

    @property
    def identity(self) -> tuple[tuple[str, int, int], ...]:
        runtime_root = self.runtime_python.parent.parent.parent
        packages = self.runtime_python.parent.parent / "Lib" / "site-packages"
        return (
            _file_identity(self.runtime_python),
            _file_identity(runtime_root / "ducai_runtime.json"),
            _file_identity(self.host_script),
            (str(self.data_root), self.batch_size, int(self.timeout_seconds)),
            *tuple(_file_identity(path) for pattern in (
                "vieneu-*.dist-info/METADATA", "torch-*.dist-info/METADATA"
            ) for path in sorted(packages.glob(pattern))),
        )

    @property
    def host_environment(self) -> dict[str, str]:
        huggingface = self.data_root / "model_cache" / "huggingface"
        return {
            "HF_HOME": str(huggingface),
            "HUGGINGFACE_HUB_CACHE": str(huggingface / "hub"),
        }

    @property
    def load_payload(self) -> dict[str, object]:
        return {
            "mode": "v3turbo",
            "max_batch_size": self.batch_size,
            "runtime_version": "3.8.1",
            "model_cache": str(self.data_root / "model_cache"),
        }


@dataclass(slots=True)
class _WarmSession:
    job_id: str
    token: str
    config: WarmTtsHostConfig
    used: bool = False
    connection: socket.socket | None = None


class WarmTtsHostClient:
    """Worker-side authenticated client for a manager-owned warm TTS host."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        timeout_seconds: float,
    ) -> None:
        if host != "127.0.0.1" or not 1 <= int(port) <= 65_535:
            raise EngineHostError("tts_warm_endpoint_invalid")
        if not 32 <= len(token.encode("utf-8")) <= _MAX_TOKEN_BYTES:
            raise EngineHostError("tts_warm_credentials_invalid")
        self._host = host
        self._port = int(port)
        self._token = token
        self.timeout_seconds = max(1.0, min(3_600.0, float(timeout_seconds)))
        self._socket: socket.socket | None = None
        self._reader: Any = None
        self._writer: Any = None
        self._lock = threading.RLock()

    @classmethod
    def from_environment(cls, *, timeout_seconds: float) -> WarmTtsHostClient | None:
        marker = os.environ.get("DUCAI_TTS_WARM_PROTOCOL", "")
        if not marker:
            return None
        if marker != "1":
            raise EngineHostError("tts_warm_protocol_invalid")
        endpoint = os.environ.get("DUCAI_TTS_WARM_ENDPOINT", "")
        token = os.environ.get("DUCAI_TTS_WARM_TOKEN", "")
        try:
            host, raw_port = endpoint.rsplit(":", 1)
            port = int(raw_port)
        except (ValueError, TypeError) as exc:
            raise EngineHostError("tts_warm_endpoint_invalid") from exc
        return cls(host=host, port=port, token=token, timeout_seconds=timeout_seconds)

    def start(self) -> None:
        with self._lock:
            if self._socket is not None:
                return
            connect_timeout = min(10.0, self.timeout_seconds)
            connection: socket.socket | None = None
            reader: Any = None
            writer: Any = None
            try:
                connection = socket.create_connection(
                    (self._host, self._port), timeout=connect_timeout
                )
                connection.settimeout(self.timeout_seconds)
                reader = connection.makefile("r", encoding="utf-8", newline="\n")
                writer = connection.makefile("w", encoding="utf-8", newline="\n")
                request_id = uuid4().hex
                writer.write(
                    json.dumps(
                        {
                            "protocol_version": _PROTOCOL_VERSION,
                            "id": request_id,
                            "action": "session",
                            "token": self._token,
                            "payload": {},
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                writer.flush()
                response = _read_message(reader)
                if response.get("id") != request_id or response.get("ok") is not True:
                    raise EngineHostError("tts_warm_auth_failed")
            except EngineHostError:
                _close_streams(reader, writer, connection)
                raise
            except (EOFError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                _close_streams(reader, writer, connection)
                raise EngineHostError("tts_warm_connect_failed") from exc
            assert connection is not None
            self._socket = connection
            self._reader = reader
            self._writer = writer
            self._token = ""

    def request(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self.start()
            request_id = uuid4().hex
            try:
                self._writer.write(
                    json.dumps(
                        {
                            "protocol_version": _PROTOCOL_VERSION,
                            "id": request_id,
                            "action": action,
                            "payload": payload or {},
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                self._writer.flush()
                response = _read_message(self._reader)
            except EngineHostError:
                raise
            except (EOFError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                raise EngineHostError("tts_warm_protocol_failed") from exc
            if response.get("id") != request_id:
                raise EngineHostError("tts_warm_response_id_mismatch")
            if response.get("ok") is not True:
                error = response.get("error")
                code = error.get("code") if isinstance(error, dict) else "tts_warm_host_failed"
                raise EngineHostError(
                    str(code) if _SAFE_CODE.fullmatch(str(code)) else "tts_warm_host_failed"
                )
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise EngineHostError("tts_warm_result_invalid")
            return result

    def close(self, *, force: bool = False) -> None:
        del force
        with self._lock:
            reader, writer, connection = self._reader, self._writer, self._socket
            self._reader = None
            self._writer = None
            self._socket = None
            self._token = ""
        _close_streams(reader, writer, connection)

    def __enter__(self) -> WarmTtsHostClient:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class WarmTtsHostPool:
    """Own exactly one reusable VieNeu host and lease it to one worker at a time."""

    def __init__(self, *, idle_timeout_seconds: float = 120.0) -> None:
        if idle_timeout_seconds <= 0:
            raise ValueError("Warm TTS host idle timeout must be positive")
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self._lock = threading.RLock()
        self._listener: socket.socket | None = None
        self._listener_thread: threading.Thread | None = None
        self._closed = False
        self._session: _WarmSession | None = None
        self._host: EngineHostClient | None = None
        self._guard: ProcessTreeGuard | None = None
        self._host_identity: tuple[tuple[str, int, int], ...] | None = None
        self._idle_since: float | None = None

    def register(self, job_id: str, config: WarmTtsHostConfig) -> dict[str, str]:
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("Warm TTS host job ID must be non-empty")
        if not isinstance(config, WarmTtsHostConfig):
            raise TypeError("Warm TTS host config must be manager-trusted")
        stale: tuple[EngineHostClient | None, ProcessTreeGuard | None] = (None, None)
        with self._lock:
            if self._closed:
                return {}
            stale = self._expire_idle_locked()
            if self._session is not None:
                if self._session.job_id != job_id or self._session.used:
                    return {}
                return self._environment_locked(self._session.token)
            if self._host is not None and self._host_identity != config.identity:
                stale = self._detach_host_locked()
            self._ensure_listener_locked()
            token = secrets.token_urlsafe(32)
            self._session = _WarmSession(job_id.strip(), token, config)
            self._idle_since = None
            environment = self._environment_locked(token)
        self._close_host(*stale)
        return environment

    def release(self, job_id: str, *, healthy: bool = True) -> bool:
        connection: socket.socket | None = None
        stale: tuple[EngineHostClient | None, ProcessTreeGuard | None] = (None, None)
        with self._lock:
            session = self._session
            if session is None or session.job_id != job_id:
                return False
            connection = session.connection
            self._session = None
            if healthy and self._host is not None:
                self._idle_since = monotonic()
            else:
                stale = self._detach_host_locked()
        _close_socket(connection)
        self._close_host(*stale)
        return True

    def discard_idle(self) -> bool:
        with self._lock:
            if self._session is not None or self._host is None:
                return False
            stale = self._detach_host_locked()
        self._close_host(*stale)
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            listener = self._listener
            self._listener = None
            connection = self._session.connection if self._session is not None else None
            self._session = None
            stale = self._detach_host_locked()
        _close_socket(connection)
        _close_socket(listener)
        self._close_host(*stale)
        thread = self._listener_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _ensure_listener_locked(self) -> None:
        if self._listener is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(0.25)
        self._listener = listener
        self._listener_thread = threading.Thread(
            target=self._serve,
            name="ducai-warm-tts-broker",
            daemon=True,
        )
        self._listener_thread.start()

    def _environment_locked(self, token: str) -> dict[str, str]:
        listener = self._listener
        if listener is None:
            raise RuntimeError("Warm TTS listener is unavailable")
        host, port = listener.getsockname()
        return {
            "DUCAI_TTS_WARM_PROTOCOL": str(_PROTOCOL_VERSION),
            "DUCAI_TTS_WARM_ENDPOINT": f"{host}:{port}",
            "DUCAI_TTS_WARM_TOKEN": token,
        }

    def _serve(self) -> None:
        while True:
            with self._lock:
                if self._closed or self._listener is None:
                    return
                listener = self._listener
                stale = self._expire_idle_locked()
            self._close_host(*stale)
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                with self._lock:
                    if self._closed or listener is not self._listener:
                        return
                continue
            threading.Thread(
                target=self._serve_connection,
                args=(connection,),
                name="ducai-warm-tts-session",
                daemon=True,
            ).start()

    def _serve_connection(self, connection: socket.socket) -> None:
        reader: Any = None
        writer: Any = None
        session: _WarmSession | None = None
        request_id = "unknown"
        try:
            connection.settimeout(10)
            reader = connection.makefile("r", encoding="utf-8", newline="\n")
            writer = connection.makefile("w", encoding="utf-8", newline="\n")
            hello = _read_message(reader)
            session = self._claim_session(hello, connection)
            if session is None:
                return
            # A paused worker may remain connected without issuing requests.
            # Host request timeouts remain bounded; manager release owns idle leases.
            connection.settimeout(None)
            host = self._host_for_session(session)
            host.request("reset_session", {})
            _write_message(writer, _response(str(hello["id"]), result={"accepted": True}))
            while True:
                message = _read_message(reader)
                request_id, action, payload = _request_parts(message)
                if action not in _ALLOWED_ACTIONS:
                    raise EngineHostError("tts_warm_action_rejected")
                if action == "load":
                    payload = session.config.load_payload
                result = host.request(action, payload)
                _write_message(writer, _response(request_id, result=result))
        except EOFError:
            pass
        except (EngineHostError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, EngineHostError):
                code = exc.code if _SAFE_CODE.fullmatch(exc.code) else "tts_warm_host_failed"
            else:
                code = "tts_warm_protocol_failed"
            if writer is not None and session is not None:
                with suppress(OSError, ValueError):
                    _write_message(writer, _response(request_id, error=code))
            if session is not None:
                stale = self._discard_failed_session(session)
                self._close_host(*stale)
        finally:
            _close_streams(reader, writer, connection)
            if session is not None:
                with self._lock:
                    if self._session is session:
                        session.connection = None

    def _claim_session(
        self, message: dict[str, Any], connection: socket.socket
    ) -> _WarmSession | None:
        request_id, action, payload = _request_parts(message)
        del request_id, payload
        token = message.get("token")
        if action != "session" or not isinstance(token, str) or not token.isascii():
            return None
        with self._lock:
            session = self._session
            if (
                session is None
                or session.used
                or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
                or not hmac.compare_digest(session.token, token)
            ):
                return None
            session.used = True
            session.token = ""
            session.connection = connection
            return session

    def _host_for_session(self, session: _WarmSession) -> EngineHostClient:
        try:
            with self._lock:
                if self._session is not session or self._closed:
                    raise EngineHostError("tts_warm_session_expired")
                if self._host is not None:
                    if self._host_identity != session.config.identity:
                        raise EngineHostError("tts_warm_identity_changed")
                    return self._host
                guard = ProcessTreeGuard()
                host = EngineHostClient(
                    python_executable=session.config.runtime_python,
                    host_script=session.config.host_script,
                    engine="vieneu",
                    timeout_seconds=session.config.timeout_seconds,
                    environment=session.config.host_environment,
                    process_started=guard.assign_pid,
                )
                self._guard = guard
                self._host = host
                self._host_identity = session.config.identity
                # Process creation is quick and must stay atomic with publication;
                # model loading still happens later on this broker thread.
                host.start()
                return host
        except BaseException:
            stale = self._discard_failed_session(session)
            self._close_host(*stale)
            raise

    def _discard_failed_session(
        self, session: _WarmSession
    ) -> tuple[EngineHostClient | None, ProcessTreeGuard | None]:
        connection: socket.socket | None = None
        with self._lock:
            if self._session is not session:
                return None, None
            connection = session.connection
            stale = self._detach_host_locked()
        _close_socket(connection)
        return stale

    def _expire_idle_locked(
        self,
    ) -> tuple[EngineHostClient | None, ProcessTreeGuard | None]:
        if (
            self._session is None
            and self._host is not None
            and self._idle_since is not None
            and monotonic() - self._idle_since >= self.idle_timeout_seconds
        ):
            return self._detach_host_locked()
        return None, None

    def _detach_host_locked(
        self,
    ) -> tuple[EngineHostClient | None, ProcessTreeGuard | None]:
        host, guard = self._host, self._guard
        self._host = None
        self._guard = None
        self._host_identity = None
        self._idle_since = None
        return host, guard

    @staticmethod
    def _close_host(
        host: EngineHostClient | None, guard: ProcessTreeGuard | None
    ) -> None:
        try:
            if guard is not None:
                guard.close()
        finally:
            if host is not None:
                host.close(force=True)


def _request_parts(message: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if message.get("protocol_version") != _PROTOCOL_VERSION:
        raise ValueError("unsupported_protocol_version")
    request_id = message.get("id")
    action = message.get("action")
    payload = message.get("payload", {})
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id_required")
    if not isinstance(action, str) or not action:
        raise ValueError("action_required")
    if not isinstance(payload, dict):
        raise ValueError("payload_must_be_object")
    return request_id, action, payload


def _read_message(reader: Any) -> dict[str, Any]:
    line = reader.readline(_MAX_MESSAGE_BYTES + 1)
    if not line:
        raise EOFError("Warm TTS peer closed")
    if len(line.encode("utf-8")) > _MAX_MESSAGE_BYTES or not line.endswith("\n"):
        raise ValueError("Warm TTS message exceeds limit")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Warm TTS message must be an object")
    return value


def _write_message(writer: Any, message: dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise ValueError("Warm TTS response exceeds limit")
    writer.write(encoded)
    writer.flush()


def _response(
    request_id: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol_version": _PROTOCOL_VERSION,
        "id": request_id,
        "ok": error is None,
    }
    if error is None:
        response["result"] = result or {}
    else:
        response["error"] = {"code": error}
    return response


def _close_streams(reader: Any, writer: Any, connection: Any) -> None:
    for stream in (reader, writer):
        if stream is not None:
            with suppress(OSError):
                stream.close()
    _close_socket(connection)


def _close_socket(connection: Any) -> None:
    if connection is None:
        return
    with suppress(OSError):
        connection.shutdown(socket.SHUT_RDWR)
    with suppress(OSError):
        connection.close()
