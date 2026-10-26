from __future__ import annotations

import hashlib
import io
import json

import pytest
from dialogue_casting import ecapa


def _install_fixture(tmp_path, monkeypatch, content: bytes):
    lock = tmp_path / "test-ecapa-lock.json"
    lock.write_text(
        json.dumps(
            {
                "model_id": "test/model",
                "revision": "test-revision",
                "files": [
                    {
                        "path": "model.safetensors",
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "url": "https://models.example.test/model.safetensors",
                    }
                ],
                "runtime": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ecapa, "LOCK", lock)
    monkeypatch.setattr(
        ecapa.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(content),
    )
    return lock


def test_ecapa_install_downloads_to_cache_verifies_and_installs_offline(tmp_path, monkeypatch):
    content = b"pinned test model"
    lock = _install_fixture(tmp_path, monkeypatch, content)
    cache = tmp_path / "downloads"
    root = tmp_path / "models"
    root.mkdir()
    (root / "config.json").write_text('{"C": 1024}', encoding="utf-8")

    receipt = ecapa.download_model(root, cache)

    assert receipt["model_id"] == "test/model"
    assert receipt["lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert (cache / "model.safetensors").read_bytes() == content
    assert (root / "model.safetensors").read_bytes() == content

    offline_root = tmp_path / "offline-models"
    offline_root.mkdir()
    (offline_root / "config.json").write_text('{"C": 1024}', encoding="utf-8")
    ecapa.download_model(offline_root, cache, offline=True)
    assert (offline_root / "model.safetensors").read_bytes() == content


def test_ecapa_offline_install_fails_when_verified_cache_is_missing(tmp_path, monkeypatch):
    content = b"pinned test model"
    _install_fixture(tmp_path, monkeypatch, content)
    root = tmp_path / "models"
    root.mkdir()
    (root / "config.json").write_text('{"C": 1024}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="Offline ECAPA cache is incomplete"):
        ecapa.download_model(root, tmp_path / "empty-cache", offline=True)


def test_ecapa_runtime_verifies_exact_installed_versions(tmp_path, monkeypatch):
    lock = tmp_path / "test-ecapa-lock.json"
    lock.write_text(
        json.dumps({"runtime": {"torch": "2.8.0+cu128", "safetensors": "0.8.0"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ecapa, "LOCK", lock)
    monkeypatch.setattr(
        ecapa.importlib.metadata,
        "version",
        lambda name: {"torch": "2.8.0+cu128", "safetensors": "0.8.0"}[name],
    )

    assert ecapa.verify_runtime() == {
        "torch": "2.8.0+cu128",
        "safetensors": "0.8.0",
    }
