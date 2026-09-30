from __future__ import annotations

import math
from dataclasses import dataclass, field

from .project import CUE_BACKGROUND_STYLES


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
    font_size: int = 0
    orientation: str = "horizontal"
    background_color: str = ""
    text_color: str = ""
    panel_x: float = -1.0
    panel_y: float = -1.0
    panel_width: float = 0.0
    panel_height: float = 0.0
    font_family: str = ""
    letter_spacing_percent: int = 0
    font_bold: bool = False
    font_italic: bool = False
    text_opacity_percent: int = 100
    background_opacity_percent: int = 90

    def __post_init__(self) -> None:
        if not self.cue_id or self.index <= 0 or self.end_us <= self.start_us:
            raise ValueError("Subtitle identity and time range are invalid")
        if not self.text.strip():
            raise ValueError("Subtitle text cannot be empty")
        if type(self.font_size) is not int or self.font_size < 0:
            raise ValueError("Subtitle font size is invalid")
        if self.orientation not in {"horizontal", "vertical"}:
            raise ValueError("Subtitle orientation is invalid")
        if (
            type(self.letter_spacing_percent) is not int
            or not 0 <= self.letter_spacing_percent <= 100
        ):
            raise ValueError("Subtitle letter spacing is invalid")
        if type(self.font_bold) is not bool or type(self.font_italic) is not bool:
            raise TypeError("Subtitle font flags are invalid")
        if (
            type(self.text_opacity_percent) is not int
            or type(self.background_opacity_percent) is not int
            or not 0 <= self.text_opacity_percent <= 100
            or not 0 <= self.background_opacity_percent <= 100
        ):
            raise ValueError("Subtitle opacity is invalid")
        for label, color in (
            ("background", self.background_color),
            ("text", self.text_color),
        ):
            if color:
                if len(color) != 7 or not color.startswith("#"):
                    raise ValueError(f"Subtitle {label} color is invalid")
                int(color[1:], 16)
        has_panel = self.panel_x >= 0 or self.panel_y >= 0
        if has_panel and (
            self.panel_x < 0
            or self.panel_y < 0
            or self.panel_width <= 0
            or self.panel_height <= 0
            or self.panel_x + self.panel_width > 1.000001
            or self.panel_y + self.panel_height > 1.000001
        ):
            raise ValueError("Subtitle panel geometry is invalid")


@dataclass(frozen=True, slots=True)
class RenderSubtitleBlur:
    cue_id: str
    index: int
    start_us: int
    end_us: int
    x: float
    y: float
    width: float
    height: float
    blur_radius: int = 22
    opacity: float = 1.0
    region_source: str = "estimated"
    concealment_mode: str = "blur"

    def __post_init__(self) -> None:
        if not self.cue_id or self.index <= 0 or self.end_us <= self.start_us:
            raise ValueError("Subtitle blur identity and time range are invalid")
        if self.region_source not in {"ocr", "estimated"}:
            raise ValueError("Subtitle blur region source is invalid")
        if type(self.blur_radius) is not int or not 1 <= self.blur_radius <= 100:
            raise ValueError("Subtitle blur radius is invalid")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("Subtitle blur opacity is invalid")
        if self.concealment_mode not in {"blur", "cover"}:
            raise ValueError("Subtitle blur concealment mode is invalid")
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("Subtitle blur geometry is invalid")
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("Subtitle blur geometry must stay inside the frame")


