from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SubtitleStyle:
    enabled: bool = True
    font_family: str = "Arial"
    font_size: int = 42
    margin_bottom: int = 28
    primary_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 3
    alignment: int = 2
    opacity_percent: int = 100

    def __post_init__(self) -> None:
        if not self.font_family.strip():
            raise ValueError("Subtitle font family is required")
        if not 12 <= self.font_size <= 120:
            raise ValueError("Subtitle font size must be between 12 and 120")
        if not 0 <= self.margin_bottom <= 500:
            raise ValueError("Subtitle bottom margin is invalid")
        if not 0 <= self.outline_width <= 12:
            raise ValueError("Subtitle outline width is invalid")
        if self.alignment not in {1, 2, 3}:
            raise ValueError("Subtitle alignment must be left, center or right")
        if not 0 <= self.opacity_percent <= 100:
            raise ValueError("Subtitle opacity must be between 0 and 100")
        for value in (self.primary_color, self.outline_color):
            if len(value) != 7 or not value.startswith("#"):
                raise ValueError("Subtitle colors must use #RRGGBB")
            int(value[1:], 16)


@dataclass(frozen=True, slots=True)
class VideoFilterSettings:
    preset_id: str = "none"
    intensity_percent: int = 100
    brightness_percent: int = 0
    contrast_percent: int = 100
    saturation_percent: int = 100

    def __post_init__(self) -> None:
        if self.preset_id not in {"none", "cinematic-warm", "cool", "grayscale"}:
            raise ValueError("Unknown video filter preset")
        if not 0 <= self.intensity_percent <= 100:
            raise ValueError("Video filter intensity must be between 0 and 100")
        if not -50 <= self.brightness_percent <= 50:
            raise ValueError("Video brightness must be between -50 and 50")
        if not 50 <= self.contrast_percent <= 150:
            raise ValueError("Video contrast must be between 50 and 150")
        if not 0 <= self.saturation_percent <= 200:
            raise ValueError("Video saturation must be between 0 and 200")


@dataclass(frozen=True, slots=True)
class ExportSettings:
    preset_id: str = "source-quality"
    backend: str = "auto"
    video_bitrate: str = "6M"
    audio_bitrate: str = "256k"
    output_width: int | None = None
    output_height: int | None = None
    fps: int = 30
    chunk_duration_us: int = 600_000_000
    overwrite: bool = False
    subtitle_style: SubtitleStyle = field(default_factory=SubtitleStyle)
    video_filter: VideoFilterSettings = field(default_factory=VideoFilterSettings)

    def __post_init__(self) -> None:
        if self.backend not in {"auto", "cpu", "nvenc"}:
            raise ValueError("Export backend must be auto, cpu or nvenc")
        if not self.preset_id.strip() or not self.video_bitrate.strip():
            raise ValueError("Export preset and bitrate are required")
        if not 10 <= self.fps <= 120:
            raise ValueError("Export FPS must be between 10 and 120")
        if not 30_000_000 <= self.chunk_duration_us <= 3_600_000_000:
            raise ValueError("Export chunks must be between 30 seconds and 60 minutes")
        if (self.output_width is None) != (self.output_height is None):
            raise ValueError("Export width and height must be both set or both omitted")
        if self.output_width is not None:
            if self.output_width < 160 or self.output_height is None or self.output_height < 90:
                raise ValueError("Export resolution is too small")
            if self.output_width % 2 or self.output_height % 2:
                raise ValueError("Export resolution must use even dimensions")


@dataclass(frozen=True, slots=True)
class RenderClip:
    clip_id: str
    track_id: str
    track_kind: str
    path: str
    timeline_start_us: int
    timeline_end_us: int
    source_start_us: int
    source_end_us: int
    speed_numerator: int = 1
    speed_denominator: int = 1
    volume: float = 1.0
    muted: bool = False

    def __post_init__(self) -> None:
        if not self.clip_id or not self.track_id or not self.path:
            raise ValueError("Render clip identity and path are required")
        if self.timeline_end_us <= self.timeline_start_us:
            raise ValueError("Render clip timeline range is invalid")
        if self.source_end_us <= self.source_start_us:
            raise ValueError("Render clip source range is invalid")
        if self.speed_numerator <= 0 or self.speed_denominator <= 0:
            raise ValueError("Render clip speed must be positive")
        if not math.isfinite(self.volume) or not 0.0 <= self.volume <= 2.0:
            raise ValueError("Render clip volume is invalid")


@dataclass(frozen=True, slots=True)
class RenderOverlay:
    overlay_id: str
    kind: str
    start_us: int
    end_us: int
    x: float
    y: float
    width: float
    height: float
    asset_path: str = ""
    opacity: float = 1.0
    blur_radius: int = 18


@dataclass(frozen=True, slots=True)
class RenderSubtitle:
    cue_id: str
    index: int
    start_us: int
    end_us: int
    text: str


@dataclass(frozen=True, slots=True)
class RenderConformSegment:
    source_start_us: int
    source_end_us: int
    output_start_us: int
    output_end_us: int
    video_speed: float

    def __post_init__(self) -> None:
        if self.source_start_us < 0 or self.source_end_us <= self.source_start_us:
            raise ValueError("Render conform source range is invalid")
        if self.output_start_us < 0 or self.output_end_us <= self.output_start_us:
            raise ValueError("Render conform output range is invalid")
        if not math.isfinite(self.video_speed) or not 0.0 < self.video_speed <= 1.0:
            raise ValueError("Render conform video speed is invalid")


@dataclass(frozen=True, slots=True)
class ExportSnapshot:
    schema_version: int
    project_id: str
    project_revision: int
    timeline_start_us: int
    timeline_end_us: int
    source_width: int
    source_height: int
    video: RenderClip
    audio: tuple[RenderClip, ...]
    overlays: tuple[RenderOverlay, ...]
    subtitles: tuple[RenderSubtitle, ...]
    settings: ExportSettings
    conform_segments: tuple[RenderConformSegment, ...] = ()

    @property
    def duration_us(self) -> int:
        return self.timeline_end_us - self.timeline_start_us

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2}:
            raise ValueError("Unsupported export snapshot schema")
        if not self.project_id or self.project_revision <= 0:
            raise ValueError("Export snapshot project identity is invalid")
        if self.timeline_end_us <= self.timeline_start_us:
            raise ValueError("Export snapshot duration is invalid")
        if self.source_width <= 0 or self.source_height <= 0:
            raise ValueError("Export source resolution is invalid")
        if not self.conform_segments and (
            self.video.timeline_start_us > self.timeline_start_us
            or self.video.timeline_end_us < self.timeline_end_us
        ):
            raise ValueError("One-pass export video does not cover the snapshot")
        if self.conform_segments:
            previous_source = self.video.source_start_us
            previous_output = self.timeline_start_us
            for segment in self.conform_segments:
                if (
                    segment.source_start_us != previous_source
                    or segment.output_start_us != previous_output
                ):
                    raise ValueError("Render conform segments must be contiguous")
                previous_source = segment.source_end_us
                previous_output = segment.output_end_us
            if (
                previous_source != self.video.source_end_us
                or previous_output != self.timeline_end_us
            ):
                raise ValueError("Render conform segments do not cover the export")
