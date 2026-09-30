from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from ducai_studio.providers.tts.host_client import EngineHostError
from ducai_studio.providers.tts.warm_host import (
    WarmTtsHostClient,
    WarmTtsHostConfig,
    WarmTtsHostPool,
)

_STUB_HOST = r'''
import json
import os
import sys
import time
from pathlib import Path

events = Path(__file__).with_suffix(".events")
loaded = False
voice = None

def record(kind, **payload):
    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, "pid": os.getpid(), **payload}) + "\n")

def response(request_id, result=None, error=None):
    value = {"protocol_version": 1, "id": request_id, "ok": error is None}
    if error is None:
        value["result"] = result or {}
    else:
        value["error"] = {"code": error}
    print(json.dumps(value, separators=(",", ":")), flush=True)

record("start")
for line in sys.stdin:
    message = json.loads(line)
    request_id = message["id"]
    action = message["action"]
    payload = message.get("payload", {})
    if action == "reset_session":
        voice = None
        record("reset")
        response(request_id, {"reset": True})
    elif action == "load":
        if not loaded:
            loaded = True
            record("engine_load")
        response(request_id, {"loaded": True})
    elif action == "bind_profile":
        voice = Path(payload["profile_path"]).read_text(encoding="utf-8")
        record("bind", voice=voice)
        response(
            request_id,
            {
                "binding": "explicit_v381_speaker_embedding_192d_ref_codes",
                "speaker_embedding_size": 192,
                "reference_codes_shape": [1, 1],
            },
        )
    elif action == "bind_preset":
        voice = payload["preset_name"]
        record("bind", voice=voice)
        response(request_id, {"binding": "vieneu_v381_builtin_preset"})
    elif action == "synthesize_batch":
        delay = float(payload.get("test_delay", 0))
        if delay:
            record("blocked")
            time.sleep(delay)
        if voice is None:
            response(request_id, error="voice_missing")
        else:
            response(request_id, {"voice": voice, "items": []})
    elif action == "inspect_profile" and payload.get("test_fail"):
        response(request_id, error="stub_failure")
    elif action == "inspect_profile":
        response(request_id, {"speaker_embedding_size": 192, "reference_codes_shape": [1, 1]})
    elif action == "shutdown":
        response(request_id, {"stopped": True})
        break
    else:
        response(request_id, {})
record("stop")
'''


