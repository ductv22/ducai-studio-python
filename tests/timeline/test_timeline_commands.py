from __future__ import annotations

import time
from dataclasses import replace

import pytest

from ducai_studio.application.timeline_commands import TimelineEditor
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import SpeedRatio, TimeRange
from ducai_studio.domain.timeline import (
    VOICE_TIMING_MATCHED,
    VOICE_TIMING_OVERFLOW,
    VOICE_TIMING_POSITION_MISMATCH,
    VOICE_TIMING_UNDERFLOW,
    VOICE_TIMING_UNKNOWN,
    ClipIntervalIndex,
    TimelineEditError,
    TimelineViewport,
    classify_voice_timing,
    resize_clip,
    source_at_timeline,
    timeline_at_source,
)

SECOND = 1_000_000


def _linked_snapshot() -> ProjectSnapshot:
    media = MediaAsset("media-1", "C:/media/movie.mp4", MediaKind.VIDEO, 60 * SECOND)
    source = TimeRange(0, 60 * SECOND)
    timeline = TimeRange(0, 60 * SECOND)
    video = TimelineClip(
        "video-1",
        "V1",
        timeline,
        source,
        "media-1",
        "av-1",
        volume=0,
    )
    audio = TimelineClip(
        "audio-1",
        "A1",
        timeline,
        source,
        "media-1",
        "av-1",
    )
    return ProjectSnapshot(
        "project-1",
        "Movie",
        1,
        media=(media,),
        tracks=(
            TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),
            TimelineTrack("A1", TrackKind.ORIGINAL_AUDIO, "Âm thanh gốc", (audio,)),
        ),
    )


def _ids():
    value = 0
    while True:
        value += 1
        yield str(value)


def test_source_timeline_mapping_keeps_trim_origin_and_exact_speed() -> None:
    speed = SpeedRatio.from_percent(85)
    clip = TimelineClip(
        "clip",
        "V1",
        TimeRange(30 * SECOND, 30 * SECOND + speed.source_to_timeline_us(20 * SECOND)),
        TimeRange(5 * SECOND, 25 * SECOND),
        speed=speed,
    )

    position = timeline_at_source(clip, 15 * SECOND)

    assert position == 41_764_706
    assert source_at_timeline(clip, position) == 15 * SECOND


def test_split_linked_av_creates_distinct_safe_link_groups() -> None:
    values = _ids()
    editor = TimelineEditor(_linked_snapshot(), id_factory=lambda: next(values))

    snapshot = editor.split("video-1", 20 * SECOND)
    video_clips = snapshot.tracks[0].clips
    audio_clips = snapshot.tracks[1].clips

    assert [clip.timeline_range for clip in video_clips] == [
        TimeRange(0, 20 * SECOND),
        TimeRange(20 * SECOND, 60 * SECOND),
    ]
    assert video_clips[0].link_group_id == audio_clips[0].link_group_id
    assert video_clips[1].link_group_id == audio_clips[1].link_group_id
    assert video_clips[0].link_group_id != video_clips[1].link_group_id


def test_speed_and_trim_can_apply_to_video_only_or_linked_pair() -> None:
    editor = TimelineEditor(_linked_snapshot())

    linked = editor.set_speed_percent("video-1", 85)
    assert linked.tracks[0].clips[0].timeline_range == linked.tracks[1].clips[0].timeline_range
    assert linked.tracks[0].clips[0].speed == SpeedRatio(17, 20)

    video_only = editor.trim_tail("video-1", 30 * SECOND, apply_linked=False)
    assert video_only.tracks[0].clips[0].timeline_range.end_us == 30 * SECOND
    assert video_only.tracks[0].clips[0].link_group_id is None
    assert video_only.tracks[1].clips[0].timeline_range.end_us > 30 * SECOND


def test_move_linked_trimmed_av_to_timeline_start_preserves_source_and_duration() -> None:
    editor = TimelineEditor(_linked_snapshot())
    trimmed = editor.trim_head("video-1", 10 * SECOND, apply_linked=True)
    assert trimmed.tracks[0].clips[0].timeline_range == TimeRange(10 * SECOND, 60 * SECOND)

    moved = editor.move("video-1", 0, apply_linked=True)

    for track in moved.tracks:
        clip = track.clips[0]
        assert clip.timeline_range == TimeRange(0, 50 * SECOND)
        assert clip.source_range == TimeRange(10 * SECOND, 60 * SECOND)
    undone = editor.undo()
    assert undone.tracks[0].clips[0].timeline_range == TimeRange(10 * SECOND, 60 * SECOND)
    assert editor.redo().tracks[1].clips[0].timeline_range == TimeRange(0, 50 * SECOND)


