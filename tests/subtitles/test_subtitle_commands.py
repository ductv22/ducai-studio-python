from __future__ import annotations

from dataclasses import replace

import pytest

from ducai_studio.application.subtitle_commands import (
    SubtitleEditError,
    SubtitleEditor,
    allocate_scene_text_lanes,
    auto_break_text,
)
from ducai_studio.domain.project import (
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
    dialogue_cues,
)
from ducai_studio.domain.timebase import TimeRange


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        "project",
        "Phim",
        1,
        source_cues=(
            SubtitleCue("cue-1", 1, TimeRange(0, 2_000_000), "我要开始修炼。然后成仙", "zh"),
            SubtitleCue("cue-2", 2, TimeRange(2_000_000, 4_000_000), "第二句话", "zh"),
        ),
    )


def _paired_snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        "project",
        "Phim",
        1,
        source_cues=(
            SubtitleCue("cue-1", 1, TimeRange(0, 2_000_000), "第一句话", "zh"),
            SubtitleCue("cue-2", 2, TimeRange(2_000_000, 4_000_000), "第二句话", "zh"),
        ),
        translated_cues=(
            SubtitleCue("vi-1", 1, TimeRange(0, 2_000_000), "Chiếc xe rất đẹp", "vi"),
            SubtitleCue("vi-2", 2, TimeRange(2_000_000, 4_000_000), "Xe này là xe mới", "vi"),
        ),
    )


def test_subtitle_split_merge_keeps_stable_first_id_and_integer_timeline() -> None:
    ids = iter(("new", "later"))
    editor = SubtitleEditor(_snapshot(), id_factory=lambda: next(ids))

    split = editor.split("cue-1", split_us=1_000_000)

    assert [cue.id for cue in split.source_cues] == ["cue-1", "cue-new", "cue-2"]
    assert split.source_cues[0].range == TimeRange(0, 1_000_000)
    assert split.source_cues[1].range == TimeRange(1_000_000, 2_000_000)
    merged = editor.merge_with_next("cue-1")
    assert [cue.id for cue in merged.source_cues] == ["cue-1", "cue-2"]
    undone = editor.undo()
    assert undone.source_cues == split.source_cues
    assert undone.revision == merged.revision + 1
    redone = editor.redo()
    assert redone.source_cues == merged.source_cues
    assert redone.revision == undone.revision + 1


def test_subtitle_time_guard_rejects_overlap_and_find_replace_is_real() -> None:
    editor = SubtitleEditor(_snapshot())
    with pytest.raises(SubtitleEditError, match="overlap"):
        editor.adjust_time("cue-2", start_us=1_500_000, end_us=4_000_000)

    updated = editor.find_replace("修炼", "修 hành")

    assert "修 hành" in updated.source_cues[0].text
    assert updated.source_cues[0].revision == 2


def test_auto_break_prefers_punctuation_and_never_drops_text() -> None:
    text = "Đây là một câu rất dài, nhưng nội dung của nó phải được giữ nguyên hoàn toàn."

    broken = auto_break_text(text, max_chars=24)

    assert "\n" in broken
    assert broken.replace("\n", " ").replace("  ", " ") == text


def test_find_replace_count_and_expected_snapshot_match_the_applied_change() -> None:
    editor = SubtitleEditor(_paired_snapshot())

    assert editor.count_matches("xe", language="translated") == (2, 2)
    updated = editor.find_replace(
        "xe",
        "ô tô",
        language="translated",
        expected_count=2,
    )

    assert [cue.text for cue in updated.source_cues] == ["第一句话", "第二句话"]
    assert [cue.text for cue in updated.translated_cues] == [
        "Chiếc ô tô rất đẹp",
        "Xe này là ô tô mới",
    ]
    with pytest.raises(SubtitleEditError, match="đã thay đổi"):
        editor.find_replace(
            "ô tô",
            "xe",
            language="translated",
            expected_count=99,
        )


def test_find_replace_preserves_leading_space_in_replacement_text() -> None:
    snapshot = replace(
        _paired_snapshot(),
        translated_cues=(
            replace(_paired_snapshot().translated_cues[0], text="Đây là một chiếnhạm"),
            *_paired_snapshot().translated_cues[1:],
        ),
    )
    editor = SubtitleEditor(snapshot)

    updated = editor.find_replace(
        "hạm",
        " hạm",
        language="translated",
        expected_count=1,
    )

    assert updated.translated_cues[0].text == "Đây là một chiến hạm"


