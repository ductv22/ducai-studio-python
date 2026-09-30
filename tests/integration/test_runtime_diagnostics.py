from __future__ import annotations

import json
from pathlib import Path

import ducai_studio.infrastructure.runtime_diagnostics as runtime_diagnostics
from ducai_studio.infrastructure.runtime_diagnostics import run_runtime_diagnostics


def test_runtime_diagnostics_reports_every_managed_boundary_without_secrets(
    tmp_path: Path,
) -> None:
    report = run_runtime_diagnostics(
        data_root=tmp_path,
        executable_resolver=lambda _name: None,
    )
    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    components = {item.component for item in report.checks}

    assert report.schema == "ducai-runtime-diagnostics-v1"
    assert components == {
        "gui-python",
        "ffmpeg",
        "paddle-ocr",
        "whisper",
        "gemini-web",
        "gemini-cli",
        "piper",
        "vieneu-3.8",
    }
    assert report.overall_status == "blocked"
    assert "api_key" not in serialized.casefold()
    assert "token=" not in serialized.casefold()


def test_whisper_diagnostics_rejects_wrong_ctranslate2_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "whisper_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    model = tmp_path / "model_cache" / "whisper" / "large-v3"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "whisper_runtime" / "ducai_runtime.json").write_text(
        json.dumps({"model_path": "model_cache/whisper/large-v3"}),
        encoding="utf-8",
    )

    versions = {"faster-whisper": "1.2.1", "ctranslate2": "4.7.0"}
    monkeypatch.setattr(
        runtime_diagnostics,
        "_python_package_version",
        lambda _runtime, package: versions.get(package, ""),
    )

    result = runtime_diagnostics._check_whisper_runtime(tmp_path)

    assert result.status == "blocked"
    assert "CTranslate2 4.8.1" in result.summary
