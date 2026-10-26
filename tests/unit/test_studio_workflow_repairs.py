from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QUrl

from ducai_studio.application.subtitle_commands import SubtitleEditError, SubtitleEditor
from ducai_studio.domain.project import ProjectSnapshot, SubtitleCue, TimelineTrack, TrackKind
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.ui.viewmodels import application_view_model as application_module
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


class MemorySettings:
    def __init__(self, *_args):
        self.values = {}

    def value(self, key, default=None, *, type=None):
        value = self.values.get(key, default)
        return type(value) if type is not None else value

    def setValue(self, key, value):
        self.values[key] = value


@pytest.fixture
def model(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({
        "id": "film", "name": "Film", "revision": 1,
        "source_cues": [{"id": "c1", "index": 1, "text": "你好", "role": "dialogue",
                         "range": {"start_us": 0, "end_us": 1_000_000}}],
    })
    view_model._set_backend_ready(True)
    calls = []
    monkeypatch.setattr(view_model.runtime, "request",
                        lambda action, params=None, context=None:
                        calls.append((action, params, context)))
    yield view_model, calls
    view_model._subtitle_command_watchdog.stop()
    view_model._subtitle_save_timer.stop()


def test_completed_ocr_history_refreshes_project_without_restarting_casting(model):
    view_model, calls = model
    project = {**view_model.project_state._canonical_snapshot,
               "dialogue_casting": {"enabled": True}}
    view_model.project_state.replace(project)
    ocr = {"id": "old-ocr", "state": "succeeded",
           "spec": {"kind": "ocr", "project_id": "film"}}
    view_model._replace_jobs([ocr])
    context = next(context for action, _params, context in calls
                   if action == "get_project" and context.get("ocr_completed"))
    assert context["ocr_live_completion"] is False
    view_model._action_succeeded("get_project", deepcopy(project), context)
    assert not any(action == "create_dialogue_casting_job" for action, _, _ in calls)


def test_live_ocr_completion_still_starts_casting_once(model):
    view_model, calls = model
    project = {**view_model.project_state._canonical_snapshot,
               "dialogue_casting": {"enabled": True}}
    view_model.project_state.replace(project)
    ocr = {"id": "new-ocr", "state": "running",
           "spec": {"kind": "ocr", "project_id": "film"}}
    view_model._replace_jobs([ocr])
    view_model._replace_jobs([{**ocr, "state": "succeeded"}])
    context = next(context for action, _params, context in calls
                   if action == "get_project" and context.get("ocr_completed"))
    assert context["ocr_live_completion"] is True
    view_model._action_succeeded("get_project", deepcopy(project), context)
    view_model._action_succeeded("get_project", deepcopy(project), context)
    assert sum(action == "create_dialogue_casting_job" for action, _, _ in calls) == 1


@pytest.mark.parametrize("active_id,returned_id", [("other", "film"), ("film", "other")])
def test_tts_completion_rejects_stale_or_wrong_project_snapshot(model, active_id, returned_id):
    view_model, calls = model
    snapshot = deepcopy(view_model.project_state._canonical_snapshot)
    job = {"id": "completed-tts", "state": "succeeded",
           "spec": {"kind": "tts", "project_id": "film",
                    "provider_config": {"purpose": "production"}}}
    view_model._replace_jobs([job])
    context = next(context for action, _params, context in calls
                   if action == "get_project" and context.get("tts_completed"))
    view_model._apply_project({**snapshot, "id": active_id, "revision": 5})
    view_model._action_succeeded(
        "get_project", {**snapshot, "id": returned_id, "revision": 2}, context,
    )
    assert view_model.project_state.projectId == active_id
    assert view_model.project_state._canonical_snapshot["revision"] == 5


def test_tts_completion_refreshes_matching_current_project(model):
    view_model, calls = model
    snapshot = deepcopy(view_model.project_state._canonical_snapshot)
    job = {"id": "completed-tts", "state": "succeeded",
           "spec": {"kind": "tts", "project_id": "film",
                    "provider_config": {"purpose": "production"}}}
    view_model._replace_jobs([job])
    context = next(context for action, _params, context in calls
                   if action == "get_project" and context.get("tts_completed"))
    view_model._action_succeeded("get_project", {**snapshot, "revision": 2}, context)
    assert view_model.project_state.projectId == "film"
    assert view_model.project_state._canonical_snapshot["revision"] == 2


def test_historical_audition_does_not_publish_playback_source(model, tmp_path):
    view_model, _calls = model
    audio = tmp_path / "old-preview.wav"
    audio.write_bytes(b"existing-audition")
    job = {"id": "preview", "state": "succeeded",
           "spec": {"kind": "tts", "project_id": "film",
                    "provider_config": {"purpose": "preview"}},
           "result": {"purpose": "preview", "output_path": str(audio)}}
    changes = []
    view_model.voicePreviewChanged.connect(lambda: changes.append(True))
    view_model._replace_jobs([job])
    assert view_model.voicePreviewSource == ""
    assert changes == []
    view_model._voice_preview_target_job_id = "preview"
    view_model._replace_jobs([job])
    assert Path(QUrl(view_model.voicePreviewSource).toLocalFile()) == audio
    assert changes == [True]


def test_custom_casting_samples_are_per_role_and_preserve_other_voices(
    model, monkeypatch, tmp_path,
):
    view_model, calls = model
    entries = [{"is_clone": True, "installed": True, "voice_id": "clone-profile"}]
    monkeypatch.setattr(view_model.voice_catalog, "get", lambda _row: entries[0])
    monkeypatch.setattr(type(view_model.voice_catalog), "count", property(lambda _self: 1))
    male = {"provider_id": "piper", "voice_id": "male"}
    view_model._dialogue_casting["config"] = {"enabled": True, "male_voice": male}
    sample = tmp_path / "giọng nữ #1.wav"
    sample.write_bytes(b"audio-to-be-validated-by-engine")
    view_model.setDialogueCastingReferenceAudio("female", sample.as_uri())
    action, params, _ = calls[-1]
    assert action == "configure_dialogue_casting"
    assert params["config"]["male_voice"] == male
    voice = params["config"]["female_voice"]
    assert voice["reference_audio_path"] == str(sample.resolve())
    assert voice["voice_id"] == "clone-profile" and voice["provider_id"] == "vieneu-3.8"
    assert view_model.vieneuReferenceAudio == ""
    assert not any(action == "create_dialogue_casting_job" for action, _, _ in calls)


@pytest.mark.parametrize("filename,payload", [("bad.txt", b"data"), ("empty.wav", b"")])
def test_invalid_casting_sample_keeps_config(model, tmp_path, filename, payload):
    view_model, calls = model
    sample = tmp_path / filename
    sample.write_bytes(payload)
    view_model.setDialogueCastingReferenceAudio("female", sample.as_uri())
    assert calls == []
    assert "Hãy chọn file giọng" in view_model.statusMessage


def test_missing_clone_runtime_reports_error(model, tmp_path, monkeypatch):
    view_model, calls = model
    monkeypatch.setattr(type(view_model.voice_catalog), "count", property(lambda _self: 0))
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"audio")
    view_model.setDialogueCastingReferenceAudio("female", str(sample))
    assert calls == []
    assert "runtime VieNeu" in view_model.statusMessage