def test_delete_removes_the_source_translation_pair_and_is_undoable() -> None:
    editor = SubtitleEditor(_paired_snapshot())

    deleted = editor.delete("cue-1")

    assert [(cue.id, cue.index, cue.range) for cue in deleted.source_cues] == [
        ("cue-2", 1, TimeRange(2_000_000, 4_000_000))
    ]
    assert [(cue.id, cue.index, cue.range) for cue in deleted.translated_cues] == [
        ("vi-2", 1, TimeRange(2_000_000, 4_000_000))
    ]
    undone = editor.undo()
    assert undone.source_cues == _paired_snapshot().source_cues
    assert undone.translated_cues == _paired_snapshot().translated_cues
    assert undone.revision == deleted.revision + 1
    redone = editor.redo()
    assert redone.source_cues == deleted.source_cues
    assert redone.translated_cues == deleted.translated_cues
    assert redone.revision == undone.revision + 1


def test_delete_last_cue_allows_empty_paired_timelines_and_rejects_unknown_id() -> None:
    one_cue = replace(
        _paired_snapshot(),
        source_cues=_paired_snapshot().source_cues[:1],
        translated_cues=_paired_snapshot().translated_cues[:1],
    )
    editor = SubtitleEditor(one_cue)

    deleted = editor.delete("vi-1")

    assert deleted.source_cues == ()
    assert deleted.translated_cues == ()
    with pytest.raises(LookupError, match="Unknown subtitle cue"):
        editor.delete("missing")


def test_delete_scene_text_also_removes_timeline_clip_and_empty_track() -> None:
    source = SubtitleCue(
        "scene-1",
        1,
        TimeRange(1_000_000, 2_000_000),
        "韩立",
        "zh",
        role="scene_text",
        background_color="#101010",
    )
    translated = replace(source, text="Hàn Lập", language="vi")
    clip = TimelineClip(
        "scene-clip-1",
        "S2",
        source.range,
        cue_id=source.id,
        cue_index=source.index,
        cue_text=translated.text,
    )
    snapshot = ProjectSnapshot(
        "project",
        "Phim",
        1,
        tracks=(TimelineTrack("S2", TrackKind.SUBTITLE, "Chữ trong cảnh", (clip,)),),
        source_cues=(source,),
        translated_cues=(translated,),
    )
    editor = SubtitleEditor(snapshot)

    deleted = editor.delete("scene-1")

    assert deleted.source_cues == ()
    assert deleted.translated_cues == ()
    assert deleted.tracks == ()
    restored = editor.undo()
    assert restored.tracks == snapshot.tracks
    assert restored.source_cues == snapshot.source_cues


def test_shared_text_format_updates_source_and_translation_for_any_subtitle_track() -> None:
    source = replace(
        _paired_snapshot().source_cues[0],
        subtitle_track_id="S1",
    )
    translated = replace(
        _paired_snapshot().translated_cues[0],
        id=source.id,
        subtitle_track_id="S1",
    )
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            source_cues=(source,),
            translated_cues=(translated,),
        )
    )

    updated = editor.set_text_format(
        source.id,
        font_size=27,
        orientation="vertical",
        background_color="#223344",
    )

    assert updated.source_cues[0].text_font_size == 27
    assert updated.translated_cues[0].text_font_size == 27
    assert updated.source_cues[0].orientation == "vertical"
    assert updated.translated_cues[0].background_color == "#223344"
    assert editor.undo().source_cues[0].text_font_size == 12
    with pytest.raises(SubtitleEditError, match="1 đến 100"):
        editor.set_text_format(
            source.id,
            font_size=101,
            orientation="horizontal",
            background_color="",
        )


