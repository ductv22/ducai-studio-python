from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .timebase import SpeedRatio, TimeRange, TimeUs
from .voice_conform import VoiceConformPlan


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

    def __post_init__(self) -> None:
        if not self.id.strip() or self.index <= 0:
            raise ValueError("Cue id and positive index are required")
        if not self.text.strip():
            raise ValueError("Cue text cannot be empty")
        if not self.language.strip() or self.revision <= 0:
            raise ValueError("Cue language and positive revision are required")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Cue confidence must be between 0 and 1")


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

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.track_id.strip():
            raise ValueError("Clip id and track id are required")
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

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ValueError("Project id and name are required")
        if self.revision <= 0 or self.schema_version <= 0:
            raise ValueError("Project revision and schema version must be positive")
        media_ids = [item.id for item in self.media]
        track_ids = [item.id for item in self.tracks]
        if len(media_ids) != len(set(media_ids)) or len(track_ids) != len(set(track_ids)):
            raise ValueError("Project media and track ids must be unique")
        clips = [clip for track in self.tracks for clip in track.clips]
        clip_ids = [clip.id for clip in clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("Project clip ids must be unique")
        known_media = set(media_ids)
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
