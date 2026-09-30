from __future__ import annotations

import argparse
import hashlib
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.version import Version

from ducai_studio.infrastructure.update_manifest import (
    PRODUCT,
    PUBLIC_KEY_BYTES,
    canonical_manifest_bytes,
    parse_manifest,
)

_EXCLUDED_NAMES = {
    "manifest.json",
    "update-manifest.json",
    "update-manifest.sig",
}
_PROTECTED_PARTS = {
    "ducai_studio_data",
    "projects",
    "cache",
    "caches",
    "models",
    "model_cache",
    ".git",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _release_files(release_root: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted(release_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file():
            continue
        relative = path.relative_to(release_root).as_posix()
        pure = PurePosixPath(relative)
        folded_parts = {part.casefold() for part in pure.parts}
        if path.name.casefold() in _EXCLUDED_NAMES:
            continue
        if path.suffix.casefold() in {".py", ".whl", ".pem", ".key"}:
            continue
        if folded_parts & _PROTECTED_PARTS or any("cache" in part for part in folded_parts):
            continue
        files.append((path, relative))
    if not any(relative.casefold() == "ducaistudio2.exe" for _path, relative in files):
        raise RuntimeError("DucaiStudio2.exe is missing from the update payload")
    return sorted(files, key=lambda item: item[1])


def _write_deterministic_zip(
    archive_path: Path, files: list[tuple[Path, str]]
) -> None:
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for source, relative in files:
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with source.open("rb") as handle:
                archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED)


def build_update_assets(
    release_root: Path,
    output_root: Path,
    private_key_path: Path,
    *,
    expected_public_key: bytes = PUBLIC_KEY_BYTES,
) -> tuple[Path, Path, Path]:
    release_root = release_root.resolve(strict=True)
    output_root.mkdir(parents=True, exist_ok=True)
    release_manifest = json.loads(
        (release_root / "manifest.json").read_text(encoding="utf-8-sig")
    )
    version = Version(str(release_manifest["version"]))
    build_identity = (release_root / "DUCAI_BUILD_VERSION").read_text(
        encoding="ascii"
    ).strip()
    if build_identity != str(version):
        raise RuntimeError("Packaged executable identity does not match release version")
    channel = "testing" if version.is_prerelease else "stable"
    files = _release_files(release_root)
    archive_path = output_root / f"DUCAI-STUDIO-{version}-win64-update.zip"
    manifest_path = output_root / "update-manifest.json"
    signature_path = output_root / "update-manifest.sig"
    _write_deterministic_zip(archive_path, files)
    manifest = {
        "schema": 1,
        "product": PRODUCT,
        "version": str(version),
        "channel": channel,
        "platform": "win64",
        "archive": {
            "name": archive_path.name,
            "size": archive_path.stat().st_size,
            "sha256": _sha256(archive_path),
        },
        "files": [
            {"path": relative, "size": source.stat().st_size, "sha256": _sha256(source)}
            for source, relative in files
        ],
    }
    encoded = canonical_manifest_bytes(manifest)
    parse_manifest(encoded)
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(), password=None
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("Update signing key must be Ed25519")
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if public_bytes != expected_public_key:
        raise RuntimeError("Signing key does not match the public key pinned in the app")
    manifest_path.write_bytes(encoded)
    signature_path.write_bytes(private_key.sign(encoded))
    return archive_path, manifest_path, signature_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    arguments = parser.parse_args(argv)
    for output in build_update_assets(
        arguments.release_root, arguments.output_root, arguments.private_key
    ):
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
