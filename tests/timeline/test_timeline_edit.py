import json
from dataclasses import asdict, replace

import pytest

from ducai_studio.application.timeline_commands import TimelineEditor
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
    VoiceConformState,
)
from ducai_studio.domain.timebase import SpeedRatio, TimeRange
from ducai_studio.domain.timeline_edit import (
    base_to_timeline_us,
    build_scan_plan,
    project_for_timeline,
    project_json_for_timeline,
    scan_time_to_base_us,
    timeline_to_base_us,
)
from ducai_studio.domain.voice_conform import (
    VoiceConformPlan,
    VoiceConformSegment,
    VoiceConformSettings,
)
from ducai_studio.infrastructure.project_repository import decode_project_snapshot

S = 1_000_000


def fixture() -> ProjectSnapshot:
    asset = MediaAsset("m", "C:/media/movie.mp4", MediaKind.VIDEO, 60*S)
    video = TimelineClip("v", "V1", TimeRange(0, 60*S), TimeRange(0, 60*S), "m", "av")
    audio = replace(video, id="a", track_id="A1")
    cues = tuple(SubtitleCue(f"c{i}", i, TimeRange(a*S, b*S), f"cue {i}", "zh")
                 for i, (a, b) in enumerate(((0, 5), (18, 32), (40, 45)), 1))
    return ProjectSnapshot("p", "Movie", 1, media=(asset,), source_cues=cues,
                           translated_cues=cues, tracks=(
        TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),
        TimelineTrack("A1", TrackKind.ORIGINAL_AUDIO, "Audio", (audio,)),
    ))


def test_cut_head_packs_source_and_keeps_atomic_undo() -> None:
    editor = TimelineEditor(fixture())
    cut = editor.cut("keep_from", 10*S, 60*S)
    projected = project_for_timeline(cut)
    clip = projected.tracks[0].clips[0]
    assert clip.timeline_range == TimeRange(0, 50*S)
    assert clip.source_range == TimeRange(10*S, 60*S)
    assert [cue.id for cue in projected.source_cues] == ["c2", "c3"]
    assert [cue.index for cue in projected.source_cues] == [1, 2]
    assert projected.source_cues[0].range == TimeRange(8*S, 22*S)
    assert editor.undo().source_cues == fixture().source_cues
    assert editor.redo().timeline_edit == cut.timeline_edit
    decoded = decode_project_snapshot(json.loads(json.dumps(asdict(cut))))
    assert decoded.timeline_edit == cut.timeline_edit


def test_remove_middle_map_does_not_clamp_removed_frames() -> None:
    cut = TimelineEditor(fixture()).cut("remove_range", 20*S, 30*S)
    clips = project_for_timeline(cut).tracks[0].clips
    assert [clip.timeline_range for clip in clips] == [TimeRange(0, 20*S), TimeRange(20*S, 50*S)]
    assert [clip.source_range for clip in clips] == [TimeRange(0, 20*S), TimeRange(30*S, 60*S)]
    assert base_to_timeline_us(cut, 25*S) is None
    assert timeline_to_base_us(cut, 20*S, "start") == 30*S
    assert timeline_to_base_us(cut, 20*S, "end") == 20*S
    assert scan_time_to_base_us(build_scan_plan(cut), 20*S) == 30*S
    raw = project_json_for_timeline(asdict(cut))
    assert all(clip["track_id"] == "V1" for clip in raw["tracks"][0]["clips"])


def test_scan_time_accepts_millisecond_srt_rounding_at_final_media_boundary() -> None:
    plan = build_scan_plan(fixture())
    duration = plan["duration_us"]

    assert scan_time_to_base_us(plan, duration + 333, "end") == duration - 1
    with pytest.raises(ValueError, match="ngoài phần video giữ lại"):
        scan_time_to_base_us(plan, duration + 1_001, "end")
    with pytest.raises(ValueError, match="ngoài phần video giữ lại"):
        scan_time_to_base_us(plan, duration + 333, "start")


