"""Signed, canonical metadata for application-only Windows updates.

The public key is supplied by the caller so release tooling can use the same
validation without ever making signing material part of the application.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import InvalidVersion, Version

PRODUCT = "DUCAI STUDIO"
PLATFORM = "win64"
SCHEMA_VERSION = 1
PUBLIC_KEY_BYTES = base64.b64decode("2GfnJAxNYKZUOaDvkHrgPt7RKYJvu9K8+JGXt/aawY4=")
PUBLIC_KEY_FINGERPRINT_SHA256 = hashlib.sha256(PUBLIC_KEY_BYTES).hexdigest()
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILES = 100_000
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024

_HEX256 = re.compile(r"[0-9a-f]{64}\Z")
_PART = re.compile(r"[^<>:\"|?*\x00-\x1f]+\Z")
_RESERVED = re.compile(
    r"(?:con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?\Z",
    re.I,
)
_PROTECTED_ROOTS = frozenset(
    {
        "ducai_studio_data",
        "bin",
        "projects",
        "profiles",
        "media",
        "models",
        "cache",
        "caches",
        "paddle_runtime",
        "piper_runtime",
        "vieneu_3_8_runtime",
        "python_runtime_311",
        "python_runtime_312",
        "whisper_runtime",
        ".git",
    }
)


class UpdateManifestError(ValueError):
    """Metadata is malformed, unsafe, or not authenticated."""


@dataclass(frozen=True, slots=True)
class UpdateFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UpdateArchive:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UpdateManifest:
    schema: int
    product: str
    version: str
    channel: str
    platform: str
    archive: UpdateArchive
    files: tuple[UpdateFile, ...]

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_manifest_bytes(self)).hexdigest()


def expected_archive_name(version: str) -> str:
    return f"DUCAI-STUDIO-{version}-win64-update.zip"


def validate_managed_path(path: str) -> str:
    """Accept only an unambiguous, app-owned, slash-separated Windows file path."""
    if not isinstance(path, str) or not path or len(path) > 240:
        raise UpdateManifestError("Invalid managed file path")
    if path.startswith(("/", "\\")) or "\\" in path or ":" in path:
        raise UpdateManifestError(f"Absolute or noncanonical managed path: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UpdateManifestError(f"Unsafe managed path component: {path!r}")
    for part in parts:
        if part[-1] in " ." or not _PART.fullmatch(part) or _RESERVED.fullmatch(part):
            raise UpdateManifestError(f"Unsafe Windows file name: {path!r}")
    if parts[0].casefold() in _PROTECTED_ROOTS:
        raise UpdateManifestError(f"Protected application root: {path!r}")
    if any(part.casefold() == ".env" or part.casefold().startswith(".env.") for part in parts):
        raise UpdateManifestError(f"Environment file is not an update payload: {path!r}")
    if path.casefold().endswith((".py", ".whl")):
        raise UpdateManifestError(f"Source/package file is not an update payload: {path!r}")
    return path


def _pairs_unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateManifestError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise UpdateManifestError(f"Invalid {label} fields")
    return value


def _uint(value: Any, label: str, *, maximum: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise UpdateManifestError(f"Invalid {label}")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX256.fullmatch(value):
        raise UpdateManifestError(f"Invalid {label} SHA-256")
    return value


def _version(value: Any) -> Version:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise UpdateManifestError("Invalid update version")
    try:
        version = Version(value)
    except InvalidVersion as exc:
        raise UpdateManifestError("Invalid PEP 440 update version") from exc
    if str(version) != value or version.is_devrelease or version.local is not None:
        raise UpdateManifestError("Update version must be canonical and public")
    return version


def parse_manifest(data: bytes) -> UpdateManifest:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_MANIFEST_BYTES:
        raise UpdateManifestError("Manifest size is invalid")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs_unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateManifestError("Manifest is not UTF-8 JSON") from exc
    root = _exact_fields(
        payload,
        {"schema", "product", "version", "channel", "platform", "archive", "files"},
        "manifest",
    )
    if root["schema"] != SCHEMA_VERSION or type(root["schema"]) is not int:
        raise UpdateManifestError("Unsupported manifest schema")
    if root["product"] != PRODUCT or root["platform"] != PLATFORM:
        raise UpdateManifestError("Wrong product or platform")
    version = _version(root["version"])
    channel = root["channel"]
    if channel not in ("stable", "testing") or (channel == "stable" and version.is_prerelease):
        raise UpdateManifestError("Invalid release channel")
    archive_data = _exact_fields(root["archive"], {"name", "size", "sha256"}, "archive")
    if archive_data["name"] != expected_archive_name(root["version"]):
        raise UpdateManifestError("Wrong update archive name")
    archive = UpdateArchive(
        archive_data["name"],
        _uint(archive_data["size"], "archive size", maximum=MAX_ARCHIVE_BYTES, minimum=1),
        _hash(archive_data["sha256"], "archive"),
    )
    raw_files = root["files"]
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_FILES:
        raise UpdateManifestError("Invalid managed file list")
    files: list[UpdateFile] = []
    seen: set[str] = set()
    directories: set[str] = set()
    total = 0
    for raw_file in raw_files:
        item = _exact_fields(raw_file, {"path", "size", "sha256"}, "file")
        path = validate_managed_path(item["path"])
        folded = path.casefold()
        parts = folded.split("/")
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if folded in seen or folded in directories or parents & seen:
            raise UpdateManifestError(f"Duplicate or overlapping managed path: {path}")
        seen.add(folded)
        directories.update(parents)
        size = _uint(item["size"], "file size", maximum=MAX_FILE_BYTES)
        total += size
        if total > MAX_TOTAL_FILE_BYTES:
            raise UpdateManifestError("Managed files exceed expansion budget")
        files.append(UpdateFile(path, size, _hash(item["sha256"], "file")))
    if not any(item.path == "DucaiStudio2.exe" for item in files):
        raise UpdateManifestError("Main executable is missing")
    if files != sorted(files, key=lambda item: item.path):
        raise UpdateManifestError("Managed file list must be sorted")
    manifest = UpdateManifest(
        SCHEMA_VERSION, PRODUCT, root["version"], channel, PLATFORM, archive, tuple(files)
    )
    if canonical_manifest_bytes(manifest) != data:
        raise UpdateManifestError("Manifest is not canonical JSON")
    return manifest


def canonical_manifest_bytes(manifest: UpdateManifest | dict[str, Any]) -> bytes:
    if isinstance(manifest, UpdateManifest):
        payload: dict[str, Any] = {
            "schema": manifest.schema,
            "product": manifest.product,
            "version": manifest.version,
            "channel": manifest.channel,
            "platform": manifest.platform,
            "archive": {
                "name": manifest.archive.name,
                "size": manifest.archive.size,
                "sha256": manifest.archive.sha256,
            },
            "files": [
                {"path": item.path, "size": item.size, "sha256": item.sha256}
                for item in manifest.files
            ],
        }
    else:
        payload = manifest
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def verify_manifest(
    data: bytes,
    signature: bytes,
    public_key: bytes | Ed25519PublicKey = PUBLIC_KEY_BYTES,
) -> UpdateManifest:
    manifest = parse_manifest(data)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise UpdateManifestError("Invalid detached signature length")
    try:
        key = (
            Ed25519PublicKey.from_public_bytes(public_key)
            if isinstance(public_key, bytes)
            else public_key
        )
        key.verify(signature, data)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise UpdateManifestError("Manifest signature verification failed") from exc
    return manifest


def version_is_eligible(candidate: str, current: str, channel: str) -> bool:
    """Compare release versions, accepting alpha/beta/RC only on testing."""
    if channel not in ("stable", "testing"):
        raise ValueError("Unknown update channel")
    candidate_version = _version(candidate)
    current_version = _version(current)
    return candidate_version > current_version and (
        channel == "testing" or not candidate_version.is_prerelease
    )
