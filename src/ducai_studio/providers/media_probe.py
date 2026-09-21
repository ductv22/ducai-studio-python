from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ducai_studio.domain.timebase import TimeUs, seconds_to_us

from .contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)


@dataclass(frozen=True, slots=True)
class MediaProbeResult:
    duration_us: TimeUs
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None
    format_name: str = ""

    def __post_init__(self) -> None:
        if self.duration_us <= 0:
            raise ValueError("Media duration must be positive")
        if not self.has_video and not self.has_audio:
            raise ValueError("Media must contain video or audio")
        if (self.width is None) != (self.height is None):
            raise ValueError("Media dimensions must be both present or both absent")
        if self.width is not None and (self.width <= 0 or self.height is None or self.height <= 0):
            raise ValueError("Media dimensions must be positive")


class FfprobeProvider:
    provider_id = "ffprobe"
    kind = ProviderKind.MEDIA_PROBE

    def __init__(self, executable: str | Path = "ffprobe", *, timeout_seconds: int = 30) -> None:
        self.executable = str(executable)
        self.timeout_seconds = timeout_seconds

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {"streams": ["video", "audio"], "duration_unit": "microseconds"}
        )

    def health(self) -> ProviderHealth:
        executable = self._resolved_executable()
        available = executable is not None
        return ProviderHealth(
            self.provider_id,
            HealthState.READY if available else HealthState.UNAVAILABLE,
            datetime.now(UTC).isoformat(),
            "FFprobe sẵn sàng" if available else "Không tìm thấy FFprobe",
            {"executable": executable or ""},
        )

    def _resolved_executable(self) -> str | None:
        candidate = Path(self.executable).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        return shutil.which(self.executable)

    def probe(self, media_path: str | Path) -> MediaProbeResult:
        path = Path(media_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        executable = self._resolved_executable()
        if executable is None:
            raise RuntimeError("FFprobe is unavailable")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            creationflags=flags,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or ["unknown error"]
            raise RuntimeError(f"FFprobe failed: {detail[0][:300]}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("FFprobe returned invalid JSON") from exc
        streams = payload.get("streams", [])
        if not isinstance(streams, list):
            raise RuntimeError("FFprobe stream payload is invalid")
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        format_data = payload.get("format", {})
        duration_values = [
            value
            for value in [format_data.get("duration")]
            + [stream.get("duration") for stream in streams]
            if value not in {None, "N/A", ""}
        ]
        if not duration_values:
            raise RuntimeError("FFprobe did not report a positive duration")
        duration_us = max(seconds_to_us(value) for value in duration_values)
        primary_video = video_streams[0] if video_streams else None
        return MediaProbeResult(
            duration_us=duration_us,
            has_video=bool(video_streams),
            has_audio=bool(audio_streams),
            width=int(primary_video["width"])
            if primary_video and primary_video.get("width")
            else None,
            height=int(primary_video["height"])
            if primary_video and primary_video.get("height")
            else None,
            format_name=str(format_data.get("format_name", "")),
        )
