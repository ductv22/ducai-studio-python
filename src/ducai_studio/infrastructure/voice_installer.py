from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ducai_studio.providers.tts.catalog import VoiceCatalog, VoiceDownloadFile

ProgressCallback = Callable[[int], None]


class VoiceInstallError(RuntimeError):
    """Raised when a catalog voice cannot be published safely."""


class PiperVoiceInstaller:
    """Download a verified Piper model without exposing partial files to the catalog."""

    def __init__(self, catalog: VoiceCatalog, *, timeout_seconds: float = 120.0) -> None:
        self.catalog = catalog
        self.timeout_seconds = max(10.0, float(timeout_seconds))

    def install(self, voice_id: str, progress: ProgressCallback | None = None) -> None:
        entry = self.catalog.get(voice_id)
        if entry.installed:
            if progress is not None:
                progress(100)
            return
        if not entry.installable or not entry.download_files:
            raise VoiceInstallError("Giọng này chưa có nguồn tải được xác minh")

        self.catalog.voice_root.mkdir(parents=True, exist_ok=True)
        total_bytes = sum(item.size_bytes for item in entry.download_files)
        completed_bytes = 0
        staged: list[tuple[VoiceDownloadFile, Path, Path]] = []
        try:
            for item in entry.download_files:
                target = self.catalog.voice_root / item.filename
                part = target.with_name(target.name + ".part")
                part.unlink(missing_ok=True)
                completed_bytes = self._download(
                    item,
                    part,
                    completed_bytes=completed_bytes,
                    total_bytes=total_bytes,
                    progress=progress,
                )
                staged.append((item, part, target))
            config = next(
                (part for item, part, _target in staged if item.filename.endswith(".json")),
                None,
            )
            if config is None or not _valid_piper_config(config):
                raise VoiceInstallError("Cấu hình model tải về không hợp lệ")
            for _item, part, target in staged:
                os.replace(part, target)
        except Exception:
            for _item, part, _target in staged:
                part.unlink(missing_ok=True)
            for item in entry.download_files:
                (self.catalog.voice_root / f"{item.filename}.part").unlink(missing_ok=True)
            raise
        if progress is not None:
            progress(100)

    def _download(
        self,
        item: VoiceDownloadFile,
        destination: Path,
        *,
        completed_bytes: int,
        total_bytes: int,
        progress: ProgressCallback | None,
    ) -> int:
        digest = hashlib.md5(usedforsecurity=False)
        received = 0
        request = urllib.request.Request(
            item.url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "DUCAI-STUDIO/voice-installer",
            },
        )
        try:
            with (
                urllib.request.urlopen(request, timeout=self.timeout_seconds) as response,
                destination.open("xb") as output,
            ):
                while block := response.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
                    received += len(block)
                    if progress is not None and total_bytes:
                        progress(min(99, int((completed_bytes + received) * 100 / total_bytes)))
                output.flush()
                os.fsync(output.fileno())
        except (OSError, urllib.error.URLError) as exc:
            raise VoiceInstallError(f"Không tải được {item.filename}: {exc}") from exc
        if received != item.size_bytes:
            raise VoiceInstallError(
                f"Dung lượng {item.filename} không đúng: {received}/{item.size_bytes} byte"
            )
        if digest.hexdigest().casefold() != item.md5.casefold():
            raise VoiceInstallError(f"Mã kiểm tra {item.filename} không khớp")
        return completed_bytes + received


def _valid_piper_config(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sample_rate = int((payload.get("audio") or {}).get("sample_rate") or 0)
        return 8_000 <= sample_rate <= 192_000 and isinstance(
            payload.get("phoneme_id_map"), dict
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