def test_shared_text_format_persists_calligraphy_and_tight_background_padding() -> None:
    source = replace(
        _paired_snapshot().source_cues[0],
        subtitle_track_id="S1",
    )
    translated = replace(
        _paired_snapshot().translated_cues[0],
        id=source.id,
        subtitle_track_id="S1",
    )
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            source_cues=(source,),
            translated_cues=(translated,),
        )
    )

    updated = editor.set_text_format_many(
        (source.id,),
        font_size=72,
        orientation="horizontal",
        background_color="#223344",
        text_color="#F4E8C1",
        style_hint="calligraphy_soft",
        background_style="ink_splash",
        background_padding_x=24,
        background_padding_y=12,
    )

    for cue in (*updated.source_cues, *updated.translated_cues):
        assert cue.text_font_size == 72
        assert cue.style_hint == "calligraphy_soft"
        assert cue.background_style == "ink_splash"
        assert cue.background_padding_x == 24
        assert cue.background_padding_y == 12

    with pytest.raises(SubtitleEditError, match="Nền ngang"):
        editor.set_text_format_many(
            (source.id,),
            font_size=12,
            orientation="horizontal",
            background_color="",
            background_padding_x=101,
        )

    with pytest.raises(SubtitleEditError, match="Kiểu nền"):
        editor.set_text_format_many(
            (source.id,),
            font_size=12,
            orientation="horizontal",
            background_color="",
            background_style="photocopy",
        )


def test_add_manual_scene_text_creates_paired_cue_and_s2_clip() -> None:
    video = TimelineClip("video-1", "V1", TimeRange(0, 10_000_000))
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),),
        ),
        id_factory=lambda: "manual",
    )

    updated = editor.add_scene_text(
        text="Hàn Lập",
        start_us=1_000_000,
        end_us=3_000_000,
        visual_box=(0.68, 0.18, 0.2, 0.12),
        font_size=18,
        orientation="vertical",
        background_color="#112233",
    )

    source = updated.source_cues[0]
    translated = updated.translated_cues[0]
    assert source.id == translated.id == "cue-manual"
    assert source.source == "manual_scene_text"
    assert source.text_font_size == 18
    assert source.orientation == "vertical"
    scene_track = next(track for track in updated.tracks if track.id == "S2")
    assert scene_track.clips[0].cue_id == source.id
    assert scene_track.clips[0].timeline_range == source.range
    assert editor.undo().source_cues == ()


def test_new_scene_text_uses_brush_white_on_opaque_black_ink_by_default() -> None:
    video = TimelineClip("video-1", "V1", TimeRange(0, 10_000_000))
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),),
        ),
        id_factory=lambda: "manual-default",
    )

    cue = editor.add_scene_text(
        text="Tên pháp bảo",
        start_us=1_000_000,
        end_us=3_000_000,
        visual_box=(0.20, 0.30, 0.30, 0.12),
    ).source_cues[0]

    assert cue.style_hint == "calligraphy_brush"
    assert cue.background_style == "ink_splash"
    assert cue.text_color == "#FFFFFF"
    assert cue.background_color == "#000000"
    assert cue.background_opacity_percent == 100


def test_scene_text_lane_allocator_splits_overlap_and_reuses_lowest_lane() -> None:
    cues = (
        SubtitleCue(
            "dialogue",
            1,
            TimeRange(0, 5_000_000),
            "对白",
            "zh",
            role="dialogue",
        ),
        SubtitleCue("scene-a", 2, TimeRange(0, 2_000_000), "甲", "zh", role="scene_text"),
        SubtitleCue("scene-b", 3, TimeRange(500_000, 2_500_000), "乙", "zh", role="scene_text"),
        SubtitleCue("scene-c", 4, TimeRange(1_000_000, 3_000_000), "丙", "zh", role="scene_text"),
        SubtitleCue("scene-d", 5, TimeRange(3_000_000, 4_000_000), "丁", "zh", role="scene_text"),
    )

    allocated = allocate_scene_text_lanes(cues)

    assert [cue.subtitle_track_id for cue in allocated] == ["S1", "S2", "S3", "S4", "S2"]


