import json
from dataclasses import replace

import pytest

from ducai_studio.application.subtitle_commands import SubtitleEditError, SubtitleEditor
from ducai_studio.domain.project import (
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
    dialogue_cues,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.infrastructure.project_repository import (
    decode_project_snapshot,
    encode_project_snapshot,
)


def role_snapshot() -> ProjectSnapshot:
    cues = (
        SubtitleCue("c1", 1, TimeRange(0, 1_000_000), "原文一", "zh", subtitle_track_id="S1"),
        SubtitleCue(
            "c2", 2, TimeRange(1_000_000, 2_000_000), "山河", "zh",
            role="scene_text", subtitle_track_id="S3", source="paddle-quality",
            confidence=0.96, evidence_ids=("frame-1", "frame-2"),
            visual_box=(0.2, 0.2, 0.1, 0.3), visual_box_source="ocr",
            panel_box=(0.2, 0.2, 0.2, 0.4), orientation="vertical",
            style_hint="calligraphy_brush", text_font_size=40,
            text_color="#FFFFFF", background_color="#080808",
            background_style="ink_splash", background_padding_x=34,
            background_padding_y=26, font_bold=True, font_italic=True,
            letter_spacing_percent=35, text_opacity_percent=83,
            background_opacity_percent=94, fade_enabled=True, fade_duration_ms=350,
        ),
        SubtitleCue("c3", 3, TimeRange(2_000_000, 3_000_000), "原文三", "zh",
                    subtitle_track_id="S4"),
    )
    translations = tuple(replace(cue, text=f"Câu {cue.index}", language="vi") for cue in cues)
    tracks = tuple(
        TimelineTrack(cue.subtitle_track_id, TrackKind.SUBTITLE, cue.subtitle_track_id,
                      (TimelineClip(f"clip-{cue.id}", cue.subtitle_track_id, cue.range,
                                    cue.range, cue_id=cue.id, cue_index=cue.index,
                                    cue_text=translations[cue.index - 1].text, volume=0.7),))
        for cue in cues
    ) + (TimelineTrack("D1", TrackKind.VOICE, "Giọng đã tạo",
                       (TimelineClip("voice", "D1", cues[0].range, cue_id="c1"),)),)
    return ProjectSnapshot("roles", "Cue roles", 1, source_cues=cues,
                           translated_cues=translations, tracks=tracks)


@pytest.mark.parametrize("role,lane", [("dialogue", "S1"), ("scene_text", "S2")])
def test_bulk_role_conversion_preserves_every_other_field_and_audio(role, lane):
    original = role_snapshot()
    editor = SubtitleEditor(original)
    updated = editor.set_cue_role_many(("c1", "c2", "c2"), role=role)
    assert updated.revision == 2
    for before, after in zip(original.source_cues, updated.source_cues, strict=True):
        expected = (replace(before, role=role, subtitle_track_id=lane,
                            revision=before.revision + 1)
                    if before.id in {"c1", "c2"}
                    and (before.role != role or before.subtitle_track_id != lane) else before)
        assert after == expected
    for before, after in zip(original.translated_cues, updated.translated_cues, strict=True):
        expected = (replace(before, role=role, subtitle_track_id=lane,
                            revision=before.revision + 1)
                    if before.id in {"c1", "c2"}
                    and (before.role != role or before.subtitle_track_id != lane) else before)
        assert after == expected
    clips = {clip.id: clip for track in updated.tracks for clip in track.clips}
    for track in original.tracks:
        for clip in track.clips:
            expected = (replace(clip, track_id=lane, payload_revision=clip.payload_revision + 1)
                        if track.kind is TrackKind.SUBTITLE and clip.cue_id in {"c1", "c2"}
                        and clip.track_id != lane else clip)
            assert clips[clip.id] == expected
    assert editor.undo().source_cues == original.source_cues
    assert editor.redo().source_cues == updated.source_cues
    reopened = decode_project_snapshot(json.loads(encode_project_snapshot(updated)[0]))
    assert reopened == updated
    eligible = {cue.id for cue in dialogue_cues(updated.source_cues)}
    assert eligible == ({"c1", "c2", "c3"} if role == "dialogue" else {"c3"})


def test_role_conversion_uses_shared_identity_before_sparse_translation_index():
    original = role_snapshot()
    sparse = replace(original, translated_cues=(replace(original.translated_cues[2], index=1),))
    editor = SubtitleEditor(sparse)
    updated = editor.set_cue_role_many(("c1",), role="scene_text")
    assert updated.translated_cues == sparse.translated_cues
    updated = editor.set_cue_role_many(("c3",), role="scene_text")
    assert updated.translated_cues[0].role == "scene_text"
    assert updated.source_cues[2].role == "scene_text"


def test_role_conversion_accepts_legacy_translation_id_and_preserves_index():
    original = role_snapshot()
    legacy = replace(original, translated_cues=(replace(original.translated_cues[1], id="t2"),))
    updated = SubtitleEditor(legacy).set_cue_role_many(("t2",), role="dialogue")
    assert updated.source_cues[1].role == "dialogue"
    assert updated.translated_cues[0] == replace(legacy.translated_cues[0], role="dialogue",
                                              subtitle_track_id="S1", revision=2)
    assert updated.source_cues[0] == original.source_cues[0]


@pytest.mark.parametrize("ids,role,error", [
    ((), "dialogue", SubtitleEditError),
    (("c1", "missing"), "scene_text", LookupError),
    (("c1",), "title_text", SubtitleEditError),
    (("c1", "title"), "dialogue", SubtitleEditError),
])
def test_invalid_conversion_is_atomic(ids, role, error):
    original = replace(role_snapshot(), title_cues=(SubtitleCue(
        "title", 1, TimeRange(0, 1_000_000), "Kênh", "vi", role="title_text",
        subtitle_track_id="T1"),))
    editor = SubtitleEditor(original)
    with pytest.raises(error):
        editor.set_cue_role_many(ids, role=role)
    assert editor.snapshot is original
    assert not editor.can_undo


def test_repeated_conversion_is_noop_and_does_not_consume_undo():
    editor = SubtitleEditor(role_snapshot())
    updated = editor.set_cue_role_many(("c2",), role="scene_text")
    assert editor.set_cue_role_many(("c2",), role="scene_text") is updated
    assert len(editor._undo) == 1


@pytest.mark.parametrize("select_both", [False, True])
def test_scene_destination_collision_rejects_all_without_silent_lane_bump(select_both):
    original = role_snapshot()
    second = replace(original.source_cues[1], subtitle_track_id="S2")
    first = replace(original.source_cues[0], range=TimeRange(0, 1_500_000))
    original = replace(original, source_cues=(first, second, original.source_cues[2]))
    editor = SubtitleEditor(original)
    with pytest.raises(SubtitleEditError, match="S2"):
        editor.set_cue_role_many(("c1", "c2") if select_both else ("c1",), role="scene_text")
    assert editor.snapshot is original


def test_dialogue_conversion_does_not_revalidate_unrelated_dialogue_overlaps():
    original = role_snapshot()
    first = replace(original.source_cues[0], range=TimeRange(0, 1_500_000))
    original = replace(original, source_cues=(first, *original.source_cues[1:]))
    updated = SubtitleEditor(original).set_cue_role_many(("c2",), role="dialogue")
    assert updated.source_cues[1].subtitle_track_id == "S1"
    assert updated.source_cues[0].range == first.range


def test_scene_conversion_rejects_overlap_with_dialogue_already_displayed_on_s2():
    original = role_snapshot()
    first = replace(original.source_cues[0], range=TimeRange(0, 1_500_000),
                    subtitle_track_id="S2")
    original = replace(original, source_cues=(first, *original.source_cues[1:]))
    editor = SubtitleEditor(original)
    with pytest.raises(SubtitleEditError, match="S2"):
        editor.set_cue_role_many(("c2",), role="scene_text")
    assert editor.snapshot is original


def test_repeat_scene_conversion_is_noop_with_preexisting_mixed_s2_overlap():
    original = role_snapshot()
    first = replace(original.source_cues[0], range=TimeRange(0, 1_500_000),
                    subtitle_track_id="S2")
    scene = replace(original.source_cues[1], subtitle_track_id="S2")
    original = replace(original, source_cues=(first, scene, original.source_cues[2]),
                       translated_cues=tuple(replace(cue, language="vi")
                                             for cue in (first, scene, original.source_cues[2])),
                       tracks=(TimelineTrack("S2", TrackKind.SUBTITLE,
                                             "Phụ đề / Chữ trong cảnh", tuple(
                           TimelineClip(f"clip-{cue.id}", "S2", cue.range,
                                        cue_id=cue.id, cue_index=cue.index)
                           for cue in (first, scene))),))
    editor = SubtitleEditor(original)
    assert editor.set_cue_role_many(("c2",), role="scene_text") is original
    assert not editor.can_undo
    updated = editor.set_cue_role_many(("c2", "c3"), role="scene_text")
    assert updated.source_cues[0] == first and updated.source_cues[1] == scene
    assert updated.source_cues[2].role == "scene_text"
    assert updated.source_cues[2].subtitle_track_id == "S2"


def rehome_snapshot() -> ProjectSnapshot:
    template = role_snapshot().source_cues[1]
    cues = (
        replace(template, id="old1", index=1, subtitle_track_id="S1"),
        replace(template, id="old2", index=2, subtitle_track_id="S1"),
        replace(template, id="target", index=3, subtitle_track_id="S3"),
        replace(template, id="occupied", index=4, subtitle_track_id="S2", role="dialogue"),
        replace(template, id="unrelated", index=5, subtitle_track_id="S1",
                range=TimeRange(3_000_000, 4_000_000)),
    )
    translated = tuple(replace(cue, text=f"Dịch {cue.id}", language="vi") for cue in cues)
    tracks = tuple(TimelineTrack(lane, TrackKind.SUBTITLE, "Phụ đề / Chữ trong cảnh",
                                 tuple(TimelineClip(
                                     f"clip-{cue.id}", lane, cue.range, cue.range,
                                     cue_id=cue.id, cue_index=cue.index,
                                     cue_text=f"Dịch {cue.id}", volume=0.7,
                                 ) for cue in cues if cue.subtitle_track_id == lane))
                   for lane in ("S1", "S2", "S3"))
    return ProjectSnapshot("rehome", "Rehome", 1, source_cues=cues,
                           translated_cues=translated, tracks=tracks)


def test_dialogue_conversion_rehomes_only_colliding_s1_scenes_to_lowest_free_lanes():
    original = rehome_snapshot()
    editor = SubtitleEditor(original)
    updated = editor.set_cue_role_many(("target",), role="dialogue")
    destinations = {"target": "S1", "old1": "S3", "old2": "S4"}
    for collection in ("source_cues", "translated_cues"):
        for before, after in zip(getattr(original, collection), getattr(updated, collection),
                                 strict=True):
            assert after == (replace(
                before, subtitle_track_id=destinations[before.id], revision=before.revision + 1,
                role="dialogue" if before.id == "target" else before.role,
            ) if before.id in destinations else before)
    clips = {clip.id: clip for track in updated.tracks for clip in track.clips}
    for track in original.tracks:
        for clip in track.clips:
            assert clips[clip.id] == (replace(
                clip, track_id=destinations[clip.cue_id],
                payload_revision=clip.payload_revision + 1,
            ) if clip.cue_id in destinations else clip)
    assert len(editor._undo) == 1
    assert editor.undo().source_cues == original.source_cues
    assert editor.redo().source_cues == updated.source_cues
    reopened = decode_project_snapshot(json.loads(encode_project_snapshot(updated)[0]))
    assert reopened == updated


def test_rehomed_scenes_keep_legacy_translation_and_clip_identities():
    original = rehome_snapshot()
    legacy = replace(original.translated_cues[0], id="legacy-old1")
    original = replace(original, translated_cues=(legacy, *original.translated_cues[1:]),
                       tracks=tuple(replace(track, clips=tuple(
                           replace(clip, cue_id=legacy.id) if clip.cue_id == "old1" else clip
                           for clip in track.clips)) for track in original.tracks))
    updated = SubtitleEditor(original).set_cue_role_many(("target",), role="dialogue")
    assert updated.translated_cues[0] == replace(legacy, subtitle_track_id="S3", revision=2)
    old_clip = next(clip for track in updated.tracks for clip in track.clips
                    if clip.id == "clip-old1")
    assert old_clip.cue_id == "legacy-old1" and old_clip.track_id == "S3"


def test_role_conversion_refreshes_only_affected_automatic_track_names():
    original = role_snapshot()
    first, second, third, voice = original.tracks
    original = replace(original, tracks=(
        replace(first, name="Phụ đề thoại", locked=True, muted=True, visible=False),
        replace(second, name="Chữ trong cảnh"),
        replace(third, name="Nhãn riêng của tôi"), voice,
    ))
    updated = SubtitleEditor(original).set_cue_role_many(("c2", "c3"), role="dialogue")
    s1 = next(track for track in updated.tracks if track.id == "S1")
    assert s1.name == "Phụ đề thoại" and s1.locked and s1.muted and not s1.visible
    scene_updated = SubtitleEditor(original).set_cue_role_many(("c1",), role="scene_text")
    s2 = next(track for track in scene_updated.tracks if track.id == "S2")
    assert s2.name == "Chữ trong cảnh"
    assert next(track for track in scene_updated.tracks if track.id == "S4") == original.tracks[2]


def test_role_change_without_lane_move_refreshes_auto_name_but_preserves_custom_name():
    original = role_snapshot()
    scene = replace(original.source_cues[1], subtitle_track_id="S2")
    first = replace(original.source_cues[0], subtitle_track_id="S2")
    original = replace(original, source_cues=(first, scene, original.source_cues[2]),
                       translated_cues=tuple(replace(cue, language="vi")
                                             for cue in (first, scene, original.source_cues[2])),
                       tracks=(TimelineTrack("S2", TrackKind.SUBTITLE,
                                             "Phụ đề / Chữ trong cảnh", tuple(
                           TimelineClip(f"clip-{cue.id}", "S2", cue.range,
                                        cue_id=cue.id, cue_index=cue.index)
                           for cue in (first, scene))),))
    updated = SubtitleEditor(original).set_cue_role_many(("c1",), role="scene_text")
    assert updated.tracks[0].name == "Chữ trong cảnh"
    custom = replace(original, tracks=(replace(original.tracks[0], name="Chữ của đoàn phim"),))
    updated = SubtitleEditor(custom).set_cue_role_many(("c1",), role="scene_text")
    assert updated.tracks[0].name == "Chữ của đoàn phim"


def test_role_conversion_restores_missing_selected_clip_without_rebuilding_others():
    original = role_snapshot()
    original = replace(original, tracks=(original.tracks[0], *original.tracks[2:]))
    updated = SubtitleEditor(original).set_cue_role_many(("c2",), role="dialogue")
    s1 = next(track for track in updated.tracks if track.id == "S1")
    assert s1.clips[0] == original.tracks[0].clips[0]
    added = next(clip for clip in s1.clips if clip.cue_id == "c2")
    assert added.timeline_range == original.source_cues[1].range
    assert added.cue_text == original.translated_cues[1].text
    assert updated.tracks[1:] == original.tracks[1:]
