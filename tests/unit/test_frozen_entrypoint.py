from __future__ import annotations

from types import SimpleNamespace

import pytest

from ducai_studio import __main__ as frozen_entrypoint


def test_frozen_entrypoint_ignores_normal_gui_arguments() -> None:
    assert frozen_entrypoint._dispatch_frozen_module([]) is None
    assert frozen_entrypoint._dispatch_frozen_module(["--user-option"]) is None
    assert frozen_entrypoint._dispatch_frozen_module(["-m", "unknown.module"]) is None


def test_frozen_diagnostic_route_works_before_activation_but_workers_stay_locked(monkeypatch):
    from ducai_studio.infrastructure import licensing_state, runtime_diagnostics

    received = []
    monkeypatch.setattr(licensing_state, "worker_activation_exit_code", lambda: 78)
    monkeypatch.setattr(runtime_diagnostics, "main", lambda arguments: (
        received.extend(arguments) or 17
    ))
    assert frozen_entrypoint._dispatch_frozen_module([
        "-m", "ducai_studio.infrastructure.runtime_diagnostics", "paddle-python.exe",
    ]) == 17
    assert received == ["paddle-python.exe"]
    assert frozen_entrypoint._dispatch_frozen_module([
        "-m", "ducai_studio.workers.preowned_launcher",
    ]) == 78


def test_frozen_entrypoint_routes_preowned_launcher(monkeypatch) -> None:
    from ducai_studio.workers import preowned_launcher

    received: list[str] = []
    monkeypatch.setattr(
        preowned_launcher,
        "main",
        lambda: received.extend(frozen_entrypoint.sys.argv[1:]) or 17,
    )

    result = frozen_entrypoint._dispatch_frozen_module(
        ["-m", "ducai_studio.workers.preowned_launcher", "gate"]
    )

    assert result == 17
    assert received == ["gate"]


def test_frozen_entrypoint_routes_cpu_wav_host_without_opening_gui(monkeypatch) -> None:
    from ducai_studio.workers import tts_postprocess

    received = []
    monkeypatch.setattr(tts_postprocess, "main", lambda: received.extend(
        frozen_entrypoint.sys.argv[1:],
    ) or 17)
    assert frozen_entrypoint._dispatch_frozen_module([
        "-m", "ducai_studio.workers.tts_postprocess", "private-staging",
    ]) == 17
    assert received == ["private-staging"]


@pytest.mark.parametrize("exit_code", [0, 2])
def test_frozen_ocr_dispatch_exits_without_finalizing_blocked_command_reader(
    monkeypatch, exit_code: int
) -> None:
    from ducai_studio.workers import ocr_worker

    received: list[list[str]] = []
    flushed: list[str] = []

    class ProcessExited(Exception):
        pass

    def exit_process(code: int) -> None:
        assert code == exit_code
        assert flushed == ["stdout", "stderr"]
        raise ProcessExited

    monkeypatch.setattr(ocr_worker, "main", lambda args: received.append(args) or exit_code)
    monkeypatch.setattr(ocr_worker.os, "_exit", exit_process)
    monkeypatch.setattr(ocr_worker, "sys", SimpleNamespace(
        stdout=SimpleNamespace(flush=lambda: flushed.append("stdout")),
        stderr=SimpleNamespace(flush=lambda: flushed.append("stderr")),
    ))

    with pytest.raises(ProcessExited):
        frozen_entrypoint._dispatch_frozen_module(
            ["-m", "ducai_studio.workers.ocr_worker", "ocr-shutdown"]
        )

    assert received == [["ocr-shutdown"]]
