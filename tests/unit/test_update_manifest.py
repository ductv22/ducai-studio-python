import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure.update_manifest import (
    UpdateManifestError,
    canonical_manifest_bytes,
    expected_archive_name,
    parse_manifest,
    validate_managed_path,
    verify_manifest,
    version_is_eligible,
)


def manifest_payload(version="2.0.0a118", channel="testing", paths=None):
    paths = paths or ["DucaiStudio2.exe", "ui/main.qml"]
    return {
        "schema": 1,
        "product": "DUCAI STUDIO",
        "version": version,
        "channel": channel,
        "platform": "win64",
        "archive": {
            "name": expected_archive_name(version),
            "size": 42,
            "sha256": "a" * 64,
        },
        "files": [
            {"path": path, "size": 3, "sha256": hashlib.sha256(b"exe").hexdigest()}
            for path in sorted(paths)
        ],
    }


def test_canonical_manifest_signature_and_tamper_rejection():
    key = Ed25519PrivateKey.generate()
    data = canonical_manifest_bytes(manifest_payload())
    signature = key.sign(data)
    manifest = verify_manifest(data, signature, key.public_key())
    assert manifest.version == "2.0.0a118"
    assert manifest.digest == hashlib.sha256(data).hexdigest()
    with pytest.raises(UpdateManifestError, match="signature"):
        verify_manifest(data, bytes(64), key.public_key())
    with pytest.raises(UpdateManifestError):
        verify_manifest(data.replace(b"win64", b"linux"), signature, key.public_key())


@pytest.mark.parametrize(
    "path",
    [
        "../evil",
        "/root/x",
        "C:/evil",
        "a\\b",
        "a//b",
        "a/./b",
        "a/../b",
        "CON",
        "aux.txt",
        "a/COM1.dll",
        "a/COM¹.dll",
        "a/trailing. ",
        "a/name:stream",
        "DUCAI_STUDIO_DATA/config",
        "bin/ffmpeg.exe",
        "models/x",
        "cache/x",
        "python_runtime_311/python.exe",
        "engine_hosts/ocr_engine_host.py",
        "config/.env.production",
    ],
)
def test_rejects_unsafe_or_protected_paths(path):
    with pytest.raises(UpdateManifestError):
        validate_managed_path(path)


def test_rejects_casefold_duplicates_and_file_directory_overlap():
    for paths in (
        ["DucaiStudio2.exe", "A/x", "a/X"],
        ["DucaiStudio2.exe", "a", "a/b"],
    ):
        with pytest.raises(UpdateManifestError):
            parse_manifest(canonical_manifest_bytes(manifest_payload(paths=paths)))


def test_rejects_noncanonical_duplicate_unknown_and_oversized_json():
    data = canonical_manifest_bytes(manifest_payload())
    with pytest.raises(UpdateManifestError, match="canonical"):
        parse_manifest(json.dumps(manifest_payload()).encode())
    with pytest.raises(UpdateManifestError, match="Duplicate JSON key"):
        parse_manifest(data.replace(b'"schema":1', b'"schema":1,"schema":1'))
    payload = manifest_payload()
    payload["created_at"] = "today"
    with pytest.raises(UpdateManifestError, match="fields"):
        parse_manifest(canonical_manifest_bytes(payload))
    with pytest.raises(UpdateManifestError, match="size"):
        parse_manifest(b" " * (4 * 1024 * 1024 + 1))


def test_pep440_channel_selection_and_stable_manifest():
    assert version_is_eligible("2.0.0a118", "2.0.0a117", "testing")
    assert not version_is_eligible("2.0.0a118", "2.0.0a117", "stable")
    assert version_is_eligible("2.0.0", "2.0.0a117", "stable")
    assert not version_is_eligible("2.0.0a116", "2.0.0a117", "testing")
    with pytest.raises(UpdateManifestError, match="channel"):
        parse_manifest(canonical_manifest_bytes(manifest_payload(channel="stable")))