def test_manual_scene_text_always_allocates_a_new_subtitle_lane() -> None:
    video = TimelineClip("video-1", "V1", TimeRange(0, 10_000_000))
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),),
        ),
        id_factory=iter(("first", "second")).__next__,
    )

    first = editor.add_scene_text(
        text="Một",
        start_us=1_000_000,
        end_us=2_000_000,
        visual_box=(0.1, 0.1, 0.2, 0.1),
    )
    assert first.source_cues[0].subtitle_track_id == "S2"
    second = editor.add_scene_text(
        text="Hai",
        start_us=3_000_000,
        end_us=4_000_000,
        visual_box=(0.3, 0.1, 0.2, 0.1),
    )

    assert {track.id for track in second.tracks if track.kind is TrackKind.SUBTITLE} == {
        "S2",
        "S3",
    }
    assert next(cue for cue in second.source_cues if cue.text == "Hai").subtitle_track_id == "S3"


def test_manual_scene_text_allocates_below_legacy_cue_lane_without_track() -> None:
    legacy = SubtitleCue(
        "legacy-scene",
        1,
        TimeRange(0, 1_000_000),
        "人物",
        "zh",
        role="scene_text",
        subtitle_track_id="S4",
    )
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim cũ",
            1,
            source_cues=(legacy,),
        ),
        id_factory=lambda: "manual",
    )

    updated = editor.add_scene_text(
        text="Thêm mới",
        start_us=2_000_000,
        end_us=3_000_000,
        visual_box=(0.1, 0.1, 0.2, 0.1),
    )

    created = next(cue for cue in updated.source_cues if cue.id == "cue-manual")
    assert created.subtitle_track_id == "S5"


def test_batch_format_geometry_and_lane_move_are_one_revision_each() -> None:
    video = TimelineClip("video-1", "V1", TimeRange(0, 10_000_000))
    first = SubtitleCue(
        "scene-1",
        1,
        TimeRange(1_000_000, 3_000_000),
        "甲",
        "zh",
        role="scene_text",
        subtitle_track_id="S2",
        visual_box=(0.1, 0.1, 0.2, 0.1),
        visual_box_source="ocr",
        panel_box=(0.08, 0.08, 0.24, 0.14),
    )
    second = replace(
        first,
        id="scene-2",
        index=2,
        text="乙",
        subtitle_track_id="S3",
        visual_box=(0.5, 0.1, 0.2, 0.1),
        panel_box=(0.48, 0.08, 0.24, 0.14),
    )
    translated = (
        replace(first, text="Một", language="vi"),
        replace(second, text="Hai", language="vi"),
    )
    tracks = (
        TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),
        TimelineTrack(
            "S2",
            TrackKind.SUBTITLE,
            "Chữ trong cảnh",
            (TimelineClip("clip-1", "S2", first.range, cue_id=first.id, cue_index=1),),
        ),
        TimelineTrack(
            "S3",
            TrackKind.SUBTITLE,
            "Chữ trong cảnh",
            (TimelineClip("clip-2", "S3", second.range, cue_id=second.id, cue_index=2),),
        ),
    )
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            tracks=tracks,
            source_cues=(first, second),
            translated_cues=translated,
        )
    )

    formatted = editor.set_text_format_many(
        ("scene-1", "scene-2"),
        font_size=31,
        orientation="vertical",
        background_color="#112233",
        text_color="#EEDDCC",
        style_hint="calligraphy",
    )
    assert formatted.revision == 2
    assert {cue.text_color for cue in formatted.source_cues} == {"#EEDDCC"}
    assert {cue.style_hint for cue in formatted.translated_cues} == {"calligraphy"}

    transformed = editor.transform_cue_geometry(
        ("scene-1", "scene-2"),
        delta_x=0.05,
        delta_y=0.10,
        scale_x=1.0,
        scale_y=1.0,
    )
    assert transformed.revision == 3
    assert transformed.source_cues[0].visual_box == first.visual_box
    assert transformed.source_cues[0].panel_box == pytest.approx((0.13, 0.18, 0.24, 0.14))
    assert {cue.text_font_size for cue in transformed.source_cues} == {31}
    assert {cue.text_font_size for cue in transformed.translated_cues} == {31}

    moved = editor.move_scene_texts(
        ("scene-1", "scene-2"),
        delta_us=1_000_000,
        lane_delta=1,
    )
    assert moved.revision == 4
    assert [cue.range for cue in moved.source_cues] == [
        TimeRange(2_000_000, 4_000_000),
        TimeRange(2_000_000, 4_000_000),
    ]
    assert [cue.subtitle_track_id for cue in moved.source_cues] == ["S3", "S4"]
    assert {track.id for track in moved.tracks if track.kind is TrackKind.SUBTITLE} == {
        "S3",
        "S4",
    }