@pytest.fixture
def casting_import(model, monkeypatch, tmp_path):
    view_model, calls = model
    monkeypatch.setattr(type(view_model.voice_catalog), "count", property(lambda _self: 1))
    monkeypatch.setattr(view_model.voice_catalog, "get", lambda _row: {
        "voice_id": "clone-profile", "installed": True, "is_clone": True,
    })
    sample = tmp_path / "giọng mới.m4a"
    sample.write_bytes(b"validated-by-external-engine")
    return view_model, calls, sample


@pytest.mark.parametrize("role", ["male", "female", "fallback"])
def test_imported_role_auto_auditions_saved_managed_voice(casting_import, tmp_path, role):
    view_model, calls, sample = casting_import
    view_model.setDialogueCastingReferenceAudio(role, sample.as_uri())
    action, params, context = calls[-1]
    assert action == "configure_dialogue_casting"
    assert not any(action == "get_tts_voice_preview" for action, _, _ in calls)
    managed = tmp_path / "managed" / sample.name
    config = deepcopy(params["config"])
    config[role + "_voice"]["reference_audio_path"] = str(managed)
    result = {"project_id": "film", "config": config, "rows": []}
    view_model._action_succeeded(action, result, context)
    assert calls[-1][0] == "get_tts_voice_preview"
    assert calls[-1][1]["provider_options"]["reference_audio_path"] == str(managed)
    assert calls[-1][1]["provider_options"]["purpose"] == "preview"
    assert calls[-1][2]["casting_preview_role"] == role
    lookup_context = calls[-1][2]
    view_model._action_succeeded(action, result, context)
    assert sum(action == "get_tts_voice_preview" for action, _, _ in calls) == 1
    view_model._action_succeeded("get_tts_voice_preview", {"available": False}, lookup_context)
    assert calls[-1][0] == "create_tts_job"
    assert calls[-1][1]["provider_options"]["reference_audio_path"] == str(managed)
    assert view_model.ttsBusy
    assert not any(action == "create_dialogue_casting_job" for action, _, _ in calls)


