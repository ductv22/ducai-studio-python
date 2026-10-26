from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .dialogue_casting import DialogueCastingState
from .timebase import SpeedRatio, TimeRange, TimeUs
from .voice_conform import VoiceConformPlan

TRANSLATION_STYLE_PRESETS = frozenset(
    {"auto", "xianxia", "wuxia", "palace", "modern", "neutral"}
)
CUE_ROLES = frozenset({"dialogue", "scene_text", "title_text"})
CUE_ORIENTATIONS = frozenset({"horizontal", "vertical"})
CUE_BACKGROUND_STYLES = frozenset(
    {
        "solid",
        "none",
        "blur",
        "ink_splash",
        "ink_wash",
        "ink_cloud",
        "brush_stroke",
    }
)
CUE_STYLE_HINTS = frozenset(
    {
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
    }
)
CUE_REVIEW_STATES = frozenset({"accepted", "review"})
SCENE_TEXT_DEFAULT_STYLE_HINT = "calligraphy_brush"
SCENE_TEXT_DEFAULT_FONT_SIZE = 40
SCENE_TEXT_DEFAULT_BACKGROUND_STYLE = "ink_splash"
SCENE_TEXT_DEFAULT_TEXT_COLOR = "#FFFFFF"
SCENE_TEXT_DEFAULT_BACKGROUND_COLOR = "#000000"
SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT = 100
SCENE_TEXT_DEFAULT_FADE_ENABLED = True
SCENE_TEXT_DEFAULT_FADE_DURATION_MS = 300


def _validate_normalized_box(
    value: tuple[float, float, float, float] | None,
    *,
    label: str,
) -> None:
    if value is None:
        return
    if len(value) != 4:
        raise ValueError(f"{label} requires x, y, width and height")
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
        raise TypeError(f"{label} values must be numeric")
    x, y, width, height = value
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError(f"{label} geometry is invalid")
    if x + width > 1.000001 or y + height > 1.000001:
        raise ValueError(f"{label} must stay inside the frame")


def grow_dialogue_panel_for_font(box, previous_size: int, next_size: int):
    """Keep an edited dialogue's anchor while allowing a larger requested font.

    A measured placement created by dragging is not a permanent font-size cap.
    Decreasing the font needs no panel change; measured backgrounds still shrink.
    """
    if box is None or next_size <= previous_size:
        return box
    x, y, width, height = box
    scale = next_size / max(1, previous_size)
    new_width, new_height = min(1., width * scale), min(1., height * scale)
    return (max(0., min(1. - new_width, x + (width - new_width) / 2)),
            max(0., min(1. - new_height, y + (height - new_height) / 2)),
            new_width, new_height)


def normalized_text_anchor(value: object) -> tuple[float, float] | None:
    """Validate a placement point independently of source crop or panel size."""
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("Text anchor requires two normalized coordinates")
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool)
               and math.isfinite(item) and 0 <= item <= 1 for item in value):
        raise ValueError("Text anchor must stay inside the frame")
    return float(value[0]), float(value[1])


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    SUBTITLE = "subtitle"


class TrackKind(StrEnum):
    VIDEO = "video"
    ORIGINAL_AUDIO = "original_audio"
    VOICE = "voice"
    BACKGROUND_MUSIC = "background_music"
    SUBTITLE = "subtitle"
    TEXT = "text"
    LOGO = "logo"
    BLUR = "blur"


class OverlayKind(StrEnum):
    LOGO = "logo"
    BLUR = "blur"


