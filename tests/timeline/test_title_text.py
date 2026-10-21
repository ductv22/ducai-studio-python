import json
from dataclasses import asdict, replace

import pytest

from ducai_studio.application.export_pipeline import build_export_snapshot
from ducai_studio.application.media_commands import MediaImportService
from ducai_studio.application.timeline_commands import TimelineEditor
from ducai_studio.application.title_text_commands import edit_title_text
from ducai_studio.domain.effects import ExportSettings, SubtitleStyle
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
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.domain.timeline_edit import project_for_timeline
from ducai_studio.domain.voice_conform import (
    VoiceConformCueInput,
    VoiceConformSettings,
    VoiceTimingMode,
    build_voice_conform_plan,
)
from ducai_studio.infrastructure.project_repository import decode_project_snapshot
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.export.ffmpeg import decode_export_snapshot
from ducai_studio.providers.media_probe import MediaProbeResult
from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel

S = 1_000_000


def project():
    video = TimelineClip("video", "V1", TimeRange(0, 60 * S), TimeRange(0, 60 * S), "media")
    dialogue = SubtitleCue("dialogue", 1, TimeRange(20 * S, 22 * S), "Thoại", "vi")
    return ProjectSnapshot(
        "project",
        "Movie",
        1,
        media=(MediaAsset("media", "movie.mp4", MediaKind.VIDEO, 60 * S),),
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),),
        source_cues=(dialogue,),
        translated_cues=(dialogue,),
    )


def add(snapshot, start=0, end=60 * S):
    return edit_title_text(
        snapshot,
        "add_title_text",
        None,
        {
            "text": "Tên phim / Kênh",
            "start_us": start,
            "end_us": end,
            "visual_box": [0.04, 0.04, 0.4, 0.14],
        },
    )


def test_titles_are_separate_roundtrip_and_preserve_subtitle_inputs():
    original = project()
    updated = add(original)
    assert updated.source_cues == original.source_cues
    assert updated.translated_cues == original.translated_cues
    assert updated.tracks[0].kind is TrackKind.TEXT
    assert updated.title_cues[0].role == "title_text"
    assert updated.title_cues[0].subtitle_track_id == "T1"
    assert decode_project_snapshot(json.loads(json.dumps(asdict(updated)))) == updated
    raw = json.loads(json.dumps(asdict(original)))
    raw.pop("title_cues")
    assert decode_project_snapshot(raw).title_cues == ()
    assert [cue["id"] for cue in JobManagerService._tts_cues_for_snapshot(updated)] == ["dialogue"]
    with pytest.raises(ValueError, match="job inputs"):
        replace(updated, source_cues=(replace(updated.title_cues[0], id="invalid-subtitle"),))


def test_overlapping_titles_share_one_text_line_and_style_is_independent():
    first = add(project(), 10 * S, 15 * S)
    second = add(first, 11 * S, 16 * S)
    assert len(second.title_cues) == 2
    assert len([track for track in second.tracks if track.kind is TrackKind.TEXT]) == 1
    cue_id = first.title_cues[0].id
    formatted = edit_title_text(
        second,
        "set_text_format",
        cue_id,
        {
            "cue_ids": [cue_id],
            "font_size": 72,
            "orientation": "horizontal",
            "text_color": "#00FF00",
            "background_color": "#000000",
            "style_hint": "display",
        },
    )
    assert next(cue for cue in formatted.title_cues if cue.id == cue_id).text_font_size == 72
    assert next(cue for cue in formatted.title_cues if cue.id != cue_id).text_font_size == 40
    assert formatted.source_cues == project().source_cues
    moved = edit_title_text(
        formatted, "move_scene_texts", cue_id, {"cue_ids": [cue_id], "delta_us": -5 * S}
    )
    assert next(cue for cue in moved.title_cues if cue.id == cue_id).range == TimeRange(
        5 * S, 10 * S
    )


def test_title_cut_delete_and_history_keep_source_data():
    updated = add(project())
    editor = TimelineEditor(updated)
    cut = editor.cut("keep_from", 10 * S, 60 * S)
    assert project_for_timeline(cut).title_cues[0].range == TimeRange(0, 50 * S)
    assert editor.undo().title_cues == updated.title_cues
    assert editor.redo().title_cues == cut.title_cues
    deleted = edit_title_text(cut, "delete", cut.title_cues[0].id, {})
    assert not deleted.title_cues
    assert deleted.source_cues == updated.source_cues