def test_move_rejects_negative_locked_and_non_media_clips() -> None:
    editor = TimelineEditor(_linked_snapshot())
    with pytest.raises(TimelineEditError, match="không hợp lệ"):
        editor.move("video-1", -1)

    editor.set_track_locked("V1", True)
    with pytest.raises(TimelineEditError, match="locked track"):
        editor.move("video-1", 1 * SECOND)

    subtitle = TimelineClip("subtitle-1", "S1", TimeRange(0, SECOND))
    snapshot = ProjectSnapshot(
        "subtitle-project",
        "Subtitle",
        1,
        tracks=(TimelineTrack("S1", TrackKind.SUBTITLE, "Phụ đề", (subtitle,)),),
    )
    with pytest.raises(TimelineEditError, match="video hoặc âm thanh gốc"):
        TimelineEditor(snapshot).move("subtitle-1", 1 * SECOND)


def test_undo_redo_create_new_monotonic_project_revisions() -> None:
    editor = TimelineEditor(_linked_snapshot())
    edited = editor.set_volume("audio-1", 0.4)
    undone = editor.undo()
    redone = editor.redo()

    assert (edited.revision, undone.revision, redone.revision) == (2, 3, 4)
    assert undone.tracks[1].clips[0].volume == 1.0
    assert redone.tracks[1].clips[0].volume == 0.4


def test_track_mute_lock_and_locked_clip_guard_are_undoable() -> None:
    editor = TimelineEditor(_linked_snapshot())

    muted = editor.set_track_muted("A1", True)
    locked = editor.set_track_locked("V1", True)

    assert muted.tracks[1].muted is True
    assert locked.tracks[0].locked is True
    with pytest.raises(TimelineEditError, match="locked track"):
        editor.set_speed_percent("video-1", 85, apply_linked=True)

    unlocked = editor.set_track_locked("V1", False)
    edited = editor.set_speed_percent("video-1", 85, apply_linked=True)
    undone = editor.undo()

    assert unlocked.tracks[0].locked is False
    assert edited.tracks[0].clips[0].speed == SpeedRatio(17, 20)
    assert undone.tracks[0].clips[0].speed == SpeedRatio(1, 1)


def test_track_volume_updates_every_clip_for_export_mixer() -> None:
    snapshot = _linked_snapshot()
    audio = snapshot.tracks[1]
    second = replace(
        audio.clips[0],
        id="audio-2",
        timeline_range=TimeRange(60 * SECOND, 120 * SECOND),
        source_range=TimeRange(0, 60 * SECOND),
    )
    snapshot = replace(
        snapshot,
        tracks=(snapshot.tracks[0], replace(audio, clips=(*audio.clips, second))),
    )

    updated = TimelineEditor(snapshot).set_track_volume("A1", 0.35)

    assert [clip.volume for clip in updated.tracks[1].clips] == [0.35, 0.35]


def test_viewport_index_does_not_allocate_all_4333_cues() -> None:
    cue_count = 4_333
    duration = cue_count * SECOND
    media = MediaAsset("srt", "C:/media/movie.srt", MediaKind.SUBTITLE, duration)
    clips = tuple(
        TimelineClip(
            f"cue-{index}",
            "S1",
            TimeRange(index * SECOND, index * SECOND + SECOND // 2),
            TimeRange(index * SECOND, index * SECOND + SECOND // 2),
            "srt",
        )
        for index in range(cue_count)
    )
    snapshot = ProjectSnapshot(
        "project",
        "Long film",
        1,
        media=(media,),
        tracks=(TimelineTrack("S1", TrackKind.SUBTITLE, "Phụ đề", clips),),
    )
    index = ClipIntervalIndex(snapshot.tracks)
    viewport = TimelineViewport(TimeRange(1_000 * SECOND, 1_010 * SECOND), 5 * SECOND)

    started = time.perf_counter()
    for _iteration in range(100):
        visible = index.query("S1", viewport)
    elapsed = time.perf_counter() - started

    assert 10 < len(visible) < 30
    assert len(visible) < cue_count // 100
    assert elapsed < 0.8


def test_project_rejects_clip_that_exceeds_media() -> None:
    media = MediaAsset("m", "C:/m.mp4", MediaKind.VIDEO, SECOND)
    clip = TimelineClip(
        "c", "V1", TimeRange(0, 2 * SECOND), TimeRange(0, 2 * SECOND), "m"
    )
    with pytest.raises(ValueError, match="exceeds media duration"):
        ProjectSnapshot(
            "p",
            "Project",
            1,
            media=(media,),
            tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (clip,)),),
        )