@dataclass(frozen=True, slots=True)
class MediaAsset:
    id: str
    path: str
    kind: MediaKind
    duration_us: TimeUs
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.path.strip():
            raise ValueError("Media id and path are required")
        if self.duration_us <= 0:
            raise ValueError("Media duration must be positive")


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    id: str
    index: int
    range: TimeRange
    text: str
    language: str
    revision: int = 1
    source: str = "manual"
    confidence: float | None = None
    evidence_ids: tuple[str, ...] = ()
    # Full-frame normalized OCR geometry. Old/imported SRT cues legitimately
    # omit it; preview/export then derive a conservative estimated region.
    visual_box: tuple[float, float, float, float] | None = None
    visual_box_source: str = ""
    # Scene-text metadata is optional and deliberately lives on the same cue
    # identity used by translation.  Old project snapshots omit these fields
    # and therefore continue to decode as ordinary spoken dialogue.
    role: str = "dialogue"
    orientation: str = "horizontal"
    style_hint: str = "standard"
    panel_box: tuple[float, float, float, float] | None = None
    background_color: str = ""
    # Optional per-cue foreground color. Empty keeps the project/export
    # subtitle primary color so legacy projects retain their visual result.
    text_color: str = ""
    review_state: str = "accepted"
    # Per-cue visual tuning for translated scene text. It multiplies the
    # source-glyph-aware automatic size. The panel is only a placement handle;
    # Preview/Export bound painting at the frame/renderer safety limit instead
    # of silently shrinking an explicit size. Optional at the JSON boundary so
    # projects created before a110 continue at 100%.
    scene_font_scale: float = 1.0
    # User-facing subtitle size used by the shared S1/S2/S3 formatting bar.
    # Twelve preserves the legacy automatic scene-text fit. Preview/export
    # resolve this relative value against the video canvas and detected panel.
    text_font_size: int = 12
    # Optional durable lane identity. Legacy snapshots infer S1 for dialogue
    # and S2 for scene text.
    subtitle_track_id: str = ""
    # Padding around the rendered Vietnamese label, expressed as a percentage
    # of its font size.  This is independent from OCR glyph geometry: shrinking
    # the label background must not expose or move the source-text concealment.
    background_padding_x: int = 20
    background_padding_y: int = 10
    # Optional visual treatment and Word-like typography controls. Defaults
    # preserve the a114 appearance for projects saved before these fields
    # existed.
    background_style: str = "solid"
    letter_spacing_percent: int = 0
    font_bold: bool = False
    font_italic: bool = False
    text_opacity_percent: int = 100
    background_opacity_percent: int = 90
    # Scene-label entrance/exit treatment. Dialogue ignores these values;
    # Preview/Export clamp the requested fade to at most half the cue.
    fade_enabled: bool = False
    fade_duration_ms: int = 300
    # OCR supplies only a centre point for dialogue placement, not a fitting
    # rectangle. An explicitly edited panel_box takes precedence over it.
    text_anchor: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or self.index <= 0:
            raise ValueError("Cue id and positive index are required")
        if not self.text.strip():
            raise ValueError("Cue text cannot be empty")
        if not self.language.strip() or self.revision <= 0:
            raise ValueError("Cue language and positive revision are required")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Cue confidence must be between 0 and 1")
        _validate_normalized_box(self.visual_box, label="Cue visual box")
        _validate_normalized_box(self.panel_box, label="Cue panel box")
        object.__setattr__(self, "text_anchor", normalized_text_anchor(self.text_anchor))
        if self.visual_box is not None:
            if self.visual_box_source not in {"ocr", "estimated"}:
                raise ValueError("Cue visual box source must be OCR or estimated")
        elif self.visual_box_source:
            raise ValueError("Cue visual box source requires geometry")
        if self.role not in CUE_ROLES:
            raise ValueError("Cue role must be dialogue, scene_text or title_text")
        if self.orientation not in CUE_ORIENTATIONS:
            raise ValueError("Cue orientation must be horizontal or vertical")
        if self.style_hint not in CUE_STYLE_HINTS:
            raise ValueError("Cue style hint is invalid")
        if self.background_style not in CUE_BACKGROUND_STYLES:
            raise ValueError("Background style is invalid")
        if self.review_state not in CUE_REVIEW_STATES:
            raise ValueError("Cue review state is invalid")
        if (
            isinstance(self.scene_font_scale, bool)
            or not isinstance(self.scene_font_scale, (int, float))
            or not math.isfinite(self.scene_font_scale)
            or not 0.5 <= self.scene_font_scale <= 2.0
        ):
            raise ValueError("Kích thước chữ S2 phải nằm trong khoảng 50% đến 200%")
        if type(self.text_font_size) is not int or self.text_font_size < 1:
            raise ValueError("Kích thước chữ phải là số nguyên dương")
        if self.subtitle_track_id and (
            not self.subtitle_track_id.startswith("T" if self.role == "title_text" else "S")
            or not self.subtitle_track_id[1:].isdigit()
            or int(self.subtitle_track_id[1:]) <= 0
        ):
            raise ValueError("Text track id must use T1…; subtitle track id must use S1…")
        if (
            type(self.background_padding_x) is not int
            or type(self.background_padding_y) is not int
            or self.background_padding_x < 0
            or self.background_padding_y < 0
        ):
            raise ValueError("Background padding must be a nonnegative integer")
        if (
            type(self.letter_spacing_percent) is not int
            or self.letter_spacing_percent < 0
        ):
            raise ValueError("Letter spacing must be a nonnegative integer")
        if type(self.font_bold) is not bool or type(self.font_italic) is not bool:
            raise TypeError("Cue bold and italic flags must be boolean")
        if (
            type(self.text_opacity_percent) is not int
            or type(self.background_opacity_percent) is not int
            or not 0 <= self.text_opacity_percent <= 100
            or not 0 <= self.background_opacity_percent <= 100
        ):
            raise ValueError("Cue opacity must be between 0 and 100")
        if type(self.fade_enabled) is not bool:
            raise TypeError("Cue fade flag must be boolean")
        if type(self.fade_duration_ms) is not int or not 50 <= self.fade_duration_ms <= 2_000:
            raise ValueError("Cue fade duration must be between 50 and 2000 ms")
        for label, color in (
            ("background", self.background_color),
            ("text", self.text_color),
        ):
            if color:
                if len(color) != 7 or not color.startswith("#"):
                    raise ValueError(f"Cue {label} color must use #RRGGBB")
                int(color[1:], 16)

    @property
    def is_dialogue(self) -> bool:
        return self.role == "dialogue"

    @property
    def is_scene_text(self) -> bool:
        return self.role == "scene_text"