def test_title_service_uses_compact_patch_and_one_undo(tmp_path):
    service = JobManagerService(
        database_path=tmp_path / "state.db", artifact_root=tmp_path / "cache"
    )
    snapshot = project()
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=snapshot)
    patch = service.subtitle_command(
        project_id=snapshot.id,
        command="add_title_text",
        params={
            "title_mode": True,
            "text": "Channel",
            "start_us": 0,
            "end_us": 60 * S,
            "visual_box": [0.04, 0.04, 0.4, 0.14],
        },
        compact=True,
        deferred=True,
    )
    assert patch["_timeline_patch"] is True
    assert len(patch["title_cue_upserts"]) == 1
    assert patch["source_cue_upserts"] == []
    assert patch["track_order"][0] == "T1"
    saved = service.projects.load(snapshot.id)
    service.save_project_edits(snapshot.id)
    assert saved.title_cues == service.projects.load(snapshot.id).title_cues
    undo = service.timeline_command(project_id=snapshot.id, command="undo", deferred=True)
    assert undo["removed_title_cue_ids"] == [saved.title_cues[0].id]
    redo = service.timeline_command(project_id=snapshot.id, command="redo", deferred=True)
    assert redo["title_cue_upserts"][0]["text"] == "Channel"


class Probe:
    def probe(self, _path):
        return MediaProbeResult(60 * S, True, True, 854, 480, "mp4")


def test_title_edit_patch_does_not_resend_twelve_thousand_subtitle_indices(tmp_path):
    cues = tuple(
        SubtitleCue(
            f"dialogue-{index}",
            index + 1,
            TimeRange(index * 4000, (index + 1) * 4000),
            "Thoại",
            "vi",
        )
        for index in range(12000)
    )
    snapshot = replace(project(), source_cues=cues, translated_cues=cues)
    service = JobManagerService(
        database_path=tmp_path / "state.db", artifact_root=tmp_path / "cache"
    )
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=snapshot)
    added = service.subtitle_command(
        project_id=snapshot.id,
        command="add_title_text",
        params={"title_mode": True, "text": "Channel", "start_us": 0, "end_us": 60 * S},
        compact=True,
        deferred=True,
    )
    cue_id = added["title_cue_upserts"][0]["id"]
    patches = [added]
    for command, params in (
        ("set_text_format", {"font_size": 72, "orientation": "horizontal", "background_color": ""}),
        ("set_scene_text_timing", {"start_us": S, "end_us": 59 * S}),
        ("delete", {}),
    ):
        patches.append(
            service.subtitle_command(
                project_id=snapshot.id,
                command=command,
                cue_id=cue_id,
                params={"title_mode": True, **params},
                compact=True,
                deferred=True,
            )
        )
    for command in ("undo", "redo"):
        patches.append(
            service.timeline_command(project_id=snapshot.id, command=command, deferred=True)
        )
    for patch in patches:
        assert not patch["source_cue_indices"] and not patch["translated_cue_indices"]
        assert len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) < 10000
    updated = service.projects.load(snapshot.id)
    assert updated.source_cues == cues and updated.translated_cues == cues


def test_titles_survive_source_and_translation_replacement_without_entering_srt(tmp_path):
    snapshot = add(project())
    importer = MediaImportService(Probe())
    source = tmp_path / "source.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\n新字幕\n", encoding="utf-8")
    updated = importer.import_srt(snapshot, source)
    translated = tmp_path / "translated.srt"
    translated.write_text("1\n00:00:01,000 --> 00:00:02,000\nThoại mới\n", encoding="utf-8")
    updated = importer.import_srt(updated, translated, language="vi")
    assert updated.title_cues == snapshot.title_cues
    assert next(t for t in updated.tracks if t.kind is TrackKind.TEXT) == snapshot.tracks[0]
    service = JobManagerService(
        database_path=tmp_path / "state.db", artifact_root=tmp_path / "cache"
    )
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=updated)
    result = service.export_subtitle_srt(
        project_id=updated.id, language="translated", output_path=str(tmp_path / "export.srt")
    )
    assert result
    assert (tmp_path / "export.srt").read_text(encoding="utf-8").strip().endswith("Thoại mới")
    assert "Tên phim" not in (tmp_path / "export.srt").read_text(encoding="utf-8")
    assert len(service._tts_cues_for_snapshot(updated)) == 1


def test_title_track_lock_delete_visibility_and_schema_compatibility(tmp_path):
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])
    snapshot = add(project())
    media = tmp_path / "movie.mp4"
    media.touch()
    snapshot = replace(snapshot, media=(replace(snapshot.media[0], path=str(media)),))
    locked = TimelineEditor(snapshot).set_track_locked("T1", True)
    with pytest.raises(ValueError, match="đang khóa"):
        edit_title_text(locked, "delete", locked.title_cues[0].id, {})
    # Explicit confirmed removal of a whole track follows the existing track
    # contract; the lock prevents cue edits, not the user's track removal.
    assert not TimelineEditor(locked).delete_tracks(("T1",)).title_cues
    hidden = replace(
        snapshot, tracks=(replace(snapshot.tracks[0], visible=False), *snapshot.tracks[1:])
    )
    model = ProjectStateModel()
    model.replace(json.loads(json.dumps(asdict(hidden))))
    assert not model.titleTextsAt(S)
    settings = ExportSettings(subtitle_style=SubtitleStyle(enabled=False))
    assert not build_export_snapshot(hidden, settings=settings, media_probe=Probe()).title_texts
    rendered = build_export_snapshot(snapshot, settings=settings, media_probe=Probe())
    assert rendered.schema_version == 5 and rendered.title_texts
    assert decode_export_snapshot(json.loads(json.dumps(asdict(rendered)))) == rendered
    legacy = json.loads(json.dumps(asdict(replace(rendered, schema_version=1, title_texts=()))))
    legacy.pop("title_texts")
    assert not decode_export_snapshot(legacy).title_texts
    deleted = TimelineEditor(snapshot).delete_tracks(("T1",))
    assert not deleted.title_cues and deleted.source_cues == snapshot.source_cues


