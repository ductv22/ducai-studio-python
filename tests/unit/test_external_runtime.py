from __future__ import annotations

from pathlib import Path

from ducai_studio.external_runtime import (
    isolated_python_environment,
    isolated_python_working_directory,
)


def test_isolated_python_environment_removes_host_python_state(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONHOME", "C:/host-python")
    monkeypatch.setenv("PYTHONPATH", "C:/host-modules")
    monkeypatch.setenv("PYTHONEXECUTABLE", "C:/host-python/python.exe")
    monkeypatch.setenv("VIRTUAL_ENV", "C:/host-venv")

    environment = isolated_python_environment({"DUCAI_ENGINE": "quality"})

    assert not {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONEXECUTABLE",
        "VIRTUAL_ENV",
    } & environment.keys()
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["DUCAI_ENGINE"] == "quality"


def test_external_python_uses_interpreter_directory_instead_of_host_cwd(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "paddle-runtime" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()

    assert isolated_python_working_directory(runtime) == runtime.parent.resolve()