def test_failed_role_import_cannot_audition_old_voice(casting_import):
    view_model, calls, sample = casting_import
    view_model._voice_preview_source = "file:///old-preview.wav"
    view_model.setDialogueCastingReferenceAudio("female", sample.as_uri())
    action, _, context = calls[-1]
    assert view_model.voicePreviewSource == ""
    view_model._action_failed(action, {"message": "import failed"}, context)
    assert [action for action, _, _ in calls] == ["configure_dialogue_casting"]
    assert not view_model.dialogueCastingSaving
    assert view_model.voicePreviewSource == ""


@pytest.mark.parametrize("action", ["get_dialogue_casting", "configure_dialogue_casting"])
def test_restored_or_regularly_saved_casting_config_never_auto_auditions(model, action):
    view_model, calls = model
    view_model._action_succeeded(action, {
        "project_id": "film", "config": {"female_voice": {
            "provider_id": "vieneu-3.8", "voice_id": "clone-profile",
            "reference_audio_path": "managed.wav",
        }}, "rows": [],
    }, {"project_id": "film", "dialogue_casting_config": True})
    assert calls == []
    assert view_model.voicePreviewSource == ""


def test_stale_role_import_cannot_audition_after_project_round_trip(casting_import):
    view_model, calls, sample = casting_import
    view_model.setDialogueCastingReferenceAudio("female", sample.as_uri())
    action, params, context = calls[-1]
    view_model._apply_project({"id": "other", "name": "Other", "revision": 1})
    view_model._apply_project({"id": "film", "name": "Film", "revision": 1})
    calls.clear()
    view_model._action_succeeded(action, {
        "project_id": "film", "config": params["config"], "rows": [],
    }, context)
    assert not any(action == "get_tts_voice_preview" for action, _, _ in calls)
    assert view_model.voicePreviewSource == ""


def test_same_role_file_can_replay_cached_clone_audition(casting_import, tmp_path):
    view_model, calls, sample = casting_import
    audio = tmp_path / "clone-audition.wav"
    audio.touch()
    publications = []
    view_model.voicePreviewChanged.connect(
        lambda: publications.append(view_model.voicePreviewSource)
    )
    for _ in range(2):
        view_model.setDialogueCastingReferenceAudio("female", sample.as_uri())
        action, params, context = calls[-1]
        view_model._action_succeeded(action, {
            "project_id": "film", "config": params["config"], "rows": [],
        }, context)
        view_model._action_succeeded("get_tts_voice_preview", {
            "available": True, "path": str(audio),
        }, calls[-1][2])
    url = QUrl.fromLocalFile(str(audio)).toString()
    assert publications == [url, "", url]
    assert not any(action == "create_tts_job" for action, _, _ in calls)


def test_manual_audition_during_import_waits_for_saved_voice(casting_import):
    view_model, calls, sample = casting_import
    view_model.setDialogueCastingReferenceAudio("female", sample.as_uri())
    view_model.previewDialogueCastingVoice("female")
    assert [action for action, _, _ in calls] == ["configure_dialogue_casting"]


@pytest.mark.parametrize("filename,payload", [("bad.txt", b"data"), ("empty.wav", b"")])
def test_rejected_role_file_stops_previous_audition(model, tmp_path, filename, payload):
    view_model, calls = model
    view_model._voice_preview_source = "file:///old-preview.wav"
    sample = tmp_path / filename
    sample.write_bytes(payload)
    view_model.setDialogueCastingReferenceAudio("female", sample.as_uri())
    assert calls == []
    assert view_model.voicePreviewSource == ""