def dialogue_cues(cues: tuple[SubtitleCue, ...]) -> tuple[SubtitleCue, ...]:
    return tuple(cue for cue in cues if cue.is_dialogue)


def scene_text_cues(cues: tuple[SubtitleCue, ...]) -> tuple[SubtitleCue, ...]:
    return tuple(cue for cue in cues if cue.is_scene_text)


def subtitle_track_number(track_id: str, *, fallback: int = 2) -> int:
    value = str(track_id or "").strip()
    if value.startswith("S") and value[1:].isdigit():
        number = int(value[1:])
        if number > 0:
            return number
    return fallback


def subtitle_ranges_overlap(first: TimeRange, second: TimeRange) -> bool:
    return first.start_us < second.end_us and second.start_us < first.end_us


def allocate_scene_text_lanes(
    cues: Iterable[SubtitleCue],
    *,
    preserve_explicit: bool = False,
) -> tuple[SubtitleCue, ...]:
    """Assign simultaneous scene labels to deterministic S2…Sn lanes.

    New OCR publication leaves scene lanes empty and therefore uses ordinary
    interval partitioning. Compatibility decoding additionally reserves valid
    explicit lanes first so manual or user-positioned cues are not silently
    moved while legacy automatic S2 overlaps are repaired around them.
    """

    values = tuple(cues)
    assigned: dict[str, str] = {}
    occupied: dict[int, list[TimeRange]] = {}
    for cue in values:
        if cue.is_dialogue:
            lane = (
                subtitle_track_number(cue.subtitle_track_id, fallback=1)
                if preserve_explicit
                else 1
            )
            assigned[cue.id] = f"S{lane}"
            occupied.setdefault(lane, []).append(cue.range)

    if preserve_explicit:
        explicit = sorted(
            (
                cue
                for cue in values
                if cue.is_scene_text
                and subtitle_track_number(cue.subtitle_track_id, fallback=0) >= 1
            ),
            key=lambda item: (
                item.source != "manual_scene_text",
                item.range.start_us,
                item.range.end_us,
                item.index,
                item.id,
            ),
        )
        for cue in explicit:
            lane = subtitle_track_number(cue.subtitle_track_id, fallback=0)
            if any(
                subtitle_ranges_overlap(cue.range, item)
                for item in occupied.get(lane, ())
            ):
                continue
            assigned[cue.id] = f"S{lane}"
            occupied.setdefault(lane, []).append(cue.range)

    for cue in sorted(
        values,
        key=lambda item: (
            item.range.start_us,
            item.range.end_us,
            item.index,
            item.id,
        ),
    ):
        if cue.is_dialogue or cue.id in assigned:
            continue
        lane = 2
        while any(
            subtitle_ranges_overlap(cue.range, item)
            for item in occupied.get(lane, ())
        ):
            lane += 1
        occupied.setdefault(lane, []).append(cue.range)
        assigned[cue.id] = f"S{lane}"
    return tuple(
        replace(cue, subtitle_track_id=assigned.get(cue.id, cue.subtitle_track_id))
        for cue in values
    )


