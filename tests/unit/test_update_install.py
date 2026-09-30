from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure.update_install import (
    current_install_root,
    prepare_update_handoff,
)
from ducai_studio.infrastructure.update_manifest import canonical_manifest_bytes


def _signed_fixture(root: Path, payload: Path) -> tuple[Path, Path, str, bytes]:
    executable = payload / "DucaiStudio2.exe"
    executable.write_bytes(b"new")
    manifest_bytes = canonical_manifest_bytes(
        {
            "schema": 1,
            "product": "DUCAI STUDIO",
            "version": "2.0.0a118",
            "channel": "testing",
            "platform": "win64",
            "archive": {
                "name": "DUCAI-STUDIO-2.0.0a118-win64-update.zip",
                "size": 1,
                "sha256": "0" * 64,
            },
            "files": [
                {
                    "path": "DucaiStudio2.exe",
                    "size": executable.stat().st_size,
                    "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                }
            ],
        }
    )
    manifest = root / "update-manifest.json"
    signature = root / "update-manifest.sig"
    manifest.write_bytes(manifest_bytes)
    key = Ed25519PrivateKey.generate()
    signature.write_bytes(key.sign(manifest_bytes))
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return manifest, signature, hashlib.sha256(manifest_bytes).hexdigest(), public


def test_prepare_handoff_keeps_payload_and_install_separate(tmp_path: Path) -> None:
    install = tmp_path / "app"
    payload = tmp_path / "stage" / "payload"
    update_root = tmp_path / "private-updates"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / "DucaiStudio2.exe").write_bytes(b"old")
    manifest, signature, digest, public = _signed_fixture(tmp_path / "stage", payload)

    handoff = prepare_update_handoff(
        install_root=install,
        payload_root=payload,
        manifest_path=manifest,
        signature_path=signature,
        update_root=update_root,
        version="2.0.0a118",
        expected_manifest_sha256=digest,
        public_key=public,
    )

    request = json.loads(handoff.request_path.read_text(encoding="utf-8"))
    assert Path(request["install_root"]) == install.resolve()
    assert Path(request["payload_root"]) == payload.resolve()
    assert request["nonce"] == handoff.nonce
    assert handoff.manifest_sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()
    helper = handoff.helper_path.read_text(encoding="utf-8-sig")
    assert "ManifestSha256" in helper
    assert "DUCAI_STUDIO_DATA" not in json.dumps(request)
    assert "taskkill.exe /PID $ProcessId /T /F" in helper
    assert "$pendingTarget" in helper
    assert "Sha256 $pendingTarget" in helper


def test_prepare_handoff_rejects_payload_inside_install(tmp_path: Path) -> None:
    install = tmp_path / "app"
    payload = install / "stage"
    install.mkdir()
    payload.mkdir()
    (install / "DucaiStudio2.exe").write_bytes(b"old")
    manifest, signature, digest, public = _signed_fixture(tmp_path, payload)

    with pytest.raises(ValueError, match="nằm ngoài"):
        prepare_update_handoff(
            install_root=install,
            payload_root=payload,
            manifest_path=manifest,
            signature_path=signature,
            update_root=tmp_path / "updates",
            version="2.0.0a118",
            expected_manifest_sha256=digest,
            public_key=public,
        )


def test_prepare_handoff_rejects_manifest_changed_after_verification(tmp_path: Path) -> None:
    install = tmp_path / "app"
    payload = tmp_path / "stage" / "payload"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / "DucaiStudio2.exe").write_bytes(b"old")
    manifest, signature, digest, public = _signed_fixture(tmp_path / "stage", payload)
    manifest.write_bytes(manifest.read_bytes().replace(b"DucaiStudio2.exe", b"DUCAI_STUDIO_DATA"))

    with pytest.raises(ValueError, match="thay đổi"):
        prepare_update_handoff(
            install_root=install,
            payload_root=payload,
            manifest_path=manifest,
            signature_path=signature,
            update_root=tmp_path / "updates",
            version="2.0.0a118",
            expected_manifest_sha256=digest,
            public_key=public,
        )


def test_current_install_root_requires_packaged_executable(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="đóng gói"):
        current_install_root(tmp_path / "python.exe")

    executable = tmp_path / "DUCAISTUDIO2.EXE"
    executable.write_bytes(b"exe")
    assert current_install_root(executable) == tmp_path.resolve()
