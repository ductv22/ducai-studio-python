from __future__ import annotations

import json
import os
from pathlib import Path

from ducai_studio.providers.tts import discover_data_root

WHISPER_MODEL_ID = "large-v3"
WHISPER_REPOSITORY_ID = "Systran/faster-whisper-large-v3"
WHISPER_RUNTIME_VERSION = "1.2.1"
WHISPER_CTRANSLATE2_VERSION = "4.8.1"


def whisper_runtime_python(data_root: str | Path | None = None) -> Path:
    configured = os.environ.get("DUCAI_WHISPER_PYTHON", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = (
        Path(data_root).expanduser().resolve()
        if data_root is not None
        else discover_data_root()
    )
    return root / "whisper_runtime" / ".venv" / "Scripts" / "python.exe"


def whisper_model_path(
    data_root: str | Path | None = None,
    *,
    model_id: str = WHISPER_MODEL_ID,
) -> Path | None:
    if model_id != WHISPER_MODEL_ID:
        return None
    configured = os.environ.get("DUCAI_WHISPER_MODEL", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        return candidate if _is_whisper_model(candidate) else None
    root = (
        Path(data_root).expanduser().resolve()
        if data_root is not None
        else discover_data_root()
    )
    metadata_path = root / "whisper_runtime" / "ducai_runtime.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    raw_model_path = metadata.get("model_path") if isinstance(metadata, dict) else None
    if isinstance(raw_model_path, str) and raw_model_path.strip():
        candidate = Path(raw_model_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if _is_whisper_model(candidate):
            return candidate

    repository_root = (
        root
        / "model_cache"
        / "huggingface"
        / "hub"
        / "models--Systran--faster-whisper-large-v3"
        / "snapshots"
    )
    if not repository_root.is_dir():
        return None
    candidates = sorted(
        (path for path in repository_root.iterdir() if _is_whisper_model(path)),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return candidates[0].resolve() if candidates else None


def whisper_model_fingerprint(model_path: Path) -> dict[str, object]:
    resolved = model_path.expanduser().resolve(strict=True)
    files = []
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
        path = resolved / name
        if not path.is_file():
            continue
        stat = path.stat()
        files.append({"name": name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    if not files or not any(item["name"] == "model.bin" for item in files):
        raise ValueError("Whisper large-v3 model is incomplete")
    return {"path": str(resolved), "files": files}


def whisper_runtime_is_ready(runtime: str | Path | None = None) -> bool:
    resolved = (
        Path(runtime).expanduser().resolve()
        if runtime is not None
        else whisper_runtime_python()
    )
    if not resolved.is_file() or len(resolved.parents) < 2:
        return False
    site_packages = resolved.parents[1] / "Lib" / "site-packages"
    return (
        (site_packages / f"faster_whisper-{WHISPER_RUNTIME_VERSION}.dist-info").is_dir()
        and (
            site_packages
            / f"ctranslate2-{WHISPER_CTRANSLATE2_VERSION}.dist-info"
        ).is_dir()
    )


def _is_whisper_model(path: Path) -> bool:
    return path.is_dir() and (path / "model.bin").is_file() and (path / "config.json").is_file()
