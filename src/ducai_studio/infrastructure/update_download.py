"""Verified archive download and extraction; no GUI or install-root access."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ducai_studio.infrastructure.github_updates import UpdateCheckError, open_update_url
from ducai_studio.infrastructure.update_manifest import (
    MAX_TOTAL_FILE_BYTES,
    UpdateManifest,
    UpdateManifestError,
    validate_managed_path,
)

ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]
_CHUNK = 1024 * 1024
_MAX_COMPRESSION_RATIO = 200


class UpdateDownloadError(RuntimeError):
    """The downloaded archive cannot be trusted or staged."""


class UpdateCancelled(UpdateDownloadError):
    """The user cancelled a download or extraction."""


def _check_cancel(cancelled: CancelCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise UpdateCancelled("Update download cancelled")


def verify_archive(path: Path, manifest: UpdateManifest) -> None:
    """Recheck signed archive bytes, including when handed to another process."""
    try:
        if path.stat().st_size != manifest.archive.size:
            raise UpdateDownloadError("Update archive size mismatch")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(_CHUNK):
                digest.update(block)
        if digest.hexdigest() != manifest.archive.sha256:
            raise UpdateDownloadError("Update archive SHA-256 mismatch")
    except OSError as exc:
        raise UpdateDownloadError(f"Cannot verify update archive: {exc}") from exc


def download_verified_archive(
    url: str,
    destination: Path,
    manifest: UpdateManifest,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
    timeout: float = 60.0,
    opener: Callable[..., Any] | None = None,
) -> Path:
    """Stream to .part, report exact received bytes, then atomically publish."""
    destination = Path(destination)
    if destination.name != manifest.archive.name:
        raise UpdateDownloadError("Destination name does not match signed archive")
    part = destination.with_name(destination.name + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part.unlink(missing_ok=True)
    received = 0
    digest = hashlib.sha256()
    try:
        _check_cancel(cancelled)
        with (
            open_update_url(url, timeout=timeout, opener=opener) as response,
            part.open("xb") as output,
        ):
            while True:
                _check_cancel(cancelled)
                block = response.read(_CHUNK)
                if not block:
                    break
                received += len(block)
                if received > manifest.archive.size:
                    raise UpdateDownloadError("Update archive exceeds signed size")
                output.write(block)
                digest.update(block)
                if progress is not None:
                    progress(received, manifest.archive.size)
            output.flush()
            os.fsync(output.fileno())
        _check_cancel(cancelled)
        if received != manifest.archive.size or digest.hexdigest() != manifest.archive.sha256:
            raise UpdateDownloadError("Downloaded archive size or SHA-256 mismatch")
        os.replace(part, destination)
        return destination
    except UpdateCheckError as exc:
        part.unlink(missing_ok=True)
        raise UpdateDownloadError(f"Update download request failed: {exc}") from exc
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _inspect_entries(
    archive: zipfile.ZipFile, manifest: UpdateManifest
) -> dict[str, zipfile.ZipInfo]:
    expected = {item.path.casefold(): item for item in manifest.files}
    found: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            raise UpdateDownloadError("Directory entries are not part of signed file list")
        try:
            name = validate_managed_path(info.filename)
        except UpdateManifestError as exc:
            raise UpdateDownloadError(f"Unsafe archive member: {info.filename!r}") from exc
        folded = name.casefold()
        signed = expected.get(folded)
        if signed is None or name != signed.path or folded in found:
            raise UpdateDownloadError("Archive files differ from signed file list")
        mode = info.external_attr >> 16
        dos_attributes = info.external_attr & 0xFFFF
        if (
            info.flag_bits & 0x1
            or stat.S_ISLNK(mode)
            or (mode & stat.S_IFMT(mode) and not stat.S_ISREG(mode))
            or dos_attributes & 0x400
        ):
            raise UpdateDownloadError("Archive link, reparse point or encrypted member rejected")
        if info.file_size != signed.size or info.file_size > MAX_TOTAL_FILE_BYTES:
            raise UpdateDownloadError("Archive entry size conflicts with signed manifest")
        if info.compress_size == 0 and info.file_size != 0:
            raise UpdateDownloadError("Invalid archive compression size")
        if info.compress_size and info.file_size > info.compress_size * _MAX_COMPRESSION_RATIO:
            raise UpdateDownloadError("Archive expansion ratio exceeds limit")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise UpdateDownloadError("Unsupported archive compression method")
        total += info.file_size
        if total > MAX_TOTAL_FILE_BYTES:
            raise UpdateDownloadError("Archive exceeds expansion budget")
        found[folded] = info
    if found.keys() != expected.keys():
        raise UpdateDownloadError("Archive is missing signed files")
    return found


def extract_verified_archive(
    archive_path: Path,
    stage_directory: Path,
    manifest: UpdateManifest,
    *,
    cancelled: CancelCallback | None = None,
) -> Path:
    """Reverify ZIP and extract exact signed files to a newly published stage."""
    archive_path = Path(archive_path)
    stage_directory = Path(stage_directory)
    if stage_directory.exists():
        raise UpdateDownloadError("Update stage already exists")
    verify_archive(archive_path, manifest)
    stage_directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".update-stage-", dir=stage_directory.parent))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            found = _inspect_entries(archive, manifest)
            for signed in manifest.files:
                _check_cancel(cancelled)
                info = found[signed.path.casefold()]
                target = temporary.joinpath(*signed.path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source, target.open("xb") as output:
                    while block := source.read(_CHUNK):
                        _check_cancel(cancelled)
                        size += len(block)
                        if size > signed.size:
                            raise UpdateDownloadError("Archive member exceeds signed size")
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if size != signed.size or digest.hexdigest() != signed.sha256:
                    raise UpdateDownloadError(f"Archive member hash mismatch: {signed.path}")
        os.replace(temporary, stage_directory)
        return stage_directory
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, UpdateDownloadError | UpdateCancelled):
            raise
        raise UpdateDownloadError(f"Cannot stage update archive: {exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
