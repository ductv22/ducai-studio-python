from __future__ import annotations

import os
from pathlib import Path

from ducai_studio.infrastructure.portable_runtime import (
    configure_portable_environment,
    paddle_cache_home_for_runtime,
)
from ducai_studio.providers.tts.catalog import discover_data_root


def test_portable_release_discovers_sibling_data_and_tools(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "DucaiStudio2.exe"
    executable.touch()
    data_root = tmp_path / "DUCAI_STUDIO_DATA"
    (data_root / "paddle_model_cache").mkdir(parents=True)
    tool_root = tmp_path / "bin"
    tool_root.mkdir()
    (tool_root / "ffmpeg.exe").touch()
    (tool_root / "ffprobe.exe").touch()
    monkeypatch.delenv("DUCAI_STUDIO_DATA_ROOT", raising=False)
    monkeypatch.delenv("PADDLE_PDX_CACHE_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "system-bin"))

    configured = configure_portable_environment(executable)

    assert configured == data_root.resolve()
    assert discover_data_root(executable_path=executable) == data_root.resolve()
    assert os.environ["DUCAI_STUDIO_DATA_ROOT"] == str(data_root.resolve())
    assert os.environ["PADDLE_PDX_CACHE_HOME"] == str(
        (data_root / "paddle_model_cache").resolve()
    )
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tool_root)


def test_portable_environment_repairs_relocated_managed_venvs(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "DucaiStudio2.exe"
    executable.touch()
    data_root = tmp_path / "DUCAI_STUDIO_DATA"
    python_homes = {
        "paddle_runtime": data_root / "python_runtime_311",
        "piper_runtime": data_root / "python_runtime_312",
        "vieneu_3_8_runtime": data_root / "python_runtime_312",
        "whisper_runtime": data_root / "python_runtime_312",
    }
    for python_home in set(python_homes.values()):
        python_home.mkdir(parents=True)
        (python_home / "python.exe").write_bytes(b"portable-console-launcher")
        (python_home / "pythonw.exe").write_bytes(b"portable-window-launcher")
        (python_home / "python3.dll").write_bytes(b"portable-stable-abi")
        version_dll = (
            "python311.dll" if python_home.name.endswith("311") else "python312.dll"
        )
        (python_home / version_dll).write_bytes(b"portable-version-runtime")
        (python_home / "vcruntime140.dll").write_bytes(b"portable-vc-runtime")
    for runtime in python_homes:
        venv = data_root / runtime / ".venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "python.exe").write_bytes(b"uv-machine-bound-shim")
        (venv / "Scripts" / "pythonw.exe").write_bytes(b"uv-machine-bound-shim")
        (venv / "pyvenv.cfg").write_text(
            "home = __DUCAI_PORTABLE_PYTHON_HOME__\nversion_info = 3.12\n",
            encoding="utf-8",
        )
    monkeypatch.delenv("DUCAI_STUDIO_DATA_ROOT", raising=False)

    configure_portable_environment(executable)

    for runtime, python_home in python_homes.items():
        expected = f"home = {python_home.resolve()}"
        config = data_root / runtime / ".venv" / "pyvenv.cfg"
        assert expected in config.read_text(encoding="utf-8")
        scripts = config.parent / "Scripts"
        assert (scripts / "python.exe").read_bytes() == b"portable-console-launcher"
        assert (scripts / "pythonw.exe").read_bytes() == b"portable-window-launcher"
        assert (scripts / "python3.dll").read_bytes() == b"portable-stable-abi"
        version_dll = (
            "python311.dll" if python_home.name.endswith("311") else "python312.dll"
        )
        assert (scripts / version_dll).read_bytes() == b"portable-version-runtime"
        assert (scripts / "vcruntime140.dll").read_bytes() == b"portable-vc-runtime"


def test_paddle_runtime_uses_its_sibling_portable_model_cache(tmp_path: Path) -> None:
    python = tmp_path / "DUCAI_STUDIO_DATA" / "paddle_runtime" / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    expected = tmp_path / "DUCAI_STUDIO_DATA" / "paddle_model_cache"
    expected.mkdir()

    assert paddle_cache_home_for_runtime(python) == expected.resolve()


def test_portable_environment_preserves_explicit_user_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "DucaiStudio2.exe"
    executable.touch()
    (tmp_path / "DUCAI_STUDIO_DATA").mkdir()
    explicit_data = tmp_path / "shared-data"
    explicit_cache = tmp_path / "shared-paddle"
    monkeypatch.setenv("DUCAI_STUDIO_DATA_ROOT", str(explicit_data))
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", str(explicit_cache))

    configure_portable_environment(executable)

    assert os.environ["DUCAI_STUDIO_DATA_ROOT"] == str(explicit_data)
    assert os.environ["PADDLE_PDX_CACHE_HOME"] == str(explicit_cache)