def test_title_display_edits_share_conform_and_repeated_cut_clock(tmp_path):
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])
    snapshot = add(project(), 10 * S, 30 * S)
    media = tmp_path / "movie.mp4"
    media.touch()
    snapshot = replace(snapshot, media=(replace(snapshot.media[0], path=str(media)),))
    plan = build_voice_conform_plan(
        (VoiceConformCueInput("dialogue", 1, 0, 10 * S, 20 * S),),
        source_duration_us=60 * S,
        settings=VoiceConformSettings(mode=VoiceTimingMode.STRETCH_VIDEO),
    )
    base = tmp_path / "base.wav"
    output = tmp_path / "output.wav"
    base.touch()
    output.touch()
    snapshot = replace(
        snapshot,
        voice_conform=VoiceConformState(plan, "base", "output", "job"),
        media=(
            *snapshot.media,
            MediaAsset("base", str(base), MediaKind.AUDIO, 60 * S),
            MediaAsset("output", str(output), MediaKind.AUDIO, 70 * S),
        ),
        tracks=(
            *snapshot.tracks,
            TimelineTrack(
                "D1",
                TrackKind.VOICE,
                "Voice",
                (
                    TimelineClip(
                        "voice", "D1", TimeRange(0, 70 * S), TimeRange(0, 70 * S), "output"
                    ),
                ),
            ),
        ),
    )
    model = ProjectStateModel()
    model.replace(json.loads(json.dumps(asdict(snapshot))))
    cue = snapshot.title_cues[0]
    assert model.title_cues.find(cue.id)["start_us"] == 20 * S
    assert model.clips._track_indexes[0][0][0]["timeline_range"] == {
        "start_us": 20 * S,
        "end_us": 40 * S,
    }
    assert model.clips._track_indexes[1][0][0]["timeline_range"]["end_us"] == 70 * S
    service = JobManagerService(
        database_path=tmp_path / "state.db", artifact_root=tmp_path / "cache"
    )
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=snapshot)
    service.subtitle_command(
        project_id=snapshot.id,
        command="set_scene_text_timing",
        cue_id=cue.id,
        params={"title_mode": True, "display_time": True, "start_us": 22 * S, "end_us": 42 * S},
        compact=True,
        deferred=True,
    )
    updated = service.projects.load(snapshot.id)
    assert updated.title_cues[0].range == TimeRange(12 * S, 32 * S)
    cut = TimelineEditor(updated).cut("keep_from", 10 * S, 70 * S)
    cut = TimelineEditor(cut).cut("keep_from", 5 * S, 60 * S)
    projected = project_for_timeline(cut)
    # The source title (12..32) maps through conform (22..42), then 15 s removed.
    assert projected.title_cues[0].range == TimeRange(7 * S, 27 * S)
    assert (
        build_export_snapshot(cut, settings=ExportSettings(), media_probe=Probe())
        .title_texts[0]
        .start_us
        == 7 * S
    )


def test_title_preview_queries_only_active_rows_and_keeps_temporary_timing(monkeypatch):
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace({"id": "titles", "revision": 1, "title_cues": [
        {"id": f"title-{i}", "index": i + 1, "role": "title_text",
         "subtitle_track_id": "T1", "text": f"Title {i}",
         "range": {"start_us": i * S, "end_us": i * S + 500_000}} for i in range(2000)
    ]})
    inspected = []
    original = model.title_cues._with_scene_timing_preview

    def inspect(item):
        inspected.append(item["cue_id"])
        return original(item)

    monkeypatch.setattr(model.title_cues, "_with_scene_timing_preview", inspect)
    assert [cue["cue_id"] for cue in model.titleTextsAt(100 * S)] == ["title-100"]
    assert len(inspected) <= 2
    assert model.titleTextsAt(100 * S + 500_000) == []
    assert model.titleTextsAt(-0.5) == []
    model.title_cues.preview_scene_text_timing("title-1999", 100 * S, 101 * S)
    model.title_cues.preview_scene_text_timing("title-100", 103 * S, 104 * S)
    inspected.clear()
    assert [cue["cue_id"] for cue in model.titleTextsAt(100 * S)] == ["title-1999"]
    assert len(inspected) <= 4
    assert not model.titleTextsAt(1999 * S)
    assert [cue["cue_id"] for cue in model.titleTextsAt(103 * S)] == ["title-100", "title-103"]
    model.title_cues._scene_timing_previews.clear()
    assert [cue["cue_id"] for cue in model.titleTextsAt(100 * S)] == ["title-100"]