def test_group_lane_move_resolves_collision_with_one_shared_offset() -> None:
    blocker = SubtitleCue(
        "blocker",
        1,
        TimeRange(1_000_000, 3_000_000),
        "阻挡",
        "zh",
        role="scene_text",
        subtitle_track_id="S2",
    )
    first = replace(
        blocker,
        id="scene-1",
        index=2,
        text="甲",
        range=TimeRange(1_500_000, 2_500_000),
    )
    second = replace(
        first,
        id="scene-2",
        index=3,
        text="乙",
        subtitle_track_id="S3",
    )
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Giữ khoảng lane",
            1,
            source_cues=(blocker, first, second),
        )
    )

    moved = editor.move_scene_texts(
        ("scene-1", "scene-2"),
        delta_us=0,
        lane_delta=0,
    )

    by_id = {cue.id: cue for cue in moved.source_cues}
    assert by_id["scene-1"].subtitle_track_id == "S3"
    assert by_id["scene-2"].subtitle_track_id == "S4"


def test_scene_text_moved_to_free_s1_becomes_dialogue_in_both_languages() -> None:
    scene = SubtitleCue(
        "scene-1",
        1,
        TimeRange(2_000_000, 3_000_000),
        "人物",
        "zh",
        role="scene_text",
        subtitle_track_id="S2",
    )
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "S1 thủ công",
            1,
            source_cues=(scene,),
            translated_cues=(replace(scene, text="Nhân vật", language="vi"),),
        )
    )

    moved = editor.move_scene_texts((scene.id,), delta_us=0, lane_delta=-1)

    assert moved.source_cues[0].subtitle_track_id == "S1"
    assert moved.source_cues[0].role == "dialogue"
    assert moved.translated_cues[0].role == "dialogue"
    assert dialogue_cues(moved.source_cues) == moved.source_cues
    assert dialogue_cues(moved.translated_cues) == moved.translated_cues
    assert moved.tracks[0].id == "S1"


def test_dialogue_moved_to_s2_keeps_spoken_role() -> None:
    dialogue = SubtitleCue(
        "dialogue-1",
        1,
        TimeRange(2_000_000, 3_000_000),
        "对白",
        "zh",
        role="dialogue",
        subtitle_track_id="S1",
    )
    editor = SubtitleEditor(
        ProjectSnapshot("project", "Thoại trên lane phụ", 1, source_cues=(dialogue,))
    )

    moved = editor.move_scene_texts((dialogue.id,), delta_us=0, lane_delta=1)

    assert moved.source_cues[0].subtitle_track_id == "S2"
    assert moved.source_cues[0].role == "dialogue"
    assert dialogue_cues(moved.source_cues) == moved.source_cues


def test_manual_dialogue_inherits_nearest_style_and_uses_free_lane() -> None:
    template = SubtitleCue(
        "dialogue-existing",
        1,
        TimeRange(1_000_000, 4_000_000),
        "原文",
        "zh",
        role="dialogue",
        subtitle_track_id="S1",
        visual_box=(0.08, 0.72, 0.84, 0.18),
        visual_box_source="estimated",
        panel_box=(0.10, 0.74, 0.80, 0.14),
        text_font_size=24,
        style_hint="calligraphy_ink",
        text_color="#F1E4C8",
        background_color="#101010",
        background_padding_x=30,
        background_padding_y=14,
        letter_spacing_percent=8,
        font_bold=True,
        text_opacity_percent=92,
        background_opacity_percent=86,
    )
    editor = SubtitleEditor(
        ProjectSnapshot("project", "Bổ sung thoại", 1, source_cues=(template,)),
        id_factory=lambda: "manual-dialogue",
    )

    updated = editor.add_subtitle_cue(
        text="Câu thoại bị sót",
        start_us=2_000_000,
        end_us=3_000_000,
        cue_role="dialogue",
    )

    created = next(cue for cue in updated.source_cues if cue.id == "cue-manual-dialogue")
    translated = next(cue for cue in updated.translated_cues if cue.id == created.id)
    assert created.role == translated.role == "dialogue"
    assert created.subtitle_track_id == translated.subtitle_track_id == "S2"
    assert created.panel_box == template.panel_box
    assert created.visual_box == template.panel_box
    assert created.text_font_size == template.text_font_size
    assert created.style_hint == template.style_hint
    assert created.text_color == template.text_color
    assert created.background_color == template.background_color
    assert created.background_padding_x == template.background_padding_x
    assert created.background_padding_y == template.background_padding_y
    assert created.letter_spacing_percent == template.letter_spacing_percent
    assert created.font_bold is True
    assert created.source == "manual_dialogue"
    assert created in dialogue_cues(updated.source_cues)