@dataclass(frozen=True, slots=True)
class RenderSceneText:
    cue_id: str
    index: int
    start_us: int
    end_us: int
    text: str
    glyph_x: float
    glyph_y: float
    glyph_width: float
    glyph_height: float
    panel_x: float
    panel_y: float
    panel_width: float
    panel_height: float
    orientation: str = "horizontal"
    style_hint: str = "display"
    font_family: str = "Be Vietnam Pro"
    font_size: int = 42
    background_color: str = ""
    region_source: str = "ocr"
    review_state: str = "accepted"
    blur_radius: int = 24
    text_color: str = ""
    background_padding_x: int = 20
    background_padding_y: int = 10
    background_style: str = "solid"
    letter_spacing_percent: int = 0
    font_bold: bool = False
    font_italic: bool = False
    text_opacity_percent: int = 100
    background_opacity_percent: int = 90
    fade_enabled: bool = False
    fade_duration_ms: int = 300

    def __post_init__(self) -> None:
        if not self.cue_id or self.index <= 0 or self.end_us <= self.start_us:
            raise ValueError("Scene text identity and time range are invalid")
        if not self.text.strip():
            raise ValueError("Scene text cannot be empty")
        for label, box in (
            (
                "glyph",
                (self.glyph_x, self.glyph_y, self.glyph_width, self.glyph_height),
            ),
            (
                "panel",
                (self.panel_x, self.panel_y, self.panel_width, self.panel_height),
            ),
        ):
            x, y, width, height = box
            if x < 0 or y < 0 or width <= 0 or height <= 0:
                raise ValueError(f"Scene text {label} geometry is invalid")
            if x + width > 1.000001 or y + height > 1.000001:
                raise ValueError(f"Scene text {label} geometry exceeds the frame")
        if self.orientation not in {"horizontal", "vertical"}:
            raise ValueError("Scene text orientation is invalid")
        if self.style_hint not in {
            "standard",
            "display",
            "calligraphy",
            "calligraphy_soft",
            "calligraphy_bold",
            "calligraphy_ink",
            "calligraphy_brush",
            "calligraphy_water",
            "calligraphy_splash",
            "calligraphy_hurricane",
        }:
            raise ValueError("Scene text style hint is invalid")
        if not self.font_family.strip() or not 1 <= self.font_size <= 16_384:
            raise ValueError("Scene text font is invalid")
        if self.background_color:
            if len(self.background_color) != 7 or not self.background_color.startswith("#"):
                raise ValueError("Scene text background color must use #RRGGBB")
            int(self.background_color[1:], 16)
        if self.text_color:
            if len(self.text_color) != 7 or not self.text_color.startswith("#"):
                raise ValueError("Scene text foreground color must use #RRGGBB")
            int(self.text_color[1:], 16)
        if self.region_source not in {"ocr", "estimated"}:
            raise ValueError("Scene text region source is invalid")
        if self.review_state not in {"accepted", "review"}:
            raise ValueError("Scene text review state is invalid")
        if type(self.blur_radius) is not int or not 1 <= self.blur_radius <= 100:
            raise ValueError("Scene text blur radius is invalid")
        if (
            type(self.background_padding_x) is not int
            or type(self.background_padding_y) is not int
            or not 0 <= self.background_padding_x <= 100
            or not 0 <= self.background_padding_y <= 100
        ):
            raise ValueError("Scene text background padding is invalid")
        if self.background_style not in CUE_BACKGROUND_STYLES:
            raise ValueError("Scene text background style is invalid")
        if (
            type(self.letter_spacing_percent) is not int
            or not 0 <= self.letter_spacing_percent <= 100
        ):
            raise ValueError("Scene text letter spacing is invalid")
        if type(self.font_bold) is not bool or type(self.font_italic) is not bool:
            raise TypeError("Scene text font flags are invalid")
        if (
            type(self.text_opacity_percent) is not int
            or type(self.background_opacity_percent) is not int
            or not 0 <= self.text_opacity_percent <= 100
            or not 0 <= self.background_opacity_percent <= 100
        ):
            raise ValueError("Scene text opacity is invalid")
        if type(self.fade_enabled) is not bool:
            raise TypeError("Scene text fade flag is invalid")
        if type(self.fade_duration_ms) is not int or not 50 <= self.fade_duration_ms <= 2_000:
            raise ValueError("Scene text fade duration is invalid")


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
    subtitle_blurs: tuple[RenderSubtitleBlur, ...] = ()
    scene_texts: tuple[RenderSceneText, ...] = ()

    @property
    def duration_us(self) -> int:
        return self.timeline_end_us - self.timeline_start_us

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2, 3, 4}:
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
