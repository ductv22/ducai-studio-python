import pytest

from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange


def test_project_rejects_duplicate_media_ids() -> None:
    first = MediaAsset("media-1", "A.mp4", MediaKind.VIDEO, 10_000_000)
    second = MediaAsset("media-1", "B.mp4", MediaKind.VIDEO, 20_000_000)
    with pytest.raises(ValueError, match="unique"):
        ProjectSnapshot("project", "Demo", 1, media=(first, second))


def test_track_rejects_clip_owned_by_another_track() -> None:
    clip = TimelineClip("clip", "wrong-track", TimeRange(0, 1_000_000))
    with pytest.raises(ValueError, match="owning track"):
        TimelineTrack("video-track", TrackKind.VIDEO, "Video", clips=(clip,))


def test_subtitle_cue_requires_text_and_valid_confidence() -> None:
    with pytest.raises(ValueError, match="empty"):
        SubtitleCue("cue", 1, TimeRange(0, 1_000_000), "", "zh")
    with pytest.raises(ValueError, match="confidence"):
        SubtitleCue("cue", 1, TimeRange(0, 1_000_000), "你好", "zh", confidence=1.1)