def _setup(tmp_path: Path, *, idle_timeout: float = 120) -> tuple[
    WarmTtsHostPool, WarmTtsHostConfig, Path
]:
    host_script = tmp_path / "stub-host.py"
    host_script.write_text(_STUB_HOST, encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()
    config = WarmTtsHostConfig(
        runtime_python=Path(sys.executable),
        host_script=host_script,
        data_root=data_root,
        timeout_seconds=5,
    )
    return WarmTtsHostPool(idle_timeout_seconds=idle_timeout), config, host_script.with_suffix(
        ".events"
    )


def _client(environment: dict[str, str], *, timeout: float = 5) -> WarmTtsHostClient:
    host, raw_port = environment["DUCAI_TTS_WARM_ENDPOINT"].rsplit(":", 1)
    return WarmTtsHostClient(
        host=host,
        port=int(raw_port),
        token=environment["DUCAI_TTS_WARM_TOKEN"],
        timeout_seconds=timeout,
    )


def _events(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _wait_for_event(path: Path, kind: str, *, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(item["kind"] == kind for item in _events(path)):
            return
        time.sleep(0.02)
    raise AssertionError(f"event {kind!r} was not recorded")


def _process_is_active(process_id: int) -> bool:
    """Return whether a Windows process is still active without importing another test."""
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
        return bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == still_active
        )
    finally:
        kernel32.CloseHandle(handle)


def test_reuses_one_engine_and_rebinds_changed_profile_between_sessions(tmp_path: Path) -> None:
    pool, config, event_path = _setup(tmp_path)
    profile = tmp_path / "same-profile.npz"
    try:
        profile.write_text("voice-a", encoding="utf-8")
        first = _client(pool.register("job-a", config))
        first.request("load", {"runtime_version": "tampered"})
        first.request("bind_profile", {"profile_path": str(profile)})
        assert first.request("synthesize_batch", {"jobs": [{}]})["voice"] == "voice-a"
        first.close()
        assert pool.release("job-a", healthy=True)

        profile.write_text("voice-b", encoding="utf-8")
        second = _client(pool.register("job-b", config))
        second.request("load", {})
        second.request("bind_profile", {"profile_path": str(profile)})
        assert second.request("synthesize_batch", {"jobs": [{}]})["voice"] == "voice-b"
        second.close()
        assert pool.release("job-b", healthy=True)

        events = _events(event_path)
        assert [item["kind"] for item in events].count("engine_load") == 1
        assert [item["voice"] for item in events if item["kind"] == "bind"] == [
            "voice-a",
            "voice-b",
        ]
        assert [item["kind"] for item in events].count("reset") == 2
    finally:
        pool.close()


def test_busy_session_falls_back_without_starting_another_host(tmp_path: Path) -> None:
    pool, config, event_path = _setup(tmp_path)
    try:
        assert pool.register("job-a", config)
        assert pool.register("job-b", config) == {}
        assert not event_path.exists(), "register must not launch the runtime"
    finally:
        pool.release("job-a", healthy=True)
        pool.close()


def test_idle_host_expires_and_next_session_starts_a_new_engine(tmp_path: Path) -> None:
    pool, config, event_path = _setup(tmp_path, idle_timeout=0.1)
    try:
        first = _client(pool.register("job-a", config))
        first.request("load", {})
        first.close()
        pool.release("job-a", healthy=True)
        time.sleep(0.45)

        second = _client(pool.register("job-b", config))
        second.request("load", {})
        second.close()
        pool.release("job-b", healthy=True)
        assert [item["kind"] for item in _events(event_path)].count("engine_load") == 2
    finally:
        pool.close()


def test_host_failure_and_cancelled_session_discard_process(tmp_path: Path) -> None:
    pool, config, event_path = _setup(tmp_path)
    try:
        failed = _client(pool.register("job-failed", config))
        with pytest.raises(EngineHostError, match="stub_failure"):
            failed.request("inspect_profile", {"test_fail": True})
        failed.close()
        pool.release("job-failed", healthy=False)

        cancelled = _client(pool.register("job-cancelled", config), timeout=5)
        cancelled.request("load", {})
        cancelled.request("bind_preset", {"preset_name": "voice"})
        error: list[BaseException] = []

        def blocked_request() -> None:
            try:
                cancelled.request("synthesize_batch", {"jobs": [{}], "test_delay": 4})
            except BaseException as exc:
                error.append(exc)

        request = threading.Thread(target=blocked_request)
        request.start()
        _wait_for_event(event_path, "blocked")
        assert pool.release("job-cancelled", healthy=False)
        request.join(timeout=3)
        assert not request.is_alive()
        assert error and isinstance(error[0], EngineHostError)
    finally:
        pool.close()


def test_close_stops_cached_host_and_rejects_new_registration(tmp_path: Path) -> None:
    pool, config, event_path = _setup(tmp_path)
    client = _client(pool.register("job-a", config))
    client.request("load", {})
    client.close()
    pool.release("job-a", healthy=True)
    starts = [item for item in _events(event_path) if item["kind"] == "start"]
    assert len(starts) == 1

    pool.close()

    assert pool.register("job-b", config) == {}
    if os.name == "nt":
        assert not _process_is_active(int(starts[0]["pid"]))
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(int(starts[0]["pid"]), 0)


def test_invalid_credentials_never_start_engine_or_consume_valid_session(tmp_path):
    pool, config, events = _setup(tmp_path)
    try:
        environment = pool.register("valid", config)
        bad = _client({**environment, "DUCAI_TTS_WARM_TOKEN": "bad" * 16})
        with pytest.raises(EngineHostError):
            bad.request("load")
        bad.close()
        assert not events.exists()
        with _client(environment) as valid:
            assert valid.request("load")["loaded"] is True
        pool.release("valid")
    finally:
        pool.close()


def test_old_session_failure_cannot_discard_next_jobs_engine(tmp_path):
    pool, config, events = _setup(tmp_path)
    try:
        with _client(pool.register("first", config)) as first:
            first.request("load")
        old_session = pool._session
        pool.release("first")
        with _client(pool.register("second", config)) as second:
            second.request("load")
            assert pool._discard_failed_session(old_session) == (None, None)
            second.request("bind_preset", {"preset_name": "second-voice"})
            assert second.request("synthesize_batch")["voice"] == "second-voice"
        pool.release("second")
        assert sum(e["kind"] == "engine_load" for e in _events(events)) == 1
    finally:
        pool.close()


def test_idle_eviction_and_identity_change_reinitialize_engine(tmp_path):
    from dataclasses import replace

    pool, config, events = _setup(tmp_path)
    try:
        with _client(pool.register("first", config)) as first:
            first.request("load")
            assert not pool.discard_idle()
        pool.release("first")
        with _client(pool.register("second", replace(config, batch_size=16))) as second:
            second.request("load")
        pool.release("second")
        assert pool.discard_idle()
        assert not pool.discard_idle()
        with _client(pool.register("third", config)) as third:
            third.request("load")
        pool.release("third")
        assert sum(e["kind"] == "engine_load" for e in _events(events)) == 3
    finally:
        pool.close()
