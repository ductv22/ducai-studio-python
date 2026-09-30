from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ducai_studio.infrastructure.voice_installer import PiperVoiceInstaller, VoiceInstallError
from ducai_studio.providers.tts.catalog import (
    VoiceCatalog,
    VoiceCatalogEntry,
    VoiceDownloadFile,
    VoiceRightsState,
)


def _download(path: Path, filename: str, *, md5: str | None = None) -> VoiceDownloadFile:
    data = path.read_bytes()
    return VoiceDownloadFile(
        filename=filename,
        url=path.as_uri(),
        size_bytes=len(data),
        md5=md5 or hashlib.md5(data, usedforsecurity=False).hexdigest(),
    )


def _catalog_with_entry(tmp_path: Path, entry: VoiceCatalogEntry) -> VoiceCatalog:
    catalog = VoiceCatalog(tmp_path / "target", preview_root=tmp_path / "previews")
    catalog.entries = lambda: (entry,)  # type: ignore[method-assign]
    return catalog


def test_installer_verifies_then_publishes_complete_pair(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    model = source / "voice.onnx"
    model.write_bytes(b"model-bytes")
    config = source / "voice.onnx.json"
    config.write_text(
        json.dumps({"audio": {"sample_rate": 22_050}, "phoneme_id_map": {"_": [0]}}),
        encoding="utf-8",
    )
    entry = VoiceCatalogEntry(
        "voice",
        "piper",
        "Voice",
        "Fixture",
        "Test voice",
        "Verified fixture",
        VoiceRightsState.VERIFIED,
        installable=True,
        download_files=(
            _download(model, "voice.onnx"),
            _download(config, "voice.onnx.json"),
        ),
    )
    catalog = _catalog_with_entry(tmp_path, entry)
    progress: list[int] = []

    PiperVoiceInstaller(catalog).install("voice", progress.append)

    assert (catalog.voice_root / "voice.onnx").read_bytes() == b"model-bytes"
    assert (catalog.voice_root / "voice.onnx.json").is_file()
    assert not list(catalog.voice_root.glob("*.part"))
    assert progress[-1] == 100


def test_installer_keeps_catalog_clean_when_digest_fails(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    model = source / "voice.onnx"
    model.write_bytes(b"model-bytes")
    config = source / "voice.onnx.json"
    config.write_text(
        json.dumps({"audio": {"sample_rate": 22_050}, "phoneme_id_map": {"_": [0]}}),
        encoding="utf-8",
    )
    entry = VoiceCatalogEntry(
        "voice",
        "piper",
        "Voice",
        "Fixture",
        "Test voice",
        "Verified fixture",
        VoiceRightsState.VERIFIED,
        installable=True,
        download_files=(
            _download(model, "voice.onnx"),
            _download(config, "voice.onnx.json", md5="0" * 32),
        ),
    )
    catalog = _catalog_with_entry(tmp_path, entry)

    with pytest.raises(VoiceInstallError, match="Mã kiểm tra"):
        PiperVoiceInstaller(catalog).install("voice")

    assert not (catalog.voice_root / "voice.onnx").exists()
    assert not (catalog.voice_root / "voice.onnx.json").exists()
    assert not list(catalog.voice_root.glob("*.part"))