def test_preparation_role_follows_active_preview_and_clears_on_completion(model):
    view_model, calls = model
    view_model._dialogue_casting["config"] = {
        role + "_voice": {"provider_id": "vieneu-3.8", "voice_id": "preset:" + role}
        for role in ("male", "female")
    }
    for role in ("male", "female"):
        view_model.previewDialogueCastingVoice(role)
        view_model._action_succeeded("get_tts_voice_preview", {"available": False}, calls[-1][2])
        assert view_model.dialogueCastingPreviewRole == role
        view_model._set_tts_busy(False)
        assert view_model.dialogueCastingPreviewRole == ""
    view_model._set_tts_busy(True)
    view_model._apply_project({"id": "other", "name": "Other", "revision": 1})
    assert view_model.dialogueCastingPreviewRole == ""


def test_explicit_profile_choice_is_not_overridden_by_global_sample(model, monkeypatch, tmp_path):
    view_model, _ = model
    profile = tmp_path / "profile.npz"
    profile.write_bytes(b"existing-profile")
    monkeypatch.setattr(view_model.voice_catalog, "find", lambda _voice_id: {
        "installed": True, "provider_id": "vieneu-3.8", "voice_id": "profile",
        "is_clone": True, "profile_path": str(profile),
    })
    view_model.settings.setValue("voice/vieneuReferenceAudio", "other-person.wav")
    assert view_model._dialogue_casting_voice("profile") == {
        "provider_id": "vieneu-3.8", "voice_id": "profile", "profile_path": str(profile),
    }


def test_export_folder_uses_completed_output_and_resets_with_project(model, tmp_path, monkeypatch):
    view_model, _ = model
    output = tmp_path / "actual" / "Thành phẩm #1.mp4"
    output.parent.mkdir()
    output.write_bytes(b"exported-video")
    job = {"id": "export", "state": "running",
           "spec": {"kind": "export", "project_id": "film"},
           "result": {"output_path": str(output)}}
    view_model._replace_jobs([job])
    assert not view_model.canOpenExportFolder
    view_model._replace_jobs([{**job, "state": "succeeded"}])
    assert view_model.canOpenExportFolder
    opened = []
    monkeypatch.setattr(application_module.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toLocalFile()) or True)
    view_model.settings.setValue("export/outputPath/film", "some-other-future-output.mp4")
    view_model.openExportFolder()
    assert [Path(path) for path in opened] == [output.parent.resolve()]
    view_model._apply_project({"id": "other", "name": "Other", "revision": 1})
    assert not view_model.canOpenExportFolder


def _scene_snapshot():
    cues = tuple(SubtitleCue(
        f"scene-{index}", index, TimeRange(index * 1_000_000, index * 1_000_000 + 900_000),
        "山河", "zh", role="scene_text", subtitle_track_id="S2", review_state="review",
    ) for index in (1, 2))
    return ProjectSnapshot("p", "Scene", 1, source_cues=cues,
                           translated_cues=tuple(replace(cue, id="vi-" + cue.id, language="vi")
                                                 for cue in cues),
                           tracks=(TimelineTrack("S2", TrackKind.SUBTITLE, "Scene"),))


def test_bulk_scene_acceptance_is_atomic_translated_and_undoable():
    original = _scene_snapshot()
    editor = SubtitleEditor(original)
    updated = editor.accept_scene_text_many(("scene-1", "scene-2", "scene-1"))
    assert updated.revision == original.revision + 1
    assert all(cue.review_state == "accepted"
               for cue in (*updated.source_cues, *updated.translated_cues))
    assert editor.undo().source_cues == original.source_cues
    assert editor.redo().source_cues == updated.source_cues


@pytest.mark.parametrize("invalid", ["missing", "locked", "dialogue"])
def test_bulk_scene_acceptance_rejects_invalid_batch_without_partial_updates(invalid):
    original = _scene_snapshot()
    cue_ids = ("scene-1", "scene-2")
    if invalid == "missing":
        cue_ids = ("scene-1", "missing")
    elif invalid == "locked":
        original = replace(original, tracks=(replace(original.tracks[0], locked=True),))
    else:
        original = replace(original, source_cues=(original.source_cues[0], replace(
            original.source_cues[1], role="dialogue")))
    editor = SubtitleEditor(original)
    with pytest.raises(SubtitleEditError):
        editor.accept_scene_text_many(cue_ids)
    assert editor.snapshot == original and not editor.can_undo