def test_repeated_cut_composes_existing_kept_ranges() -> None:
    editor = TimelineEditor(fixture())
    editor.cut("remove_range", 20*S, 30*S)
    cut = editor.cut("keep_range", 15*S, 25*S)
    clips = project_for_timeline(cut).tracks[0].clips
    assert [clip.source_range for clip in clips] == [TimeRange(15*S, 20*S), TimeRange(30*S, 35*S)]
    assert clips[-1].timeline_range.end_us == 10*S


@pytest.mark.parametrize("operation,start,end", [
    ("remove_range", 0, 60*S), ("keep_range", 30*S, 20*S),
    ("keep_from", 60*S, 60*S), ("keep_until", 0, 0), ("keep_range", -1, 10*S),
])
def test_invalid_cut_is_non_mutating(operation, start, end) -> None:
    original = fixture()
    editor = TimelineEditor(original)
    with pytest.raises(ValueError):
        editor.cut(operation, start, end)
    assert editor.snapshot == original
    assert not editor.can_undo


def test_legacy_trim_move_scan_plan_starts_at_source_offset() -> None:
    editor = TimelineEditor(fixture())
    editor.trim_head("v", 10*S)
    moved = editor.move("v", 0)
    plan = build_scan_plan(moved)
    assert plan["duration_us"] == 50*S
    assert plan["segments"][0]["source_start_us"] == 10*S


def test_cut_composes_voice_conform_and_original_and_voice_audio() -> None:
    original = fixture()
    plan = VoiceConformPlan(1, VoiceConformSettings(), 60*S, 80*S, (
        VoiceConformSegment(0, 20*S, 0, 40*S, .5),
        VoiceConformSegment(20*S, 60*S, 40*S, 80*S, 1.0),
    ), (), (), "conform-fixture")
    voice = MediaAsset("voice", "voice.wav", MediaKind.AUDIO, 80*S)
    base_voice = replace(voice, id="base", path="base.wav")
    d1 = TimelineClip("d", "D1", TimeRange(0, 80*S), TimeRange(0, 80*S), "voice")
    original = replace(original, media=(*original.media, voice, base_voice),
                       voice_conform=VoiceConformState(plan, "base", "voice", "tts"),
                       tracks=(*original.tracks,
                               TimelineTrack("D1", TrackKind.VOICE, "D1", (d1,))))
    editor = TimelineEditor(original)
    cut = editor.cut("keep_from", 30*S, 80*S)
    view = project_for_timeline(cut)
    assert view.voice_conform is None
    assert [clip.source_range for clip in view.tracks[0].clips] == [
        TimeRange(15*S, 20*S), TimeRange(20*S, 60*S),
    ]
    assert [clip.timeline_range for clip in view.tracks[0].clips] == [
        TimeRange(0, 10*S), TimeRange(10*S, 50*S),
    ]
    assert [clip.source_range for clip in view.tracks[1].clips] == [
        TimeRange(15*S, 20*S), TimeRange(20*S, 60*S),
    ]
    assert view.tracks[2].clips[0].source_range == TimeRange(30*S, 40*S)
    assert view.tracks[2].clips[1].source_range == TimeRange(40*S, 80*S)
    assert timeline_to_base_us(cut, 0) == 15*S
    # A second edit is expressed in the already-packed, conformed display time.
    again = editor.cut("keep_range", 5*S, 15*S)
    assert project_for_timeline(again).tracks[0].clips[-1].source_range == TimeRange(20*S, 25*S)


def test_speed_and_source_offset_are_retained_in_scan_plan() -> None:
    original = fixture()
    video = replace(original.tracks[0].clips[0], timeline_range=TimeRange(0, 20*S),
                    source_range=TimeRange(10*S, 50*S), speed=SpeedRatio(2, 1))
    audio = replace(video, id="a", track_id="A1")
    original = replace(original, tracks=(replace(original.tracks[0], clips=(video,)),
                                        replace(original.tracks[1], clips=(audio,))))
    cut = TimelineEditor(original).cut("remove_range", 5*S, 10*S)
    plan = build_scan_plan(cut)
    assert plan["duration_us"] == 15*S
    assert [(s["source_start_us"], s["source_end_us"]) for s in plan["segments"]] == [
        (10*S, 20*S), (30*S, 50*S),
    ]
    assert project_for_timeline(cut).tracks[0].clips[0].speed.source_to_timeline_us(10*S) == 5*S