@dataclass(frozen=True, slots=True)
class TimelineClip:
    id: str
    track_id: str
    timeline_range: TimeRange
    source_range: TimeRange | None = None
    media_id: str | None = None
    link_group_id: str | None = None
    speed: SpeedRatio = field(default_factory=lambda: SpeedRatio(1, 1))
    volume: float = 1.0
    payload_revision: int = 1
    # Optional cue metadata used by the cue-level D1 timeline.  These fields
    # are deliberately appended so old positional constructors and project
    # files remain compatible.
    cue_id: str | None = None
    cue_index: int | None = None
    cue_text: str = ""
    timing_slot: TimeRange | None = None
    timing_unresolved: bool = False
    content_warning: bool = False
    issue_code: str = ""
    muted: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.track_id.strip():
            raise ValueError("Clip id and track id are required")
        if type(self.muted) is not bool:
            raise TypeError("Clip muted state must be boolean")
        if not 0.0 <= self.volume <= 2.0:
            raise ValueError("Clip volume must be between 0 and 2")
        if self.payload_revision <= 0:
            raise ValueError("Clip revision must be positive")
        if self.cue_id is not None and not self.cue_id.strip():
            raise ValueError("Clip cue id cannot be blank")
        if self.cue_index is not None and (
            type(self.cue_index) is not int or self.cue_index <= 0
        ):
            raise ValueError("Clip cue index must be positive")
        if self.timing_slot is not None and self.timing_slot.duration_us <= 0:
            raise ValueError("Clip timing slot must be positive")
        if self.link_group_id is not None and not self.link_group_id.strip():
            raise ValueError("Clip link group cannot be blank")
        if self.source_range is not None:
            expected_duration = self.speed.source_to_timeline_us(
                self.source_range.duration_us
            )
            if abs(expected_duration - self.timeline_range.duration_us) > 1:
                raise ValueError("Clip source, timeline and speed durations disagree")


@dataclass(frozen=True, slots=True)
class TimelineTrack:
    id: str
    kind: TrackKind
    name: str
    clips: tuple[TimelineClip, ...] = ()
    locked: bool = False
    visible: bool = True
    muted: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ValueError("Track id and name are required")
        if any(clip.track_id != self.id for clip in self.clips):
            raise ValueError("Every clip must reference its owning track")


@dataclass(frozen=True, slots=True)
class Overlay:
    id: str
    kind: OverlayKind
    range: TimeRange
    x: float
    y: float
    width: float
    height: float
    asset_id: str | None = None
    enabled: bool = True
    opacity: float = 1.0
    blur_radius: int = 18
    tracking_mode: str = "fixed"
    padding_x: float = 0.012
    padding_y: float = 0.008
    concealment_mode: str = "blur"

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Overlay id is required")
        for value in (self.x, self.y, self.width, self.height):
            if not 0.0 <= value <= 1.0:
                raise ValueError("Overlay geometry uses normalized values between 0 and 1")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Overlay size must be positive")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("Overlay opacity must be between 0 and 1")
        if type(self.blur_radius) is not int or not 1 <= self.blur_radius <= 100:
            raise ValueError("Overlay blur radius must be between 1 and 100")
        if self.tracking_mode not in {
            "fixed",
            "source_subtitle",
            "ocr_watermark",
        }:
            raise ValueError("Overlay tracking mode is invalid")
        if self.kind is not OverlayKind.BLUR and self.tracking_mode != "fixed":
            raise ValueError("Only blur overlays can follow source subtitles")
        if self.concealment_mode not in {"blur", "cover"}:
            raise ValueError("Overlay concealment mode is invalid")
        if self.kind is not OverlayKind.BLUR and self.concealment_mode != "blur":
            raise ValueError("Only blur overlays can use subtitle concealment")
        if not 0.0 <= self.padding_x <= 0.2 or not 0.0 <= self.padding_y <= 0.2:
            raise ValueError("Overlay padding must be between 0 and 0.2")


