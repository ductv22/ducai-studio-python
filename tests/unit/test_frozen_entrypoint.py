from __future__ import annotations

from ducai_studio import __main__ as frozen_entrypoint


def test_frozen_entrypoint_ignores_normal_gui_arguments() -> None:
    assert frozen_entrypoint._dispatch_frozen_module([]) is None
    assert frozen_entrypoint._dispatch_frozen_module(["--user-option"]) is None
    assert frozen_entrypoint._dispatch_frozen_module(["-m", "unknown.module"]) is None


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