def test_scene_fade_updates_pair_in_one_revision() -> None:
    source = SubtitleCue(
        "scene-fade",
        1,
        TimeRange(0, 1_000_000),
        "法宝",
        "zh",
        role="scene_text",
        subtitle_track_id="S2",
    )
    translated = replace(source, text="Pháp bảo", language="vi")
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Mờ dần",
            1,
            source_cues=(source,),
            translated_cues=(translated,),
        )
    )

    updated = editor.set_scene_fade_many(
        (source.id,), enabled=True, duration_ms=450
    )

    assert updated.revision == 2
    assert updated.source_cues[0].fade_enabled is True
    assert updated.translated_cues[0].fade_enabled is True
    assert updated.source_cues[0].fade_duration_ms == 450
    assert updated.translated_cues[0].fade_duration_ms == 450


def test_scene_text_move_to_s1_rejects_dialogue_overlap_instead_of_bumping_lane() -> None:
    dialogue = SubtitleCue(
        "dialogue-1",
        1,
        TimeRange(1_000_000, 4_000_000),
        "对白",
        "zh",
        subtitle_track_id="S1",
    )
    scene = SubtitleCue(
        "scene-1",
        2,
        TimeRange(2_000_000, 3_000_000),
        "人物",
        "zh",
        role="scene_text",
        subtitle_track_id="S2",
    )
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "S1 bận",
            1,
            source_cues=(dialogue, scene),
        )
    )

    with pytest.raises(SubtitleEditError, match="S1.*cue thoại"):
        editor.move_scene_texts((scene.id,), delta_us=0, lane_delta=-1)


def test_translated_auto_break_defaults_to_sixty_eight_characters() -> None:
    text = "Một câu phụ đề ngắn vừa đủ để không bị tự ý xuống dòng khó chịu."
    assert len(text) <= 68
    source = SubtitleCue("source", 1, TimeRange(0, 1_000_000), "原文", "zh")
    translated = replace(source, id="translated", text=text, language="vi")
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Ngắt dòng",
            1,
            source_cues=(source,),
            translated_cues=(translated,),
        )
    )

    updated = editor.auto_break(language="translated")

    assert updated.translated_cues[0].text == text


def test_set_ocr_regions_is_undoable_and_validates_full_frame_geometry() -> None:
    editor = SubtitleEditor(_snapshot())

    updated = editor.set_ocr_regions(
        dialogue_region=(0.08, 0.74, 0.84, 0.22),
        exclusion_regions=((0.82, 0.02, 0.16, 0.08),),
    )

    assert updated.ocr_dialogue_region == (0.08, 0.74, 0.84, 0.22)
    assert updated.ocr_exclusion_regions == ((0.82, 0.02, 0.16, 0.08),)
    assert editor.undo().ocr_dialogue_region is None
    with pytest.raises(SubtitleEditError, match="Vùng OCR không hợp lệ"):
        editor.set_ocr_regions(
            dialogue_region=(0.8, 0.8, 0.4, 0.3),
            exclusion_regions=(),
        )


def test_accept_scene_text_promotes_source_and_translation_and_is_undoable() -> None:
    source = SubtitleCue(
        "scene-1",
        1,
        TimeRange(0, 1_000_000),
        "海王兽",
        "zh",
        role="scene_text",
        orientation="vertical",
        style_hint="calligraphy",
        review_state="review",
    )
    translated = replace(source, text="Hải Vương Thú", language="vi")
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            source_cues=(source,),
            translated_cues=(translated,),
        )
    )

    accepted = editor.accept_scene_text("scene-1")

    assert accepted.source_cues[0].review_state == "accepted"
    assert accepted.translated_cues[0].review_state == "accepted"
    assert editor.undo().source_cues[0].review_state == "review"