@dataclass(frozen=True, slots=True)
class TranslationStylePreset:
    """A user-named translation style saved inside a project."""

    name: str
    preset: str = "auto"
    custom_rules: str = ""

    def __post_init__(self) -> None:
        normalized_name = self.name.strip()
        if not normalized_name:
            raise ValueError("Translation style name is required")
        if len(normalized_name) > 80:
            raise ValueError("Translation style name cannot exceed 80 characters")
        if any(ord(character) < 32 for character in normalized_name):
            raise ValueError("Translation style name contains control characters")
        if self.preset not in TRANSLATION_STYLE_PRESETS:
            raise ValueError("Translation style preset is invalid")
        if len(self.custom_rules) > 4_000:
            raise ValueError("Translation custom rules cannot exceed 4000 characters")
        object.__setattr__(self, "name", normalized_name)

    @property
    def normalized_name(self) -> str:
        return self.name.strip()


@dataclass(frozen=True, slots=True)
class TranslationPreferences:
    preset: str = "auto"
    custom_rules: str = ""
    saved_styles: tuple[TranslationStylePreset, ...] = ()
    active_style_name: str = ""

    def __post_init__(self) -> None:
        if self.preset not in TRANSLATION_STYLE_PRESETS:
            raise ValueError("Translation style preset is invalid")
        if len(self.custom_rules) > 4_000:
            raise ValueError("Translation custom rules cannot exceed 4000 characters")
        names = [style.normalized_name.casefold() for style in self.saved_styles]
        if len(names) != len(set(names)):
            raise ValueError("Translation style names must be unique")
        if len(self.saved_styles) > 100:
            raise ValueError("A project cannot contain more than 100 saved translation styles")
        if self.active_style_name:
            active = next(
                (style for style in self.saved_styles if style.name == self.active_style_name),
                None,
            )
            if active is None or (active.preset, active.custom_rules) != (
                self.preset,
                self.custom_rules,
            ):
                raise ValueError("Active translation style must match a saved style")


@dataclass(frozen=True, slots=True)
class VoiceConformState:
    plan: VoiceConformPlan
    base_voice_media_id: str
    conformed_voice_media_id: str
    parent_tts_job_id: str

    def __post_init__(self) -> None:
        if not self.base_voice_media_id.strip() or not self.conformed_voice_media_id.strip():
            raise ValueError("Voice conform media ids are required")
        if self.base_voice_media_id == self.conformed_voice_media_id:
            raise ValueError("Voice conform base and output media must differ")
        if not self.parent_tts_job_id.strip():
            raise ValueError("Voice conform parent TTS job id is required")


