from __future__ import annotations

from dataclasses import replace

import pytest

from ducai_studio.application.subtitle_commands import (
    SubtitleEditError,
    SubtitleEditor,
    auto_break_text,
)
from ducai_studio.domain.project import ProjectSnapshot, SubtitleCue
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
