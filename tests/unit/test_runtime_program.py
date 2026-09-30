from __future__ import annotations

from pathlib import Path

from ducai_studio.infrastructure import runtime_program
from ducai_studio.job_manager import service as service_module


def test_runtime_program_prefers_existing_python_executable(
    monkeypatch, tmp_path: Path
) -> None:
    python = tmp_path / "python.exe"
    app = tmp_path / "DucaiStudio2.exe"
    python.touch()
    app.touch()
    monkeypatch.setattr(runtime_program.sys, "executable", str(python))
    monkeypatch.setattr(runtime_program.sys, "argv", [str(app)])

    assert runtime_program.runtime_program_path() == python.resolve()


def test_runtime_program_falls_back_to_real_frozen_binary(
    monkeypatch, tmp_path: Path
) -> None:
    missing_alias = tmp_path / "python.exe"
    app = tmp_path / "DucaiStudio2.exe"
    app.touch()
    monkeypatch.setattr(runtime_program.sys, "executable", str(missing_alias))
    monkeypatch.setattr(runtime_program.sys, "argv", [str(app)])

    assert runtime_program.runtime_program_path() == app.resolve()


def test_engine_host_path_falls_back_to_packaged_host(
    monkeypatch, tmp_path: Path
) -> None:
    release = tmp_path / "release"
    app = release / "DucaiStudio2.exe"
    host = release / "engine_hosts" / "ocr_engine_host.py"
    host.parent.mkdir(parents=True)
    app.touch()
    host.touch()
    fake_service = release / "ducai_studio" / "job_manager" / "service.py"
    monkeypatch.setattr(service_module, "__file__", str(fake_service))
    monkeypatch.setattr(service_module, "runtime_program_path", lambda: app)

    assert service_module._engine_host_path("ocr_engine_host.py") == host.resolve()


def test_engine_host_path_prefers_packaged_bytecode_over_stale_source(
    monkeypatch, tmp_path: Path
) -> None:
    release = tmp_path / "release"
    app = release / "DucaiStudio2.exe"
    source_host = release / "engine_hosts" / "ocr_engine_host.py"
    compiled_host = source_host.with_suffix(".pyc")
    source_host.parent.mkdir(parents=True)
    app.touch()
    source_host.write_text("stale", encoding="utf-8")
    compiled_host.write_bytes(b"current")
    fake_service = release / "ducai_studio" / "job_manager" / "service.py"
    monkeypatch.setattr(service_module, "__file__", str(fake_service))
    monkeypatch.setattr(service_module, "runtime_program_path", lambda: app)

    assert service_module._engine_host_path("ocr_engine_host.py") == compiled_host.resolve()
