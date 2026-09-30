from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure.update_manifest import verify_manifest

PROJECT_ROOT = Path(__file__).parents[2]


def _load_builder():
    script = PROJECT_ROOT / "scripts" / "build-update-assets.py"
    spec = importlib.util.spec_from_file_location("ducai_update_builder", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_builder_emits_signed_binary_only_exact_payload(tmp_path: Path) -> None:
    builder = _load_builder()
    release = tmp_path / "DUCAI-STUDIO-2.0.0a118-win64"
    release.mkdir()
    (release / "DucaiStudio2.exe").write_bytes(b"new-app")
    (release / "runtime.dll").write_bytes(b"dll")
    (release / "DUCAI_BUILD_VERSION").write_text("2.0.0a118", encoding="ascii")
    (release / "secret.py").write_text("private source", encoding="utf-8")
    (release / "ducai_studio-2.0.0a118.whl").write_bytes(b"wheel")
    (release / "DUCAI_STUDIO_DATA").mkdir()
    (release / "DUCAI_STUDIO_DATA" / "voice.wav").write_bytes(b"private")
    (release / "manifest.json").write_text(
        json.dumps({"version": "2.0.0a118"}), encoding="utf-8"
    )
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "private.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    archive, manifest_path, signature_path = builder.build_update_assets(
        release, tmp_path / "out", key_path, expected_public_key=public
    )

    manifest = verify_manifest(manifest_path.read_bytes(), signature_path.read_bytes(), public)
    assert manifest.version == "2.0.0a118"
    assert manifest.channel == "testing"
    with zipfile.ZipFile(archive) as package:
        assert package.namelist() == [
            "DUCAI_BUILD_VERSION",
            "DucaiStudio2.exe",
            "runtime.dll",
        ]
        assert all(not item.is_dir() for item in package.infolist())