@pytest.mark.parametrize(
    ("timeline_range", "expected"),
    [
        (TimeRange(1_000_000, 3_000_000), VOICE_TIMING_MATCHED),
        (TimeRange(1_050_000, 3_050_000), VOICE_TIMING_MATCHED),
        (TimeRange(1_050_001, 3_050_001), VOICE_TIMING_POSITION_MISMATCH),
        (TimeRange(1_000_000, 3_050_001), VOICE_TIMING_OVERFLOW),
        (TimeRange(1_000_000, 2_949_999), VOICE_TIMING_UNDERFLOW),
    ],
)
def test_voice_timing_classifier_uses_strict_50_ms_boundary(
    timeline_range: TimeRange,
    expected: str,
) -> None:
    slot = TimeRange(1_000_000, 3_000_000)

    assert classify_voice_timing(timeline_range, slot) == expected
    assert classify_voice_timing(timeline_range, None) == VOICE_TIMING_UNKNOWN


def test_voice_resize_recomputes_overflow_instead_of_clearing_it_blindly() -> None:
    clip = TimelineClip(
        "voice-1",
        "D1",
        TimeRange(0, 4_000_000),
        TimeRange(0, 4_000_000),
        timing_slot=TimeRange(0, 2_000_000),
        timing_unresolved=True,
        issue_code="CUE_SYNC_LIMIT_EXCEEDED",
    )

    still_long = resize_clip(clip, TimeRange(0, 3_000_000))
    matched = resize_clip(still_long, TimeRange(0, 2_000_000))
    short = resize_clip(matched, TimeRange(0, 1_500_000))

    assert still_long.timing_unresolved is True
    assert still_long.issue_code == "CUE_SYNC_LIMIT_EXCEEDED"
    assert matched.timing_unresolved is False
    assert matched.issue_code == ""
    assert short.timing_unresolved is False


def test_voice_resize_exact_three_x_limit_is_safe_for_odd_microsecond_duration() -> None:
    source_duration_us = 1_000_001
    minimum_timeline_duration_us = (source_duration_us + 2) // 3
    clip = TimelineClip(
        "voice-odd",
        "D1",
        TimeRange(0, source_duration_us),
        TimeRange(0, source_duration_us),
        timing_slot=TimeRange(0, minimum_timeline_duration_us),
    )

    resized = resize_clip(
        clip,
        TimeRange(0, minimum_timeline_duration_us),
    )

    assert resized.timeline_range.duration_us == 333_334
    assert resized.speed.numerator / resized.speed.denominator <= 3.0


def test_auto_fit_voice_timing_changes_only_overflow_cues() -> None:
    media = MediaAsset("voice", "C:/voice.wav", MediaKind.AUDIO, 12 * SECOND)
    overflow = TimelineClip(
        "overflow",
        "D1",
        TimeRange(0, 3 * SECOND),
        TimeRange(0, 4 * SECOND),
        "voice",
        speed=SpeedRatio(4, 3),
        timing_slot=TimeRange(0, 2 * SECOND),
        timing_unresolved=True,
    )
    underflow = TimelineClip(
        "underflow",
        "D1",
        TimeRange(4 * SECOND, 5 * SECOND),
        TimeRange(4 * SECOND, 5 * SECOND),
        "voice",
        timing_slot=TimeRange(4 * SECOND, 6 * SECOND),
    )
    misplaced = TimelineClip(
        "misplaced",
        "D1",
        TimeRange(8 * SECOND + 100_000, 10 * SECOND + 100_000),
        TimeRange(6 * SECOND, 8 * SECOND),
        "voice",
        timing_slot=TimeRange(8 * SECOND, 10 * SECOND),
    )
    snapshot = ProjectSnapshot(
        "project",
        "Voice",
        1,
        media=(media,),
        tracks=(
            TimelineTrack(
                "D1",
                TrackKind.VOICE,
                "Thuyết minh",
                (overflow, underflow, misplaced),
            ),
        ),
    )

    updated = TimelineEditor(snapshot).auto_fit_voice_timing()
    clips = {clip.id: clip for clip in updated.tracks[0].clips}

    assert clips["overflow"].timeline_range == TimeRange(0, 2 * SECOND)
    assert clips["overflow"].timing_unresolved is False
    assert clips["underflow"] == underflow
    assert clips["misplaced"] == misplaced
    assert updated.revision == 2