@dataclass(frozen=True, slots=True)
class TimelineEditState:
    retained_ranges: tuple[TimeRange, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.retained_ranges:
            raise ValueError("Phạm vi cắt video không hợp lệ")
        previous_end = -1
        for interval in self.retained_ranges:
            if interval.duration_us <= 0:
                raise ValueError("Đoạn video giữ lại không được rỗng")
            if interval.start_us < previous_end:
                raise ValueError("Các đoạn video giữ lại phải đúng thứ tự, không chồng nhau")
            previous_end = interval.end_us


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    id: str
    name: str
    revision: int
    media: tuple[MediaAsset, ...] = ()
    tracks: tuple[TimelineTrack, ...] = ()
    source_cues: tuple[SubtitleCue, ...] = ()
    translated_cues: tuple[SubtitleCue, ...] = ()
    overlays: tuple[Overlay, ...] = ()
    schema_version: int = 1
    voice_conform: VoiceConformState | None = None
    translation_preferences: TranslationPreferences = field(
        default_factory=TranslationPreferences
    )
    # Optional normalized full-frame OCR controls. The dialogue region feeds
    # S1, text outside it feeds S2, and exclusions win over both.
    ocr_dialogue_region: tuple[float, float, float, float] | None = None
    ocr_exclusion_regions: tuple[tuple[float, float, float, float], ...] = ()
    timeline_edit: TimelineEditState | None = None
    # User-authored timeline bookmarks are display-time positions in
    # microseconds.  They are deliberately separate from media/cue data so
    # adding or removing a mark never changes OCR, translation or TTS inputs.
    timeline_markers: tuple[TimeUs, ...] = ()
    # Editor-authored labels are not OCR/translation/TTS input. Keeping this
    # collection separate preserves channel/film titles across stage reruns.
    title_cues: tuple[SubtitleCue, ...] = ()
    dialogue_casting: DialogueCastingState | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ValueError("Project id and name are required")
        if self.revision <= 0 or self.schema_version <= 0:
            raise ValueError("Project revision and schema version must be positive")
        _validate_normalized_box(
            self.ocr_dialogue_region,
            label="OCR dialogue region",
        )
        for index, region in enumerate(self.ocr_exclusion_regions, start=1):
            _validate_normalized_box(region, label=f"OCR exclusion region {index}")
        if any(type(value) is not int or value < 0 for value in self.timeline_markers):
            raise ValueError("Timeline markers must be non-negative integer timestamps")
        if tuple(sorted(set(self.timeline_markers))) != self.timeline_markers:
            raise ValueError("Timeline markers must be sorted and unique")
        media_ids = [item.id for item in self.media]
        track_ids = [item.id for item in self.tracks]
        if len(media_ids) != len(set(media_ids)) or len(track_ids) != len(set(track_ids)):
            raise ValueError("Project media and track ids must be unique")
        clips = [clip for track in self.tracks for clip in track.clips]
        clip_ids = [clip.id for clip in clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("Project clip ids must be unique")
        known_media = set(media_ids)
        title_ids = [cue.id for cue in self.title_cues]
        if len(title_ids) != len(set(title_ids)):
            raise ValueError("Title text ids must be unique")
        if set(title_ids) & {cue.id for cue in (*self.source_cues, *self.translated_cues)}:
            raise ValueError("Title text identity must be separate from subtitles")
        if any(cue.role != "title_text" for cue in self.title_cues):
            raise ValueError("Title collection requires title_text cues")
        if any(cue.role == "title_text" for cue in (*self.source_cues, *self.translated_cues)):
            raise ValueError("Titles cannot be included in subtitle job inputs")
        for overlay in self.overlays:
            if overlay.asset_id is not None and overlay.asset_id not in known_media:
                raise ValueError(f"Overlay references unknown media: {overlay.asset_id}")
        for clip in clips:
            if clip.media_id is not None and clip.media_id not in known_media:
                raise ValueError(f"Clip references unknown media: {clip.media_id}")
            if clip.media_id is None or clip.source_range is None:
                continue
            media = self.media[media_ids.index(clip.media_id)]
            if clip.source_range.end_us > media.duration_us:
                raise ValueError("Clip source range exceeds media duration")
        if self.voice_conform is not None:
            conform_media = {
                self.voice_conform.base_voice_media_id,
                self.voice_conform.conformed_voice_media_id,
            }
            if not conform_media.issubset(known_media):
                raise ValueError("Voice conform references unknown media")
            voice_media_ids = {
                clip.media_id
                for track in self.tracks
                if track.kind is TrackKind.VOICE
                for clip in track.clips
                if clip.media_id is not None
            }
            if self.voice_conform.conformed_voice_media_id not in voice_media_ids:
                raise ValueError("Voice conform output is not active on the voice track")


def normalize_legacy_subtitle_lanes(snapshot: ProjectSnapshot) -> ProjectSnapshot:
    """Repair pre-multilane scene text without changing cue identity or timing.

    Projects created before scene-lane allocation legitimately decode with an
    empty ``subtitle_track_id`` while their clips all occupy S2. Preview renders
    every active cue, but Timeline delegates then overlap. Normalize only that
    legacy/invalid shape; an already-valid explicit layout is returned exactly
    as stored.
    """

    occupied: dict[int, list[TimeRange]] = {}
    needs_normalization = False
    for cue in sorted(
        snapshot.source_cues,
        key=lambda item: (
            item.range.start_us,
            item.range.end_us,
            item.index,
            item.id,
        ),
    ):
        if not cue.is_scene_text:
            continue
        lane = subtitle_track_number(cue.subtitle_track_id, fallback=0)
        if lane == 0:
            needs_normalization = True
            continue
        if lane == 1 and any(
            subtitle_ranges_overlap(cue.range, dialogue.range)
            for dialogue in snapshot.source_cues
            if dialogue.is_dialogue
        ):
            needs_normalization = True
            continue
        if any(
            subtitle_ranges_overlap(cue.range, item)
            for item in occupied.get(lane, ())
        ):
            needs_normalization = True
        occupied.setdefault(lane, []).append(cue.range)
    if not needs_normalization:
        return snapshot

    source = allocate_scene_text_lanes(
        snapshot.source_cues,
        preserve_explicit=True,
    )
    lanes_by_index = {cue.index: cue.subtitle_track_id for cue in source}
    translated = tuple(
        replace(
            cue,
            subtitle_track_id=lanes_by_index.get(cue.index, cue.subtitle_track_id),
        )
        for cue in snapshot.translated_cues
    )
    translated_by_index = {cue.index: cue for cue in translated}

    subtitle_tracks = {
        track.id: track
        for track in snapshot.tracks
        if track.kind is TrackKind.SUBTITLE
    }
    clips_by_cue_id: dict[str, TimelineClip] = {}
    clips_by_index: dict[int, TimelineClip] = {}
    for track in subtitle_tracks.values():
        for clip in track.clips:
            if clip.cue_id:
                clips_by_cue_id[clip.cue_id] = clip
            if clip.cue_index is not None:
                clips_by_index[clip.cue_index] = clip

    clips_by_track: dict[str, list[TimelineClip]] = {}
    for cue in source:
        track_id = cue.subtitle_track_id or ("S1" if cue.is_dialogue else "S2")
        clip = clips_by_cue_id.get(cue.id) or clips_by_index.get(cue.index)
        translated_cue = translated_by_index.get(cue.index)
        cue_text = translated_cue.text if translated_cue is not None else cue.text
        if clip is None:
            clip = TimelineClip(
                id=f"subtitle-{cue.id}",
                track_id=track_id,
                timeline_range=cue.range,
                cue_id=cue.id,
                cue_index=cue.index,
                cue_text=cue_text,
            )
        else:
            clip = replace(
                clip,
                track_id=track_id,
                timeline_range=cue.range,
                source_range=None,
                cue_id=cue.id,
                cue_index=cue.index,
                cue_text=cue_text,
            )
        clips_by_track.setdefault(track_id, []).append(clip)

    projected: list[TimelineTrack] = []
    for track_id, clips in sorted(
        clips_by_track.items(),
        key=lambda item: subtitle_track_number(item[0], fallback=10_000),
    ):
        previous = subtitle_tracks.get(track_id)
        projected.append(
            TimelineTrack(
                track_id,
                TrackKind.SUBTITLE,
                previous.name
                if previous is not None
                else ("Phụ đề nguồn" if track_id == "S1" else "Chữ trong cảnh"),
                tuple(
                    sorted(
                        clips,
                        key=lambda item: (
                            item.timeline_range.start_us,
                            item.timeline_range.end_us,
                            item.id,
                        ),
                    )
                ),
                locked=previous.locked if previous is not None else False,
                visible=previous.visible if previous is not None else True,
                muted=previous.muted if previous is not None else False,
            )
        )

    first_subtitle = next(
        (
            index
            for index, track in enumerate(snapshot.tracks)
            if track.kind is TrackKind.SUBTITLE
        ),
        len(snapshot.tracks),
    )
    before = tuple(
        track
        for track in snapshot.tracks[:first_subtitle]
        if track.kind is not TrackKind.SUBTITLE
    )
    after = tuple(
        track
        for track in snapshot.tracks[first_subtitle:]
        if track.kind is not TrackKind.SUBTITLE
    )
    return replace(
        snapshot,
        source_cues=source,
        translated_cues=translated,
        tracks=before + tuple(projected) + after,
    )
