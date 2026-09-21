from __future__ import annotations

import json
from pathlib import Path

from ducai_studio.infrastructure.whisper_runtime import (
    whisper_model_path,
    whisper_runtime_is_ready,
    whisper_runtime_python,
)


def test_whisper_runtime_and_relative_model_receipt_are_resolved_from_data_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DUCAI_WHISPER_PYTHON", raising=False)
    monkeypatch.delenv("DUCAI_WHISPER_MODEL", raising=False)
    runtime = tmp_path / "whisper_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    site_packages = runtime.parents[1] / "Lib" / "site-packages"
    (site_packages / "faster_whisper-1.2.1.dist-info").mkdir(parents=True)
    (site_packages / "ctranslate2-4.8.1.dist-info").mkdir()
    model = tmp_path / "model_cache" / "whisper" / "large-v3"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "whisper_runtime" / "ducai_runtime.json").write_text(
        json.dumps({"model_path": "model_cache/whisper/large-v3"}),
        encoding="utf-8",
    )

    assert whisper_runtime_python(tmp_path) == runtime
    assert whisper_runtime_is_ready(runtime) is True
    assert whisper_model_path(tmp_path) == model.resolve()


def test_whisper_runtime_rejects_partial_installation(tmp_path: Path) -> None:
    runtime = tmp_path / "whisper_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()

    assert whisper_runtime_is_ready(runtime) is False
    assert whisper_model_path(tmp_path) is None
