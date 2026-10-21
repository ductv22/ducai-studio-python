from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import monotonic
from uuid import uuid4


class TileKind(StrEnum):
    THUMBNAIL = "thumbnail"
    WAVEFORM = "waveform"


@dataclass(frozen=True, slots=True)
class PreviewTileRequest:
    media_path: Path
    media_fingerprint: str
    kind: TileKind
    start_us: int
    duration_us: int
    width: int
    height: int

    def __post_init__(self) -> None:
        path = self.media_path.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if not self.media_fingerprint.strip():
            raise ValueError("media_fingerprint cannot be empty")
        if type(self.start_us) is not int or self.start_us < 0:
            raise ValueError("start_us must be a non-negative integer")
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("duration_us must be a positive integer")
        if type(self.width) is not int or type(self.height) is not int:
            raise ValueError("tile dimensions must be integers")
        if not 16 <= self.width <= 4096 or not 16 <= self.height <= 2160:
            raise ValueError("tile dimensions are outside the supported range")
        object.__setattr__(self, "media_path", path)

    @property
    def cache_key(self) -> str:
        raw = "|".join(
            (
                "v2" if self.kind is TileKind.WAVEFORM else "v1",
                self.media_fingerprint,
                self.kind.value,
                str(self.start_us),
                str(self.duration_us),
                str(self.width),
                str(self.height),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fast_media_fingerprint(media_path: str | Path) -> str:
    """Fingerprint identity without hashing a multi-hour video on the UI path."""
    path = Path(media_path).expanduser().resolve()
    stat = path.stat()
    raw = f"{path}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PreviewTileCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, request: PreviewTileRequest) -> Path:
        return self.root / request.kind.value / request.cache_key[:2] / (
            request.cache_key + ".png"
        )

    def get(self, request: PreviewTileRequest) -> Path | None:
        path = self.path_for(request)
        return path if path.is_file() and path.stat().st_size > 0 else None

    def commit(self, request: PreviewTileRequest, staged_path: str | Path) -> Path:
        staged = Path(staged_path).resolve()
        if not staged.is_file() or staged.stat().st_size <= 0:
            raise ValueError("Generated preview tile is empty")
        target = self.path_for(request)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.stem}-{uuid4().hex}.tmp.png")
        shutil.copyfile(staged, temporary)
        os.replace(temporary, target)
        return target


class FfmpegPreviewTileProvider:
    """Render real tiles in a worker process; never call this on the GUI thread."""

    def __init__(self, ffmpeg_path: str | Path | None = None, *, timeout: float = 30) -> None:
        requested = str(ffmpeg_path) if ffmpeg_path is not None else "ffmpeg"
        candidate = Path(requested).expanduser()
        executable = str(candidate.resolve()) if candidate.is_file() else shutil.which(requested)
        if not executable:
            raise FileNotFoundError("ffmpeg executable was not found")
        self.ffmpeg_path = str(Path(executable).resolve())
        self.timeout = timeout

    def render(
        self,
        request: PreviewTileRequest,
        cache: PreviewTileCache,
    ) -> tuple[Path, bool]:
        cached = cache.get(request)
        if cached is not None:
            return cached, True
        staging_root = cache.root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staged = staging_root / f"{request.cache_key}-{uuid4().hex}.png"
        try:
            command = self._command(request, staged)
            deadline = monotonic() + self.timeout
            for attempt in range(2):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, self.timeout)
                result = subprocess.run(
                    command, capture_output=True, timeout=remaining, check=False,
                    creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"FFmpeg preview tile failed with exit code {result.returncode}"
                    )
                if (staged.is_file() and staged.stat().st_size > 0
                        or attempt or request.kind is not TileKind.THUMBNAIL
                        or not request.start_us):
                    break
                # Seeking between the final frame PTS and EOF can succeed
                # without a frame. Only then try the last frame before the
                # requested time, reversing a one-second window at tile size.
                # Long-GOP sources may require additional decoder seek preroll.
                command = self._command(request, staged, previous_frame=True)
            return cache.commit(request, staged), False
        finally:
            staged.unlink(missing_ok=True)

    def _command(
        self, request: PreviewTileRequest, output: Path, *, previous_frame: bool = False,
    ) -> list[str]:
        start = f"{request.start_us / 1_000_000:.6f}"
        if request.kind is TileKind.THUMBNAIL:
            earlier = max(0, request.start_us - 1_000_000)
            if previous_frame:
                start = f"{earlier / 1_000_000:.6f}"
            return [
                self.ffmpeg_path,
                "-v",
                "error",
                "-threads", "1",
                "-ss",
                start,
                *(["-t", f"{(request.start_us - earlier) / 1_000_000:.6f}"]
                  if previous_frame else []),
                "-i",
                str(request.media_path),
                "-frames:v",
                "1",
                "-vf",
                (
                    f"scale={request.width}:{request.height}:"
                    "force_original_aspect_ratio=decrease" + (",reverse" if previous_frame else "")
                ),
                "-y",
                str(output),
            ]
        duration = f"{request.duration_us / 1_000_000:.6f}"
        return [
            self.ffmpeg_path,
            "-v",
            "error",
            "-threads", "1",
            "-ss",
            start,
            "-t",
            duration,
            "-i",
            str(request.media_path),
            "-filter_complex",
            # Rectify before reducing sample rate so high-frequency speech
            # retains its envelope. Memory is bounded even for long overviews.
            "aformat=channel_layouts=mono,aeval=abs(val(0)),aresample=2000,"
            f"showwavespic=s={request.width}x{request.height}:colors=#27BFA3:scale=sqrt",
            "-frames:v",
            "1",
            "-y",
            str(output),
        ]