def test_scene_text_font_scale_updates_pair_and_is_undoable() -> None:
    source = SubtitleCue(
        "scene-1",
        1,
        TimeRange(0, 1_000_000),
        "海王兽",
        "zh",
        role="scene_text",
        orientation="vertical",
    )
    translated = replace(source, text="Hải Vương Thú", language="vi")
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            source_cues=(source,),
            translated_cues=(translated,),
        )
    )

    scaled = editor.set_scene_text_font_scale("scene-1", 1.25)

    assert scaled.source_cues[0].scene_font_scale == 1.25
    assert scaled.translated_cues[0].scene_font_scale == 1.25
    assert editor.undo().source_cues[0].scene_font_scale == 1.0
    assert editor.redo().translated_cues[0].scene_font_scale == 1.25

    with pytest.raises(ValueError, match="50%.*200%"):
        editor.set_scene_text_font_scale("scene-1", 2.5)


def test_dialogue_cue_rejects_scene_text_font_scale_command() -> None:
    editor = SubtitleEditor(_paired_snapshot())

    with pytest.raises(ValueError, match="Chỉ cue chữ trong cảnh"):
        editor.set_scene_text_font_scale("cue-1", 1.25)


def test_scene_text_timing_updates_pair_track_and_undo_redo() -> None:
    source = SubtitleCue(
        "scene-1",
        1,
        TimeRange(1_000_000, 3_000_000),
        "韩立",
        "zh",
        role="scene_text",
    )
    translated = replace(source, text="Hàn Lập", language="vi")
    video = TimelineClip("video-1", "V1", TimeRange(0, 10_000_000))
    scene = TimelineClip(
        "scene-clip-1",
        "S2",
        source.range,
        source.range,
        cue_id=source.id,
        cue_index=source.index,
        cue_text=translated.text,
    )
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            tracks=(
                TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),
                TimelineTrack("S2", TrackKind.SUBTITLE, "Chữ trong cảnh", (scene,)),
            ),
            source_cues=(source,),
            translated_cues=(translated,),
        )
    )

    updated = editor.set_scene_text_timing(
        "scene-1",
        start_us=500_000,
        end_us=4_000_000,
    )

    expected = TimeRange(500_000, 4_000_000)
    assert updated.source_cues[0].range == expected
    assert updated.translated_cues[0].range == expected
    assert updated.tracks[1].clips[0].timeline_range == expected
    assert updated.tracks[1].clips[0].source_range is None
    assert updated.tracks[1].clips[0].cue_text == "Hàn Lập"
    assert editor.undo().source_cues[0].range == source.range
    assert editor.redo().tracks[1].clips[0].timeline_range == expected


def test_subtitle_timing_rejects_short_and_out_of_video_ranges_but_allows_dialogue() -> None:
    source = SubtitleCue(
        "scene-1",
        1,
        TimeRange(1_000_000, 3_000_000),
        "韩立",
        "zh",
        role="scene_text",
    )
    video = TimelineClip("video-1", "V1", TimeRange(0, 5_000_000))
    editor = SubtitleEditor(
        ProjectSnapshot(
            "project",
            "Phim",
            1,
            tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),),
            source_cues=(source,),
        )
    )

    with pytest.raises(SubtitleEditError, match="100 ms"):
        editor.set_scene_text_timing("scene-1", start_us=1_000_000, end_us=1_050_000)
    with pytest.raises(SubtitleEditError, match="cuối video"):
        editor.set_scene_text_timing("scene-1", start_us=4_000_000, end_us=6_000_000)
    dialogue_editor = SubtitleEditor(_paired_snapshot())
    dialogue = dialogue_editor.set_scene_text_timing(
        "cue-1",
        start_us=0,
        end_us=1_000_000,
    )
    assert dialogue.source_cues[0].range == TimeRange(0, 1_000_000)
    assert dialogue.translated_cues[0].range == TimeRange(0, 1_000_000)
