from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, Qt, QUrl
from PySide6.QtGui import QColor, QImage

from ducai_studio.domain.voice_conform import (
    VoiceConformCueInput,
    VoiceConformSettings,
    VoiceTimingMode,
    build_voice_conform_plan,
)
from ducai_studio.ui.viewmodels import application_view_model as application_module
from ducai_studio.ui.viewmodels import voice_models as voice_models_module
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel, JobRuntime
from ducai_studio.ui.viewmodels.project_job_models import (
    JobListModel,
    MultiVideoListModel,
    ProjectStateModel,
    SubtitleCueListModel,
    TimelineClipListModel,
    TimelineTrackListModel,
    VoiceConformCueListModel,
)
from ducai_studio.ui.viewmodels.voice_models import VoiceCatalogModel


def test_application_routes_update_reservation_without_touching_project_jobs(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.updater_model._set_state("installing", "waiting for reservation")

    view_model._action_succeeded(
        "reserve_update_install",
        {"eligible": False, "active_job_count": 2, "multi_state": "idle"},
        {"updater": True},
    )

    assert view_model.updater.state == "waiting_jobs"
    assert "2 tác vụ" in view_model.updater.message


class MemorySettings:
    values: dict[str, object] = {}

    def __init__(self, _parent=None) -> None:
        pass

    def value(self, key: str, default=None, *, type=None):
        value = self.values.get(key, default)
        return type(value) if type is not None else value

    def setValue(self, key: str, value: object) -> None:
        self.values[key] = value


@pytest.mark.parametrize("override", ["exists", "missing", "unset"])
def test_ocr_runtime_override_matches_diagnostics_without_overwriting_saved_choice(
    tmp_path, monkeypatch, override,
):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    saved = str(tmp_path / "older-installation" / "python.exe")
    MemorySettings.values = {"engines/paddlePython": saved}
    candidate = tmp_path / "new-runtime" / "python.exe"
    if override == "exists":
        candidate.parent.mkdir()
        candidate.touch()
    if override == "unset":
        monkeypatch.delenv("DUCAI_PADDLE_PYTHON", raising=False)
    else:
        monkeypatch.setenv("DUCAI_PADDLE_PYTHON", str(candidate))
    model = ApplicationViewModel()
    assert model.ocrEnginePython == (str(candidate.resolve()) if override == "exists" else saved)
    assert MemorySettings.values["engines/paddlePython"] == saved


def test_timeline_clip_selection_delete_and_scissors_are_unambiguous(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(application_module, 'QSettings', MemorySettings)
    MemorySettings.values = {}
    model = ApplicationViewModel()
    interval = {'start_us': 0, 'end_us': 10_000_000}
    model.project_state.replace({'id': 'p', 'name': 'Selection', 'revision': 1,
        'source_cues': [{'id': 'c', 'index': 1, 'text': '字幕', 'range': interval}],
        'tracks': [{'id': 'V1', 'kind': 'video', 'name': 'Video', 'clips': [
            {'id': 'v', 'track_id': 'V1', 'timeline_range': interval}]},
            {'id': 'S1', 'kind': 'subtitle', 'name': 'Sub', 'clips': [
                {'id': 's', 'track_id': 'S1', 'cue_id': 'c', 'timeline_range': interval}]}]})
    requests = []
    model.runtime.request = lambda a, p=None, context=None: requests.append((a, p, context))
    model.setPlayheadMs(3000)
    model.selectTimelineClip('s', False)
    assert model.selectedCueId == 'c' and model.playheadUs == 3_000_000
    model.deleteTimelineSelection()
    assert requests[-1][1]['command'] == 'delete_cues'
    assert requests[-1][1]['values']['cue_ids'] == ['c']
    model._set_subtitle_busy(False)
    model.selectTimelineClip('v', False)
    assert not model.selectedCueId
    model.selectCue('c', 0)
    assert not model.selectedClipId  # An editor selection must not delete stale V1.
    model.setPlayheadMs(3000)
    model.splitVideoAtPlayhead()
    assert requests[-1][1]['command'] == 'split_video'
    assert 'clip_id' not in requests[-1][1]
    assert requests[-1][1]['timeline_us'] == 3_000_000
    model._finish_timeline_cut(True, requests[-1][2])
    assert model.playheadUs == 3_000_000 and not model.timelineBusy


def test_title_clip_selection_and_deletion_keep_title_identity(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    MemorySettings.values = {}
    model = ApplicationViewModel()
    interval = {"start_us": 0, "end_us": 10_000_000}
    model.project_state.replace({"id": "p", "name": "Text", "revision": 1,
        "title_cues": [{"id": "title", "index": 1, "role": "title_text",
                        "text": "Channel", "range": interval}],
        "tracks": [{"id": "T1", "kind": "text", "name": "Text", "clips": [
            {"id": "title-clip", "track_id": "T1", "cue_id": "title",
             "timeline_range": interval}]}]})
    requests = []
    model.runtime.request = lambda a, p=None, context=None: requests.append((a, p, context))
    model.setPlayheadMs(3000)
    model.selectTimelineCue("title")
    model.selectTimelineClip("title-clip", False)
    assert model.selectedCueId == "title" and model.selectedClipId == "title-clip"
    assert model.playheadUs == 3_000_000
    model.selectClip("title-clip")
    assert model.selectedCueId == "title"
    model.deleteSelected(True)
    assert requests[-1][1]["command"] == "delete_cues"
    assert requests[-1][1]["values"]["cue_ids"] == ["title"]
    assert requests[-1][1]["values"]["title_mode"] is True


def test_partial_translation_uses_shared_identity_not_stale_cut_index():
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    cues = [{'id': f'c{i}', 'index': i, 'text': f'字幕{i}',
             'range': {'start_us': i*100, 'end_us': i*100+90}} for i in range(1, 4)]
    model.replace({'id': 'p', 'name': 'Sparse', 'revision': 1, 'source_cues': cues,
                   'translated_cues': [{**cues[-1], 'index': 1, 'text': 'Câu thứ ba'}],
                   'tracks': [{'id': 'S1', 'kind': 'subtitle', 'name': 'Sub', 'clips': [
                       {'id': c['id'], 'cue_id': c['id'], 'timeline_range': c['range']}
                       for c in cues]}]})
    assert not model.subtitle_cues.find('c1')['translated_text']
    assert model.subtitle_cues.find('c3')['translated_text'] == 'Câu thứ ba'
    assert model.clip_info('c1')['cue_text'] == '字幕1'
    assert model.clip_info('c3')['cue_text'] == 'Câu thứ ba'


def test_subtitle_role_projection_is_cached_and_invalidated_after_edit() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = [{"id": "c1", "index": 1, "text": "原文",
               "range": {"start_us": 0, "end_us": 1_000_000}}]
    model.replace(source, [{"id": "t1", "index": 1, "text": "Bản dịch"}])

    index = model.index(0, 0)
    assert model.data(index, model.SourceTextRole) == "原文"
    assert model.data(index, model.TranslatedTextRole) == "Bản dịch"
    assert len(model._role_data_cache) == 1

    model.optimistic_update_text("c1", "原文 mới", language="source")
    assert model.data(index, model.SourceTextRole) == "原文 mới"
    assert model.data(index, model.TranslatedTextRole) == "Bản dịch"


@pytest.mark.parametrize("succeeded", [True, False])
def test_background_subtitle_response_does_not_release_active_timing_edit(
    monkeypatch, succeeded: bool,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({"id": "save-race", "name": "Race", "revision": 1})
    model._set_subtitle_busy(True)
    model._set_timeline_busy(True)
    context = {"project_id": "save-race", "background_subtitle_save": True,
               "subtitle_command": "update_text"}
    if succeeded:
        model._action_succeeded("subtitle_command", {
            "id": "save-race", "revision": 2, "_subtitle_patch": True,
        }, context)
    else:
        model._action_failed("subtitle_command", {"message": "disk full"}, context)
    assert model.subtitleBusy is True
    assert model.timelineBusy is True


def test_timing_settlement_ignores_responses_from_an_older_gesture(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    cue = {"id": "cue", "index": 1, "role": "scene_text",
           "range": {"start_us": 1_000_000, "end_us": 2_000_000}, "text": "人物"}
    model.project_state.replace({"id": "timing", "name": "Timing", "revision": 1,
                                 "source_cues": [cue]})
    model.selectSubtitleCueIds(["cue"])
    requests = []
    monkeypatch.setattr(model.runtime, "request",
                        lambda action, payload=None, context=None:
                        requests.append((action, payload, context)))
    settled = []
    model.sceneTimingSettled.connect(lambda generation, ok: settled.append((generation, ok)))
    first = model.moveSelectedSceneTexts(100_000, 0)
    first_context = requests[-1][2]
    model._action_failed("subtitle_command", {"message": "disk full"}, first_context)
    second = model.moveSelectedSceneTexts(200_000, 0)
    second_context = requests[-1][2]
    assert second > first > 0
    for succeeded in (False, True):
        if succeeded:
            model._action_succeeded("subtitle_command", {
                "id": "timing", "revision": 99, "_subtitle_patch": True,
            }, first_context)
        else:
            model._action_failed("subtitle_command", {"message": "old failure"}, first_context)
        assert model.subtitleBusy and settled == [(first, False)]
        assert model.project_state.revision == 1
        assert model.project_state.subtitle_cues.find("cue")["start_us"] == 1_200_000
    model._action_succeeded("subtitle_command", {
        "id": "timing", "revision": 2, "_subtitle_patch": True,
        "source_cue_upserts": [{**cue, "range": {
            "start_us": 1_200_000, "end_us": 2_200_000}}],
    }, second_context)
    assert settled == [(first, False), (second, True)] and not model.subtitleBusy


def test_stale_background_ack_is_saved_without_rolling_back_new_head(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({"id": "late", "name": "Late", "revision": 3})
    model._subtitle_save_in_flight = True
    model._action_succeeded("subtitle_command", {
        "id": "late", "revision": 2, "_subtitle_patch": True,
    }, {"project_id": "late", "background_subtitle_save": True})
    assert model.project_state.revision == 3 and model.saveState == "Đã lưu"
    assert not model._subtitle_save_in_flight


def test_timing_submit_failure_returns_rejection_and_restores_confirmed_timing(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({"id": "reject", "name": "Reject", "revision": 1,
                                 "source_cues": [{"id": "cue", "index": 1, "text": "名字",
                                     "range": {"start_us": 1_000_000, "end_us": 2_000_000}}]})
    model.selectSubtitleCueIds(["cue"])

    def failed_request(*args, **kwargs):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(model.runtime, "request", failed_request)
    assert model.moveSelectedSceneTexts(200_000, 0) == 0
    assert not model.subtitleBusy and not model._scene_timing_context
    assert model.project_state.subtitle_cues.find("cue")["start_us"] == 1_000_000


@pytest.mark.parametrize("change_text", [False, True])
def test_geometry_measurement_uses_inputs_not_unrelated_revision(monkeypatch, change_text):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    MemorySettings.values = {}
    model = ApplicationViewModel()
    cue = {"id": "cue", "index": 1, "text": "Cha",
           "range": {"start_us": 0, "end_us": 1_000_000}}
    project = {"id": "geometry-race", "name": "Geometry", "revision": 1,
               "source_cues": [cue]}
    model.project_state.replace(project)
    context = {"generation": 1, "project_id": "geometry-race", "revision": 1,
               "cue_ids": ["cue"], "delta_x": 0, "delta_y": -.1,
               "scale_x": 1, "scale_y": 1,
               "measured_cues": [model.project_state.subtitle_cues.find("cue")],
               "measured_style": model._subtitle_artwork_style()}
    model._subtitle_geometry_context = context
    model._set_subtitle_busy(True)
    requests = []
    monkeypatch.setattr(model.runtime, "request",
                        lambda a, p=None, context=None: requests.append((a, p, context)))
    # A previously queued save can acknowledge during measurement. Only actual
    # layout-input changes invalidate the worker's measured placement.
    model.project_state.replace({**project, "revision": 2, "source_cues": [
        {**cue, "revision": 2, "text": "Câu đã sửa" if change_text else cue["text"]}]})
    model._subtitle_geometry_measured(True, {"cue": [.1, .8, .5, .1]}, context)
    assert len(requests) == (0 if change_text else 1)
    assert model.subtitleBusy is not change_text
    if not change_text:
        assert "measured_cues" not in requests[0][1]["values"]
        assert "measured_style" not in requests[0][1]["values"]


def test_geometry_pending_survives_unrelated_ack_and_releases_on_disconnect(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    MemorySettings.values = {}
    model = ApplicationViewModel()
    settled = []
    model.subtitleGeometrySettled.connect(lambda generation, ok: settled.append((generation, ok)))
    model.project_state.replace({"id": "geometry", "name": "Geometry", "revision": 1})
    model._subtitle_geometry_context = {"generation": 1, "project_id": "geometry"}
    model._set_subtitle_busy(True)
    monkeypatch.setattr(model.runtime, "request", lambda *a, **k: None)
    model._action_succeeded("get_project", {
        "id": "geometry", "name": "Geometry", "revision": 1,
    }, {"project_id": "geometry"})
    assert model.subtitleBusy and model.saveState != "Đã lưu"
    model._finish_background_subtitle_save()
    assert model.subtitleBusy and model.saveState != "Đã lưu"
    assert settled == []
    model._backend_ready = True
    monkeypatch.setattr(model, "_begin_timeline_reconcile", lambda project_id: None)
    model._set_backend_ready(False)
    assert not model.subtitleBusy and not model._subtitle_geometry_context
    assert settled == [(1, False)]
    # Completion from the disconnected measurement cannot send a new edit.
    model._subtitle_geometry_measured(True, {}, {"generation": 1, "project_id": "geometry"})
    assert not model.subtitleBusy
    assert settled == [(1, False)]


def test_geometry_submit_exception_releases_pending_preview(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    MemorySettings.values = {}
    model = ApplicationViewModel()
    model.project_state.replace({"id": "geometry", "name": "Geometry", "revision": 1,
        "source_cues": [{"id": "cue", "index": 1, "text": "Cha",
                         "panel_box": [.1, .8, .5, .1],
                         "range": {"start_us": 0, "end_us": 1_000_000}}]})
    model.selectSubtitleCueIds(["cue"])

    def failed_request(*args, **kwargs):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(model.runtime, "request", failed_request)
    assert not model.transformSelectedSubtitleGeometry(0, -.1, 1, 1)
    assert not model.subtitleBusy and not model._subtitle_geometry_context
    assert model.project_state.subtitle_cues.find("cue")["panel_box"] == [.1, .8, .5, .1]


def test_background_flush_waits_for_timing_ack_before_deferred_action(monkeypatch) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({"id": "flush", "name": "Flush", "revision": 1})
    model._scene_timing_context = {"scene_timing_generation": 1}
    called = []
    assert model._defer_until_subtitle_saves_flushed(
        "export", lambda: called.append(True), "Waiting")
    model._finish_background_subtitle_save()
    app.processEvents()
    assert not called and model.saveState != "Đã lưu"
    model._finish_scene_timing_command(True)
    app.processEvents()
    assert called == [True]


def test_background_subtitle_save_coalesces_rapid_edits_and_flushes_once(
    monkeypatch,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-save-queue",
            "name": "Save queue",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                }
            ],
            "translated_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "Câu cũ",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    for index in range(20):
        view_model.updateSubtitleText("cue-1", f"Câu mới {index}", "translated")

    assert requests == []
    assert len(view_model._subtitle_save_queue) == 1
    assert view_model.project_state.subtitle_cues.get(0)["translated_text"] == "Câu mới 19"

    flushed: list[str] = []
    assert view_model._defer_until_subtitle_saves_flushed(
        "test", lambda: flushed.append("done"), "Đang flush"
    )
    assert len(requests) == 1
    assert requests[0][1]["values"]["text"] == "Câu mới 19"
    view_model._finish_background_subtitle_save()
    application.processEvents()

    assert flushed == ["done"]
    assert view_model.saveState == "Đã lưu"


@pytest.mark.parametrize("failure", ["disconnect", "send", "watchdog"])
def test_pending_delete_is_cancelled_on_uncertain_background_save(monkeypatch, failure):
    app = QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model._set_backend_ready(True)
    model.project_state.replace({"id": "save-loss", "name": "Save loss", "revision": 1,
        "source_cues": [{"id": "c", "index": 1, "text": "旧文字",
                         "range": {"start_us": 0, "end_us": 1_000_000}}]})
    requests = []

    def request(action, params=None, context=None):
        if failure == "send" and action == "subtitle_command":
            raise OSError("closed socket")
        requests.append((action, params, context))

    monkeypatch.setattr(model.runtime, "request", request)
    deleted = []
    model.updateSubtitleText("c", "新文字", "source")
    model._defer_until_subtitle_saves_flushed("delete", lambda: deleted.append(True), "Chờ lưu")
    old_context = dict(model._subtitle_save_context)
    if failure == "disconnect":
        model._set_backend_ready(False)
    elif failure == "watchdog":
        model._background_subtitle_save_timed_out()
    app.processEvents()
    assert not deleted
    assert not model._subtitle_save_in_flight and not model._subtitle_save_queue
    assert not model._subtitle_flush_callbacks
    assert model.project_state.subtitle_cues.find("c")["source_text"] == "旧文字"
    assert model._timeline_reconcile_pending
    if old_context:
        # Late ACK from the uncertain write may not overwrite the new durable
        # head or unlock a newer save operation.
        model._subtitle_save_context = {**old_context,
            "background_save_generation": old_context["background_save_generation"] + 1}
        model._subtitle_save_in_flight = True
        model._action_succeeded("subtitle_command", {"_subtitle_patch": True,
            "id": "save-loss", "revision": 2}, old_context)
        assert model._subtitle_save_in_flight
        assert model.project_state.revision == 1


def test_video_copy_and_paste_capture_selection_and_playhead_before_flush(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({"id": "copy", "revision": 1, "name": "Copy", "tracks": [
        {"id": "V1", "kind": "video", "name": "V1", "clips": [
            {"id": value, "track_id": "V1", "timeline_range": {"start_us": start,
             "end_us": start + 10_000_000}, "source_range": {"start_us": 0,
             "end_us": 10_000_000}} for value, start in (("a", 0), ("b", 10_000_000))]}]})
    deferred = []
    requests = []
    monkeypatch.setattr(model, "_defer_until_subtitle_saves_flushed",
                        lambda key, callback, status: deferred.append(callback) or True)
    monkeypatch.setattr(model.runtime, "request",
                        lambda a, p=None, context=None: requests.append((a, p, context)))
    model.selectClip("a")
    model.copySelectedVideo()
    model.selectClip("b")
    monkeypatch.setattr(model, "_defer_until_subtitle_saves_flushed", lambda *a: False)
    deferred.pop()()
    assert requests[-1][1]["clip_id"] == "a"
    model._video_clipboard_project_id = "copy"
    model.setPlayheadMs(1234)
    monkeypatch.setattr(model, "_defer_until_subtitle_saves_flushed",
                        lambda key, callback, status: deferred.append(callback) or True)
    model.pasteVideoAtPlayhead()
    model.setPlayheadMs(8000)
    monkeypatch.setattr(model, "_defer_until_subtitle_saves_flushed", lambda *a: False)
    deferred.pop()()
    assert requests[-1][1]["timeline_us"] == 1_234_000


def test_flush_dispatches_distinct_queued_subtitle_edits_without_debounce_delay(
    monkeypatch,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-fast-flush",
            "name": "Fast flush",
            "revision": 1,
            "source_cues": [
                {
                    "id": f"cue-{index}",
                    "index": index,
                    "range": {
                        "start_us": (index - 1) * 1_000_000,
                        "end_us": index * 1_000_000,
                    },
                    "text": f"原文 {index}",
                }
                for index in (1, 2)
            ],
            "translated_cues": [
                {
                    "id": f"cue-{index}",
                    "index": index,
                    "range": {
                        "start_us": (index - 1) * 1_000_000,
                        "end_us": index * 1_000_000,
                    },
                    "text": f"Câu cũ {index}",
                }
                for index in (1, 2)
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.updateSubtitleText("cue-1", "Câu mới 1", "translated")
    view_model.updateSubtitleText("cue-2", "Câu mới 2", "translated")
    flushed: list[bool] = []
    assert view_model._defer_until_subtitle_saves_flushed(
        "export", lambda: flushed.append(True), "Đang lưu trước khi xuất"
    )
    assert len(requests) == 1

    view_model._finish_background_subtitle_save()
    application.processEvents()
    assert len(requests) == 2
    assert view_model._subtitle_save_timer.isActive() is False

    view_model._finish_background_subtitle_save()
    application.processEvents()
    assert flushed == [True]


def test_failed_background_subtitle_save_drops_deferred_action_and_rolls_back(
    monkeypatch,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-save-failure",
            "name": "Save failure",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                }
            ],
            "translated_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "Câu cũ",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    callback_runs: list[bool] = []

    view_model.updateSubtitleText("cue-1", "Câu chưa lưu", "translated")
    assert view_model._defer_until_subtitle_saves_flushed(
        "export", lambda: callback_runs.append(True), "Đang flush"
    )
    view_model._action_failed(
        "subtitle_command",
        {"message": "disk full"},
        requests[0][2],
    )
    application.processEvents()

    assert callback_runs == []
    assert view_model.project_state.subtitle_cues.get(0)["translated_text"] == "Câu cũ"


def test_translation_preferences_save_is_serialized_and_releases_on_failure(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    project = {
        "id": "project-style-save",
        "name": "Style save",
        "revision": 1,
        "media": [],
        "tracks": [],
        "source_cues": [],
        "translated_cues": [],
        "overlays": [],
        "translation_preferences": {"preset": "auto", "custom_rules": ""},
    }
    view_model._apply_project(project)
    view_model._backend_ready = True
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.setTranslationPreferences("xianxia", "Dùng ta/ngươi.")
    view_model.setTranslationPreferences("modern", "Dùng anh/em.")

    assert view_model.translationPreferencesBusy is True
    assert [item[0] for item in requests] == ["set_translation_preferences"]

    saved = {
        **project,
        "revision": 2,
        "translation_preferences": {
            "preset": "xianxia",
            "custom_rules": "Dùng ta/ngươi.",
        },
    }
    view_model._action_succeeded(
        "set_translation_preferences",
        saved,
        requests[0][2],
    )

    assert view_model.translationPreferencesBusy is False
    assert view_model.project_state.translationStylePreset == "xianxia"

    view_model.setTranslationPreferences("modern", "Dùng anh/em.")
    assert view_model.translationPreferencesBusy is True
    assert len([item for item in requests if item[0] == "set_translation_preferences"]) == 2

    view_model._action_failed(
        "set_translation_preferences",
        {"message": "write failed"},
        requests[-1][2],
    )

    assert view_model.translationPreferencesBusy is False
    assert view_model.project_state.translationStylePreset == "xianxia"


def test_named_translation_style_save_updates_project_and_is_exposed_to_qml(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    project = {
        "id": "project-named-style",
        "name": "Named style",
        "revision": 1,
        "media": [],
        "tracks": [],
        "source_cues": [],
        "translated_cues": [],
        "overlays": [],
        "translation_preferences": {"preset": "auto", "custom_rules": ""},
    }
    view_model._apply_project(project)
    view_model._backend_ready = True
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.saveTranslationStyle("  Bản riêng  ", "modern", "Dùng anh/em.")

    assert requests[0][0] == "save_translation_style"
    assert requests[0][1]["name"] == "Bản riêng"
    saved = {
        **project,
        "revision": 2,
        "translation_preferences": {
            "preset": "modern",
            "custom_rules": "Dùng anh/em.",
            "active_style_name": "Bản riêng",
            "saved_styles": [
                {"name": "Bản riêng", "preset": "modern", "custom_rules": "Dùng anh/em."}
            ],
        },
    }
    view_model._action_succeeded("save_translation_style", saved, requests[0][2])

    assert view_model.translationPreferencesBusy is False
    assert view_model.project_state.translationStylePreset == "modern"
    assert view_model.project_state.translationActiveStyleName == "Bản riêng"
    assert view_model.project_state.translationStylePresets == [
        {"name": "Bản riêng", "preset": "modern", "custom_rules": "Dùng anh/em."}
    ]

    view_model.setTranslationPreferences("modern", "Dùng anh/em.", "Bản riêng")
    assert requests[-1][1]["active_style_name"] == "Bản riêng"
    view_model._action_failed(
        "set_translation_preferences", {"message": "write failed"}, requests[-1][2]
    )
    assert not view_model.translationPreferencesBusy
    assert view_model.project_state.translationActiveStyleName == "Bản riêng"

    view_model.saveTranslationStyle("Bản riêng 2", "neutral", "Quy tắc mới")
    view_model._action_failed(
        "save_translation_style", {"message": "disk full"}, requests[-1][2]
    )
    assert not view_model.translationPreferencesBusy
    assert len(view_model.project_state.translationStylePresets) == 1


@pytest.mark.parametrize("response", [{}, [], "invalid", None])
def test_malformed_translation_preferences_response_keeps_current_project(
    monkeypatch,
    response,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    project = {
        "id": "project-style-response",
        "name": "Style response",
        "revision": 1,
        "media": [],
        "tracks": [],
        "source_cues": [],
        "translated_cues": [],
        "overlays": [],
        "translation_preferences": {"preset": "auto", "custom_rules": ""},
    }
    view_model._apply_project(project)
    view_model._backend_ready = True
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.setTranslationPreferences("xianxia", "Dùng ta/ngươi.")
    request = next(item for item in requests if item[0] == "set_translation_preferences")
    view_model._action_succeeded("set_translation_preferences", response, request[2])

    assert view_model.translationPreferencesBusy is False
    assert view_model.project_state.projectId == "project-style-response"
    assert view_model.project_state.translationStylePreset == "auto"


@pytest.mark.parametrize("action", ["set_translation_preferences", "save_translation_style"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_style_save_ack_preserves_newer_and_optimistic_cue_edits(
    monkeypatch, action: str, confirmed: bool,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    interval = {"start_us": 0, "end_us": 1_000_000}
    project = {"id": "style-race", "name": "Styles", "revision": 1,
               "source_cues": [{"id": "c1", "index": 1, "range": interval,
                                "text": "原文"}],
               "translated_cues": [{"id": "c1", "index": 1, "range": interval,
                                    "text": "Câu cũ"}],
               "translation_preferences": {"preset": "auto", "custom_rules": ""}}
    model._apply_project(project)
    model._backend_ready = True
    requests = []
    model.runtime.request = lambda a, p=None, context=None: requests.append((a, p, context))
    if action == "save_translation_style":
        model.saveTranslationStyle("Bản riêng", "modern", "Dùng anh/em.")
    else:
        model.setTranslationPreferences("modern", "Dùng anh/em.")
    context = requests[0][2]
    preferences = {"preset": "modern", "custom_rules": "Dùng anh/em."}
    if action == "save_translation_style":
        preferences.update(active_style_name="Bản riêng", saved_styles=[
            {"name": "Bản riêng", "preset": "modern", "custom_rules": "Dùng anh/em."}])
    model.selectCue("c1", 0)
    if confirmed:
        model.project_state.apply_subtitle_patch({
            "id": "style-race", "revision": 3, "_subtitle_patch": True,
            "translated_cue_upserts": [{"id": "c1", "index": 1, "range": interval,
                                        "text": "Câu mới"}],
        })
        model._observe_project_persistence({"id": "style-race", "_project_dirty": True,
                                           "_saved_revision": 2})
    else:
        model.updateSubtitleText("c1", "Câu mới", "translated")
    # A separate operation can still be saving when the disjoint style ACK arrives.
    model._set_subtitle_busy(True)
    model._set_timeline_busy(True)
    cue_resets = []
    model.project_state.subtitle_cues.modelReset.connect(lambda: cue_resets.append(True))
    model._action_succeeded(action, {**project, "revision": 2,
                                    "translation_preferences": preferences}, context)

    assert model.project_state.subtitle_cues.find("c1")["translated_text"] == "Câu mới"
    assert model.project_state.revision == (3 if confirmed else 2)
    assert model.project_state.translationStylePreset == "modern"
    assert model.selectedCueId == "c1"
    assert not cue_resets
    assert model.subtitleBusy and model.timelineBusy
    assert not model.translationPreferencesBusy
    if confirmed:
        assert model.projectDirty and model._last_saved_revision == 2
        assert model.project_state._canonical_snapshot["translated_cues"][0]["text"] == (
            "Câu mới")
    else:
        assert model._subtitle_save_queue or model._subtitle_save_in_flight
        assert model.saveState != "Đã lưu"


@pytest.mark.parametrize("kind", ["subtitle", "timeline"])
@pytest.mark.parametrize("style_revision", [2, 4])
def test_prepared_cue_patch_keeps_styles_saved_after_projection_started(
    kind: str, style_revision: int,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    state = ProjectStateModel()
    project = {"id": "style-projection", "name": "Styles", "revision": 1,
               "tracks": [], "source_cues": [], "translated_cues": [],
               "translation_preferences": {"preset": "auto", "custom_rules": ""}}
    state.replace(project)
    patch = {"id": "style-projection", "revision": 3, f"_{kind}_patch": True,
             "source_cue_upserts": [{"id": "new-cue", "index": 1, "text": "原文",
                                    "range": {"start_us": 0, "end_us": 1_000_000}}]}
    prepare = (state.prepare_subtitle_patch if kind == "subtitle"
               else state.prepare_timeline_patch)
    canonical, projected = prepare(state._canonical_snapshot, patch)
    preferences = {"preset": "modern", "custom_rules": "Dùng anh/em.",
                   "active_style_name": "Bản riêng", "saved_styles": [
                       {"name": "Bản riêng", "preset": "modern",
                        "custom_rules": "Dùng anh/em."}]}
    state.apply_translation_preferences({"id": "style-projection", "revision": style_revision,
                                         "translation_preferences": preferences})
    apply = (state.apply_prepared_subtitle_patch if kind == "subtitle"
             else state.apply_prepared_timeline_patch)
    apply(canonical, projected)

    assert state.revision == max(3, style_revision)
    assert state.translationActiveStyleName == "Bản riêng"
    assert state._canonical_snapshot["translation_preferences"] == preferences
    assert state._canonical_snapshot["source_cues"][0]["id"] == "new-cue"
    assert state.subtitle_cues.find("new-cue")


@pytest.mark.parametrize("kind", ["subtitle", "timeline", "effect"])
def test_content_patch_after_style_ack_keeps_head_and_rejects_older_content(kind: str) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    state = ProjectStateModel()
    state.replace({"id": "styles", "name": "Styles", "revision": 1,
                   "tracks": [], "source_cues": [], "translated_cues": [], "overlays": []})
    state.apply_translation_preferences({"id": "styles", "revision": 3,
                                         "translation_preferences": {
                                             "preset": "modern", "custom_rules": "Mới"}})
    patch = {"id": "styles", "revision": 2, f"_{kind}_patch": True}
    apply = getattr(state, f"apply_{kind}_patch")
    apply(patch)
    assert state.revision == 3 and state._canonical_snapshot["revision"] == 3
    assert state.translationCustomRules == "Mới"
    with pytest.raises(ValueError, match="revision"):
        apply({**patch, "revision": 1})


def test_stale_style_ack_does_not_replace_newer_preferences() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    state = ProjectStateModel()
    state.replace({"id": "styles", "name": "Styles", "revision": 4,
                   "translation_preferences": {"preset": "modern", "custom_rules": "Mới"}})
    state.apply_translation_preferences({"id": "styles", "revision": 2,
                                         "translation_preferences": {
                                             "preset": "neutral", "custom_rules": "Cũ"}})
    assert state.revision == 4
    assert state.translationCustomRules == "Mới"


def test_style_ack_before_pending_cue_patch_retains_durable_saved_revision(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    cue = {"id": "c", "index": 1, "text": "Cũ",
           "range": {"start_us": 0, "end_us": 1_000_000}}
    model._apply_project({"id": "styles", "name": "Styles", "revision": 1,
                          "source_cues": [cue], "tracks": []})
    model._backend_ready = True
    requests = []
    model.runtime.request = lambda a, p=None, context=None: requests.append((a, p, context))
    model.updateSubtitleText("c", "Mới", "source")
    model._subtitle_save_timer.stop()
    model._dispatch_background_subtitle_save()
    model.saveTranslationStyle("Bản riêng", "modern", "Quy tắc")
    assert [request[0] for request in requests] == ["subtitle_command", "save_translation_style"]
    model._action_succeeded("save_translation_style", {
        "id": "styles", "revision": 3, "_project_dirty": False, "_saved_revision": 3,
        "translation_preferences": {"preset": "modern", "custom_rules": "Quy tắc",
                                    "active_style_name": "Bản riêng", "saved_styles": [
                                        {"name": "Bản riêng", "preset": "modern",
                                         "custom_rules": "Quy tắc"}]},
    }, requests[1][2])
    assert model._last_saved_revision == 3
    assert model.saveState != "Đã lưu"
    model._action_succeeded("subtitle_command", {
        "id": "styles", "revision": 2, "_subtitle_patch": True,
        "source_cue_upserts": [{**cue, "text": "Mới"}],
        "_project_dirty": True, "_saved_revision": 1,
    }, requests[0][2])
    assert model.project_state.revision == 3
    assert model.project_state._canonical_snapshot["source_cues"][0]["text"] == "Mới"
    assert model.project_state.translationActiveStyleName == "Bản riêng"
    assert not model.projectDirty and model._last_saved_revision == 3
    assert model.saveState == "Đã lưu"


def test_voice_catalog_refresh_does_not_run_blocking_vieneu_health_probe(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])

    def fail_health(_self):
        raise AssertionError("voice catalog must not run the CUDA health probe")

    monkeypatch.setattr(voice_models_module.VieNeuTtsProvider, "health", fail_health)

    model = VoiceCatalogModel()
    model.refresh()

    assert model.count >= 1


def test_job_runtime_never_queues_a_second_job_poll_while_one_is_pending(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._ready = True
    request_ids = iter(("poll-1", "poll-2"))
    requests: list[tuple[str, object]] = []

    def request(action: str, params=None) -> str:
        requests.append((action, params))
        return next(request_ids)

    monkeypatch.setattr(runtime.client, "request", request)

    runtime._poll_jobs()
    runtime._poll_jobs()

    assert requests == [("list_jobs", None)]
    runtime._pending_requests.clear()

    runtime._poll_jobs()

    assert requests == [("list_jobs", None), ("list_jobs", None)]


def test_job_runtime_deduplicates_direct_list_jobs_refreshes(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._ready = True
    requests: list[tuple[str, object]] = []

    def request(action: str, params=None) -> str:
        requests.append((action, params))
        return f"request-{len(requests)}"

    monkeypatch.setattr(runtime.client, "request", request)

    first = runtime.request("list_jobs")
    duplicate = runtime.request("list_jobs")

    assert first == duplicate == "request-1"
    assert requests == [("list_jobs", None)]


def test_job_runtime_releases_ready_state_when_socket_disconnects() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._ready = True
    states: list[bool] = []
    runtime.readyChanged.connect(lambda value: states.append(bool(value)))

    runtime._connected(False)

    assert runtime._ready is False
    assert states == [False]


def test_job_runtime_suppresses_transient_socket_errors_during_slow_startup(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._state = "starting"
    runtime._attempts = 200
    runtime._startup_deadline = 100.0
    statuses: list[str] = []
    runtime.statusChanged.connect(statuses.append)
    monkeypatch.setattr(application_module, "monotonic", lambda: 50.0)

    runtime._client_error("QLocalSocket::connectToServer: Invalid name")

    assert runtime._state == "starting"
    assert statuses == []


def test_job_runtime_reports_a_friendly_error_after_startup_deadline(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._state = "starting"
    runtime._startup_deadline = 100.0
    statuses: list[str] = []
    runtime.statusChanged.connect(statuses.append)
    monkeypatch.setattr(application_module, "monotonic", lambda: 101.0)

    runtime._client_error("QLocalSocket::connectToServer: Invalid name")

    assert runtime._state == "error"
    assert statuses == [
        "Không thể khởi động Job Manager. Dữ liệu dự án vẫn được giữ nguyên; "
        "hãy đóng ứng dụng rồi mở lại."
    ]


def test_legacy_settings_map_to_current_qsettings_without_losing_safe_values() -> None:
    values = application_module._legacy_qsettings_values(
        {
            "font_file": "Noto Sans",
            "export_preset": "1080p",
            "export_output": "D:/final.mp4",
            "sub_font_size": 44,
            "sub_margin_bottom": 36,
            "tts_provider": "VieNeu Voice Clone Local",
        },
        "project-1",
    )

    assert values["export/fontFamily"] == "Noto Sans"
    assert values["export/presetId"] == "1080p"
    assert values["export/outputPath/project-1"] == "D:/final.mp4"
    assert values["export/fontSize"] == 44
    assert values["export/subtitleMarginBottom"] == 36
    assert values["migration/legacy/tts_provider"] == "VieNeu Voice Clone Local"


def test_default_ocr_runtime_prefers_managed_paddle_location(monkeypatch, tmp_path: Path) -> None:
    runtime = tmp_path / "paddle_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    monkeypatch.delenv("DUCAI_PADDLE_PYTHON", raising=False)
    monkeypatch.setattr(application_module, "discover_data_root", lambda: tmp_path)

    assert application_module._default_ocr_engine_python() == str(runtime.resolve())


def test_ocr_sampling_rate_is_persisted_for_single_and_multi_runs(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)

    view_model = ApplicationViewModel()
    assert view_model.ocrSampleFps == 1.25

    view_model.setOcrSampleFps(2.0)

    assert view_model.ocrSampleFps == 2.0
    assert MemorySettings.values["ocr/sampleFps"] == 2.0

    restored = ApplicationViewModel()
    assert restored.ocrSampleFps == 2.0


def test_whisper_extraction_method_persists_and_dispatches_external_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    runtime = tmp_path / "whisper_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    model = tmp_path / "large-v3"
    model.mkdir()
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(application_module, "whisper_runtime_python", lambda: runtime)
    monkeypatch.setattr(application_module, "whisper_model_path", lambda: model)
    monkeypatch.setattr(application_module, "whisper_runtime_is_ready", lambda *_args: True)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-whisper", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.setOcrExtractionMethod("whisper_audio")
    view_model.startOcr(9.0)

    assert MemorySettings.values["ocr/extractionMethod"] == "whisper_audio"
    assert view_model.ocrExtractionMethod == "whisper_audio"
    action, payload, _context = requests[-1]
    assert action == "create_ocr_job"
    assert payload["extraction_method"] == "whisper_audio"
    assert payload["engine_python"] == str(runtime)
    assert payload["model_path"] == str(model)
    assert payload["device"] == "auto"
    restored = ApplicationViewModel()
    assert restored.ocrExtractionMethod == "whisper_audio"


def test_switching_extraction_method_does_not_resume_other_method_cache(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    view_model._ocr_job_id = "old-whisper"
    view_model._ocr_job_method = "whisper_audio"
    view_model._set_ocr_job_state("failed")
    started: list[float] = []
    resumed: list[bool] = []
    monkeypatch.setattr(view_model, "startOcr", lambda fps: started.append(fps))
    monkeypatch.setattr(view_model, "resumeOcr", lambda: resumed.append(True))

    view_model.toggleOcr(1.25)

    assert started == [1.25]
    assert resumed == []


def _job(job_id: str, kind: str) -> dict:
    return {
        "id": job_id,
        "spec": {"kind": kind, "project_id": "project-1"},
        "state": "running",
        "created_at": "2026-08-24T12:00:00Z",
    }


def test_job_models_filter_real_job_kinds_without_duplicate_resets() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = JobListModel(accepted_kinds=frozenset({"ocr", "translate"}))
    count_changes: list[None] = []
    model.countChanged.connect(lambda: count_changes.append(None))

    jobs = [_job("ocr-1", "ocr"), _job("tts-1", "tts"), _job("tr-1", "translate")]
    model.replace(jobs)
    model.replace(jobs)

    assert model.count == 2
    assert len(count_changes) == 1


def test_import_paths_routes_one_video_to_single_import_and_many_to_multi(
    monkeypatch, tmp_path: Path
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._backend_ready = True
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    single_imports: list[str] = []
    multi_starts: list[tuple[Path, ...]] = []
    monkeypatch.setattr(view_model, "importPath", single_imports.append)
    monkeypatch.setattr(
        view_model,
        "_create_next_multi_project",
        lambda: multi_starts.append(tuple(view_model._multi_import_paths)),
    )

    view_model.importPaths([str(first)])
    view_model.importPaths([str(first), str(second)])

    assert single_imports == [str(first.resolve())]
    assert multi_starts == [(first.resolve(), second.resolve())]
    assert view_model.importBusy is True
    assert view_model.saveState == "Đang nhập 0/2 video"


def test_multi_import_accepts_one_video_without_using_single_project_flow(
    monkeypatch, tmp_path: Path
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._backend_ready = True
    video = tmp_path / "single multi.mp4"
    video.write_bytes(b"video")
    single_imports: list[str] = []
    multi_starts: list[tuple[Path, ...]] = []
    monkeypatch.setattr(view_model, "importPath", single_imports.append)
    monkeypatch.setattr(
        view_model,
        "_create_next_multi_project",
        lambda: multi_starts.append(tuple(view_model._multi_import_paths)),
    )

    view_model.importMultiPaths([QUrl.fromLocalFile(str(video)).toString()])

    assert single_imports == []
    assert multi_starts == [(video.resolve(),)]
    assert view_model.importBusy is True
    assert view_model.saveState == "Đang nhập 0/1 video"


def test_select_all_multi_videos_updates_every_card_before_backend_response(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._replace_multi_videos(
        {
            "batch": {"state": "idle"},
            "items": [
                {"project_id": "first", "selected": False},
                {"project_id": "second", "selected": False},
            ],
        }
    )
    requests: list[tuple[str, object, object]] = []

    def request(action: str, params=None, *, context=None):
        requests.append((action, params, context))
        return "pending-selection"

    monkeypatch.setattr(view_model.runtime, "request", request)

    view_model.selectAllMultiVideos(True)

    selected_role = next(
        role for role, name in view_model.multi_videos.roleNames().items() if name == b"selected"
    )
    assert view_model.multiSelectedCount == 2
    assert all(
        view_model.multi_videos.data(view_model.multi_videos.index(row, 0), selected_role) is True
        for row in range(2)
    )
    assert requests[0][0:2] == ("multi_select_all", {"selected": True})


def test_select_one_multi_video_updates_card_before_backend_response(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._replace_multi_videos(
        {
            "batch": {"state": "idle"},
            "items": [{"project_id": "first", "selected": False}],
        }
    )
    requests: list[tuple[str, object, object]] = []

    def request(action: str, params=None, *, context=None):
        requests.append((action, params, context))
        return "pending-selection"

    monkeypatch.setattr(view_model.runtime, "request", request)

    view_model.selectMultiVideo("first", True)

    selected_role = next(
        role for role, name in view_model.multi_videos.roleNames().items() if name == b"selected"
    )
    assert view_model.multiSelectedCount == 1
    assert view_model.multi_videos.data(view_model.multi_videos.index(0, 0), selected_role)
    assert requests[0][0:2] == (
        "multi_select",
        {"project_id": "first", "selected": True},
    )


def test_failed_optimistic_multi_selection_rolls_cards_back(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._replace_multi_videos(
        {
            "batch": {"state": "idle"},
            "items": [
                {"project_id": "first", "selected": False},
                {"project_id": "second", "selected": False},
            ],
        }
    )
    requests: list[tuple[str, object, object]] = []

    def request(action: str, params=None, *, context=None):
        requests.append((action, params, context))
        return "pending-selection"

    monkeypatch.setattr(view_model.runtime, "request", request)
    view_model.selectAllMultiVideos(True)
    context = requests[0][2]

    view_model._action_failed(
        "multi_select_all",
        {"message": "Không thể ghi cơ sở dữ liệu"},
        context,
    )

    assert view_model.multiSelectedCount == 0
    assert "Không thể lưu lựa chọn Multi video" in view_model.statusMessage


def test_stale_failed_selection_does_not_undo_a_newer_click(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._replace_multi_videos(
        {
            "batch": {"state": "idle"},
            "items": [{"project_id": "first", "selected": False}],
        }
    )
    requests: list[tuple[str, object, object]] = []

    def request(action: str, params=None, *, context=None):
        requests.append((action, params, context))
        return f"request-{len(requests)}"

    monkeypatch.setattr(view_model.runtime, "request", request)
    view_model.selectAllMultiVideos(True)
    stale_context = requests[0][2]
    view_model.selectAllMultiVideos(False)

    view_model._action_failed(
        "multi_select_all",
        {"message": "Phản hồi cũ thất bại"},
        stale_context,
    )

    assert view_model.multiSelectedCount == 0
    assert requests[-1][0] == "multi_list"


def test_open_multi_detail_navigates_immediately_and_ignores_stale_response(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._backend_ready = True
    requests: list[tuple[str, object, object]] = []

    def request(action: str, params=None, *, context=None):
        requests.append((action, params, context))
        return f"request-{len(requests)}"

    monkeypatch.setattr(view_model.runtime, "request", request)

    view_model.openMultiVideoDetail("project-old")
    old_context = requests[-1][2]

    assert view_model.currentWorkspace == "timeline"
    assert view_model.multiDetailBusy is True

    old_generation = int(old_context["generation"])
    view_model._multi_detail_timeout(old_generation)
    view_model.openMultiVideoDetail("project-new")

    view_model._action_succeeded(
        "get_project",
        {"id": "project-old", "name": "Old", "revision": 1},
        old_context,
    )

    assert view_model.multiDetailBusy is True
    assert view_model._pending_multi_detail_project_id == "project-new"
    assert view_model.project_state.projectId != "project-old"

    new_context = requests[-1][2]
    view_model._action_failed(
        "get_project",
        {"message": "checksum mismatch"},
        new_context,
    )

    assert view_model.multiDetailBusy is False
    assert view_model.currentWorkspace == "multi"


def test_open_multi_translation_review_routes_to_translation_and_pins_failed_cache(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._backend_ready = True
    view_model._replace_multi_videos(
        {
            "batch": {"state": "completed_with_errors"},
            "items": [
                {
                    "project_id": "project-review",
                    "stage": "translate",
                    "state": "failed",
                    "error_code": "translation_review_required",
                    "child_job_id": "translation-review-job",
                    "error_payload": {"cjk_cue_ids": [4]},
                }
            ],
        }
    )
    requests: list[tuple[str, object, object]] = []

    def request(action: str, params=None, *, context=None):
        requests.append((action, params, context))
        return f"request-{len(requests)}"

    monkeypatch.setattr(view_model.runtime, "request", request)
    view_model.openMultiVideoDetail("project-review")

    assert view_model.currentWorkspace == "translate"
    assert view_model.multiDetailBusy is True
    assert requests[-1][0] == "get_project"
    assert requests[-1][2]["multi_detail_target"] == "translate"
    assert requests[-1][2]["translation_review_job_id"] == "translation-review-job"


def test_multi_import_commits_completed_files_before_reporting_later_failure(
    monkeypatch, tmp_path: Path
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._multi_import_paths = [tmp_path / "second.mp4", tmp_path / "third.mp4"]
    view_model._multi_import_project_ids = ["project-first"]
    view_model._set_import_busy(True)
    requests: list[tuple[str, object, object]] = []

    def request(action: str, params=None, *, context=None):
        requests.append((action, params, context))
        return f"request-{len(requests)}"

    monkeypatch.setattr(view_model.runtime, "request", request)

    view_model._action_failed(
        "create_project_with_media",
        {"message": "Không đọc được video"},
        {"multi_import_path": str(tmp_path / "second.mp4")},
    )

    action, params, context = requests[0]
    assert action == "multi_add"
    assert params == {"project_ids": ["project-first"]}
    assert context["count"] == 1
    assert context["total"] == 3
    assert context["multi_import_partial_error"] == "Không đọc được video"
    assert view_model.importBusy is True
    assert view_model._multi_import_project_ids == ["project-first"]

    view_model._action_succeeded("multi_add", {"items": []}, context)

    assert view_model.importBusy is False
    assert view_model._multi_import_project_ids == []
    assert "Đã thêm và chọn 1/3 video" in view_model.statusMessage
    assert "second.mp4" in view_model.statusMessage


def test_multi_card_uses_manual_status_then_falls_back_to_artifacts() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = MultiVideoListModel()
    roles = {name: role for role, name in model.roleNames().items()}
    model.replace(
        [
            {
                "project_id": "manual",
                "state": "waiting",
                "batch_state": "idle",
                "progress": 0.0,
                "project_status_override": True,
                "project_status_state": "running",
                "project_status_text": "Thủ công · Đang dịch 25%",
                "project_status_progress": 0.25,
                "artifact_status_state": "idle",
                "artifact_status_text": "Đã trích xuất SRT",
                "artifact_status_progress": 1 / 3,
            },
            {
                "project_id": "artifact",
                "state": "waiting",
                "batch_state": "idle",
                "progress": 0.0,
                "project_status_override": False,
                "artifact_status_state": "completed",
                "artifact_status_text": "D1 sẵn sàng",
                "artifact_status_progress": 1.0,
            },
            {
                "project_id": "batch-current",
                "state": "running",
                "batch_state": "running",
                "is_batch_current": True,
                "stage": "tts",
                "progress": 5 / 6,
                "project_status_override": True,
                "project_status_state": "running",
                "project_status_text": "Thủ công · Đang dịch 90%",
                "project_status_progress": 0.9,
            },
        ]
    )

    assert model.data(model.index(0, 0), roles[b"state"]) == "running"
    assert model.data(model.index(0, 0), roles[b"statusText"]) == ("Thủ công · Đang dịch 25%")
    assert model.data(model.index(0, 0), roles[b"progress"]) == 0.25
    assert model.data(model.index(1, 0), roles[b"state"]) == "completed"
    assert model.data(model.index(1, 0), roles[b"statusText"]) == "D1 sẵn sàng"
    assert model.data(model.index(1, 0), roles[b"progress"]) == 1.0
    assert model.data(model.index(2, 0), roles[b"statusText"]) == "Đang lồng tiếng 50%"
    assert model.data(model.index(2, 0), roles[b"progress"]) == 5 / 6
    assert model.data(model.index(2, 0), roles[b"stageProgress"]) == pytest.approx(0.5)


def test_multi_card_displays_progress_within_the_current_stage() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = MultiVideoListModel()
    roles = {name: role for role, name in model.roleNames().items()}
    model.replace(
        [
            {
                "project_id": "ocr",
                "state": "running",
                "batch_state": "running",
                "stage": "ocr",
                "progress": 0.2,
            },
            {
                "project_id": "translate",
                "state": "running",
                "batch_state": "running",
                "stage": "translate",
                "progress": 0.5,
            },
            {
                "project_id": "tts",
                "state": "running",
                "batch_state": "running",
                "stage": "tts",
                "progress": 0.75,
            },
            {
                "project_id": "next-stage",
                "state": "running",
                "batch_state": "running",
                "stage": "translate",
                "progress": 1 / 3,
            },
            {
                "project_id": "completed",
                "state": "completed",
                "batch_state": "completed",
                "stage": "done",
                "progress": 1.0,
            },
        ]
    )

    expected = (
        (0.6, "Đang trích xuất SRT 60%"),
        (0.5, "Đang dịch 50%"),
        (0.25, "Đang lồng tiếng 25%"),
        (0.0, "Đang dịch 0%"),
        (1.0, "Đã hoàn thành 100%"),
    )
    for row, (progress, text) in enumerate(expected):
        card = model.index(row, 0)
        assert model.data(card, roles[b"stageProgress"]) == pytest.approx(progress)
        assert model.data(card, roles[b"statusText"]) == text


def test_multi_video_card_model_keeps_all_50_cards_and_exposes_edge_roles() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = MultiVideoListModel()
    items = [
        {
            "project_id": f"project-{index:02d}",
            "name": f"Video {index:02d}",
            "video_path": f"C:/fixtures/video-{index:02d}.mp4",
            "duration_us": (index + 1) * 1_000_000,
            "selected": index % 2 == 0,
            "stage": "translate" if index == 49 else "ocr",
            "state": "running" if index == 49 else "waiting",
            "progress": 0.5 if index == 49 else 0.0,
        }
        for index in range(50)
    ]

    model.replace(items)

    roles = {name: role for role, name in model.roleNames().items()}
    first = model.index(0, 0)
    last = model.index(49, 0)
    assert model.count == model.rowCount() == 50
    assert model.data(first, roles[b"projectId"]) == "project-00"
    assert model.data(first, roles[b"selected"]) is True
    assert model.data(last, roles[b"projectId"]) == "project-49"
    assert model.data(last, roles[b"progress"]) == 0.5
    assert model.data(last, roles[b"statusText"]) == "Đang dịch 50%"
    assert (
        model.data(first, roles[b"thumbnailUrl"]).toString().startswith("image://multi-thumbnail/")
    )


def test_multi_card_updates_live_cjk_count_and_manual_ready_state() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = MultiVideoListModel()
    roles = {name: role for role, name in model.roleNames().items()}
    model.replace(
        [
            {
                "project_id": "needs-edit",
                "stage": "translate",
                "state": "failed",
                "error_code": "translation_review_required",
                "error_payload": {"cjk_blocker_count": 10},
            },
            {
                "project_id": "ready",
                "stage": "translate",
                "state": "failed",
                "manual_review_ready": True,
                "error_code": "translation_review_required",
                "error_payload": {"cjk_blocker_count": 0},
            },
        ]
    )

    needs_edit = model.index(0, 0)
    ready = model.index(1, 0)
    assert model.data(needs_edit, roles[b"statusText"]) == ("Còn chữ Trung · 10 cue · đã bỏ qua")
    assert model.data(ready, roles[b"state"]) == "completed"
    assert model.data(ready, roles[b"statusText"]) == (
        "Đã sửa xong chữ Trung · sẵn sàng lồng tiếng"
    )


def test_partial_multi_status_merges_without_resetting_catalog_cards(monkeypatch) -> None:
    """Lightweight status events keep card media and GridView delegates intact."""

    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._replace_multi_videos(
        {
            "batch": {"state": "idle"},
            "items": [
                {
                    "project_id": "project-1",
                    "name": "Video gốc",
                    "video_path": "C:/fixtures/source.mp4",
                    "duration_us": 1_000_000,
                    "state": "waiting",
                    "stage": "ocr",
                    "progress": 0.0,
                }
            ],
            "hidden_count": 0,
        }
    )
    resets: list[None] = []
    view_model.multi_videos.modelReset.connect(lambda: resets.append(None))

    view_model._replace_multi_videos(
        {
            "batch": {"state": "running"},
            "items": [
                {
                    "project_id": "project-1",
                    "state": "running",
                    "stage": "translate",
                    "progress": 0.5,
                }
            ],
            "partial": True,
        }
    )

    roles = {name: role for role, name in view_model.multi_videos.roleNames().items()}
    card = view_model.multi_videos.index(0, 0)
    assert resets == []
    assert view_model.multi_videos.data(card, roles[b"videoUrl"]).toString().endswith("/source.mp4")
    assert view_model.multi_videos.data(card, roles[b"progress"]) == 0.5
    assert view_model.multi_videos.data(card, roles[b"stageProgress"]) == pytest.approx(0.5)
    assert view_model.multi_videos.data(card, roles[b"statusText"]) == "Đang dịch 50%"


def test_multi_pause_button_updates_state_before_ipc_response(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model._replace_multi_videos(
        {
            "batch": {"state": "running", "current_project_id": "project-1"},
            "items": [
                {
                    "project_id": "project-1",
                    "selected": True,
                    "stage": "ocr",
                    "state": "running",
                    "progress": 0.1,
                }
            ],
        }
    )
    requests: list[str] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, *_args, **_kwargs: requests.append(action),
    )

    view_model.pauseMultiVideo()

    assert requests == ["multi_pause"]
    assert view_model.multiBatchState == "pause_requested"
    roles = {name: role for role, name in view_model.multi_videos.roleNames().items()}
    card = view_model.multi_videos.index(0, 0)
    assert "Đang tạm dừng" in view_model.multi_videos.data(card, roles[b"statusText"])


def test_multi_resume_optimistic_state_preserves_current_stage(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model._replace_multi_videos(
        {
            "batch": {"state": "paused", "current_project_id": "project-1"},
            "items": [
                {
                    "project_id": "project-1",
                    "selected": True,
                    "stage": "translate",
                    "state": "running",
                    "progress": 0.5,
                    "child_job_id": "translation-job",
                }
            ],
        }
    )
    requests: list[str] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, *_args, **_kwargs: requests.append(action),
    )

    view_model.resumeMultiVideo()

    card = view_model.multi_videos.item("project-1")
    assert requests == ["multi_resume"]
    assert view_model.multiBatchState == "running"
    assert card["stage"] == "translate"
    assert card["child_job_id"] == "translation-job"


def test_rich_terminal_multi_refresh_merges_artifacts_without_resetting_cards(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._replace_multi_videos(
        {
            "batch": {"state": "idle"},
            "items": [
                {
                    "project_id": "project-1",
                    "name": "Video gốc",
                    "video_path": "C:/fixtures/source.mp4",
                    "state": "waiting",
                    "artifact_status_state": "idle",
                    "artifact_status_text": "Chưa xử lý",
                }
            ],
        }
    )
    resets: list[None] = []
    view_model.multi_videos.modelReset.connect(lambda: resets.append(None))

    view_model._replace_multi_videos(
        {
            "batch": {"state": "idle"},
            "items": [
                {
                    "project_id": "project-1",
                    "name": "Video gốc",
                    "video_path": "C:/fixtures/source.mp4",
                    "state": "waiting",
                    "has_voice_track": True,
                    "voice_audio_path": "C:/fixtures/d1.wav",
                    "artifact_status_state": "completed",
                    "artifact_status_text": "D1 sẵn sàng",
                    "artifact_status_progress": 1.0,
                }
            ],
            "partial": True,
        }
    )

    roles = {name: role for role, name in view_model.multi_videos.roleNames().items()}
    card = view_model.multi_videos.index(0, 0)
    assert resets == []
    assert view_model.multi_videos.data(card, roles[b"hasVoiceTrack"]) is True
    assert view_model.multi_videos.data(card, roles[b"statusText"]) == "D1 sẵn sàng"


def test_multi_control_response_is_marked_as_merge_update() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._pending_requests["request-1"] = ("multi_start", {})
    received: list[dict] = []
    runtime.multiVideosReceived.connect(lambda payload: received.append(dict(payload)))

    runtime._response(
        SimpleNamespace(
            kind="response",
            payload={
                "request_id": "request-1",
                "ok": True,
                "result": {"batch": {"state": "running"}, "items": []},
            },
        )
    )

    assert received[0]["partial"] is True


def test_selection_response_does_not_overwrite_optimistic_card_state() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._pending_requests["request-1"] = ("multi_select_all", {})
    received: list[dict] = []
    succeeded: list[str] = []
    runtime.multiVideosReceived.connect(lambda payload: received.append(dict(payload)))
    runtime.actionSucceeded.connect(lambda action, _result, _context: succeeded.append(action))

    runtime._response(
        SimpleNamespace(
            kind="response",
            payload={
                "request_id": "request-1",
                "ok": True,
                "result": {
                    "batch": {"state": "idle"},
                    "items": [{"project_id": "first", "selected": False}],
                },
            },
        )
    )

    assert received == []
    assert succeeded == ["multi_select_all"]


def test_multi_card_reports_paused_interrupted_and_final_failure_truthfully() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = MultiVideoListModel()
    roles = {name: role for role, name in model.roleNames().items()}
    model.replace(
        [
            {
                "project_id": "paused",
                "state": "running",
                "batch_state": "paused",
                "stage": "translate",
                "progress": 0.5,
            },
            {
                "project_id": "interrupted",
                "state": "retrying",
                "batch_state": "interrupted",
                "stage": "tts",
                "progress": 0.75,
            },
            {
                "project_id": "failed",
                "state": "failed",
                "batch_state": "completed_with_errors",
                "stage": "ocr",
                "progress": 0.2,
            },
        ]
    )

    assert model.data(model.index(0, 0), roles[b"state"]) == "paused"
    assert model.data(model.index(0, 0), roles[b"statusText"]) == ("Đã tạm dừng · dịch 50%")
    assert model.data(model.index(1, 0), roles[b"state"]) == "interrupted"
    assert model.data(model.index(1, 0), roles[b"statusText"]) == (
        "Đã gián đoạn · lồng tiếng 25% · cache vẫn được giữ"
    )
    assert model.data(model.index(2, 0), roles[b"statusText"]) == (
        "Lỗi trích xuất SRT 60% · đã bỏ qua"
    )


def test_multi_card_distinguishes_worker_start_cache_restore_and_unconfirmed_pause() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = MultiVideoListModel()
    roles = {name: role for role, name in model.roleNames().items()}
    model.replace(
        [
            {
                "project_id": "starting",
                "state": "running",
                "batch_state": "running",
                "stage": "tts",
                "progress": 0.8,
                "child_job_state": "start_requested",
            },
            {
                "project_id": "restoring",
                "state": "running",
                "batch_state": "running",
                "stage": "tts",
                "progress": 0.8,
                "child_job_state": "running",
                "child_phase": "cache_restore",
            },
            {
                "project_id": "not-paused-yet",
                "state": "running",
                "batch_state": "paused",
                "stage": "ocr",
                "progress": 0.1,
                "child_job_state": "running",
            },
        ]
    )

    assert model.data(model.index(0, 0), roles[b"statusText"]) == (
        "Đang khởi động bộ máy từ cache · lồng tiếng 40%"
    )
    assert model.data(model.index(1, 0), roles[b"statusText"]) == (
        "Đang kiểm tra cache · lồng tiếng 40%"
    )
    assert model.data(model.index(2, 0), roles[b"state"]) == "pause_requested"
    assert model.data(model.index(2, 0), roles[b"statusText"]) == (
        "Đang tạm dừng · trích xuất SRT 30%"
    )


def test_multi_card_reports_completed_ocr_review_without_calling_it_a_retry() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = MultiVideoListModel()
    roles = {name: role for role, name in model.roleNames().items()}
    model.replace(
        [
            {
                "project_id": "ocr-review",
                "state": "deferred",
                "batch_state": "running",
                "stage": "ocr",
                "progress": 1 / 3,
                "error_code": "ocr_review_required",
                "error_payload": {
                    "blocker_count": 2,
                    "message": "OCR đã quét xong.",
                },
            }
        ]
    )

    card = model.index(0, 0)
    assert model.data(card, roles[b"statusText"]) == (
        "OCR đã xong · 2 mục cần xem lại · chờ cuối lượt"
    )
    assert model.data(card, roles[b"errorMessage"]) == "OCR đã quét xong."


def test_multi_card_reports_pending_safe_removal() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = MultiVideoListModel()
    roles = {name: role for role, name in model.roleNames().items()}
    model.replace(
        [
            {
                "project_id": "removing",
                "state": "running",
                "batch_state": "paused",
                "stage": "tts",
                "progress": 0.75,
                "error_code": "membership_removal_pending",
            }
        ]
    )

    assert model.data(model.index(0, 0), roles[b"statusText"]) == (
        "Đang dừng an toàn để xóa khỏi Multi…"
    )


def test_multi_remove_reports_pending_before_terminal_cleanup(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()

    view_model._action_succeeded(
        "multi_remove",
        {
            "items": [
                {
                    "project_id": "project-1",
                    "error_code": "membership_removal_pending",
                }
            ]
        },
        {"project_id": "project-1"},
    )

    assert view_model.statusMessage == (
        "Đang chờ tác vụ dừng an toàn; video sẽ tự biến mất khỏi Multi sau khi dừng."
    )


def test_tts_review_does_not_reload_project_or_jump_to_timeline(monkeypatch) -> None:
    """A guarded TTS review has no project mutation to reload."""

    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {"id": "project-1", "name": "Phim", "revision": 1, "media": [], "tracks": []}
    )
    requests: list[tuple[str, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append((action, payload)),
    )

    view_model._replace_jobs(
        [
            {
                "id": "tts-review",
                "spec": {
                    "kind": "tts",
                    "project_id": "project-1",
                    "provider_config": {"purpose": "production"},
                },
                "state": "failed",
                "error_code": "tts_review_required",
            }
        ]
    )

    assert requests == []
    assert view_model.ttsJobState == "review"
    assert view_model.ttsBusy is False


def test_timing_review_can_explicitly_request_full_voice_timeline_override(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {"id": "project-1", "name": "Phim", "revision": 1, "media": [], "tracks": []}
    )
    view_model._set_voice_timing_qa(
        {
            "status": "REVIEW",
            "job_id": "tts-review",
            "issues": [{"id": 2, "cue_id": "cue-2", "code": "CUE_TOO_LONG"}],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.addCurrentVoiceToTimeline()
    view_model.addCurrentVoiceToTimeline()

    assert requests == [
        (
            "create_tts_timing_override_job",
            {"project_id": "project-1", "parent_tts_job_id": "tts-review"},
            {"voice_timing_override": True, "parent_tts_job_id": "tts-review"},
        )
    ]
    assert view_model.ttsBusy is True
    assert view_model.ttsJobState == "start_requested"
    assert "đang được ghép vào Timeline" in view_model.statusMessage


def test_failed_timing_override_releases_the_button_for_a_new_attempt(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    view_model._voice_timing_override_requests.add("tts-review")

    view_model._replace_jobs(
        [
            {
                "id": "tts-override-failed",
                "spec": {
                    "kind": "tts",
                    "project_id": "project-1",
                    "provider_config": {
                        "purpose": "production",
                        "allow_timeline_overflow": True,
                        "timing_override_parent_job_id": "tts-review",
                    },
                },
                "state": "failed",
                "error_code": "worker_failed",
                "created_at": "2026-08-27T00:00:00Z",
            }
        ]
    )

    assert "tts-review" not in view_model._voice_timing_override_requests
    assert view_model.ttsBusy is False
    assert view_model.ttsJobState == "failed"


def test_job_model_exposes_review_required_error_code() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = JobListModel(accepted_kinds=frozenset({"ocr"}))
    model.replace(
        [
            {
                **_job("ocr-review", "ocr"),
                "state": "failed",
                "error_code": "ocr_review_required",
            }
        ]
    )

    error_code_role = next(role for role, name in model.roleNames().items() if name == b"errorCode")

    assert model.data(model.index(0, 0), error_code_role) == "ocr_review_required"


def test_job_model_hides_stale_eta_after_job_reaches_terminal_state() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = JobListModel(accepted_kinds=frozenset({"tts"}))
    running = {
        **_job("tts-running", "tts"),
        "progress": {"current": 64, "total": 5_172, "eta_ms": 11_915_766},
    }
    cancelled = {
        **_job("tts-cancelled", "tts"),
        "state": "cancelled",
        "progress": {"current": 64, "total": 5_172, "eta_ms": 11_915_766},
    }
    paused = {
        **_job("tts-paused", "tts"),
        "state": "paused",
        "progress": {"current": 64, "total": 5_172, "eta_ms": 11_915_766},
    }
    model.replace([running, cancelled, paused])
    eta_role = next(role for role, name in model.roleNames().items() if name == b"etaMs")

    live_eta = model.data(model.index(0, 0), eta_role)
    assert 0 <= 11_915_766 - live_eta < 1_000
    assert model.data(model.index(1, 0), eta_role) is None
    assert model.data(model.index(2, 0), eta_role) is None


def test_job_model_exposes_phase_progress_and_cache_restore_details() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = JobListModel(accepted_kinds=frozenset({"tts"}))
    model.replace(
        [
            {
                **_job("tts-cache", "tts"),
                "progress": {
                    "phase": "cache_restore",
                    "current": 3_840,
                    "total": 5_172,
                    "phase_current": 5_172,
                    "phase_total": 5_172,
                    "cache_restored_cues": 3_840,
                    "reused_processed_cues": 2_640,
                    "reprocessed_raw_cues": 1_092,
                    "pending_synthesis_cues": 1_332,
                },
            }
        ]
    )
    roles = {name: role for role, name in model.roleNames().items()}
    index = model.index(0, 0)

    assert model.data(index, roles[b"phaseCurrent"]) == 5_172
    assert model.data(index, roles[b"phaseTotal"]) == 5_172
    assert model.data(index, roles[b"cacheRestoredCues"]) == 3_840
    assert model.data(index, roles[b"pendingSynthesisCues"]) == 1_332


def test_job_model_keeps_elapsed_and_eta_moving_between_backend_polls(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    clock = [500.0]
    monkeypatch.setattr(
        "ducai_studio.ui.viewmodels.project_job_models.monotonic",
        lambda: clock[0],
    )
    model = JobListModel(accepted_kinds=frozenset({"translate"}))
    model.replace(
        [
            {
                **_job("translate-live", "translate"),
                "progress": {
                    "phase": "translation",
                    "current": 3_781,
                    "total": 13_385,
                    "elapsed_ms": 120_000,
                    "eta_ms": 600_000,
                    "chunk_current": 25,
                    "chunk_total": 93,
                },
            }
        ]
    )
    roles = {name: role for role, name in model.roleNames().items()}
    index = model.index(0, 0)

    clock[0] += 5.0

    assert model.data(index, roles[b"elapsedMs"]) == 125_000
    assert model.data(index, roles[b"etaMs"]) == 595_000
    assert model.data(index, roles[b"chunkCurrent"]) == 25
    assert model.data(index, roles[b"chunkTotal"]) == 93


def test_job_model_exposes_live_progress_age_between_confirmed_updates(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    clock = [700.0]
    monkeypatch.setattr(
        "ducai_studio.ui.viewmodels.project_job_models.monotonic",
        lambda: clock[0],
    )
    model = JobListModel(accepted_kinds=frozenset({"tts"}))
    model.replace(
        [
            {
                **_job("tts-starting", "tts"),
                "state": "start_requested",
            }
        ]
    )
    roles = {name: role for role, name in model.roleNames().items()}
    index = model.index(0, 0)

    clock[0] += 4.25

    assert model.data(index, roles[b"progressAgeMs"]) == 4_250

    model.replace(
        [
            {
                **_job("tts-starting", "tts"),
                "state": "running",
                "progress": {
                    "phase": "cache_restore",
                    "phase_current": 16,
                    "phase_total": 13_385,
                    "elapsed_ms": 900,
                },
            }
        ]
    )
    index = model.index(0, 0)

    assert model.data(index, roles[b"progressAgeMs"]) == 0


def test_virtual_subtitle_model_patches_4333_cues_without_full_reset() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = [
        {
            "id": f"cue-{index}",
            "index": index,
            "range": {
                "start_us": (index - 1) * 1_000_000,
                "end_us": index * 1_000_000,
            },
            "text": f"第{index}句",
            "source": "paddle-fast:upper",
            "revision": 1,
        }
        for index in range(1, 4_334)
    ]
    model.replace(source, [])
    resets: list[None] = []
    changes: list[None] = []
    inserts: list[None] = []
    model.modelReset.connect(lambda: resets.append(None))
    model.dataChanged.connect(lambda *_args: changes.append(None))
    model.rowsInserted.connect(lambda *_args: inserts.append(None))

    changed = [dict(item) for item in source]
    changed[2_000] = {**changed[2_000], "text": "已修正"}
    model.replace(changed, [])
    inserted = [
        *changed[:2],
        {
            "id": "cue-new",
            "index": 3,
            "range": {"start_us": 1_500_000, "end_us": 2_000_000},
            "text": "新增",
            "source": "manual",
            "revision": 1,
        },
        *changed[2:],
    ]
    model.replace(inserted, [])

    assert model.count == 4_334
    assert resets == []
    assert changes
    assert inserts == [None]


def test_subtitle_lookup_interleaves_scene_text_appended_after_dialogue() -> None:
    """S2 is stored after S1 but preview lookup must follow timeline time."""

    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = [
        {
            "id": "dialogue-1",
            "index": 1,
            "range": {"start_us": 7_000_000, "end_us": 8_500_000},
            "text": "你到了",
            "role": "dialogue",
        },
        {
            "id": "dialogue-195",
            "index": 195,
            "range": {"start_us": 900_000_000, "end_us": 901_000_000},
            "text": "走吧",
            "role": "dialogue",
        },
        {
            "id": "scene-196",
            "index": 196,
            "range": {"start_us": 7_500_000, "end_us": 10_740_000},
            "text": "乱星海",
            "role": "scene_text",
            "review_state": "accepted",
            "scene_font_scale": 1.25,
        },
    ]
    translated = [
        {
            "id": "translated-dialogue-1",
            "index": 1,
            "range": {"start_us": 7_000_000, "end_us": 8_500_000},
            "text": "Ngươi đã tới.",
        },
        {
            "id": "translated-scene-196",
            "index": 196,
            "range": {"start_us": 7_500_000, "end_us": 10_740_000},
            "text": "Loạn Tinh Hải",
        },
    ]

    model.replace(source, translated)

    assert [item["start_us"] for item in model._items] == sorted(model._start_us)
    assert model.rowForCueId("scene-196") == 1
    assert model.rowForCueId("missing") == -1
    assert model.at_time(8_000_000)["translated_text"] == "Ngươi đã tới."
    assert model.sceneTextsAtTime(8_000_000) == [
        {
            **model.find("scene-196"),
        }
    ]
    assert model.sceneTextsAtTime(8_000_000)[0]["translated_text"] == "Loạn Tinh Hải"
    assert model.sceneTextsAtTime(8_000_000)[0]["scene_font_scale"] == 1.25

    assert model.optimistic_update_scene_font_scale("scene-196", 0.75) is True
    assert model.sceneTextsAtTime(8_000_000)[0]["scene_font_scale"] == 0.75
    model.rollback_text_changes(
        [
            {
                "kind": "scene_font_scale",
                "cue_id": "scene-196",
                "previous": 1.25,
                "applied": 0.75,
            }
        ]
    )
    assert model.sceneTextsAtTime(8_000_000)[0]["scene_font_scale"] == 1.25


def test_scene_text_scale_previews_immediately_and_rolls_back_failed_save(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-scene-scale",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "scene-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "韩立",
                    "language": "zh",
                    "role": "scene_text",
                    "visual_box": [0.3, 0.2, 0.1, 0.08],
                    "visual_box_source": "ocr",
                }
            ],
            "translated_cues": [
                {
                    "id": "scene-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "Hàn Lập",
                    "language": "vi",
                    "role": "scene_text",
                    "visual_box": [0.3, 0.2, 0.1, 0.08],
                    "visual_box_source": "ocr",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    visual_revision = view_model.project_state.subtitleVisualRevision

    view_model.previewSceneTextFontScale("scene-1", 1.25)

    assert view_model.project_state.subtitle_cues.find("scene-1")["scene_font_scale"] == 1.25
    assert view_model.project_state.subtitleVisualRevision > visual_revision

    view_model.setSceneTextFontScale("scene-1", 1.25, 1.0)

    assert requests[-1][0] == "subtitle_command"
    assert requests[-1][1]["command"] == "set_scene_text_font_scale"
    assert requests[-1][1]["values"] == {"scale": 1.25}
    view_model._action_failed(
        "subtitle_command",
        {"message": "disk full"},
        requests[-1][2],
    )
    assert view_model.project_state.subtitle_cues.find("scene-1")["scene_font_scale"] == 1.0


def test_scene_text_timing_preview_is_live_and_failed_save_rolls_back(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-scene-timing",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "scene-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "韩立",
                    "language": "zh",
                    "role": "scene_text",
                }
            ],
            "translated_cues": [
                {
                    "id": "scene-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "Hàn Lập",
                    "language": "vi",
                    "role": "scene_text",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.previewSceneTextTiming("scene-1", 3_000_000, 5_000_000)

    assert view_model.project_state.sceneTextsAt(1_000_000) == []
    assert view_model.project_state.sceneTextsAt(4_000_000)[0]["translated_text"] == "Hàn Lập"
    assert view_model.project_state.subtitle_cues.find("scene-1")["start_us"] == 3_000_000

    view_model.setSceneTextTiming(
        "scene-1",
        3_000_000,
        5_000_000,
        0,
        2_000_000,
    )

    assert requests[-1][0] == "subtitle_command"
    assert requests[-1][1]["command"] == "set_scene_text_timing"
    assert requests[-1][1]["values"] == {
        "start_us": 3_000_000,
        "end_us": 5_000_000,
            "display_time": True,
    }
    view_model._action_failed(
        "subtitle_command",
        {"message": "disk full"},
        requests[-1][2],
    )
    assert view_model.project_state.sceneTextsAt(1_000_000)[0]["cue_id"] == "scene-1"
    assert view_model.project_state.sceneTextsAt(4_000_000) == []


def test_application_view_model_exposes_filtered_jobs_and_persisted_ui_state(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})

    view_model._replace_jobs(
        [
            _job("ocr-1", "ocr"),
            _job("translate-1", "translate"),
            _job("tts-1", "tts"),
            _job("export-1", "export"),
        ]
    )
    view_model.setUiStringValue("voiceInspectorTab", "history")
    view_model.setUiRealValue("voiceInspectorRatio", 0.31)
    view_model._set_runtime_state("ready")

    assert view_model.jobModel.count == 4
    assert view_model.subtitleJobModel.count == 2
    assert view_model.voiceJobModel.count == 1
    assert view_model.exportJobModel.count == 1
    assert view_model.uiStringValue("voiceInspectorTab", "voice") == "history"
    assert view_model.uiRealValue("voiceInspectorRatio", 0.30) == 0.31
    assert view_model.runtimeState == "ready"

    MemorySettings.values["ui/layout/corruptRatio"] = "not-a-number"
    assert view_model.uiRealValue("corruptRatio", 0.42) == 0.42

    restored_view_model = ApplicationViewModel()
    assert restored_view_model.uiStringValue("voiceInspectorTab", "voice") == "history"
    assert restored_view_model.uiRealValue("voiceInspectorRatio", 0.30) == 0.31


def test_application_export_pause_dispatches_control_for_the_running_job(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload, context=None: requests.append((action, payload, context)),
    )
    view_model._replace_jobs([_job("export-running", "export")])

    view_model.pauseExport()

    assert view_model.exportJobState == "pause_requested"
    assert requests == [("pause_job", {"job_id": "export-running"}, {"job_kind": "export"})]


def test_adaptive_voice_timing_is_always_enabled_for_automatic_voice_creation(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()

    assert view_model.voiceAdaptiveTimingEnabled is True
    view_model.setVoiceAdaptiveTimingEnabled(False)

    assert view_model.voiceAdaptiveTimingEnabled is True
    assert MemorySettings.values["voice/adaptiveTimingEnabled"] is True


def test_voice_speed_ui_defaults_and_clamps_stay_separate(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()

    assert view_model.voiceReadSpeed == 1.3
    assert view_model.voiceCueMaxSpeed == 1.5

    view_model.setVoiceReadSpeed(1.0)
    assert view_model.voiceReadSpeed == 1.1
    assert view_model.voiceCueMaxSpeed == 1.5

    view_model.setVoiceReadSpeed(2.0)
    assert view_model.voiceReadSpeed == 2.0
    assert view_model.voiceCueMaxSpeed == 2.0

    view_model.setVoiceCueMaxSpeed(1.5)
    assert view_model.voiceCueMaxSpeed == 2.0


def test_subtitle_and_video_filter_settings_are_live_clamped_and_resettable(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()

    view_model.setExportSubtitlePrimaryColor("#12ab34")
    view_model.setExportSubtitleOutlineColor("#102030")
    view_model.setExportSubtitleOutlineWidth(99)
    view_model.setExportSubtitleOpacityPercent(-10)
    view_model.setExportSubtitleAlignment(3)
    view_model.setExportVideoFilterPreset("cinematic-warm")
    view_model.setExportVideoFilterIntensity(65)
    view_model.setExportVideoBrightness(99)
    view_model.setExportVideoContrast(20)
    view_model.setExportVideoSaturation(250)

    assert view_model.exportSubtitlePrimaryColor == "#12AB34"
    assert view_model.exportSubtitleOutlineColor == "#102030"
    assert view_model.exportSubtitleOutlineWidth == 12
    assert view_model.exportSubtitleOpacityPercent == 0
    assert view_model.exportSubtitleAlignment == 3
    assert view_model.exportVideoFilterPreset == "cinematic-warm"
    assert view_model.exportVideoFilterIntensity == 65
    assert view_model.exportVideoBrightness == 50
    assert view_model.exportVideoContrast == 50
    assert view_model.exportVideoSaturation == 200

    view_model.resetExportSubtitleStyle()
    view_model.resetExportVideoFilter()

    assert view_model.exportSubtitlePrimaryColor == "#FFFFFF"
    assert view_model.exportSubtitleOutlineColor == "#000000"
    assert view_model.exportSubtitleOutlineWidth == 3
    assert view_model.exportSubtitleOpacityPercent == 100
    assert view_model.exportSubtitleAlignment == 2
    assert view_model.exportVideoFilterPreset == "none"
    assert view_model.exportVideoBrightness == 0
    assert view_model.exportVideoContrast == 100
    assert view_model.exportVideoSaturation == 100


def test_project_state_returns_translated_subtitle_at_the_playhead() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project-subtitle-preview",
            "name": "Preview phụ đề",
            "revision": 2,
            "source_cues": [
                {
                    "id": "cue-source-1",
                    "index": 1,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "你好。",
                }
            ],
            "translated_cues": [
                {
                    "id": "cue-vi-1",
                    "index": 1,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "Xin chào.",
                }
            ],
            "tracks": [],
        }
    )

    assert model.subtitleAt(999_999) == {}
    assert model.subtitleAt(1_500_000)["translated_text"] == "Xin chào."
    assert model.subtitleAt(2_000_000) == {}


def test_vietnamese_find_replace_is_optimistic_compact_and_rolls_back_on_failure(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-subtitle-edit",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "第一句话",
                    "language": "zh",
                }
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "Đây là chiếnhạm",
                    "language": "vi",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    assert view_model.previewSubtitleReplacement("hạm") == {
        "match_count": 1,
        "cue_count": 1,
    }
    view_model.findReplaceSubtitles("hạm", " hạm", 1)

    assert view_model.project_state.subtitle_cues.get(0)["source_text"] == "第一句话"
    assert view_model.project_state.subtitle_cues.get(0)["translated_text"] == "Đây là chiến hạm"
    assert requests[-1][0] == "subtitle_command"
    assert requests[-1][1]["language"] == "translated"
    assert requests[-1][1]["compact"] is True
    assert requests[-1][1]["values"]["replacement"] == " hạm"

    view_model._action_failed(
        "subtitle_command",
        {"message": "disk full"},
        requests[-1][2],
    )

    assert view_model.project_state.subtitle_cues.get(0)["translated_text"] == "Đây là chiếnhạm"
    assert "đã được hoàn nguyên" in view_model.statusMessage


def test_manual_cjk_edit_updates_multi_card_before_ipc_response(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-cjk-live",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                },
                {
                    "id": "source-2",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "第二句",
                },
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "Còn 第一",
                },
                {
                    "id": "translated-2",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "Còn 第二",
                },
            ],
        }
    )
    view_model._replace_multi_videos(
        {
            "batch": {"state": "completed_with_errors"},
            "items": [
                {
                    "project_id": "project-cjk-live",
                    "selected": True,
                    "stage": "translate",
                    "state": "failed",
                    "manual_review_available": True,
                    "error_code": "translation_review_required",
                    "error_payload": {"cjk_blocker_count": 2},
                }
            ],
        }
    )
    requests: list[str] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, *_args, **_kwargs: requests.append(action),
    )

    view_model.updateSubtitleText("translated-1", "Đã sửa", "translated")
    view_model._dispatch_background_subtitle_save()

    roles = {name: role for role, name in view_model.multi_videos.roleNames().items()}
    card = view_model.multi_videos.index(0, 0)
    assert requests == ["subtitle_command"]
    assert view_model.multi_videos.data(card, roles[b"statusText"]) == (
        "Còn chữ Trung · 1 cue · đã bỏ qua"
    )


def test_compact_subtitle_patch_updates_only_cues_and_revision() -> None:
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project-compact",
            "name": "Phim",
            "revision": 4,
            "tracks": [],
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "第一句话",
                    "language": "zh",
                }
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "Câu cũ",
                    "language": "vi",
                }
            ],
        }
    )

    model.apply_subtitle_patch(
        {
            "id": "project-compact",
            "revision": 5,
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "第一句话",
                    "language": "zh",
                }
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "Câu mới",
                    "language": "vi",
                }
            ],
        }
    )

    assert model.revision == 5
    assert model.subtitle_cues.get(0)["translated_text"] == "Câu mới"


def test_subtitle_multi_selection_uses_stable_cue_ids(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-subtitle-selection",
            "name": "Chọn cue",
            "revision": 1,
            "tracks": [],
            "source_cues": [
                {
                    "id": "dialogue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 500_000},
                    "text": "对话",
                    "role": "dialogue",
                    "subtitle_track_id": "S1",
                },
                {
                    "id": "scene-1",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "人物",
                    "role": "scene_text",
                    "subtitle_track_id": "S2",
                },
                {
                    "id": "scene-2",
                    "index": 3,
                    "range": {"start_us": 3_000_000, "end_us": 4_000_000},
                    "text": "法宝",
                    "role": "scene_text",
                    "subtitle_track_id": "S3",
                },
            ],
            "translated_cues": [],
        }
    )
    control = Qt.KeyboardModifier.ControlModifier.value

    view_model.selectCue("scene-1", 1_000_000)
    view_model.selectCueWithModifiers("scene-2", 3_000_000, control)

    assert view_model.selectedCueIds == ["scene-1", "scene-2"]
    assert view_model.selectedCueId == "scene-2"

    view_model.selectCueWithModifiers("scene-1", 1_000_000, control)

    assert view_model.selectedCueIds == ["scene-2"]
    assert view_model.selectedCueId == "scene-2"

    view_model.selectCueWithAdditive("scene-1", 1_000_000, False)

    assert view_model.selectedCueIds == ["scene-1"]
    assert view_model.selectedCueId == "scene-1"

    view_model.selectAllSubtitleCues()

    assert view_model.selectedCueIds == ["dialogue-1", "scene-1", "scene-2"]

    view_model.selectCueWithModifiers("scene-2", 3_000_000, 0)

    assert view_model.selectedCueIds == ["scene-2"]
    assert view_model.selectedCueId == "scene-2"


def test_timeline_cue_selection_does_not_seek_and_blank_click_can_clear(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-timeline-selection",
            "name": "Chọn cue Timeline",
            "revision": 1,
            "tracks": [],
            "source_cues": [
                {
                    "id": "scene-1",
                    "index": 1,
                    "range": {"start_us": 2_000_000, "end_us": 3_000_000},
                    "text": "人物",
                    "role": "scene_text",
                    "subtitle_track_id": "S2",
                }
            ],
            "translated_cues": [],
        }
    )
    view_model.setPlayheadMs(750)

    view_model.selectTimelineCue("scene-1", False)

    assert view_model.selectedCueIds == ["scene-1"]
    assert view_model.playheadUs == 750_000
    view_model.clearTimelineCueSelection()
    assert view_model.selectedCueIds == []
    assert view_model.playheadUs == 750_000


def test_manual_scene_text_waits_for_current_subtitle_save_then_dispatches(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-queued-manual-text",
            "name": "Thêm chữ nối tiếp",
            "revision": 1,
            "media": [
                {
                    "id": "video-1",
                    "path": "C:/fixtures/video.mp4",
                    "kind": "video",
                    "duration_us": 10_000_000,
                }
            ],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    view_model._set_subtitle_busy(True)

    view_model.addManualSceneText(
        "Tên pháp bảo",
        2_000_000,
        4_000_000,
        0.2,
        0.3,
        0.25,
        0.1,
        16,
        "horizontal",
        "#101010",
    )

    assert requests == []
    assert view_model.subtitleBusy is True

    view_model._action_failed(
        "subtitle_command",
        {"message": "Lệnh trước thất bại"},
        {"project_id": view_model.project_state.projectId, "optimistic_changes": []},
    )

    assert len(requests) == 1
    assert requests[0][0] == "subtitle_command"
    assert requests[0][1]["command"] == "add_subtitle_cue"
    assert requests[0][1]["values"]["text"] == "Tên pháp bảo"
    assert requests[0][1]["values"]["cue_role"] == "scene_text"
    assert view_model.subtitleBusy is True


def test_manual_dialogue_defaults_use_nearest_spoken_cue_and_send_full_role_payload(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    cue = {
        "id": "dialogue-near",
        "index": 1,
        "range": {"start_us": 8_000_000, "end_us": 9_000_000},
        "text": "原文",
        "language": "zh",
        "role": "dialogue",
        "subtitle_track_id": "S1",
        "visual_box": [0.08, 0.72, 0.84, 0.18],
        "visual_box_source": "estimated",
        "panel_box": [0.10, 0.74, 0.80, 0.14],
        "text_font_size": 26,
        "style_hint": "calligraphy_ink",
        "text_color": "#F1E4C8",
        "background_color": "#101010",
        "background_padding_x": 30,
        "background_padding_y": 14,
        "background_style": "ink_splash",
        "letter_spacing_percent": 7,
        "font_bold": True,
        "font_italic": False,
        "text_opacity_percent": 94,
        "background_opacity_percent": 88,
    }
    view_model.project_state.replace(
        {
            "id": "project-manual-dialogue",
            "name": "Thoại bị sót",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [cue],
            "translated_cues": [],
        }
    )
    defaults = view_model.manualSubtitleCueDefaults("dialogue", 8_200_000)
    assert defaults["visual_box"] == [0.1, 0.74, 0.8, 0.14]
    assert defaults["font_size"] == 26
    assert defaults["style_hint"] == "calligraphy_ink"
    assert defaults["text_color"] == "#F1E4C8"

    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    view_model.addManualSubtitleCue(
        "Câu thoại bị sót",
        8_200_000,
        9_200_000,
        defaults["visual_box"],
        defaults,
    )

    values = requests[0][1]["values"]
    assert requests[0][1]["command"] == "add_subtitle_cue"
    assert values["cue_role"] == "dialogue"
    assert values["font_size"] == 26
    assert values["background_style"] == "ink_splash"
    assert values["fade_enabled"] is False


def test_manual_scene_text_defaults_use_brush_white_on_opaque_black_ink(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()

    defaults = view_model.manualSubtitleCueDefaults("scene_text", 2_000_000)

    assert defaults["style_hint"] == "calligraphy_brush"
    assert defaults["background_style"] == "ink_splash"
    assert defaults["text_color"] == "#FFFFFF"
    assert defaults["background_color"] == "#000000"
    assert defaults["background_opacity_percent"] == 100
    assert defaults["fade_enabled"] is True
    assert defaults["fade_duration_ms"] == 300


def test_selected_scene_move_preview_is_not_compounded_and_rolls_back(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-scene-preview",
            "name": "Preview nhóm cue",
            "revision": 1,
            "tracks": [],
            "source_cues": [
                {
                    "id": "scene-1",
                    "index": 1,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "人物",
                    "role": "scene_text",
                    "subtitle_track_id": "S2",
                },
                {
                    "id": "scene-2",
                    "index": 2,
                    "range": {"start_us": 3_000_000, "end_us": 4_000_000},
                    "text": "法宝",
                    "role": "scene_text",
                    "subtitle_track_id": "S3",
                },
            ],
            "translated_cues": [],
        }
    )
    view_model.selectSubtitleCueIds(["scene-1", "scene-2"])

    view_model.previewSelectedSceneTextMove(500_000)
    view_model.previewSelectedSceneTextMove(600_000)

    assert view_model.project_state.subtitle_cues.find("scene-1")["start_us"] == 1_600_000
    assert view_model.project_state.subtitle_cues.find("scene-2")["start_us"] == 3_600_000

    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    view_model.moveSelectedSceneTexts(600_000, 1)

    assert requests[-1][0] == "subtitle_command"
    assert requests[-1][1]["values"] == {
        "cue_ids": ["scene-1", "scene-2"],
        "delta_us": 600_000,
        "lane_delta": 1,
        "display_time": True,
    }
    context = requests[-1][2]
    assert isinstance(context, dict)
    assert [item["previous_start_us"] for item in context["optimistic_changes"]] == [
        1_000_000,
        3_000_000,
    ]

    view_model._action_failed(
        "subtitle_command",
        {"message": "Không thể lưu"},
        context,
    )

    assert view_model.project_state.subtitle_cues.find("scene-1")["start_us"] == 1_000_000
    assert view_model.project_state.subtitle_cues.find("scene-2")["start_us"] == 3_000_000


def test_selected_scene_resize_previews_group_and_dispatches_one_command(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-scene-resize",
            "name": "Resize nhóm cue",
            "revision": 1,
            "tracks": [],
            "source_cues": [
                {
                    "id": "scene-1",
                    "index": 1,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "人物",
                    "role": "scene_text",
                    "subtitle_track_id": "S2",
                },
                {
                    "id": "scene-2",
                    "index": 2,
                    "range": {"start_us": 3_000_000, "end_us": 4_000_000},
                    "text": "法宝",
                    "role": "scene_text",
                    "subtitle_track_id": "S3",
                },
            ],
            "translated_cues": [],
        }
    )
    view_model.selectSubtitleCueIds(["scene-1", "scene-2"])

    view_model.previewSelectedSceneTextResize("right", 350_000)

    assert view_model.project_state.subtitle_cues.find("scene-1")["end_us"] == 2_350_000
    assert view_model.project_state.subtitle_cues.find("scene-2")["end_us"] == 4_350_000
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.resizeSelectedSceneTexts("right", 350_000)

    assert len(requests) == 1
    assert requests[0][1]["command"] == "resize_scene_texts"
    assert requests[0][1]["values"] == {
        "cue_ids": ["scene-1", "scene-2"],
        "edge": "right",
        "delta_us": 350_000,
        "display_time": True,
    }


def test_compact_subtitle_patch_moves_clip_without_leaving_it_in_old_lane() -> None:
    model = ProjectStateModel()
    source_cues = [
        {
            "id": "scene-1",
            "index": 1,
            "range": {"start_us": 0, "end_us": 1_000_000},
            "text": "人物",
            "role": "scene_text",
            "subtitle_track_id": "S2",
        },
        {
            "id": "scene-2",
            "index": 2,
            "range": {"start_us": 2_000_000, "end_us": 3_000_000},
            "text": "法宝",
            "role": "scene_text",
            "subtitle_track_id": "S2",
        },
    ]
    model.replace(
        {
            "id": "project-lane-patch",
            "name": "Chuyển lane",
            "revision": 1,
            "source_cues": source_cues,
            "translated_cues": [],
            "tracks": [
                {
                    "id": "S2",
                    "kind": "subtitle",
                    "name": "Chữ trong cảnh",
                    "clips": [
                        {
                            "id": "clip-1",
                            "track_id": "S2",
                            "timeline_range": {"start_us": 0, "end_us": 1_000_000},
                            "cue_id": "scene-1",
                            "cue_index": 1,
                        },
                        {
                            "id": "clip-2",
                            "track_id": "S2",
                            "timeline_range": {
                                "start_us": 2_000_000,
                                "end_us": 3_000_000,
                            },
                            "cue_id": "scene-2",
                            "cue_index": 2,
                        },
                    ],
                }
            ],
        }
    )
    moved_source = [
        {**source_cues[0], "subtitle_track_id": "S3"},
        source_cues[1],
    ]

    model.apply_subtitle_patch(
        {
            "_subtitle_patch": True,
            "id": "project-lane-patch",
            "revision": 2,
            "source_cues": moved_source,
            "translated_cues": [],
            "subtitle_clip_upserts": [
                {
                    "id": "clip-1",
                    "track_id": "S3",
                    "timeline_range": {"start_us": 0, "end_us": 1_000_000},
                    "cue_id": "scene-1",
                    "cue_index": 1,
                }
            ],
            "removed_subtitle_clip_ids": [],
            "subtitle_track_upserts": [{"id": "S3", "kind": "subtitle", "name": "Chữ trong cảnh"}],
            "removed_subtitle_track_ids": [],
        }
    )

    assert [clip["id"] for clip in model.track("S2")["clips"]] == ["clip-2"]
    assert [clip["id"] for clip in model.track("S3")["clips"]] == ["clip-1"]


def test_marquee_range_selects_only_intersecting_subtitle_lanes() -> None:
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project-marquee",
            "name": "Quét chọn",
            "revision": 1,
            "source_cues": [
                {
                    "id": "dialogue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "对白",
                    "role": "dialogue",
                    "subtitle_track_id": "S1",
                },
                {
                    "id": "scene-1",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "人物",
                    "role": "scene_text",
                    "subtitle_track_id": "S2",
                },
                {
                    "id": "scene-2",
                    "index": 3,
                    "range": {"start_us": 3_000_000, "end_us": 4_000_000},
                    "text": "法宝",
                    "role": "scene_text",
                    "subtitle_track_id": "S3",
                },
            ],
            "translated_cues": [],
            "tracks": [
                {"id": "S1", "kind": "subtitle", "name": "Thoại", "clips": []},
                {"id": "S2", "kind": "subtitle", "name": "Cảnh", "clips": []},
                {"id": "S3", "kind": "subtitle", "name": "Cảnh", "clips": []},
            ],
        }
    )

    scene_track = model.subtitleTrackIndexForCueId("scene-1")
    assert model.subtitleCueIdsInRange(900_000, 2_100_000, scene_track, scene_track) == ["scene-1"]
    assert model.subtitleCueIdsInRange(0, 5_000_000, scene_track, scene_track + 1) == [
        "scene-1",
        "scene-2",
    ]


def test_saved_subtitle_edit_requests_lightweight_multi_status_refresh(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-live-review",
            "name": "Phim",
            "revision": 4,
            "tracks": [],
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "第一句话",
                }
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "Câu cũ",
                }
            ],
        }
    )
    view_model._replace_multi_videos(
        {
            "batch": {"state": "completed_with_errors"},
            "items": [{"project_id": "project-live-review", "selected": True}],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model._action_succeeded(
        "subtitle_command",
        {
            "_subtitle_patch": True,
            "_subtitle_can_undo": True,
            "_subtitle_can_redo": False,
            "id": "project-live-review",
            "revision": 5,
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "第一句话",
                }
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "Đã sửa xong",
                }
            ],
        },
        {"subtitle_command": "edit", "project_id": "project-live-review"},
    )

    assert requests == [
        (
            "multi_status",
            {},
            {
                "multi_refresh_after_subtitle": True,
                "project_id": "project-live-review",
            },
        )
    ]


def test_invalid_compact_subtitle_patch_releases_busy_state(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-patch-error",
            "name": "Phim",
            "revision": 4,
            "source_cues": [],
            "translated_cues": [],
        }
    )
    view_model._set_subtitle_busy(True)

    view_model._action_succeeded(
        "subtitle_command",
        {
            "_subtitle_patch": True,
            "id": "another-project",
            "revision": 5,
            "source_cues": [],
            "translated_cues": [],
        },
        {"project_id": "project-patch-error", "optimistic_changes": []},
    )

    assert view_model.subtitleBusy is False
    assert "Không thể lưu thay đổi phụ đề" in view_model.statusMessage


def test_manual_vietnamese_edit_clears_stale_cjk_qa_immediately_and_restores_on_error(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-cjk-edit",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "我的朋友",
                    "language": "zh",
                }
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "Đây là 朋友 của tôi",
                    "language": "vi",
                }
            ],
        }
    )
    view_model._set_translation_qa(
        {
            "status": "REVIEW",
            "checked_cues": 1,
            "cue_total": 1,
            "blocking_count": 1,
            "issues": [
                {
                    "id": 1,
                    "cue_id": "source-1",
                    "code": "cjk_remaining",
                    "message": "Bản dịch còn chữ Hán/CJK.",
                    "translated_text": "Đây là 朋友 của tôi",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.updateSubtitleText("translated-1", "Đây là bạn của tôi", "translated")
    view_model._dispatch_background_subtitle_save()

    assert view_model.translationQa["status"] == "PASS"
    assert view_model.translationQa["issues"] == []
    assert view_model.project_state.subtitle_cues.get(0)["qa_issue_code"] == ""

    view_model._set_translation_qa(
        {
            "status": "REVIEW",
            "checked_cues": 1,
            "cue_total": 1,
            "blocking_count": 1,
            "issues": [
                {
                    "id": 1,
                    "cue_id": "source-1",
                    "code": "cjk_remaining",
                    "message": "Bản dịch còn chữ Hán/CJK.",
                    "translated_text": "Đây là 朋友 của tôi",
                }
            ],
        }
    )
    assert view_model.translationQa["issues"] == []

    view_model._action_failed(
        "subtitle_command",
        {"message": "disk full"},
        requests[-1][2],
    )

    assert view_model.translationQa["status"] == "REVIEW"
    assert view_model.translationQa["issues"][0]["code"] == "cjk_remaining"
    assert view_model.project_state.subtitle_cues.get(0)["translated_text"] == "Đây là 朋友 của tôi"


def test_navigation_feedback_stays_inside_budget_and_preserves_job_models(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    model_ids = (
        id(view_model.jobModel),
        id(view_model.subtitleJobModel),
        id(view_model.voiceJobModel),
        id(view_model.exportJobModel),
    )

    samples = []
    for _iteration in range(20):
        for workspace in (
            "dashboard",
            "transcribe",
            "translate",
            "subtitles",
            "voice",
            "timeline",
            "export",
        ):
            started = time.perf_counter()
            view_model.navigate(workspace)
            samples.append(time.perf_counter() - started)

    assert max(samples) < 0.1
    assert model_ids == (
        id(view_model.jobModel),
        id(view_model.subtitleJobModel),
        id(view_model.voiceJobModel),
        id(view_model.exportJobModel),
    )
    assert ApplicationViewModel._screen_index("transcribe") == 2
    assert ApplicationViewModel._screen_index("translate") == 2


def test_every_navigation_workspace_routes_to_an_intentional_screen(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()

    expected_indexes = {
        "dashboard": 0,
        "projects": 1,
        "transcribe": 2,
        "translate": 2,
        "subtitles": 2,
        "voice": 3,
        "multi": 4,
        "timeline": 5,
        "export": 6,
        "queue": 6,
        "settings": 7,
    }

    for workspace, expected_index in expected_indexes.items():
        view_model.navigate(workspace)
        assert view_model.currentWorkspace == workspace
        assert view_model.currentIndex == expected_index
        assert "sẽ được nối" not in view_model.statusMessage

    for workspace in ("effects", "mixer"):
        view_model.navigate(workspace)
        assert view_model.currentWorkspace == "timeline"
        assert view_model.currentIndex == 5


def test_opening_multi_refreshes_catalog_on_the_first_click(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {"ui/currentWorkspace": "dashboard"}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._backend_ready = True
    requests: list[str] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, _payload=None, context=None: requests.append(action),
    )

    view_model.navigate("multi")

    assert view_model.currentWorkspace == "multi"
    assert requests == ["multi_list"]


def test_import_path_accepts_percent_encoded_unicode_file_url(monkeypatch, tmp_path: Path) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    media = tmp_path / "Những người tu luyện bất tử nhìn tôi.mp4"
    media.write_bytes(b"fixture")
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload, context=None: requests.append((action, payload, context)),
    )

    view_model.importPath(QUrl.fromLocalFile(str(media)).toString())

    assert requests
    action, payload, context = requests[0]
    assert action == "inspect_media_usage"
    assert payload["media_path"] == str(media.resolve())
    view_model._action_succeeded(
        action,
        {"media_path": str(media.resolve()), "exists": False, "usages": []},
        context,
    )
    action, payload, context = requests[1]
    assert action == "create_project"
    assert payload["name"] == media.stem
    assert context["media_path"] == str(media.resolve())


def test_successful_media_import_opens_updated_timeline_without_reload(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.navigate("projects")
    model._set_import_busy(True)
    requests = []
    model.runtime.request = lambda action, params=None, context=None: requests.append(action)
    interval = {"start_us": 0, "end_us": 10_000_000}
    snapshot = {"id": "imported", "name": "Imported", "revision": 2,
                "media": [{"id": "media", "path": "E:/fixture.mp4", "kind": "video"}],
                "tracks": [{"id": "V1", "name": "Video", "kind": "video", "clips": [
                    {"id": "video", "track_id": "V1", "media_id": "media",
                     "timeline_range": interval, "source_range": interval}]}]}
    model._action_succeeded("import_media", snapshot, {"media_path": "E:/fixture.mp4"})
    assert model.currentWorkspace == "timeline"
    assert model.project_state.projectId == "imported"
    assert model.project_state.clip_info("video") is not None
    assert not model.importBusy
    assert "get_project" not in requests


def test_failed_media_import_keeps_current_workspace_and_project(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({"id": "existing", "name": "Existing", "revision": 1})
    model.navigate("projects")
    model._set_import_busy(True)
    model._action_failed("import_media", {"message": "Cannot read media"}, {})
    assert model.currentWorkspace == "projects"
    assert model.project_state.projectId == "existing"
    assert not model.importBusy


def test_received_snapshot_ownership_avoids_recopy_but_regular_replace_is_isolated(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    interval = {"start_us": 0, "end_us": 1_000_000}
    snapshot = {"id": "owned", "name": "Owned", "revision": 1,
                "source_cues": [{"id": "cue", "index": 1, "text": "字幕",
                                 "range": interval}]}
    preserved = deepcopy(snapshot)
    regular = ProjectStateModel()
    regular.replace(snapshot)
    assert regular._canonical_snapshot is not snapshot
    model = ApplicationViewModel()
    model._apply_project(snapshot)
    assert model.project_state._canonical_snapshot is snapshot
    assert snapshot == preserved
    # Preview edits are not allowed to mutate either the received canonical
    # head or another model which took its own defensive copy.
    model.project_state.subtitle_cues.preview_scene_text_timing("cue", 100_000, 900_000)
    assert snapshot == preserved
    assert regular._canonical_snapshot == preserved


def test_create_project_from_media_never_appends_to_the_open_project(
    monkeypatch, tmp_path: Path
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    media = tmp_path / "Phim mới có dấu.mp4"
    media.write_bytes(b"fixture")
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "already-open", "name": "Dự án đang mở", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload, context=None: requests.append((action, payload, context)),
    )

    view_model.createProjectFromPath(QUrl.fromLocalFile(str(media)).toString())

    assert requests
    action, payload, context = requests[0]
    assert action == "inspect_media_usage"
    view_model._action_succeeded(
        action,
        {"media_path": str(media.resolve()), "exists": False, "usages": []},
        context,
    )
    action, payload, context = requests[1]
    assert action == "create_project"
    assert payload["name"] == media.stem
    assert context["media_path"] == str(media.resolve())
    assert all(request[0] != "import_media" for request in requests)


def test_duplicate_media_requires_an_explicit_non_destructive_choice(
    monkeypatch, tmp_path: Path
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    media = tmp_path / "Phim đã nhập.mp4"
    media.write_bytes(b"fixture")
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    requests: list[tuple[str, dict, object]] = []
    prompts: list[bool] = []
    view_model.duplicateMediaDialogRequested.connect(lambda: prompts.append(True))
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.createProjectFromPath(QUrl.fromLocalFile(str(media)).toString())
    inspect_action, _payload, inspect_context = requests[-1]
    view_model._action_succeeded(
        inspect_action,
        {
            "media_path": str(media.resolve()),
            "exists": True,
            "usages": [
                {
                    "project_id": "project-old",
                    "project_name": "Bản dựng cũ",
                    "project_path": str(tmp_path / "old.ducai"),
                    "updated_at": "2026-08-25T00:00:00Z",
                    "media_count": 1,
                    "media_kinds": ["video"],
                }
            ],
        },
        inspect_context,
    )

    assert prompts == [True]
    assert view_model.importBusy is True
    assert view_model.duplicateMediaInfo["suggested_project_name"] == f"{media.stem} (2)"
    assert [request[0] for request in requests] == ["inspect_media_usage"]

    view_model.cancelDuplicateMediaResolution()

    assert view_model.importBusy is False
    assert view_model.duplicateMediaInfo == {}
    assert [request[0] for request in requests] == ["inspect_media_usage"]
    assert "không thay đổi" in view_model.statusMessage


def test_duplicate_media_can_open_existing_or_create_a_renamed_copy(
    monkeypatch, tmp_path: Path
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    media = tmp_path / "Phim trùng.mp4"
    media.write_bytes(b"fixture")
    usage = {
        "project_id": "project-old",
        "project_name": "Bản dựng cũ",
        "project_path": str(tmp_path / "old.ducai"),
        "updated_at": "2026-08-25T00:00:00Z",
        "media_count": 1,
        "media_kinds": ["video"],
    }

    def prepare() -> tuple[ApplicationViewModel, list[tuple[str, dict, object]]]:
        model = ApplicationViewModel()
        model._set_backend_ready(True)
        captured: list[tuple[str, dict, object]] = []
        monkeypatch.setattr(
            model.runtime,
            "request",
            lambda action, payload=None, context=None: captured.append(
                (action, payload or {}, context)
            ),
        )
        model.createProjectFromPath(QUrl.fromLocalFile(str(media)).toString())
        action, _payload, context = captured[-1]
        model._action_succeeded(
            action,
            {"media_path": str(media.resolve()), "exists": True, "usages": [usage]},
            context,
        )
        return model, captured

    open_model, open_requests = prepare()
    open_model.resolveDuplicateMedia("use_existing", "")
    assert open_requests[-1] == ("get_project", {"project_id": "project-old"}, None)

    copy_model, copy_requests = prepare()
    copy_model.resolveDuplicateMedia("create_copy", "Bản dựng mới")
    action, payload, context = copy_requests[-1]
    assert action == "create_project"
    assert payload["name"] == "Bản dựng mới"
    assert context["media_path"] == str(media.resolve())


def test_open_project_requests_the_selected_real_project(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload, context=None: requests.append((action, payload, context)),
    )

    view_model.openProject("project-unicode")

    assert requests == [("get_project", {"project_id": "project-unicode"}, None)]
    assert view_model.statusMessage == "Đang mở dự án"


def test_start_ocr_rejects_an_out_of_range_fps_before_dispatch(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-ocr", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.startOcr(9.0)

    assert requests == []
    assert view_model.ocrBusy is False
    assert view_model.statusMessage == "FPS OCR phải nằm trong khoảng 0,67–2,5"


def test_export_subtitle_srt_dispatches_current_project_and_releases_busy_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-export", "name": "Phim", "revision": 8})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    output = tmp_path / "edited.srt"

    view_model.exportSubtitleSrt("translated", QUrl.fromLocalFile(str(output)).toString())

    assert requests == [
        (
            "export_subtitle_srt",
            {
                "project_id": "project-export",
                "language": "translated",
                "output_path": str(output.resolve()),
            },
            {"language": "translated"},
        )
    ]
    assert view_model.subtitleExportBusy is True
    view_model._action_succeeded(
        "export_subtitle_srt",
        {
            "path": str(output),
            "cue_count": 12,
            "language": "translated",
        },
        {},
    )
    assert view_model.subtitleExportBusy is False
    assert view_model.statusMessage == f"Đã xuất 12 cue SRT tiếng Việt: {output}"


def test_export_subtitle_srt_failure_is_non_mutating_and_releases_busy(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._subtitle_export_busy = True

    view_model._action_failed(
        "export_subtitle_srt",
        {"message": "ổ đĩa chỉ đọc"},
        {},
    )

    assert view_model.subtitleExportBusy is False
    assert view_model.statusMessage == (
        "Không thể xuất SRT; dự án và cache không bị thay đổi: ổ đĩa chỉ đọc"
    )


def test_toggle_ocr_dispatches_start_pause_and_resume_from_current_state(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-ocr", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.toggleOcr(1.25)
    assert requests[-1][0] == "create_ocr_job"
    assert view_model.ocrJobState == "start_requested"

    view_model._ocr_job_id = "ocr-running"
    view_model._set_ocr_busy(True)
    view_model._set_ocr_job_state("running")
    view_model.toggleOcr(1.25)
    assert requests[-1] == (
        "pause_job",
        {"job_id": "ocr-running"},
        {"job_kind": "ocr"},
    )
    assert view_model.ocrJobState == "pause_requested"

    view_model._set_ocr_job_state("paused")
    view_model.toggleOcr(1.25)
    assert requests[-1] == (
        "resume_job",
        {"job_id": "ocr-running"},
        {"job_kind": "ocr"},
    )
    assert view_model.ocrJobState == "resume_requested"


def test_failed_ocr_is_retained_and_retried_from_its_checkpoint(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    failed = {
        **_job("ocr-failed", "ocr"),
        "state": "failed",
        "error_code": "ocr_review_required",
    }
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    view_model._replace_jobs([failed])
    requests.clear()

    view_model.resumeOcr()

    assert view_model.ocrJobId == "ocr-failed"
    assert view_model.ocrJobState == "resume_requested"
    assert view_model.ocrBusy is False
    assert requests == [
        (
            "retry_job",
            {"job_id": "ocr-failed"},
            {"resume_ocr": True, "job_id": "ocr-failed"},
        )
    ]


def test_job_controls_publish_requested_state_before_manager_poll(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    view_model._replace_jobs([_job("translate-1", "translate")])

    view_model.pauseTranslation()

    assert view_model.translationJobState == "pause_requested"
    assert requests[-1][0] == "pause_job"

    view_model._replace_jobs([{**_job("translate-1", "translate"), "state": "paused"}])
    view_model.resumeTranslation()

    assert view_model.translationJobState == "resume_requested"
    assert requests[-1][0] == "resume_job"


def test_cancelled_translation_resumes_same_job_from_ledger_cache(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
        }
    )
    view_model._translation_job_id = "translate-cancelled"
    view_model._translation_job_state = "cancelled"
    view_model._translation_provider_id = "gemini-web"
    view_model._translation_model_id = "gemini-web-selected"
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.startTranslation("gemini-web", "gemini-web-selected", guest=True)

    assert view_model.translationBusy is True
    assert view_model.translationJobState == "resume_requested"
    assert [action for action, _payload, _context in requests] == ["retry_job"]
    assert requests[0][1]["job_id"] == "translate-cancelled"
    assert requests[0][2] == {"resume_translation": True, "job_id": "translate-cancelled"}
    assert requests[0][1]["provider_options"]["guest"] is True


def test_toggle_translation_pauses_running_and_resumes_cancelled_cache(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._translation_job_id = "translate-cache"
    view_model._translation_provider_id = "gemini-web"
    view_model._translation_model_id = "selected-on-web"
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model._translation_job_state = "running"
    view_model.toggleTranslation("gemini-web", "selected-on-web", guest=True)
    assert view_model.translationJobState == "pause_requested"
    assert requests[-1][0] == "pause_job"

    view_model._translation_job_state = "cancelled"
    view_model._set_translation_busy(False)
    view_model.toggleTranslation("gemini-web", "selected-on-web", guest=True)
    assert view_model.translationJobState == "resume_requested"
    assert requests[-1][0] == "retry_job"
    assert requests[-1][1]["job_id"] == "translate-cache"


def test_translation_job_state_and_api_references_survive_navigation(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {
        "translation/geminiApiCredentialReferences": ["project-b", "project-a"],
    }
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})

    view_model._replace_jobs([_job("translate-1", "translate")])
    view_model.navigate("voice")
    view_model.navigate("translate")

    assert view_model.translationBusy is True
    assert view_model.translationJobId == "translate-1"
    assert view_model.translationJobState == "running"
    assert view_model.geminiApiCredentialReferences == ["project-b", "project-a"]


def test_startup_keeps_provider_pools_scoped_and_does_not_claim_orphaned_vault_names(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {
        "translation/geminiApiCredentialReferences": ["gemini-1"],
        "translation/openai-compatible/credentialReferences": ["seekai-main"],
    }
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()

    view_model._action_succeeded(
        "list_credentials",
        {"references": ["tqboyvn", "gemini-1", "seekai-main"]},
        {},
    )

    assert view_model.geminiApiCredentialReferences == ["gemini-1"]
    assert view_model.openaiCompatibleCredentialReferences == ["seekai-main"]


@pytest.mark.parametrize("stopped_state", ["failed", "cancelled", "paused", "queued"])
def test_failed_translation_with_different_provider_starts_a_new_job(
    monkeypatch, stopped_state,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {
        "translation/geminiApiCredentialReferences": ["gemini-1"],
    }
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "start_us": 0,
                    "end_us": 1_000_000,
                    "text": "Ni hao",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
        }
    )
    view_model._translation_job_id = "failed-web"
    view_model._translation_job_state = stopped_state
    view_model._translation_provider_id = "gemini-web"
    view_model._translation_model_id = "selected-on-web"
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.toggleTranslation("gemini-api", "gemini-3.5-flash-lite")

    if stopped_state in {"paused", "queued"}:
        assert requests[0][0] == "cancel_job"
        assert not any(action == "resume_job" for action, _, _ in requests)
        view_model._replace_jobs([{
            "id": "failed-web", "state": "cancelled", "spec": {
                "kind": "translate", "project_id": "project-1", "provider_id": "gemini-web",
                "model_id": "selected-on-web",
            },
        }])
        requests[:] = [r for r in requests if r[0] == "create_translation_job"]
    assert requests[0][0] == "create_translation_job"
    assert requests[0][1]["provider_id"] == "gemini-api"
    assert requests[0][1]["model_id"] == "gemini-3.5-flash-lite"


@pytest.mark.parametrize("state", ["paused", "queued", "cancelled", "failed"])
@pytest.mark.parametrize("change", ["preset", "rules"])
def test_changed_translation_style_replaces_stale_job(monkeypatch, state, change) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {"translation/geminiApiCredentialReferences": ["gemini-1"]}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model._set_backend_ready(True)
    model.project_state.replace({
        "id": "project-1", "name": "Phim", "revision": 2,
        "source_cues": [{"id": "cue-1", "index": 1, "text": "你好",
                         "start_us": 0, "end_us": 1_000_000}],
        "translation_preferences": {
            "preset": "xianxia" if change == "preset" else "auto",
            "custom_rules": "Giữ nguyên nghĩa." if change == "rules" else "",
        },
    })
    old_job = {
        "id": "old-style", "state": state, "spec": {
            "kind": "translate", "project_id": "project-1", "provider_id": "gemini-api",
            "model_id": "same-model", "provider_config": {
                "translation_style_preset": "auto", "translation_custom_rules": "",
                "credential_references": ["gemini-1"],
            },
        },
    }
    requests = []
    monkeypatch.setattr(model.runtime, "request", lambda action, payload=None, context=None:
                        requests.append((action, payload or {}, context)))
    model._replace_jobs([old_job])
    requests.clear()
    assert not model.translationSelectionMatches("gemini-api", "same-model", "")
    model.toggleTranslation("gemini-api", "same-model")
    if state in {"paused", "queued"}:
        assert [r[0] for r in requests] == ["cancel_job"]
        model._replace_jobs([{**old_job, "state": "cancelled"}])
        model._replace_jobs([{**old_job, "state": "cancelled"}])
    assert len([r for r in requests if r[0] == "create_translation_job"]) == 1
    assert not any(r[0] in {"resume_job", "retry_job"} for r in requests)


def test_translation_draft_is_saved_before_continue_and_failed_save_does_not_start(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model._set_backend_ready(True)
    model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    requests = []
    monkeypatch.setattr(model.runtime, "request", lambda action, payload=None, context=None:
                        requests.append((action, payload or {}, context)))
    controls = []
    monkeypatch.setattr(model, "toggleTranslation", lambda *args: controls.append(args))
    arguments = ("gemini-api", "new-model", False, False, "", "", "xianxia", "Quy tắc mới")
    model.toggleTranslationWithStyle(*arguments)
    assert model.translationPreferencesBusy
    assert [r[0] for r in requests] == ["set_translation_preferences"]
    assert controls == []
    model._translation_preferences_saved("set_translation_preferences", {
        "id": "project-1", "revision": 2,
        "translation_preferences": {"preset": "xianxia", "custom_rules": "Quy tắc mới"},
    }, {"project_id": "project-1"})
    assert controls == [arguments[:6]]
    assert model.project_state.translationCustomRules == "Quy tắc mới"
    model.toggleTranslationWithStyle(*arguments[:6], "neutral", "Khác")
    model._action_failed("set_translation_preferences", {"message": "save failed"}, {})
    assert controls == [arguments[:6]]
    assert not model.translationPreferencesBusy


def test_failed_api_translation_stores_new_key_then_retries_same_cache(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {
        "translation/geminiApiCredentialReferences": ["gemini-1"],
    }
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "start_us": 0,
                    "end_us": 1_000_000,
                    "text": "Ni hao",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
        }
    )
    view_model._translation_job_id = "failed-api"
    view_model._translation_job_state = "failed"
    view_model._translation_provider_id = "gemini-api"
    view_model._translation_model_id = "gemini-3.5-flash-lite"
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.startTranslation(
        "gemini-api",
        "gemini-3.5-flash-lite",
        credential_input="fresh=temporary-secret",
    )
    assert requests[0][0] == "store_credentials"

    view_model._action_succeeded(
        "store_credentials",
        {"references": ["fresh"], "stored": 1},
        requests[0][2],
    )

    assert requests[-1] == (
        "retry_job",
        {"job_id": "failed-api", "credential_references": ["fresh", "gemini-1"]},
        {"resume_translation": True, "job_id": "failed-api"},
    )


def test_failed_api_translation_resume_refreshes_pool_for_existing_job(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {
        "translation/geminiApiCredentialReferences": ["gemini-1", "tqboyvn"],
    }
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._translation_job_id = "translate-existing"
    view_model._translation_job_state = "failed"
    view_model._translation_provider_id = "gemini-api"
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.resumeTranslation()

    assert requests[0][0] == "retry_job"
    assert requests[0][1] == {
        "job_id": "translate-existing",
        "credential_references": ["gemini-1", "tqboyvn"],
    }


def test_review_translation_can_retry_same_gemini_web_job_from_cache(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    reviewed = _job("translate-review", "translate")
    reviewed["state"] = "failed"
    reviewed["error_code"] = "translation_review_required"
    reviewed["spec"]["provider_id"] = "gemini-web"
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    view_model._replace_jobs([reviewed])
    requests.clear()

    assert view_model.translationJobState == "review"
    view_model.resumeTranslation(headed=True, guest=True)

    assert requests == [
        (
            "retry_job",
            {
                "job_id": "translate-review",
                "provider_options": view_model._gemini_web_options(headed=True, guest=True),
            },
            {"resume_translation": True, "job_id": "translate-review"},
        )
    ]


def test_external_translation_import_dispatches_local_srt_to_current_project(
    monkeypatch, tmp_path: Path
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    translated = tmp_path / "bản dịch ngoài.srt"
    translated.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nBản dịch.\n",
        encoding="utf-8",
    )
    view_model = ApplicationViewModel()
    view_model._backend_ready = True
    view_model.project_state.replace(
        {
            "id": "project-translation-import",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "原文。",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.importTranslationPath(QUrl.fromLocalFile(str(translated)).toString())

    assert requests == [
        (
            "import_translation_srt",
            {
                "project_id": "project-translation-import",
                "subtitle_path": str(translated.resolve()),
            },
            None,
        )
    ]


def test_bulk_api_key_parser_preserves_named_order_and_generates_stable_names() -> None:
    parsed = application_module._parse_gemini_api_credentials(
        "project-a=secret-a\nsecret-b\nproject-c = secret-c",
        existing_references=("gemini-1",),
    )

    assert [(item["reference"], item["secret"]) for item in parsed] == [
        ("project-a", "secret-a"),
        ("gemini-2", "secret-b"),
        ("project-c", "secret-c"),
    ]


def test_bulk_api_key_parser_rejects_duplicate_names_without_echoing_keys() -> None:
    try:
        application_module._parse_gemini_api_credentials(
            "same=first-secret\nsame=second-secret",
            existing_references=(),
        )
    except ValueError as exc:
        assert "same" in str(exc)
        assert "first-secret" not in str(exc)
        assert "second-secret" not in str(exc)
    else:
        raise AssertionError("Duplicate references must be rejected")


def test_bulk_api_key_parser_rejects_secret_shaped_reference_without_echoing_it() -> None:
    leaked_reference = "AIza" + "A" * 34

    try:
        application_module._parse_gemini_api_credentials(f"{leaked_reference}=stored-secret")
    except ValueError as exc:
        assert leaked_reference not in str(exc)
    else:
        raise AssertionError("Secret-shaped credential reference must be rejected")


def test_api_start_saves_pasted_pool_then_dispatches_once_without_a_second_click(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.startTranslation(
        "gemini-api",
        "gemini-3.5-flash-lite",
        False,
        False,
        "first=secret-a;second=secret-b",
    )

    assert [action for action, _payload, _context in requests] == ["store_credentials"]
    assert "secret-a" not in repr(requests[0][2])
    view_model._action_succeeded(
        "store_credentials",
        {"references": ["first", "second"], "stored": 2},
        requests[0][2],
    )

    assert [action for action, _payload, _context in requests] == [
        "store_credentials",
        "create_translation_job",
    ]
    provider_options = requests[1][1]["provider_options"]
    assert provider_options["credential_references"] == ["first", "second"]
    assert view_model.geminiApiCredentialReferences == ["first", "second"]


def test_provider_specific_api_key_pool_does_not_mix_gemini_and_deepseek(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()

    view_model._action_succeeded(
        "store_credentials",
        {"references": ["deepseek-1"], "stored": 1},
        {"credential_provider_id": "deepseek-api", "start_translation": False},
    )

    assert view_model.deepseekApiCredentialReferences == ["deepseek-1"]
    assert view_model.geminiApiCredentialReferences == []


def test_job_history_model_hides_rows_until_a_project_is_selected() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = JobListModel(accepted_kinds=frozenset({"ocr", "translate"}))
    model.replace(
        [
            {**_job("ocr-a", "ocr"), "spec": {"kind": "ocr", "project_id": "a"}},
            {**_job("ocr-b", "ocr"), "spec": {"kind": "ocr", "project_id": "b"}},
        ]
    )
    assert model.count == 2
    model.setProjectFilter("a")
    assert model.count == 1
    model.setProjectFilter("")
    assert model.count == 0


def test_history_delete_and_clear_report_busy_state_and_refresh(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-history", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.deleteJobHistory("job-finished")
    assert view_model.jobHistoryBusy is True
    assert requests[-1][0] == "delete_job_history"
    view_model._action_succeeded("delete_job_history", {}, requests[-1][2])
    assert view_model.jobHistoryBusy is False
    assert requests[-1][0] == "list_jobs"

    view_model.clearSubtitleJobHistory()
    assert view_model.jobHistoryBusy is True
    assert requests[-1][0] == "clear_project_job_history"
    clear_context = requests[-1][2]
    view_model._action_succeeded("clear_project_job_history", {"deleted_jobs": 2}, clear_context)
    assert view_model.jobHistoryBusy is False
    assert "Đã xoá 2" in view_model.statusMessage


def test_cancelled_cache_selection_binds_primary_toggle_to_the_exact_job(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-cache", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    caches = [
        {
            "job_id": "cache-full",
            "label": "13.385/13.385 cue · Cần rà soát",
            "completed_cues": 13_385,
            "cue_total": 13_385,
            "state": "failed",
            "error_code": "translation_review_required",
            "provider_id": "gemini-web",
            "model_id": "selected-on-web",
        },
        {
            "job_id": "cache-cancelled",
            "label": "434/13.385 cue · Đã hủy",
            "completed_cues": 434,
            "cue_total": 13_385,
            "state": "cancelled",
            "display_state": "cancelled",
            "error_code": "",
            "provider_id": "gemini-web",
            "model_id": "selected-on-web",
        },
    ]

    view_model._action_succeeded("list_translation_caches", caches, {})
    assert view_model.selectedTranslationCacheId == "cache-full"
    # The selected terminal ledger is actionable before the async preview
    # response arrives; this is what enables "Tiếp tục dịch từ cache".
    assert view_model.translationJobId == "cache-full"
    assert view_model.translationJobState == "failed"
    assert view_model.translationBusy is False
    assert requests[-1][:2] == (
        "get_translation_preview",
        {"project_id": "project-cache", "job_id": "cache-full"},
    )

    view_model.selectTranslationCache("cache-cancelled")
    assert view_model.selectedTranslationCacheId == "cache-cancelled"
    assert view_model.translationJobId == "cache-cancelled"
    assert view_model.translationJobState == "cancelled"
    assert requests[-1][1]["job_id"] == "cache-cancelled"

    requests.clear()
    view_model.toggleTranslation("gemini-web", "selected-on-web", guest=True)

    assert requests[0][0] == "retry_job"
    assert requests[0][1]["job_id"] == "cache-cancelled"
    assert requests[0][2] == {
        "resume_translation": True,
        "job_id": "cache-cancelled",
    }


def test_interrupted_voice_conform_retries_the_same_cache_job(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-conform", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model._action_succeeded(
        "create_voice_conform_job",
        {"id": "conform-cache", "state": "interrupted"},
        {"voice_conform_create": True},
    )

    assert view_model.voiceConformJobState == "resume_requested"
    assert view_model.voiceConformBusy is True
    assert requests[-1] == (
        "retry_job",
        {"job_id": "conform-cache"},
        {
            "job_kind": "tts",
            "voice_conform": True,
            "resume_voice_conform": True,
            "job_id": "conform-cache",
        },
    )

    view_model._action_succeeded("retry_job", {}, requests[-1][2])
    assert view_model.voiceConformJobState == "start_requested"
    assert requests[-1][0] == "start_job"
    assert requests[-1][1]["job_id"] == "conform-cache"


def test_translation_cache_listing_binds_default_terminal_cache_before_preview(monkeypatch) -> None:
    """A stale busy flag must not disable retry for the auto-selected cache."""
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-cache", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    # Simulate cancellation reconciliation leaving the previous operation busy
    # while a fresh cache listing arrives.
    view_model._set_translation_busy(True)
    caches = [
        {
            "job_id": "cache-default-failed",
            "state": "failed",
            "display_state": "failed",
            "provider_id": "gemini-web",
            "model_id": "selected-on-web",
            "completed_cues": 3,
            "cue_total": 4,
        }
    ]

    view_model._action_succeeded("list_translation_caches", caches, {})

    assert view_model.selectedTranslationCacheId == "cache-default-failed"
    assert view_model.translationJobId == "cache-default-failed"
    assert view_model.translationJobState == "failed"
    assert view_model.translationBusy is False
    assert requests[-1][:2] == (
        "get_translation_preview",
        {"project_id": "project-cache", "job_id": "cache-default-failed"},
    )

    requests.clear()
    view_model.toggleTranslation("gemini-web", "selected-on-web", guest=True)
    assert requests[0][0] == "retry_job"
    assert requests[0][1]["job_id"] == "cache-default-failed"


@pytest.mark.parametrize("state", ["start_requested", "running", "paused"])
def test_delayed_cache_listing_cannot_select_an_older_translation(monkeypatch, state) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    requests = []
    monkeypatch.setattr(model.runtime, "request", lambda action, payload=None, context=None:
                        requests.append((action, payload or {}, context)))
    model._translation_job_id = "current-job" if state != "start_requested" else ""
    model._set_translation_job_state(state)
    model._set_translation_busy(True)
    model._action_succeeded("list_translation_caches", [{
        "job_id": "older-job", "state": "interrupted", "provider_id": "gemini-web",
        "model_id": "old-model", "completed_cues": 626,
    }], {"project_id": "project-1"})
    assert model.translationJobId == ("current-job" if state != "start_requested" else "")
    assert model.translationJobState == state
    assert model.selectedTranslationCacheId == model.translationJobId
    assert model.translationBusy
    assert not any(request[0] == "get_translation_preview" for request in requests)


@pytest.mark.parametrize("event", ["preview", "delta", "qa"])
@pytest.mark.parametrize("starting", [False, True])
def test_older_translation_results_cannot_overwrite_current_preview(monkeypatch, event, starting):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({
        "id": "project-1", "name": "Phim", "revision": 1,
        "source_cues": [{"id": "cue-1", "index": 1, "text": "你好",
                         "range": {"start_us": 0, "end_us": 1_000_000}}],
    })
    model._translation_job_id = "" if starting else "current-job"
    model._set_translation_job_state("start_requested" if starting else "running")
    model._set_translation_busy(True)
    model.project_state.subtitle_cues.apply_translation_delta(
        [{"id": "cue-1", "index": 1, "text": "Bản hiện tại"}])
    model._set_translation_qa({"status": "RUNNING", "checked_cues": 10})
    current_qa = model.translationQa
    # The old response was requested before a new translation began. It can
    # arrive after the selector was cleared or after the new job was bound.
    values = [{"id": "cue-1", "index": 1, "text": "Bản lỗi thời"}]
    qa = {"status": "PASS", "checked_cues": 626}
    if event == "preview":
        model._action_succeeded("get_translation_preview", {
            "project_id": "project-1", "job_id": "older-job", "values": values, "qa": qa,
        }, {"project_id": "project-1", "cache_job_id": "older-job"})
    else:
        kind = "translation_delta" if event == "delta" else "translation_qa_status"
        model._action_succeeded("list_events", [{
            "sequence": 100, "kind": kind,
            "payload": {"values": values} if event == "delta" else qa,
        }], {"project_id": "project-1", "job_id": "older-job",
             "translation_deltas" if event == "delta" else "translation_qa_status": True})
    assert model.project_state.subtitle_cues.get(0)["translated_text"] == "Bản hiện tại"
    assert model.translationQa == current_qa
    assert model._translation_event_sequences.get("older-job", 0) == 0
    assert model._translation_qa_event_sequences.get("older-job", 0) == 0


@pytest.mark.parametrize("event", ["start", "preview", "qa"])
def test_new_translation_qa_does_not_count_previous_preview_rows(monkeypatch, event):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({
        "id": "project-1", "name": "Phim", "revision": 1,
        "source_cues": [
            {"id": "cue-1", "index": 1, "text": "你好",
             "range": {"start_us": 0, "end_us": 1_000_000}},
            {"id": "cue-2", "index": 2, "text": "再见",
             "range": {"start_us": 1_000_000, "end_us": 2_000_000}},
        ],
    })
    model.project_state.subtitle_cues.apply_translation_delta([
        {"id": "cue-1", "index": 1, "text": "Bản dịch cũ"},
        {"id": "cue-2", "index": 2, "text": "Bản cũ còn 你"},
    ])
    monkeypatch.setattr(model.runtime, "request", lambda *args, **kwargs: None)
    if event == "start":
        model._begin_translation("gemini-web", "new-model", False, False)
    else:
        model._translation_job_id = "current-job"
        model._selected_translation_cache_id = "current-job"
        model._set_translation_job_state("paused")
        model._set_translation_busy(True)
        qa = {"status": "PENDING", "checked_cues": 0,
              "completed_cues": 0, "cue_total": 2, "issues": []}
        if event == "preview":
            model._action_succeeded("get_translation_preview", {
                "project_id": "project-1", "job_id": "current-job", "qa": qa,
                "values": [],
            }, {"project_id": "project-1", "cache_job_id": "current-job"})
        else:
            model._action_succeeded("list_events", [{
                "sequence": 50, "kind": "translation_qa_status", "payload": qa,
            }], {"project_id": "project-1", "job_id": "current-job",
                 "translation_qa_status": True})
    assert model.translationQa["status"] == "PENDING"
    assert model.translationQa["checked_cues"] == 0
    assert model.translationQa["completed_cues"] == 0
    assert model.translationQa["cue_total"] == 2
    assert model.translationQa["issues"] == []
    assert model.project_state.subtitle_cues.get(0)["translated_text"] == "Bản dịch cũ"


def test_created_and_discovered_translation_owns_its_cache_selection(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    monkeypatch.setattr(model.runtime, "request", lambda *args, **kwargs: None)
    model._selected_translation_cache_id = "older-job"
    model._action_succeeded("create_translation_job", _job("created-job", "translate"), {})
    assert model.selectedTranslationCacheId == "created-job"
    model._selected_translation_cache_id = "older-job"
    model._replace_jobs([_job("discovered-job", "translate")])
    assert model.selectedTranslationCacheId == "discovered-job"


def test_new_terminal_translation_refreshes_all_cache_choices(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-cache", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    failed = _job("cache-new-failed", "translate")
    failed["spec"]["project_id"] = "project-cache"
    failed["state"] = "failed"
    failed["finished_at"] = "2026-08-28T01:00:00Z"
    view_model._replace_jobs([failed])

    assert any(action == "list_translation_caches" for action, _payload, _context in requests)


def test_project_delete_exposes_name_immediately_and_clears_on_failure(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.deleteProject("project-delete", "Dự án thử")
    assert view_model.projectDeleteBusy is True
    assert view_model.deletingProjectName == "Dự án thử"
    assert view_model.statusMessage == "Đang xoá: Dự án thử"
    view_model._action_failed("delete_project", {"message": "bị khóa"}, requests[-1][2])
    assert view_model.projectDeleteBusy is False
    assert view_model.deletingProjectName == ""


def test_live_translation_delta_patches_only_matching_vietnamese_rows() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = [
        {
            "id": f"cue-{index}",
            "index": index,
            "range": {"start_us": index * 1_000_000, "end_us": (index + 1) * 1_000_000},
            "text": f"第{index}句",
            "source": "paddle",
            "revision": 1,
        }
        for index in range(1, 4)
    ]
    model.replace(source, [])

    changed = model.apply_translation_delta(
        [
            {"id": "cue-1", "index": 1, "text": "Câu thứ nhất."},
            {"id": "cue-3", "index": 3, "text": "Câu thứ ba."},
        ]
    )

    assert changed == 2
    assert model.get(0)["translated_text"] == "Câu thứ nhất."
    assert model.get(0)["status"] == "Đang dịch"
    assert model.get(1)["translated_text"] == ""
    assert model.get(2)["translated_text"] == "Câu thứ ba."


def test_replayed_translation_delta_does_not_reset_live_cache_state() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = [
        {
            "id": "cue-1",
            "index": 1,
            "range": {"start_us": 0, "end_us": 1_000_000},
            "text": "第一句",
            "source": "paddle",
            "revision": 1,
        }
    ]
    model.replace(source, [])
    values = [{"id": "cue-1", "index": 1, "text": "Câu thứ nhất."}]

    assert model.apply_translation_delta(values) == 1
    assert model.get(0)["status"] == "Đang dịch"
    assert model.apply_translation_delta(values) == 0
    assert model.get(0)["status"] == "Đang dịch"

    model.finalize_translation_preview()
    assert model.get(0)["status"] == "Đã lưu cache"
    assert model.apply_translation_delta(values) == 0
    assert model.get(0)["status"] == "Đã lưu cache"


def test_overlay_geometry_settles_only_after_latest_queued_save(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    settled: list[tuple[str, bool]] = []
    view_model.overlayGeometrySettled.connect(
        lambda overlay_id, saved: settled.append((overlay_id, saved))
    )

    def snapshot(x: float) -> dict:
        return {
            "id": "project-overlay",
            "name": "Overlay",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [
                {
                    "id": "overlay-default-logo",
                    "kind": "logo",
                    "enabled": True,
                    "x": x,
                    "y": 0.1,
                    "width": 0.2,
                    "height": 0.1,
                }
            ],
        }

    view_model._apply_project(snapshot(0.1))
    view_model.setOverlayGeometry("overlay-default-logo", 0.2, 0.1, 0.2, 0.1)
    view_model.setOverlayGeometry("overlay-default-logo", 0.3, 0.1, 0.2, 0.1)
    first = next(item for item in requests if item[0] == "effect_command")

    view_model._action_succeeded("effect_command", snapshot(0.2), first[2])

    effects = [item for item in requests if item[0] == "effect_command"]
    assert len(effects) == 2
    assert effects[-1][1]["values"]["x"] == 0.3
    assert settled == []

    view_model._action_succeeded("effect_command", snapshot(0.3), effects[-1][2])

    assert settled == [("overlay-default-logo", True)]
    assert MemorySettings.values["effects/logo/x"] == 0.3


def test_compact_effect_patch_updates_stable_overlay_model_without_project_reset(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    project = {
        "id": "project-effect-patch",
        "name": "Effect patch",
        "revision": 1,
        "media": [],
        "tracks": [],
        "source_cues": [],
        "translated_cues": [],
        "overlays": [
            {
                "id": "overlay-default-logo",
                "kind": "logo",
                "range": {"start_us": 0, "end_us": 1_000_000},
                "enabled": True,
                "x": 0.1,
                "y": 0.1,
                "width": 0.2,
                "height": 0.1,
            }
        ],
    }
    view_model._apply_project(project)
    overlay_model = view_model.project_state.overlay_model
    changed: list[object] = []
    overlay_model.dataChanged.connect(lambda *_args: changed.append(object()))
    context = {
        "command": "update",
        "overlay_id": "overlay-default-logo",
        "geometry": True,
        "values": {"x": 0.35, "y": 0.1, "width": 0.2, "height": 0.1},
        "generation_key": "overlay-default-logo",
        "generation": 1,
    }

    view_model._action_succeeded(
        "effect_command",
        {
            "_effect_patch": True,
            "id": "project-effect-patch",
            "revision": 2,
            "overlay_upserts": [
                {
                    **project["overlays"][0],
                    "x": 0.35,
                }
            ],
            "removed_overlay_ids": [],
            "media_upserts": [],
            "removed_media_ids": [],
        },
        context,
    )

    assert view_model.project_state.overlay_model is overlay_model
    assert view_model.project_state.overlay("overlay-default-logo")["x"] == 0.35
    assert view_model.project_state.revision == 2
    assert len(changed) == 1


def test_effect_controls_update_immediately_and_roll_back_on_failure(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._apply_project(
        {
            "id": "project-optimistic-effect",
            "name": "Optimistic effect",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [
                {
                    "id": "overlay-default-blur",
                    "kind": "blur",
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "enabled": True,
                    "x": 0.1,
                    "y": 0.7,
                    "width": 0.8,
                    "height": 0.15,
                    "blur_radius": 22,
                    "tracking_mode": "fixed",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.setOverlayEnabled("overlay-default-blur", False)

    assert view_model.project_state.overlay("overlay-default-blur")["enabled"] is False
    assert "effects/blur/enabled" not in MemorySettings.values
    request = next(item for item in requests if item[0] == "effect_command")

    view_model._action_failed(
        "effect_command",
        {"message": "write failed"},
        request[2],
    )

    assert view_model.project_state.overlay("overlay-default-blur")["enabled"] is True
    assert "effects/blur/enabled" not in MemorySettings.values


def test_overlay_range_is_sent_in_final_display_time(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._apply_project(
        {
            "id": "project-overlay-range",
            "name": "Overlay range",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [
                {
                    "id": "overlay-1",
                    "kind": "blur",
                    "range": {"start_us": 0, "end_us": 1_000_000},
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.setOverlayRange("overlay-1", 1.25, 3.5)

    assert requests[-1][0] == "effect_command"
    assert requests[-1][1]["values"] == {
        "start_us": 1_250_000,
        "end_us": 3_500_000,
        "display_time": True,
    }


def test_source_blur_cover_toggle_is_immediate_and_new_defaults_are_strong(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._apply_project(
        {
            "id": "project-cover-effect",
            "name": "Cover effect",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [
                {
                    "id": "overlay-default-blur",
                    "kind": "blur",
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "enabled": True,
                    "x": 0.1,
                    "y": 0.7,
                    "width": 0.8,
                    "height": 0.15,
                    "blur_radius": 22,
                    "tracking_mode": "source_subtitle",
                    "concealment_mode": "blur",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.setOverlayConcealmentMode("overlay-default-blur", "cover")

    assert view_model.project_state.overlay("overlay-default-blur")["concealment_mode"] == "cover"
    assert requests[-1][1]["values"] == {"concealment_mode": "cover"}
    defaults = view_model._default_overlay_import_options()["overlay_preset"]["blur"]
    assert defaults["blur_radius"] == 46
    assert defaults["padding_x"] == pytest.approx(0.02)
    assert defaults["padding_y"] == pytest.approx(0.014)
    assert defaults["concealment_mode"] == "cover"


def test_add_blur_is_visible_immediately_and_removed_if_save_fails(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._apply_project(
        {
            "id": "project-optimistic-add",
            "name": "Optimistic add",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    selected: list[str] = []
    view_model.overlaySelectionRequested.connect(selected.append)

    view_model.addBlurRegion()

    overlay_id = selected[-1]
    assert view_model.project_state.overlay(overlay_id)["kind"] == "blur"
    request = next(item for item in requests if item[0] == "effect_command")

    view_model._action_failed(
        "effect_command",
        {"message": "write failed"},
        request[2],
    )

    assert view_model.project_state.overlay(overlay_id) == {}


def test_two_failed_optimistic_effect_updates_restore_last_confirmed_state(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._apply_project(
        {
            "id": "project-double-effect-failure",
            "name": "Double failure",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [
                {
                    "id": "overlay-default-blur",
                    "kind": "blur",
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "enabled": True,
                    "x": 0.1,
                    "y": 0.7,
                    "width": 0.8,
                    "height": 0.15,
                    "blur_radius": 22,
                    "tracking_mode": "fixed",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.setOverlayEnabled("overlay-default-blur", False)
    view_model.setOverlayBlurRadius("overlay-default-blur", 30)
    first = [item for item in requests if item[0] == "effect_command"][0]

    view_model._action_failed("effect_command", {"message": "first failed"}, first[2])

    optimistic = view_model.project_state.overlay("overlay-default-blur")
    assert optimistic["enabled"] is False
    assert optimistic["blur_radius"] == 30
    second = [item for item in requests if item[0] == "effect_command"][1]

    view_model._action_failed("effect_command", {"message": "second failed"}, second[2])

    restored = view_model.project_state.overlay("overlay-default-blur")
    assert restored["enabled"] is True
    assert restored["blur_radius"] == 22


def test_stale_effect_patch_cannot_move_overlay_backwards(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._apply_project(
        {
            "id": "project-stale-effect",
            "name": "Stale effect",
            "revision": 3,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [
                {
                    "id": "logo",
                    "kind": "logo",
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "enabled": True,
                    "x": 0.4,
                    "y": 0.1,
                    "width": 0.2,
                    "height": 0.1,
                }
            ],
        }
    )
    view_model._effect_generations["logo"] = 2

    view_model._action_succeeded(
        "effect_command",
        {
            "_effect_patch": True,
            "id": "project-stale-effect",
            "revision": 2,
            "overlay_upserts": [
                {
                    "id": "logo",
                    "kind": "logo",
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "enabled": True,
                    "x": 0.2,
                    "y": 0.1,
                    "width": 0.2,
                    "height": 0.1,
                }
            ],
            "removed_overlay_ids": [],
            "media_upserts": [],
            "removed_media_ids": [],
        },
        {
            "command": "update",
            "overlay_id": "logo",
            "geometry": True,
            "generation_key": "logo",
            "generation": 1,
        },
    )

    assert view_model.project_state.overlay("logo")["x"] == 0.4
    assert view_model.project_state.revision == 3


def test_overlay_geometry_queues_each_distinct_overlay_without_dropping(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    settled: list[tuple[str, bool]] = []
    view_model.overlayGeometrySettled.connect(
        lambda overlay_id, saved: settled.append((overlay_id, saved))
    )

    def snapshot() -> dict:
        return {
            "id": "project-multi-overlay",
            "name": "Overlay",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [
                {"id": "overlay-a", "kind": "blur", "enabled": True},
                {"id": "overlay-b", "kind": "blur", "enabled": True},
                {"id": "overlay-c", "kind": "blur", "enabled": True},
            ],
        }

    view_model._apply_project(snapshot())
    view_model.setOverlayGeometry("overlay-a", 0.1, 0.1, 0.2, 0.2)
    view_model.setOverlayGeometry("overlay-b", 0.2, 0.2, 0.2, 0.2)
    view_model.setOverlayGeometry("overlay-c", 0.3, 0.3, 0.2, 0.2)
    effects = [item for item in requests if item[0] == "effect_command"]
    assert [item[1]["overlay_id"] for item in effects] == ["overlay-a"]

    view_model._action_succeeded("effect_command", snapshot(), effects[-1][2])
    effects = [item for item in requests if item[0] == "effect_command"]
    assert [item[1]["overlay_id"] for item in effects] == ["overlay-a", "overlay-b"]
    assert settled == [("overlay-a", True)]

    view_model._action_succeeded("effect_command", snapshot(), effects[-1][2])
    effects = [item for item in requests if item[0] == "effect_command"]
    assert [item[1]["overlay_id"] for item in effects] == [
        "overlay-a",
        "overlay-b",
        "overlay-c",
    ]
    assert settled == [("overlay-a", True), ("overlay-b", True)]

    view_model._action_succeeded("effect_command", snapshot(), effects[-1][2])
    assert settled == [
        ("overlay-a", True),
        ("overlay-b", True),
        ("overlay-c", True),
    ]
    assert not view_model._pending_effect_commands
    assert "effects/logo/x" not in MemorySettings.values


def test_logo_asset_waits_for_inflight_geometry_and_is_not_dropped(
    monkeypatch, tmp_path: Path
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    project = {
        "id": "project-logo-queue",
        "name": "Logo queue",
        "revision": 1,
        "media": [],
        "tracks": [],
        "source_cues": [],
        "translated_cues": [],
        "overlays": [
            {
                "id": "overlay-default-logo",
                "kind": "logo",
                "enabled": False,
                "x": 0.8,
                "y": 0.04,
                "width": 0.15,
                "height": 0.08,
            }
        ],
    }
    view_model._apply_project(project)
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    selected: list[str] = []
    view_model.overlaySelectionRequested.connect(selected.append)
    logo_path = tmp_path / "logo.png"
    logo_path.write_bytes(b"png")

    view_model.setOverlayGeometry("overlay-default-logo", 0.7, 0.04, 0.2, 0.1)
    view_model.setLogoPath(str(logo_path))

    effects = [item for item in requests if item[0] == "effect_command"]
    assert [item[1]["command"] for item in effects] == ["update"]
    assert selected == ["overlay-default-logo"]

    view_model._action_succeeded("effect_command", project, effects[0][2])

    effects = [item for item in requests if item[0] == "effect_command"]
    assert [item[1]["command"] for item in effects] == ["update", "set_logo_asset"]
    assert effects[-1][1]["values"]["path"] == str(logo_path)


def test_project_effect_snapshot_preserves_live_translation_and_rehydrates_preview(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    source_cue = {
        "id": "cue-1",
        "index": 1,
        "range": {"start_us": 0, "end_us": 1_000_000},
        "text": "第一句",
        "source": "paddle",
        "revision": 1,
    }
    project = {
        "id": "project-live-translation",
        "name": "Live translation",
        "revision": 1,
        "media": [],
        "tracks": [],
        "source_cues": [source_cue],
        "translated_cues": [],
        "overlays": [],
    }
    view_model._apply_project(project)
    view_model.project_state.subtitle_cues.apply_translation_delta(
        [{"id": "cue-1", "index": 1, "text": "Câu thứ nhất."}]
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model._action_succeeded(
        "effect_command",
        {**project, "revision": 2},
        {"command": "update", "overlay_id": "overlay-default-logo", "geometry": False},
    )

    row = view_model.project_state.subtitle_cues.get(0)
    assert row["translated_text"] == "Câu thứ nhất."
    assert row["status"] == "Đang dịch"
    assert not [item for item in requests if item[0] == "list_translation_caches"]
    assert not [item for item in requests if item[0] == "list_projects"]


def test_failed_overlay_geometry_rolls_back_without_saving_preset(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._apply_project(
        {
            "id": "project-overlay",
            "name": "Overlay",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    settled: list[tuple[str, bool]] = []
    view_model.overlayGeometrySettled.connect(
        lambda overlay_id, saved: settled.append((overlay_id, saved))
    )
    view_model.setOverlayGeometry("overlay-default-logo", 0.4, 0.1, 0.2, 0.1)
    effect = next(item for item in requests if item[0] == "effect_command")

    view_model._action_failed("effect_command", {"message": "write failed"}, effect[2])

    assert settled == [("overlay-default-logo", False)]
    assert view_model.timelineBusy is False
    assert "effects/logo/x" not in MemorySettings.values


def test_terminal_translation_preview_marks_checkpointed_rows_as_cached() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = [
        {
            "id": "cue-1",
            "index": 1,
            "range": {"start_us": 0, "end_us": 1_000_000},
            "text": "第一句",
            "source": "paddle",
            "revision": 1,
        }
    ]
    model.replace(source, [])
    model.apply_translation_delta([{"id": "cue-1", "index": 1, "text": "Đã dịch và checkpoint."}])

    changed = model.finalize_translation_preview()

    assert changed == 1
    assert model.get(0)["translated_text"] == "Đã dịch và checkpoint."
    assert model.get(0)["status"] == "Đã lưu cache"


def test_open_project_requests_and_applies_checkpoint_translation_preview(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    project = {
        "id": "project-1",
        "name": "Phim",
        "revision": 1,
        "source_cues": [
            {
                "id": "cue-1",
                "index": 1,
                "range": {"start_us": 0, "end_us": 1_000_000},
                "text": "第一句",
                "source": "paddle",
                "revision": 1,
            }
        ],
    }

    view_model._action_succeeded("get_project", project, {})
    cache_request = next(item for item in requests if item[0] == "list_translation_caches")
    view_model._action_succeeded(
        "list_translation_caches",
        [{"job_id": "translation-1", "completed_cues": 1, "cue_total": 1}],
        cache_request[2],
    )
    preview_request = next(item for item in requests if item[0] == "get_translation_preview")
    view_model._action_succeeded(
        "get_translation_preview",
        {
            "project_id": "project-1",
            "job_id": "translation-1",
            "values": [{"id": "cue-1", "index": 1, "text": "Câu thứ nhất."}],
        },
        preview_request[2],
    )

    assert view_model.project_state.subtitle_cues.get(0)["translated_text"] == "Câu thứ nhất."
    assert view_model.project_state.subtitle_cues.get(0)["status"] == "Đã lưu cache"


def test_late_translation_delta_cannot_overwrite_terminal_project_rows() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = [
        {
            "id": "cue-1",
            "index": 1,
            "range": {"start_us": 0, "end_us": 1_000_000},
            "text": "第一句",
            "source": "paddle",
            "revision": 1,
        }
    ]
    translated = [
        {
            "id": "translated-1",
            "index": 1,
            "range": {"start_us": 0, "end_us": 1_000_000},
            "text": "Bản dịch đã qua QA.",
            "source": "gemini",
            "revision": 1,
        }
    ]
    model.replace(source, translated)

    changed = model.apply_translation_delta([{"id": "cue-1", "index": 1, "text": "Checkpoint cũ."}])

    assert changed == 0
    assert model.get(0)["translated_text"] == "Bản dịch đã qua QA."
    assert model.get(0)["status"] == "Đã dịch"


def test_view_model_polls_only_unseen_live_translation_events(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model._replace_jobs([_job("translate-1", "translate")])

    assert requests[0][0] == "list_events"
    assert requests[0][1] == {
        "job_id": "translate-1",
        "after_sequence": 0,
        "kind": "translation_delta",
    }
    context = requests[0][2]
    view_model._action_succeeded(
        "list_events",
        [
            {
                "sequence": 42,
                "kind": "translation_delta",
                "payload": {"values": [{"id": "cue-1", "index": 1, "text": "Câu thứ nhất."}]},
            }
        ],
        context,
    )
    view_model._replace_jobs([_job("translate-1", "translate")])

    assert view_model.project_state.subtitle_cues.get(0)["translated_text"] == "Câu thứ nhất."
    assert requests[-1][1]["after_sequence"] == 42


def test_view_model_applies_live_translation_qa_and_navigates_to_failing_cue(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                },
                {
                    "id": "cue-2",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "第二句",
                    "source": "paddle",
                    "revision": 1,
                },
            ],
        }
    )
    view_model._action_succeeded(
        "list_events",
        [
            {
                "sequence": 7,
                "kind": "translation_qa_status",
                "payload": {
                    "status": "REVIEW",
                    "completed_cues": 2,
                    "checked_cues": 2,
                    "cue_total": 2,
                    "blocking_count": 1,
                    "blocking_issue_count": 1,
                    "warning_count": 0,
                    "issues": [
                        {
                            "id": 2,
                            "cue_id": "cue-2",
                            "code": "cjk_remaining",
                            "message": "Còn chữ Hán",
                            "suggestion": "Sửa câu này",
                        }
                    ],
                },
            }
        ],
        {
            "translation_qa_status": True,
            "job_id": "translate-1",
            "project_id": "project-1",
        },
    )

    assert view_model.translationQa["status"] == "REVIEW"
    assert view_model.translationQa["issues"][0]["id"] == 2
    view_model.selectTranslationQaIssue(2)
    assert view_model.selectedCueId == "cue-2"
    assert view_model.playheadUs == 1_000_000


def test_voice_timing_review_opens_translation_and_selects_first_long_cue(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {"voice/autoCondenseLongCuesEnabled": False}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                },
                {
                    "id": "cue-2",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "第二句",
                    "source": "paddle",
                    "revision": 1,
                },
            ],
        }
    )

    view_model._action_succeeded(
        "get_voice_timing_review",
        {
            "status": "REVIEW",
            "job_id": "tts-review",
            "project_id": "project-1",
            "issue_count": 1,
            "repairable": True,
            "message": "Có 1 cue dài.",
            "issues": [
                {
                    "id": 2,
                    "cue_id": "cue-2",
                    "code": "CUE_TOO_LONG",
                    "message": "Cue dài.",
                }
            ],
        },
        {"job_id": "tts-review"},
    )

    assert view_model.currentWorkspace == "translate"
    assert view_model.selectedCueId == "cue-2"
    assert view_model.playheadUs == 1_000_000
    assert view_model.voiceTimingQa["issues"][0]["code"] == "CUE_TOO_LONG"
    assert view_model.statusMessage == "Có 1 cue dài."


def test_open_voice_cue_in_editor_seeks_and_navigates_to_zero_based_row(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                },
                {
                    "id": "cue-2",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "第二句",
                    "source": "paddle",
                    "revision": 1,
                },
            ],
        }
    )
    rows: list[int] = []
    view_model.translationQaNavigationRequested.connect(rows.append)

    view_model.openCueInEditor("cue-2", 1_000_000, 2)

    assert view_model.currentWorkspace == "translate"
    assert view_model.selectedCueId == "cue-2"
    assert view_model.playheadUs == 1_000_000
    assert rows == [1]


def test_voice_timing_issue_navigation_emits_zero_based_row(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                },
                {
                    "id": "cue-2",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "第二句",
                    "source": "paddle",
                    "revision": 1,
                },
            ],
        }
    )
    rows: list[int] = []
    view_model.translationQaNavigationRequested.connect(rows.append)

    view_model.selectVoiceConformIssue(2)

    assert view_model.currentWorkspace == "translate"
    assert view_model.selectedCueId == "cue-2"
    assert rows == [1]


def test_terminal_statuses_emit_readable_notifications_without_progress_spam(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    notifications: list[tuple[str, str, int]] = []
    view_model.statusNotificationRequested.connect(
        lambda message, kind, duration: notifications.append((message, kind, duration))
    )

    view_model._set_status("Đang tiếp tục từ cache")
    view_model._set_status("Không thể tạo lại voice từ cache: tts_review_required")
    view_model._set_status("Đã thêm voice vào Timeline")

    assert notifications == [
        (
            "Không thể tạo lại voice từ cache: tts_review_required",
            "error",
            10_000,
        ),
        ("Đã thêm voice vào Timeline", "success", 10_000),
    ]


def test_polled_terminal_job_failure_emits_code_reason_and_cache_guidance(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    notifications: list[tuple[str, str, int]] = []
    view_model.statusNotificationRequested.connect(
        lambda message, kind, duration: notifications.append((message, kind, duration))
    )
    queued = {
        "id": "tts-job-1234",
        "state": "running",
        "spec": {
            "kind": "tts",
            "project_id": "project-1",
            "provider_config": {"purpose": "production"},
        },
    }
    failed = {
        **queued,
        "state": "failed",
        "finished_at": "2026-08-28T10:00:00Z",
        "error_code": "worker_exit_without_ack",
        "error_payload": {"message": "worker stopped before acknowledging the checkpoint"},
    }

    view_model._replace_jobs([queued])
    view_model._replace_jobs([failed])

    assert len(notifications) == 1
    message, kind, duration = notifications[0]
    assert kind == "error"
    assert duration == 10_000
    assert "Tạo giọng thất bại" in message
    assert "worker_exit_without_ack" not in message
    assert "worker stopped" in message
    assert "Cache/checkpoint vẫn được giữ lại" in message


def test_polled_tts_replacement_suppresses_old_cancel_banner(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 2})
    notifications: list[tuple[str, str, int]] = []
    dismissals: list[bool] = []
    view_model.statusNotificationRequested.connect(
        lambda message, kind, duration: notifications.append((message, kind, duration))
    )
    view_model.statusNotificationDismissRequested.connect(lambda: dismissals.append(True))
    old = {
        "id": "tts-old",
        "state": "running",
        "spec": {
            "kind": "tts",
            "project_id": "project-1",
            "provider_config": {"purpose": "production"},
        },
    }
    replacement = {
        "id": "tts-new",
        "state": "queued",
        "spec": {
            "kind": "tts",
            "project_id": "project-1",
            "provider_config": {
                "purpose": "production",
                "replaces_job_id": "tts-old",
            },
        },
    }

    view_model._replace_jobs([old])
    view_model._replace_jobs([replacement, {**old, "state": "cancelled", "finished_at": "now"}])

    assert view_model.ttsJobId == "tts-new"
    assert "tts-old" in view_model._superseded_job_ids
    assert dismissals == [True]
    assert notifications == []


def test_polled_tts_overlap_failure_is_actionable_and_hides_internal_code(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    notifications: list[tuple[str, str, int]] = []
    view_model.statusNotificationRequested.connect(
        lambda message, kind, duration: notifications.append((message, kind, duration))
    )
    queued = {
        "id": "tts-job-overlap",
        "state": "running",
        "spec": {
            "kind": "tts",
            "project_id": "project-1",
            "provider_config": {"purpose": "production"},
        },
    }
    failed = {
        **queued,
        "state": "failed",
        "finished_at": "2026-08-28T10:00:00Z",
        "error_code": "tts_timeline_overlap_guard_failed",
        "error_payload": {},
    }

    view_model._replace_jobs([queued])
    view_model._replace_jobs([failed])

    assert len(notifications) == 1
    message, kind, duration = notifications[0]
    assert kind == "error"
    assert duration == 10_000
    assert "tts_timeline_overlap_guard_failed" not in message
    assert "chồng lên nhau" in message
    assert "Bỏ qua cảnh báo" in message
    assert "Cache/checkpoint vẫn được giữ lại" in message


def test_action_error_localizes_known_worker_code() -> None:
    message = ApplicationViewModel._format_action_error(
        {"code": "tts_timeline_overlap_guard_failed"}
    )

    assert "tts_timeline_overlap_guard_failed" not in message
    assert "chồng lên nhau" in message


def test_qa_issue_labels_are_vietnamese_and_hide_internal_identifiers() -> None:
    assert (
        ApplicationViewModel._localized_issue_label("NAME_INCONSISTENCY, CUE_TOO_LONG")
        == "Tên nhân vật không nhất quán / Cue dài hơn khung thời gian"
    )
    assert "NAME_INCONSISTENCY" not in ApplicationViewModel._localized_issue_label(
        "NAME_INCONSISTENCY"
    )
    assert ApplicationViewModel._localized_issue_label("unknown_internal_code") == (
        "Lỗi kiểm tra bản dịch"
    )
    fallback_message = ApplicationViewModel._format_action_error(
        {"message": "unknown_internal_code"}
    )
    assert "unknown_internal_code" not in fallback_message
    assert "Tác vụ không thể hoàn tất" in fallback_message


def test_polled_interrupted_job_is_a_readable_warning(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    monkeypatch.setattr(view_model.runtime, "request", lambda *args, **kwargs: "request")
    notifications: list[tuple[str, str, int]] = []
    view_model.statusNotificationRequested.connect(
        lambda message, kind, duration: notifications.append((message, kind, duration))
    )
    running = {
        "id": "translate-job-1234",
        "state": "running",
        "spec": {
            "kind": "translate",
            "project_id": "project-1",
            "provider_config": {},
        },
    }
    interrupted = {
        **running,
        "state": "interrupted",
        "finished_at": "2026-08-28T10:00:00Z",
        "error_payload": {"reason": "Job Manager was closed safely"},
    }

    view_model._replace_jobs([running])
    view_model._replace_jobs([interrupted])

    assert len(notifications) == 1
    message, kind, duration = notifications[0]
    assert kind == "warning"
    assert duration == 8_000
    assert "Dịch thuật bị gián đoạn" in message
    assert "tiếp tục từ cache" in message


def test_create_job_failure_does_not_reference_unbound_job_kind(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._action_failed(
        "create_tts_job",
        {"message": "engine unavailable", "code": "tts_engine_unavailable"},
        {},
    )

    assert view_model.ttsBusy is False
    assert "Tạo giọng chưa thể tiếp tục" in view_model.statusMessage
    assert "tts_engine_unavailable" not in view_model.statusMessage


def test_voice_generation_waits_for_latest_edit_revision_to_be_saved(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model._set_backend_ready(True)
    model._apply_project({"id": "voice-edits", "name": "Edited", "revision": 2,
                          "_project_dirty": True, "_saved_revision": 1})
    requests = []
    starts = []
    monkeypatch.setattr(model.runtime, "request",
                        lambda a, p=None, context=None: requests.append((a, p, context)))
    monkeypatch.setattr(model, "_start_voice_job",
                        lambda purpose: starts.append((purpose, model.project_state.revision)))
    model.startVoiceGeneration()
    assert requests[-1][0] == "save_project_edits" and not starts
    first_context = requests[-1][2]
    model._apply_project({"id": "voice-edits", "name": "Edited", "revision": 3,
                          "_project_dirty": True, "_saved_revision": 1})
    model._action_succeeded("save_project_edits", {"id": "voice-edits", "revision": 2,
        "_saved_revision": 2, "_project_dirty": False}, first_context)
    QCoreApplication.processEvents()
    assert not starts and model.projectDirty
    assert len(requests) == 2 and requests[-1][0] == "save_project_edits"
    model._action_succeeded("save_project_edits", {"id": "voice-edits", "revision": 3,
        "_saved_revision": 3, "_project_dirty": False}, requests[-1][2])
    QCoreApplication.processEvents()
    assert starts == [("production", 3)]
    assert not model.projectDirty


def test_manual_voice_rerun_releases_playback_and_removes_d1_before_job_creation(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model._selected_voice_id = "fixture-voice"
    monkeypatch.setattr(
        view_model.voice_catalog,
        "find",
        lambda _voice_id: {
            "voice_id": "fixture-voice",
            "provider_id": "piper",
            "installed": True,
        },
    )
    project = {
        "id": "project-1",
        "name": "Phim",
        "revision": 4,
        "media": [
            {
                "id": "voice-media",
                "path": "C:/Phim/voice.wav",
                "kind": "audio",
                "duration_us": 1_000_000,
            }
        ],
        "tracks": [
            {
                "id": "D1",
                "kind": "voice",
                "name": "Thuyết minh",
                "clips": [
                    {
                        "id": "voice-1",
                        "track_id": "D1",
                        "media_id": "voice-media",
                        "source_range": {"start_us": 0, "end_us": 1_000_000},
                        "timeline_range": {"start_us": 0, "end_us": 1_000_000},
                        "speed": 1.0,
                        "volume": 1.0,
                    }
                ],
            }
        ],
    }
    view_model.project_state.replace(project)
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    playback_states: list[bool] = []
    view_model.voicePlaybackEnabledChanged.connect(
        lambda: playback_states.append(view_model.voicePlaybackEnabled)
    )

    view_model.startVoiceGeneration()

    assert requests == [
        (
            "prepare_tts_rerun",
            {"project_id": "project-1"},
            {"tts_purpose": "production"},
        )
    ]
    assert view_model.voicePlaybackEnabled is False
    assert view_model.ttsBusy is True

    prepared = {**project, "revision": 5, "media": [], "tracks": []}
    view_model._action_succeeded(
        "prepare_tts_rerun",
        prepared,
        {"tts_purpose": "production"},
    )

    assert requests[-1][0] == "create_tts_job"
    assert requests[-1][1]["project_id"] == "project-1"
    assert view_model.project_state.hasVoiceTrack is False
    assert view_model.voicePlaybackEnabled is True
    assert playback_states == [False, True]


def test_failed_voice_rerun_preparation_never_creates_tts_job(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_tts_busy(True)
    view_model._pending_tts_start = {"purpose": "production"}
    view_model._set_voice_playback_enabled(False)
    requests: list[tuple[str, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, *_args, **_kwargs: requests.append((action, _kwargs)),
    )

    view_model._action_failed(
        "prepare_tts_rerun",
        {"message": "Không lưu được project"},
        {"tts_purpose": "production"},
    )

    assert view_model.ttsBusy is False
    assert view_model.voicePlaybackEnabled is True
    assert view_model._pending_tts_start is None
    assert requests == []
    assert "chưa tạo tác vụ voice mới" in view_model.statusMessage


def test_successful_timing_repair_reloads_project_then_creates_tts_retry(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    project = {"id": "project-1", "name": "Phim", "revision": 2}
    view_model.project_state.replace(project)
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model._action_succeeded(
        "create_voice_timing_repair_job",
        {"id": "repair-1", "state": "succeeded", "spec": {"provider_id": "gemini-web"}},
        {"tts_job_id": "tts-review"},
    )

    assert requests[-1][0] == "get_project"
    reload_context = requests[-1][2]
    view_model._action_succeeded("get_project", project, reload_context)

    retry_request = next(
        request for request in requests if request[0] == "create_tts_timing_retry_job"
    )
    assert retry_request == (
        "create_tts_timing_retry_job",
        {
            "project_id": "project-1",
            "parent_tts_job_id": "tts-review",
            "repair_job_id": "repair-1",
        },
        {
            "voice_timing_retry": True,
            "parent_tts_job_id": "tts-review",
            "repair_job_id": "repair-1",
        },
    )
    assert view_model.currentWorkspace == "translate"
    assert view_model.ttsBusy is True
    assert "đang chuẩn bị tạo lại" in view_model.statusMessage


def test_voice_conform_keeps_production_parent_separate_from_conform_job(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-conform", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    estimate = {
        "project_id": "project-conform",
        "parent_tts_job_id": "tts-production",
        "can_apply": True,
        "applied": False,
        "unresolved_count": 0,
    }
    view_model._action_succeeded(
        "estimate_voice_conform",
        estimate,
        {"voice_conform_estimate": True},
    )

    assert view_model._voice_conform_parent_tts_job_id == "tts-production"
    assert view_model._voice_conform_job_id == ""
    view_model.applyVoiceConform()
    assert requests[-1][0] == "create_voice_conform_job"
    assert requests[-1][1]["parent_tts_job_id"] == "tts-production"

    view_model._action_succeeded(
        "create_voice_conform_job",
        {"id": "tts-conform", "state": "succeeded"},
        {"voice_conform_create": True},
    )

    assert view_model._voice_conform_parent_tts_job_id == "tts-production"
    assert view_model._voice_conform_job_id == "tts-conform"
    view_model.refreshVoiceConformEstimate()
    assert requests[-1][0] == "estimate_voice_conform"
    assert requests[-1][1]["parent_tts_job_id"] == "tts-production"


def test_voice_conform_does_not_apply_stale_estimate_after_mode_change(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-conform", "name": "Phim", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model.refreshVoiceConformEstimate()
    first_context = requests[-1][2]
    assert first_context["generation"] == 0
    view_model.setVoiceConformMode("speed_audio")
    assert len(requests) == 1
    assert view_model.voiceConformEstimate["status"] == "PENDING"

    view_model._action_succeeded(
        "estimate_voice_conform",
        {
            "project_id": "project-conform",
            "parent_tts_job_id": "tts-production",
            "mode": "hybrid",
            "max_audio_speed": 1.25,
            "unresolved_count": 186,
            "can_apply": False,
        },
        first_context,
    )

    assert view_model.voiceConformEstimate["status"] == "PENDING"
    assert requests[-1][0] == "estimate_voice_conform"
    assert requests[-1][2]["generation"] == 1
    assert requests[-1][2]["mode"] == "speed_audio"


def test_translation_create_request_stays_busy_until_backend_publishes_job(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    view_model._set_translation_job_state("start_requested")
    view_model._set_translation_busy(True)

    view_model._replace_jobs([])
    view_model._replace_jobs([])

    assert view_model.translationBusy is True
    assert view_model.translationJobState == "start_requested"


def test_multi_gemini_web_snapshot_always_disables_floating_window(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {
        "translation/provider": "gemini-web",
        "translation/model": "selected-on-web",
    }
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._selected_voice_id = "voice-ready"
    monkeypatch.setattr(
        view_model.voice_catalog,
        "find",
        lambda _voice_id: {
            "id": "voice-ready",
            "provider_id": "piper",
            "installed": True,
        },
    )

    snapshot = view_model._multi_settings_snapshot()

    assert snapshot["translation"]["provider_options"]["headed"] is False


def test_multi_snapshot_keeps_explicit_whisper_method(monkeypatch, tmp_path: Path) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {
        "translation/provider": "gemini-web",
        "translation/model": "selected-on-web",
        "ocr/extractionMethod": "whisper_audio",
    }
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    runtime = tmp_path / "whisper-runtime-python.exe"
    runtime.touch()
    model = tmp_path / "large-v3"
    model.mkdir()
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(application_module, "whisper_runtime_python", lambda: runtime)
    monkeypatch.setattr(application_module, "whisper_model_path", lambda: model)
    monkeypatch.setattr(application_module, "whisper_runtime_is_ready", lambda *_args: True)
    view_model = ApplicationViewModel()
    view_model._selected_voice_id = "voice-ready"
    monkeypatch.setattr(
        view_model.voice_catalog,
        "find",
        lambda _voice_id: {
            "id": "voice-ready",
            "provider_id": "piper",
            "installed": True,
        },
    )

    snapshot = view_model._multi_settings_snapshot()

    assert snapshot["ocr"]["extraction_method"] == "whisper_audio"
    assert snapshot["ocr"]["engine_python"] == str(runtime)
    assert snapshot["ocr"]["model_path"] == str(model)


def test_selected_translation_cache_is_not_overwritten_by_another_active_job(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-1",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
        }
    )
    view_model._translation_caches = [{"job_id": "cache-full", "completed_cues": 1, "cue_total": 1}]
    view_model._selected_translation_cache_id = "cache-full"
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    active = _job("translate-new", "translate")
    cached = _job("cache-full", "translate")
    cached["state"] = "failed"
    cached["error_code"] = "translation_review_required"

    view_model.selectTranslationCache("cache-full")
    requests.clear()
    view_model._replace_jobs([active, cached])

    assert view_model.translationBusy is True
    assert view_model.translationJobId == "translate-new"
    assert not any(
        action == "list_events" and payload.get("job_id") == "translate-new"
        for action, payload, _context in requests
    )
    view_model._action_succeeded(
        "list_events",
        [
            {
                "sequence": 1,
                "kind": "translation_delta",
                "payload": {"values": [{"id": "cue-1", "index": 1, "text": "Bản dịch mới."}]},
            }
        ],
        {
            "translation_deltas": True,
            "job_id": "translate-new",
            "project_id": "project-1",
        },
    )
    assert view_model.project_state.subtitle_cues.get(0)["translated_text"] == ""


def test_deleted_translation_cache_refreshes_once_after_job_disappears(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    view_model._translation_caches = [{"job_id": "deleted-cache"}]
    view_model._selected_translation_cache_id = "deleted-cache"
    view_model._pending_history_deletion_ids.add("deleted-cache")
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model._replace_jobs([])
    view_model._replace_jobs([])

    assert [action for action, _payload, _context in requests].count("list_translation_caches") == 1


def test_old_selected_cache_absent_from_limited_job_poll_does_not_refresh_forever(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    view_model._translation_caches = [{"job_id": "older-compatible-cache"}]
    view_model._selected_translation_cache_id = "older-compatible-cache"
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )

    view_model._replace_jobs([])
    view_model._replace_jobs([])

    assert not any(action == "list_translation_caches" for action, _payload, _ in requests)


def test_active_translation_state_uses_active_id_not_first_translation_row(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
    monkeypatch.setattr(view_model.runtime, "request", lambda *_args, **_kwargs: None)
    finished = _job("translate-newer-finished", "translate")
    finished["state"] = "succeeded"
    active = _job("translate-older-active", "translate")

    view_model._replace_jobs([finished, active])

    assert view_model.translationJobId == "translate-older-active"
    assert view_model.translationJobState == "running"
    assert view_model.translationBusy is True


def test_inflight_translation_delta_is_replayable_after_switching_projects(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace({"id": "project-1", "name": "A", "revision": 1})
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    job = _job("translate-1", "translate")
    view_model._replace_jobs([job])
    context = requests[-1][2]
    context["terminal"] = True
    view_model.project_state.replace({"id": "project-2", "name": "B", "revision": 1})

    view_model._action_succeeded(
        "list_events",
        [
            {
                "sequence": 42,
                "kind": "translation_delta",
                "payload": {"values": [{"id": "cue-1", "index": 1, "text": "Câu thứ nhất."}]},
            }
        ],
        context,
    )

    assert view_model._translation_event_sequences.get("translate-1", 0) == 0
    assert "translate-1" not in view_model._translation_terminal_event_jobs


def test_project_state_keeps_multi_hour_microseconds_and_real_track_counts() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    duration_us = 30_000_000_000
    model.replace(
        {
            "id": "project",
            "name": "Phim dài",
            "revision": 2,
            "tracks": [
                {
                    "id": "V1",
                    "kind": "video",
                    "name": "Video 1",
                    "clips": [
                        {
                            "id": "clip",
                            "track_id": "V1",
                            "timeline_range": {"start_us": 0, "end_us": duration_us},
                            "speed": {"numerator": 17, "denominator": 20},
                            "volume": 0.0,
                        }
                    ],
                }
            ],
        }
    )

    assert model.hasProject is True
    assert model.durationUs == float(duration_us)
    assert model.durationText == "08:20:00"
    assert model.trackModel.count == 2  # Independent empty Text line above the media.
    assert model.tracks._items[0]["id"] == "T1"
    assert model.clipModel.count == 1


def test_clip_and_track_selection_are_mutually_exclusive_on_timeline(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-selection",
            "name": "Phim",
            "revision": 1,
            "tracks": [
                {
                    "id": "D1",
                    "kind": "voice",
                    "name": "Thuyết minh",
                    "clips": [
                        {
                            "id": "voice-1",
                            "track_id": "D1",
                            "timeline_range": {"start_us": 0, "end_us": 2_000_000},
                            "source_range": {"start_us": 0, "end_us": 2_000_000},
                            "speed": {"numerator": 1, "denominator": 1},
                            "volume": 1.0,
                        }
                    ],
                },
                {
                    "id": "S1",
                    "kind": "subtitle",
                    "name": "Phụ đề nguồn",
                    "clips": [],
                },
            ],
        }
    )

    view_model.selectAllTracks()
    assert view_model.selectedTrackIds == ["D1", "S1"]

    view_model.selectClip("voice-1")

    assert view_model.selectedClipId == "voice-1"
    assert view_model.selectedTrackIds == []
    assert view_model.selectedTrackId == ""

    view_model.selectTrack("D1")

    assert view_model.selectedTrackIds == ["D1"]
    assert view_model.selectedClipId == ""


def test_translation_qa_clears_when_the_last_translated_cue_is_deleted(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(
        {
            "id": "project-delete-last",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "朋友",
                }
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "朋友",
                }
            ],
        }
    )
    view_model._set_translation_qa(
        {
            "status": "REVIEW",
            "issues": [
                {
                    "id": 1,
                    "cue_id": "source-1",
                    "code": "cjk_remaining",
                    "message": "Còn chữ Hán",
                }
            ],
        }
    )

    view_model.project_state.apply_subtitle_patch(
        {
            "id": "project-delete-last",
            "revision": 2,
            "source_cues": [],
            "translated_cues": [],
        }
    )
    view_model._set_translation_qa(view_model._translation_qa)

    assert view_model.translationQa["status"] == "NONE"
    assert view_model.translationQa["issues"] == []
    assert view_model.translationQa["blocking_count"] == 0


def test_failed_optimistic_subtitle_save_keeps_newer_qa_event(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-qa-race",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "朋友",
                }
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "Đây là 朋友",
                }
            ],
        }
    )
    view_model._set_translation_qa(
        {
            "status": "REVIEW",
            "issues": [
                {
                    "id": 1,
                    "cue_id": "source-1",
                    "code": "cjk_remaining",
                    "message": "Còn chữ Hán",
                }
            ],
        }
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    view_model.updateSubtitleText("translated-1", "Đây là bạn", "translated")
    view_model._dispatch_background_subtitle_save()
    view_model._set_translation_qa(
        {
            "status": "REVIEW",
            "issues": [
                {
                    "id": 1,
                    "cue_id": "source-1",
                    "code": "NAME_INCONSISTENCY",
                    "message": "Tên riêng chưa nhất quán",
                }
            ],
        }
    )

    view_model._action_failed(
        "subtitle_command",
        {"message": "disk full"},
        requests[-1][2],
    )

    assert view_model.translationQa["status"] == "REVIEW"
    assert view_model.translationQa["issues"][0]["code"] == "NAME_INCONSISTENCY"


def test_manual_edit_does_not_promote_partial_translation_qa_to_pass(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(
        {
            "id": "project-partial-qa",
            "name": "Phim",
            "revision": 1,
            "source_cues": [
                {
                    "id": "source-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "第一句",
                },
                {
                    "id": "source-2",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "text": "第二句",
                },
            ],
            "translated_cues": [
                {
                    "id": "translated-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "Câu thứ nhất",
                }
            ],
        }
    )
    view_model._set_translation_qa(
        {
            "status": "PENDING",
            "completed_cues": 1,
            "checked_cues": 1,
            "cue_total": 2,
            "issues": [],
        }
    )
    monkeypatch.setattr(view_model.runtime, "request", lambda *_args, **_kwargs: None)

    view_model.updateSubtitleText("translated-1", "Câu đầu tiên", "translated")

    assert view_model.translationQa["status"] == "PENDING"
    assert view_model.translationQa["cue_total"] == 2
    assert view_model.translationQa["checked_cues"] == 1


def test_project_state_recovers_legacy_voice_cue_metadata_from_clip_id() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project",
            "name": "Legacy D1",
            "revision": 2,
            "source_cues": [
                {
                    "id": "cue-0be2f06bbb1744c8941f50de02ccd542",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_400_000},
                    "text": "你知道中国家庭买SUV",
                }
            ],
            "translated_cues": [
                {
                    "id": "cue-0be2f06bbb1744c8941f50de02ccd542",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_400_000},
                    "text": "Biết gia đình Trung Quốc mua SUV",
                }
            ],
            "tracks": [
                {
                    "id": "D1",
                    "kind": "voice",
                    "name": "Thuyết minh",
                    "clips": [
                        {
                            "id": "voice-000001-50de02ccd542",
                            "track_id": "D1",
                            "timeline_range": {"start_us": 0, "end_us": 1_600_000},
                            "source_range": {"start_us": 0, "end_us": 1_600_000},
                            "speed": {"numerator": 1, "denominator": 1},
                            "volume": 1.0,
                            "cue_id": None,
                            "cue_index": None,
                            "cue_text": "",
                            "timing_slot": None,
                            "timing_unresolved": False,
                        }
                    ],
                }
            ],
        }
    )

    index = model.clipModel.index(0, 0)
    assert (
        model.clipModel.data(index, TimelineClipListModel.CueIdRole)
        == "cue-0be2f06bbb1744c8941f50de02ccd542"
    )
    assert model.clipModel.data(index, TimelineClipListModel.CueIndexRole) == 1
    assert (
        model.clipModel.data(index, TimelineClipListModel.CueTextRole)
        == "Biết gia đình Trung Quốc mua SUV"
    )
    assert model.clipModel.data(index, TimelineClipListModel.SlotStartUsRole) == 0
    assert model.clipModel.data(index, TimelineClipListModel.SlotEndUsRole) == 1_400_000
    assert model.clipModel.data(index, TimelineClipListModel.TimingUnresolvedRole) is True
    assert model.clipModel.data(index, TimelineClipListModel.TimingStateRole) == "overflow"
    assert model.clipModel.data(index, TimelineClipListModel.TimingDeltaUsRole) == 200_000
    assert model.clipModel.data(
        index,
        TimelineClipListModel.TimingRequiredSpeedRole,
    ) == pytest.approx(1_600_000 / 1_400_000)
    assert (
        model.clipModel.data(index, TimelineClipListModel.IssueCodeRole)
        == "CUE_SYNC_LIMIT_EXCEEDED"
    )
    assert model.clip_info("voice-000001-50de02ccd542")["cue_index"] == 1


def test_project_state_marks_short_voice_yellow_state_without_legacy_red_flag() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project",
            "name": "Short D1",
            "revision": 1,
            "tracks": [
                {
                    "id": "D1",
                    "kind": "voice",
                    "name": "Thuyết minh",
                    "clips": [
                        {
                            "id": "voice-short",
                            "track_id": "D1",
                            "timeline_range": {"start_us": 0, "end_us": 900_000},
                            "source_range": {"start_us": 0, "end_us": 900_000},
                            "timing_slot": {"start_us": 0, "end_us": 1_200_000},
                            "speed": {"numerator": 1, "denominator": 1},
                            "volume": 1.0,
                            "timing_unresolved": True,
                        }
                    ],
                }
            ],
        }
    )

    index = model.clipModel.index(0, 0)

    assert model.clipModel.data(index, TimelineClipListModel.TimingStateRole) == "underflow"
    assert model.clipModel.data(index, TimelineClipListModel.TimingUnresolvedRole) is False
    assert model.clip_timing_info("voice-short")["duration_delta_us"] == -300_000


def test_full_project_view_aggregates_4333_cues_without_losing_source_model() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineClipListModel()
    cue_count = 4_333
    cues = [
        {
            "id": f"cue-{index + 1}",
            "track_id": "S1",
            "timeline_range": {
                "start_us": index * 1_000_000,
                "end_us": (index + 1) * 1_000_000,
            },
            "speed": {"numerator": 1, "denominator": 1},
            "volume": 1.0,
        }
        for index in range(cue_count)
    ]

    model.setViewport(0, cue_count * 1_000_000, 0)
    model.replace(
        [
            {
                "id": "S1",
                "kind": "subtitle",
                "name": "Phụ đề",
                "clips": cues,
            }
        ]
    )

    assert model.count <= 480
    assert model.count < cue_count
    assert all(
        model.data(model.index(row, 0), TimelineClipListModel.AggregateRole)
        for row in range(model.count)
    )
    assert (
        sum(
            model.data(model.index(row, 0), TimelineClipListModel.AggregateCountRole)
            for row in range(model.count)
        )
        == cue_count
    )

    model.setViewport(2_000_000, 8_000_000, 0)
    assert model.count == 6
    assert all(
        not model.data(model.index(row, 0), TimelineClipListModel.AggregateRole)
        for row in range(model.count)
    )


def test_full_project_view_keeps_sparse_scene_text_tracks_editable() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineClipListModel()
    dialogue_count = 4_333
    scene_count = 75

    def clips(track_id: str, count: int) -> list[dict]:
        return [
            {
                "id": f"{track_id}-cue-{index + 1}",
                "track_id": track_id,
                "timeline_range": {
                    "start_us": index * 1_000_000,
                    "end_us": (index + 1) * 1_000_000,
                },
                "speed": {"numerator": 1, "denominator": 1},
                "volume": 1.0,
            }
            for index in range(count)
        ]

    model.setViewport(0, dialogue_count * 1_000_000, 0)
    model.replace(
        [
            {
                "id": "S1",
                "kind": "subtitle",
                "name": "Phụ đề thoại",
                "clips": clips("S1", dialogue_count),
            },
            {
                "id": "S2",
                "kind": "subtitle",
                "name": "Chữ trong cảnh",
                "clips": clips("S2", scene_count),
            },
        ]
    )

    scene_rows = [
        model.index(row, 0)
        for row in range(model.count)
        if model.data(model.index(row, 0), TimelineClipListModel.TrackIdRole) == "S2"
    ]
    assert len(scene_rows) == scene_count
    assert all(not model.data(index, TimelineClipListModel.AggregateRole) for index in scene_rows)
    assert model.count <= 480


def test_subtitle_track_labels_preserve_canonical_lane_ids_and_legacy_fallback() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineTrackListModel()

    def labels(tracks: list[dict[str, object]]) -> dict[str, str]:
        model.replace(tracks)
        return {
            str(model.data(model.index(row, 0), TimelineTrackListModel.IdRole)):
            str(model.data(model.index(row, 0), TimelineTrackListModel.DisplayLabelRole))
            for row in range(model.count)
        }

    assert labels([{"id": "S2", "kind": "subtitle", "name": "Scene", "clips": []}]) == {
        "S2": "S2"
    }
    assert labels(
        [
            {"id": "S1", "kind": "subtitle", "name": "Dialogue", "clips": []},
            {"id": "S3", "kind": "subtitle", "name": "Scene 3", "clips": []},
            {"id": "S10", "kind": "subtitle", "name": "Scene 10", "clips": []},
        ]
    ) == {"S1": "S1", "S3": "S3", "S10": "S10"}
    assert labels(
        [
            {"id": "S1", "kind": "subtitle", "name": "Dialogue", "clips": []},
            {"id": "S3", "kind": "subtitle", "name": "Scene 3", "clips": []},
            {"id": "S10", "kind": "subtitle", "name": "Scene 10", "clips": []},
            {"id": "legacy-scene", "kind": "subtitle", "name": "Legacy", "clips": []},
            {"id": "V8", "kind": "video", "name": "Video", "clips": []},
            {"id": "B9", "kind": "blur", "name": "Blur", "clips": []},
        ]
    ) == {
        "S1": "S1",
        "S3": "S3",
        "S10": "S10",
        "legacy-scene": "S4",
        "V8": "V1",
        "B9": "B1",
    }


def test_timeline_clip_model_materializes_only_tracks_in_vertical_viewport() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineClipListModel()
    tracks = [
        {
            "id": f"S{track_index + 1}",
            "kind": "subtitle",
            "name": f"Phụ đề S{track_index + 1}",
            "clips": [
                {
                    "id": f"clip-{track_index}",
                    "track_id": f"S{track_index + 1}",
                    "timeline_range": {"start_us": 0, "end_us": 1_000_000},
                    "speed": {"numerator": 1, "denominator": 1},
                    "volume": 1.0,
                }
            ],
        }
        for track_index in range(50)
    ]
    model.setViewport(0, 2_000_000, 0)
    model.setTrackViewport(10, 14, 1)
    model.replace(tracks)

    visible_indexes = {
        model.data(model.index(row, 0), TimelineClipListModel.TrackIndexRole)
        for row in range(model.count)
    }

    assert visible_indexes == set(range(9, 16))
    assert model.count == 7


def test_timeline_clip_model_can_narrow_initial_all_track_materialization() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineClipListModel()
    tracks = [
        {
            "id": f"S{track_index + 1}",
            "kind": "subtitle",
            "name": f"Phụ đề S{track_index + 1}",
            "clips": [
                {
                    "id": f"clip-{track_index}",
                    "track_id": f"S{track_index + 1}",
                    "timeline_range": {"start_us": 0, "end_us": 1_000_000},
                    "speed": {"numerator": 1, "denominator": 1},
                    "volume": 1.0,
                }
            ],
        }
        for track_index in range(50)
    ]
    model.setViewport(0, 2_000_000, 0)
    model.replace(tracks)
    assert model.count == 50

    model.setTrackViewport(10, 14, 1)

    visible_indexes = {
        model.data(model.index(row, 0), TimelineClipListModel.TrackIndexRole)
        for row in range(model.count)
    }
    assert visible_indexes == set(range(9, 16))


def test_timeline_clip_model_reuses_prefetched_window_during_small_pans() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineClipListModel()
    cues = [
        {
            "id": f"cue-{index}",
            "track_id": "S1",
            "timeline_range": {
                "start_us": index * 250_000,
                "end_us": index * 250_000 + 220_000,
            },
            "speed": {"numerator": 1, "denominator": 1},
            "volume": 1.0,
        }
        for index in range(2_000)
    ]
    model.setViewport(10_000_000, 20_000_000, 5_000_000)
    model.replace(
        [
            {
                "id": "S1",
                "kind": "subtitle",
                "name": "Phụ đề",
                "clips": cues,
            }
        ]
    )
    resets: list[None] = []
    model.modelReset.connect(lambda: resets.append(None))

    for offset in range(100_000, 5_000_000, 100_000):
        model.setViewport(
            10_000_000 + offset,
            20_000_000 + offset,
            5_000_000,
        )

    assert resets == []

    model.setViewport(15_100_000, 25_100_000, 5_000_000)
    assert resets == []  # Existing clip delegates survive the window refresh.


def test_timeline_clip_model_limits_resets_during_continuous_zoom() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineClipListModel()
    cues = [
        {
            "id": f"cue-{index}",
            "track_id": "S1",
            "timeline_range": {
                "start_us": index * 100_000,
                "end_us": index * 100_000 + 90_000,
            },
            "speed": {"numerator": 1, "denominator": 1},
            "volume": 1.0,
        }
        for index in range(12_000)
    ]
    model.setViewport(20_000_000, 80_000_000, 10_000_000)
    model.replace(
        [
            {
                "id": "S1",
                "kind": "subtitle",
                "name": "Phụ đề",
                "clips": cues,
            }
        ]
    )
    resets: list[None] = []
    model.modelReset.connect(lambda: resets.append(None))

    for step in range(1, 251):
        duration = int(60_000_000 * (0.992**step))
        prefetch = min(10_000_000, max(250_000, int(duration * 0.25)))
        model.setViewport(20_000_000, 20_000_000 + duration, prefetch)

    assert len(resets) <= 4
    assert model.count <= 480


def test_bulk_typography_indexes_cues_and_notifies_once() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    model.replace(
        [
            {
                "id": f"c{i}",
                "index": i + 1,
                "text": "test",
                "range": {"start_us": i * 1_000_000, "end_us": (i + 1) * 1_000_000},
            }
            for i in range(12_000)
        ],
        [],
    )

    class IndexedOnlyList(list):
        def __iter__(self):
            raise AssertionError("A bulk edit must not scan all rows per cue")

    model._items = IndexedOnlyList(model._items)
    notifications = []
    model.dataChanged.connect(
        lambda first, last, roles: notifications.append((first.row(), last.row()))
    )
    changes = model.optimistic_update_text_format_many(
        [f"c{i}" for i in range(12_000)],
        font_size=16,
        orientation="horizontal",
        background_color="",
    )
    assert len(changes) == 12_000
    assert notifications == [(0, 11_999)]
    assert model.find("c11999")["text_font_size"] == 16


def test_bulk_typography_notifies_once_for_interleaved_scene_rows() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = [
        {
            "id": f"c{i}",
            "index": i + 1,
            "text": "test",
            "role": "dialogue" if i % 2 == 0 else "scene_text",
            "range": {"start_us": i * 1_000_000, "end_us": (i + 1) * 1_000_000},
        }
        for i in range(1000)
    ]
    model.replace(source, [])
    notifications = []
    model.dataChanged.connect(
        lambda first, last, roles: notifications.append((first.row(), last.row()))
    )
    model.optimistic_update_text_format_many(
        [f"c{i}" for i in range(0, 1000, 2)],
        font_size=16,
        orientation="horizontal",
        background_color="",
    )
    assert notifications == [(0, 998)]
    assert model.find("c998")["text_font_size"] == 16
    assert model.find("c999")["text_font_size"] == 12


def test_preview_uses_final_cue_timing_and_style_while_preserving_source_text() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    source = {
        "id": "cue",
        "index": 1,
        "text": "原文",
        "range": {"start_us": 0, "end_us": 1_000_000},
        "text_color": "#FFFF00",
    }
    final = {
        **source,
        "text": "Bản dịch",
        "language": "vi",
        "text_color": "#FFFFFF",
        "range": {"start_us": 2_000_000, "end_us": 3_000_000},
    }
    model.replace([source], [final])
    assert not model.at_time(500_000)
    cue = model.at_time(2_500_000)
    assert cue["source_text"] == "原文" and cue["translated_text"] == "Bản dịch"
    assert cue["text_color"] == "#FFFFFF"
    assert model.regionsAtTime(2_500_000)[0] == cue


@pytest.mark.parametrize(
    "replacement",
    [
        [],
        [("a", 0, 1_000_000), ("b", 2_000_000, 3_000_000)],
        [("a", 0, 1_000_000), ("c", 4_000_000, 5_000_000)],
        [
            ("a", 0, 1_000_000),
            ("d", 1_500_000, 1_800_000),
            ("b", 2_000_000, 3_000_000),
            ("c", 4_000_000, 5_000_000),
        ],
        [("a", 500_000, 1_500_000), ("b", 2_000_000, 3_000_000),
         ("c", 4_000_000, 5_000_000)],
        [("a", 0, 1_000_000), ("c", 1_500_000, 1_800_000),
         ("b", 2_000_000, 3_000_000)],
        [("a", 3_000_000, 4_000_000), ("d", 4_250_000, 4_750_000),
         ("c", 5_000_000, 6_000_000)],
    ],
    ids=["clear", "remove-tail", "remove-middle", "insert-middle", "retime", "reorder",
         "replace-and-retime-retained"],
)
def test_subtitle_lookup_is_consistent_during_model_notifications(replacement) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()

    def cues(specs):
        return [
            {
                "id": cue_id,
                "index": ord(cue_id) - ord("a") + 1,
                "text": cue_id,
                "role": "scene_text" if cue_id == "b" else "dialogue",
                "range": {"start_us": start_us, "end_us": end_us},
            }
            for cue_id, start_us, end_us in specs
        ]

    model.replace(cues([("a", 0, 1_000_000), ("b", 2_000_000, 3_000_000),
                       ("c", 4_000_000, 5_000_000)]), [])
    errors: list[str] = []
    notifications: list[int] = []

    def inspect_notification(*_args):
        notifications.append(model.count)
        try:
            assert model._start_us == sorted(int(item["start_us"]) for item in model._items)
            for position in range(0, 6_100_000, 100_000):
                expected = [
                    item for item in model._items
                    if int(item["start_us"]) <= position < int(item["end_us"])
                ]
                assert model._items_at_time(position) == expected
                model.at_time(position)
                model.regionsAtTime(position)
                model.sceneTextsAtTime(position)
            for cue_id in ("a", "b", "c", "d"):
                expected = next((item for item in model._items if item["cue_id"] == cue_id), {})
                assert model.find(cue_id) == expected
        except (AssertionError, IndexError) as error:
            errors.append(f"{type(error).__name__}: {error}")

    for signal in (model.rowsAboutToBeRemoved, model.rowsRemoved,
                   model.rowsAboutToBeInserted, model.rowsInserted,
                   model.dataChanged, model.countChanged):
        signal.connect(inspect_notification)

    model.replace(cues(replacement), [])
    assert notifications
    assert errors == []
    inspect_notification()
    assert errors == []


def test_timeline_recycles_rows_and_preserves_common_clip_identity_and_role_values() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineClipListModel()
    model.setViewport(0, 3_000_000, 0)
    tracks = [
        {
            "id": "S1",
            "kind": "subtitle",
            "name": "S1",
            "clips": [
                {
                    "id": f"clip{i}",
                    "track_id": "S1",
                    "cue_id": f"c{i}",
                    "cue_text": f"text{i}",
                    "timeline_range": {"start_us": i * 1_000_000, "end_us": (i + 1) * 1_000_000},
                }
                for i in range(10)
            ],
        }
    ]
    model.replace(tracks)
    assert model.data(model.index(0), model.CueTextRole) == "text0"
    common_row = next(row for row, item in enumerate(model._items) if item["id"] == "clip2")
    resets = []
    model.modelReset.connect(lambda: resets.append(True))
    model.setViewport(2_000_000, 5_000_000, 0)
    assert model._items[common_row]["id"] == "clip2"
    assert {item["id"] for item in model._items} == {"clip2", "clip3", "clip4"}
    for row, item in enumerate(model._items):
        assert model.data(model.index(row), model.CueTextRole) == item["cue_text"]
    tracks[0]["clips"][2]["cue_text"] = "edited"
    model.replace(tracks)
    assert model.data(model.index(common_row), model.CueTextRole) == "edited"
    model.setViewport(0, 1_000_000, 0)
    assert model.count == 1 and model.data(model.index(0), model.IdRole) == "clip0"
    model.setViewport(15_000_000, 16_000_000, 0)
    assert model.count == 0 and not resets


def test_translation_cache_refresh_batches_contiguous_qml_notifications() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = SubtitleCueListModel()
    cue_count = 13_400
    source = [
        {
            "id": f"cue-{index}",
            "index": index,
            "range": {
                "start_us": (index - 1) * 1_000_000,
                "end_us": index * 1_000_000,
            },
            "text": f"Nguồn {index}",
        }
        for index in range(1, cue_count + 1)
    ]
    model.replace(source, [])
    changes: list[tuple[int, int]] = []
    model.dataChanged.connect(lambda first, last, _roles: changes.append((first.row(), last.row())))
    values = [
        {"id": f"cue-{index}", "index": index, "text": f"Dịch {index}"}
        for index in range(1, cue_count + 1)
    ]

    assert model.apply_translation_delta(values) == cue_count
    assert changes == [(0, cue_count - 1)]

    assert model.finalize_translation_preview() == cue_count
    assert changes == [(0, cue_count - 1), (0, cue_count - 1)]


def test_preview_descriptor_maps_trim_speed_and_linked_audio_volume() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project",
            "name": "Preview mapping",
            "revision": 1,
            "media": [
                {
                    "id": "media",
                    "kind": "video",
                    "path": "C:/media/phim.mp4",
                },
                {
                    "id": "voice-media",
                    "kind": "audio",
                    "path": "C:/media/phim_voice_vi.wav",
                },
            ],
            "tracks": [
                {
                    "id": "V1",
                    "kind": "video",
                    "name": "Video",
                    "clips": [
                        {
                            "id": "video",
                            "track_id": "V1",
                            "media_id": "media",
                            "link_group_id": "linked",
                            "timeline_range": {
                                "start_us": 30_000_000,
                                "end_us": 53_529_412,
                            },
                            "source_range": {
                                "start_us": 5_000_000,
                                "end_us": 25_000_000,
                            },
                            "speed": {"numerator": 17, "denominator": 20},
                            "volume": 0.0,
                        }
                    ],
                },
                {
                    "id": "A1",
                    "kind": "original_audio",
                    "name": "Audio",
                    "clips": [
                        {
                            "id": "audio",
                            "track_id": "A1",
                            "link_group_id": "linked",
                            "timeline_range": {
                                "start_us": 30_000_000,
                                "end_us": 53_529_412,
                            },
                            "volume": 0.35,
                        }
                    ],
                },
                {
                    "id": "D1",
                    "kind": "voice",
                    "name": "Thuyết minh",
                    "clips": [
                        {
                            "id": "voice",
                            "track_id": "D1",
                            "media_id": "voice-media",
                            "timeline_range": {
                                "start_us": 30_000_000,
                                "end_us": 53_529_412,
                            },
                            "source_range": {
                                "start_us": 0,
                                "end_us": 23_529_412,
                            },
                            "speed": {"numerator": 1, "denominator": 1},
                            "volume": 0.8,
                        }
                    ],
                },
            ],
        }
    )

    descriptor = model.previewDescriptor(41_764_706)

    assert descriptor["available"] is True
    assert descriptor["clipId"] == "video"
    assert abs(descriptor["sourcePositionMs"] - 15_000) <= 1
    assert descriptor["speedRate"] == 0.85
    assert descriptor["audioVolume"] == 0.35
    assert descriptor["voiceAvailable"] is True
    assert descriptor["voiceSourceUrl"].endswith("phim_voice_vi.wav")
    assert abs(descriptor["voicePositionMs"] - 11_764.706) <= 1
    assert descriptor["voicePlaybackRate"] == 1.0
    assert descriptor["voiceVolume"] == 0.8
    assert model.hasVoiceTrack is True
    assert model.voiceSource.toLocalFile().endswith("phim_voice_vi.wav")
    assert model.previewDescriptor(10_000_000) == {
        "available": False,
        "nextTimelineStartUs": 30_000_000,
    }

    model.replace(
        {
            "id": "project",
            "name": "Preview mapping",
            "revision": 2,
            "media": [
                {
                    "id": "media",
                    "kind": "video",
                    "path": "C:/media/phim.mp4",
                }
            ],
            "tracks": [],
        }
    )
    assert model.hasVideo is False


def test_conformed_preview_maps_continuously_across_segment_boundary() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    plan = build_voice_conform_plan(
        (
            VoiceConformCueInput("cue-long", 1, 0, 2_000_000, 4_000_000),
            VoiceConformCueInput("cue-normal", 2, 3_000_000, 4_000_000, 800_000),
        ),
        source_duration_us=5_000_000,
        settings=VoiceConformSettings(mode=VoiceTimingMode.STRETCH_VIDEO),
    )
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project-conformed-preview",
            "name": "Preview conform",
            "revision": 1,
            "voice_conform": {
                "plan": plan.as_json(),
                "parent_tts_job_id": "tts-production",
            },
            "media": [
                {"id": "video-media", "kind": "video", "path": "C:/media/phim.mp4"},
                {"id": "voice-media", "kind": "audio", "path": "C:/media/d1.wav"},
            ],
            "tracks": [
                {
                    "id": "V1",
                    "kind": "video",
                    "clips": [
                        {
                            "id": "video",
                            "track_id": "V1",
                            "media_id": "video-media",
                            "timeline_range": {"start_us": 0, "end_us": 5_000_000},
                            "source_range": {"start_us": 0, "end_us": 5_000_000},
                            "speed": {"numerator": 1, "denominator": 1},
                            "volume": 1.0,
                        }
                    ],
                },
                {
                    "id": "D1",
                    "kind": "voice",
                    "clips": [
                        {
                            "id": "voice",
                            "track_id": "D1",
                            "media_id": "voice-media",
                            "timeline_range": {"start_us": 0, "end_us": 7_000_000},
                            "source_range": {"start_us": 0, "end_us": 7_000_000},
                            "speed": {"numerator": 1, "denominator": 1},
                            "volume": 1.0,
                        }
                    ],
                },
            ],
        }
    )

    before = model.previewDescriptor(3_999_999)
    at_boundary = model.previewDescriptor(4_000_000)
    after = model.previewDescriptor(4_000_001)

    assert before["speedRate"] == 0.5
    assert abs(before["sourcePositionMs"] - 2000.0) <= 0.001
    assert at_boundary["speedRate"] == 1.0
    assert at_boundary["sourcePositionMs"] == 2000.0
    assert after["sourcePositionMs"] == 2000.001
    assert before["timelineEndUs"] == at_boundary["timelineStartUs"] == 4_000_000
    assert at_boundary["voicePositionMs"] == 4000.0
    assert model.outputToSourceUs(4_000_000) == 2_000_000.0
    assert model.sourceToOutputUs(2_000_000) == 4_000_000.0


def test_timeline_voice_cue_model_uses_source_srt_slots_and_real_wav_speed() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    plan = build_voice_conform_plan(
        (
            VoiceConformCueInput("cue-long", 1, 0, 2_000_000, 4_000_000),
            VoiceConformCueInput("cue-short", 2, 3_000_000, 4_000_000, 800_000),
        ),
        source_duration_us=5_000_000,
        settings=VoiceConformSettings(
            mode=VoiceTimingMode.SPEED_AUDIO,
            max_audio_speed=2.0,
        ),
    )
    model = VoiceConformCueListModel()
    model.setViewport(0, 5_000_000, 0)
    model.replace(plan)

    assert model.count == 2
    first = model.index(0, 0)
    second = model.index(1, 0)
    assert model.data(first, VoiceConformCueListModel.CueIdRole) == "cue-long"
    assert model.data(first, VoiceConformCueListModel.SourceStartUsRole) == 0.0
    assert model.data(first, VoiceConformCueListModel.SourceEndUsRole) == 2_000_000.0
    assert model.data(first, VoiceConformCueListModel.AudioDurationUsRole) == 4_000_000.0
    assert model.data(first, VoiceConformCueListModel.RenderedAudioDurationUsRole) == (2_000_000.0)
    assert model.data(first, VoiceConformCueListModel.AudioSpeedRole) == 2.0
    assert model.data(first, VoiceConformCueListModel.AdjustedRole) is True
    assert model.data(second, VoiceConformCueListModel.AudioSpeedRole) == 1.0
    assert model.data(second, VoiceConformCueListModel.AdjustedRole) is False


def test_timeline_voice_cue_model_accepts_live_tts_rows_before_conform_is_applied() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = VoiceConformCueListModel()
    model.setViewport(0, 5_000_000, 0)

    model.replace_timing_rows(
        [
            {
                "cue_id": "cue-long",
                "index": 1,
                "source_start_us": 0,
                "source_end_us": 2_000_000,
                "output_start_us": 0,
                "output_end_us": 2_000_000,
                "voice_start_us": 0,
                "voice_end_us": 2_400_000,
                "audio_duration_us": 4_800_000,
                "rendered_audio_duration_us": 2_400_000,
                "audio_speed": 2.0,
                "adjusted": True,
                "unresolved": True,
                "issue_code": "CUE_SYNC_LIMIT_EXCEEDED",
            },
            {
                "cue_id": "cue-short",
                "index": 2,
                "source_start_us": 3_000_000,
                "source_end_us": 4_000_000,
                "output_start_us": 3_000_000,
                "output_end_us": 4_000_000,
                "voice_start_us": 3_000_000,
                "voice_end_us": 3_700_000,
                "audio_duration_us": 1_050_000,
                "rendered_audio_duration_us": 700_000,
                "audio_speed": 1.5,
                "adjusted": False,
                "unresolved": False,
                "issue_code": "",
            },
        ]
    )

    assert model.totalCount == 2
    assert model.count == 2
    first = model.index(0, 0)
    assert model.data(first, VoiceConformCueListModel.VoiceEndUsRole) == 2_400_000.0
    assert model.data(first, VoiceConformCueListModel.UnresolvedRole) is True
    assert model.data(first, VoiceConformCueListModel.IssueCodeRole) == "CUE_SYNC_LIMIT_EXCEEDED"


def test_timeline_voice_cue_model_reuses_prefetched_window_while_panning() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = VoiceConformCueListModel()
    model.setViewport(10_000_000, 20_000_000, 5_000_000)
    rows = []
    for index in range(2_000):
        start_us = index * 250_000
        end_us = start_us + 220_000
        rows.append(
            {
                "cue_id": f"cue-{index}",
                "index": index,
                "source_start_us": start_us,
                "source_end_us": end_us,
                "output_start_us": start_us,
                "output_end_us": end_us,
                "voice_start_us": start_us,
                "voice_end_us": end_us,
                "audio_duration_us": 220_000,
                "rendered_audio_duration_us": 220_000,
                "audio_speed": 1.0,
                "adjusted": False,
                "unresolved": False,
                "issue_code": "",
            }
        )
    model.replace_timing_rows(rows)
    resets: list[None] = []
    model.modelReset.connect(lambda: resets.append(None))

    for offset in range(100_000, 5_000_000, 100_000):
        model.setViewport(
            10_000_000 + offset,
            20_000_000 + offset,
            5_000_000,
        )

    assert resets == []

    model.setViewport(15_100_000, 25_100_000, 5_000_000)
    assert len(resets) == 1


def test_conformed_subtitle_lookup_and_cue_seek_use_output_timeline(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    plan = build_voice_conform_plan(
        (
            VoiceConformCueInput("cue-long", 1, 0, 2_000_000, 4_000_000),
            VoiceConformCueInput("cue-later", 2, 4_000_000, 5_000_000, 1_000_000),
        ),
        source_duration_us=6_000_000,
        settings=VoiceConformSettings(mode=VoiceTimingMode.STRETCH_VIDEO),
    )
    snapshot = {
        "id": "project-conformed-subtitle",
        "name": "Khớp subtitle",
        "revision": 1,
        "voice_conform": {
            "plan": plan.as_json(),
            "parent_tts_job_id": "tts-production",
        },
        "source_cues": [
            {
                "id": "cue-long",
                "index": 1,
                "range": {"start_us": 0, "end_us": 2_000_000},
                "text": "原文",
                "source": "paddle",
                "revision": 1,
            },
            {
                "id": "cue-later",
                "index": 2,
                "range": {"start_us": 4_000_000, "end_us": 5_000_000},
                "text": "后文",
                "source": "paddle",
                "revision": 1,
            },
        ],
        "translated_cues": [
            {
                "id": "cue-long",
                "index": 1,
                "range": {"start_us": 0, "end_us": 2_000_000},
                "text": "Bản dịch",
                "source": "translate",
                "revision": 1,
            },
            {
                "id": "cue-later",
                "index": 2,
                "range": {"start_us": 4_000_000, "end_us": 5_000_000},
                "text": "Bản dịch sau",
                "source": "translate",
                "revision": 1,
            },
        ],
        "tracks": [],
    }
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model.project_state.replace(snapshot)

    assert view_model.project_state.subtitleAt(3_000_000)["cue_id"] == "cue-long"
    assert view_model.project_state.subtitleAt(6_500_000)["cue_id"] == "cue-later"
    view_model.selectCue("cue-long", 1_000_000)
    assert view_model.playheadUs == 2_000_000


def test_source_subtitle_regions_returns_every_overlapping_cue() -> None:
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project-overlap-regions",
            "name": "Overlap regions",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-a",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 2_000_000},
                    "text": "第一行",
                    "visual_box": [0.1, 0.7, 0.35, 0.1],
                    "visual_box_source": "ocr",
                },
                {
                    "id": "cue-b",
                    "index": 2,
                    "range": {"start_us": 1_000_000, "end_us": 3_000_000},
                    "text": "第二行",
                    "visual_box": [0.55, 0.7, 0.35, 0.1],
                    "visual_box_source": "ocr",
                },
            ],
            "tracks": [],
        }
    )

    active = model.sourceSubtitleRegionsAt(1_500_000)

    assert [item["cue_id"] for item in active] == ["cue-a", "cue-b"]


def test_project_state_exposes_logo_media_as_a_local_qml_source(tmp_path: Path) -> None:
    logo = tmp_path / "Logo tiếng Việt.png"
    logo.write_bytes(b"png")
    model = ProjectStateModel()
    model.replace(
        {
            "id": "project-effects-preview",
            "name": "Preview hiệu ứng",
            "revision": 1,
            "media": [
                {
                    "id": "media-default-logo",
                    "kind": "image",
                    "path": str(logo),
                }
            ],
            "tracks": [],
            "overlays": [],
        }
    )

    source = model.mediaSource("media-default-logo")

    assert source.isLocalFile() is True
    assert Path(source.toLocalFile()) == logo
    assert model.mediaName("media-default-logo") == logo.name
    assert model.mediaSource("missing").isEmpty() is True
    assert model.mediaName("missing") == ""


def test_preview_color_sampler_clamps_pixels_and_returns_opaque_hex() -> None:
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))

    sampled = ApplicationViewModel.samplePreviewColor(object(), image, 99, -10)

    assert sampled == "#123456"


def _cuttable_project_snapshot() -> dict:
    return {
        "id": "project-cut",
        "name": "Cut",
        "revision": 1,
        "media": [{"id": "video-media", "kind": "video", "path": "C:/video.mp4"}],
        "tracks": [
            {
                "id": "V1",
                "kind": "video",
                "name": "Video",
                "clips": [
                    {
                        "id": "video-1",
                        "track_id": "V1",
                        "media_id": "video-media",
                        "timeline_range": {"start_us": 0, "end_us": 10_000_000},
                        "source_range": {"start_us": 0, "end_us": 10_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 1.0,
                    }
                ],
            },
            {
                "id": "S1",
                "kind": "subtitle",
                "name": "Phụ đề",
                "clips": [
                    {
                        "id": "subtitle-1",
                        "track_id": "S1",
                        "timeline_range": {"start_us": 1_000_000, "end_us": 2_000_000},
                        "source_range": {"start_us": 1_000_000, "end_us": 2_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 1.0,
                        "cue_id": "cue-1",
                        "cue_index": 1,
                        "cue_text": "Một",
                    }
                ],
            },
        ],
        "source_cues": [
            {
                "id": "cue-1",
                "index": 1,
                "text": "一",
                "range": {"start_us": 1_000_000, "end_us": 2_000_000},
            },
            {
                "id": "cue-2",
                "index": 2,
                "text": "二",
                "range": {"start_us": 6_000_000, "end_us": 7_000_000},
            },
        ],
        "translated_cues": [],
        "overlays": [],
    }


def test_timeline_patch_preserves_clip_track_identity_for_every_model_role() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(_cuttable_project_snapshot())

    model.apply_timeline_patch(
        {
            "_timeline_patch": True,
            "id": "project-cut",
            "revision": 2,
            "track_upserts": [],
            "removed_track_ids": [],
            "clip_upserts": [
                {
                    "id": "subtitle-1",
                    "track_id": "S1",
                    "timeline_range": {"start_us": 2_000_000, "end_us": 3_000_000},
                    "source_range": {"start_us": 1_000_000, "end_us": 2_000_000},
                    "speed": {"numerator": 1, "denominator": 1},
                    "volume": 1.0,
                    "cue_id": "cue-1",
                    "cue_index": 1,
                    "cue_text": "Một",
                }
            ],
            "removed_clip_ids": [],
        }
    )

    item = model.clip_info("subtitle-1")
    assert item is not None and item["track_id"] == "S1"
    row = next(
        index
        for index, value in enumerate(model.clips._items)
        if value["id"] == "subtitle-1"
    )
    index = model.clips.index(row, 0)
    for role in model.clips.roleNames():
        model.clips.data(index, role)
    assert model.clips.data(index, TimelineClipListModel.TrackIdRole) == "S1"


def test_invalid_timeline_patch_is_atomic_and_keeps_previous_model() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(_cuttable_project_snapshot())
    before = deepcopy(model._canonical_snapshot)
    before_rows = deepcopy(model.clips._items)

    with pytest.raises(ValueError, match="owning track"):
        model.apply_timeline_patch(
            {
                "_timeline_patch": True,
                "id": "project-cut",
                "revision": 2,
                "track_upserts": [],
                "removed_track_ids": [],
                "clip_upserts": [{"id": "subtitle-1"}],
                "removed_clip_ids": [],
            }
        )

    assert model.revision == 1
    assert model._canonical_snapshot == before
    assert model.clips._items == before_rows


def test_timeline_cut_patch_projects_canonical_snapshot_and_reindexes_cues() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(_cuttable_project_snapshot())

    model.apply_timeline_patch(
        {
            "_timeline_patch": True,
            "id": "project-cut",
            "revision": 2,
            "track_upserts": [],
            "removed_track_ids": [],
            "clip_upserts": [],
            "removed_clip_ids": [],
            "timeline_edit": {
                "retained_ranges": [
                    {"start_us": 0, "end_us": 4_000_000},
                    {"start_us": 6_000_000, "end_us": 10_000_000},
                ]
            },
            "source_cue_upserts": [],
            "removed_source_cue_ids": ["cue-1"],
            "source_cue_indices": {"cue-2": 1},
            "translated_cue_upserts": [],
            "removed_translated_cue_ids": [],
            "translated_cue_indices": {},
        }
    )

    assert model.durationUs == 8_000_000
    assert model._canonical_snapshot["timeline_edit"] is not None
    assert model._canonical_snapshot["source_cues"][0]["id"] == "cue-2"
    assert model._canonical_snapshot["source_cues"][0]["index"] == 1
    assert model.subtitle_cues.find("cue-1") == {}
    assert model.subtitle_cues.find("cue-2")["start_us"] == 4_000_000
    assert model.sourceToOutputUs(6_500_000) == 4_500_000
    assert model.outputToSourceUs(4_500_000) == 6_500_000
    descriptor = model.previewDescriptor(4_500_000)
    assert descriptor["available"] is True
    assert descriptor["sourcePositionMs"] == 6_500.0


def test_materialized_cut_patch_preserves_both_exact_fragment_ids() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(_cuttable_project_snapshot())

    model.apply_timeline_patch(
        {
            "_timeline_patch": True,
            "id": "project-cut",
            "revision": 2,
            "track_upserts": [],
            "removed_track_ids": [],
            "clip_upserts": [
                {
                    "id": "video-1",
                    "track_id": "V1",
                    "media_id": "video-media",
                    "timeline_range": {"start_us": 0, "end_us": 4_000_000},
                    "source_range": {"start_us": 0, "end_us": 4_000_000},
                    "speed": {"numerator": 1, "denominator": 1},
                    "volume": 1.0,
                },
                {
                    "id": "video-1::cut::1",
                    "track_id": "V1",
                    "media_id": "video-media",
                    "timeline_range": {"start_us": 4_000_000, "end_us": 8_000_000},
                    "source_range": {"start_us": 6_000_000, "end_us": 10_000_000},
                    "speed": {"numerator": 1, "denominator": 1},
                    "volume": 1.0,
                },
            ],
            "removed_clip_ids": ["video-1"],
            "timeline_edit": None,
        }
    )

    video_ids = [
        clip["id"]
        for track in model._canonical_snapshot["tracks"]
        if track["id"] == "V1"
        for clip in track["clips"]
    ]
    assert video_ids == ["video-1", "video-1::cut::1"]
    assert model.clip_info("video-1") is not None
    assert model.clip_info("video-1::cut::1") is not None


def test_timeline_patch_applies_materialized_overlay_and_voice_conform_deltas() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    snapshot = _cuttable_project_snapshot()
    snapshot["timeline_edit"] = {
        "retained_ranges": [{"start_us": 0, "end_us": 10_000_000}]
    }
    snapshot["voice_conform"] = {"plan": {"segments": []}}
    snapshot["overlays"] = [
        {
            "id": "overlay-keep",
            "kind": "blur",
            "range": {"start_us": 1_000_000, "end_us": 4_000_000},
            "enabled": True,
        },
        {
            "id": "overlay-remove",
            "kind": "blur",
            "range": {"start_us": 6_000_000, "end_us": 8_000_000},
            "enabled": True,
        },
    ]
    model.replace(snapshot)

    model.apply_timeline_patch(
        {
            "_timeline_patch": True,
            "id": "project-cut",
            "revision": 2,
            "track_upserts": [],
            "removed_track_ids": [],
            "clip_upserts": [],
            "removed_clip_ids": [],
            "timeline_edit": None,
            "voice_conform": None,
            "overlay_upserts": [
                {
                    "id": "overlay-keep",
                    "kind": "blur",
                    "range": {"start_us": 500_000, "end_us": 2_500_000},
                    "enabled": True,
                }
            ],
            "removed_overlay_ids": ["overlay-remove"],
        }
    )

    assert model._canonical_snapshot["voice_conform"] is None
    assert model._canonical_snapshot["overlays"] == [
        {
            "id": "overlay-keep",
            "kind": "blur",
            "range": {"start_us": 500_000, "end_us": 2_500_000},
            "enabled": True,
        }
    ]
    assert model.overlays == model._canonical_snapshot["overlays"]

    restored_plan = build_voice_conform_plan(
        (VoiceConformCueInput("cue-1", 1, 1_000_000, 2_000_000, 1_000_000),),
        source_duration_us=10_000_000,
        settings=VoiceConformSettings(mode=VoiceTimingMode.STRETCH_VIDEO),
    )
    restored_conform = {
        "plan": restored_plan.as_json(),
        "base_voice_media_id": "voice-base",
        "conformed_voice_media_id": "voice-conformed",
        "parent_tts_job_id": "tts-parent",
    }
    model.apply_timeline_patch(
        {
            "_timeline_patch": True,
            "id": "project-cut",
            "revision": 3,
            "track_upserts": [],
            "removed_track_ids": [],
            "clip_upserts": [],
            "removed_clip_ids": [],
            "voice_conform": restored_conform,
        }
    )

    assert model._canonical_snapshot["voice_conform"] == restored_conform
    assert model.voiceConformApplied is True


def test_legacy_media_commands_preserve_selected_fragment_id(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    snapshot = _cuttable_project_snapshot()
    snapshot["tracks"][0]["clips"].append(
        {
            **snapshot["tracks"][0]["clips"][0],
            "id": "video-1::cut::1",
            "timeline_range": {"start_us": 4_000_000, "end_us": 8_000_000},
            "source_range": {"start_us": 6_000_000, "end_us": 10_000_000},
        }
    )
    view_model.project_state.replace(snapshot)
    view_model.selectClip("video-1::cut::1")
    requests: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {})
        ),
    )

    view_model.resizeSelectedClip(4_000_000, 7_000_000)
    view_model._set_timeline_busy(False)
    view_model.moveSelectedClip(3_000_000)
    view_model._set_timeline_busy(False)
    view_model.setSelectedSpeed(125.0, False)

    assert [payload["clip_id"] for _, payload in requests] == [
        "video-1::cut::1",
        "video-1::cut::1",
        "video-1::cut::1",
    ]


def test_apply_timeline_cut_sends_revisioned_request_without_clip_selection(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(_cuttable_project_snapshot())
    requests: list[tuple[str, dict, dict]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, params=None, *, context=None: requests.append(
            (action, params or {}, context or {})
        ),
    )

    view_model.applyTimelineCut("remove_range", 2_000_000, 4_000_000)

    action, params, context = requests[-1]
    assert action == "timeline_command"
    assert params["command"] == "cut"
    assert params["operation"] == "remove_range"
    assert params["range_start_us"] == 2_000_000
    assert params["range_end_us"] == 4_000_000
    assert params["base_revision"] == 1
    assert params["request_id"] == context["request_id"]
    assert "clip_id" not in params
    assert view_model.timelineBusy is True
    assert "timeline_edit" not in view_model.project_state._canonical_snapshot


def test_timeline_cut_ack_projects_off_thread_then_commits_as_one_batch(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(_cuttable_project_snapshot())
    monkeypatch.setattr(view_model.runtime, "request", lambda *_args, **_kwargs: "ipc")
    projected: list[tuple[dict, dict, dict]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "project_timeline_patch",
        lambda snapshot, patch, context: projected.append((snapshot, patch, context)),
    )
    view_model.applyTimelineCut("remove_range", 2_000_000, 4_000_000)
    context = dict(view_model._timeline_cut_context)
    patch = {
        "_timeline_patch": True,
        "id": "project-cut",
        "revision": 2,
        "timeline_edit": {
            "retained_ranges": [
                {"start_us": 0, "end_us": 2_000_000},
                {"start_us": 4_000_000, "end_us": 10_000_000},
            ]
        },
        "track_upserts": [],
        "removed_track_ids": [],
        "clip_upserts": [],
        "removed_clip_ids": [],
    }

    view_model._action_succeeded("timeline_command", patch, context)

    assert len(projected) == 1
    assert view_model.project_state.revision == 1
    canonical, display = ProjectStateModel.prepare_timeline_patch(projected[0][0], patch)
    view_model._timeline_patch_projected(
        True,
        {"canonical": canonical, "projected": display, "patch": patch},
        context,
    )
    assert view_model.project_state.revision == 2
    assert view_model.project_state.durationUs == 8_000_000
    assert view_model.timelineBusy is False


def test_bulk_timeline_patch_is_linear_and_preserves_moves_and_track_order() -> None:
    from time import perf_counter

    clips = [
        {"id": f"c{i}", "track_id": "s1", "timeline_range": {
            "start_us": i * 1000, "end_us": i * 1000 + 500}}
        for i in range(12_000)
    ]
    current = [{"id": "s1", "clips": clips}, {"id": "s2", "clips": []}]
    before = perf_counter()
    patched = ProjectStateModel._patched_tracks(
        current, track_upserts=[], removed_track_ids=[],
        clip_upserts=[{**clip, "track_id": "s2"} for clip in clips],
        removed_clip_ids=[], track_order=["s2", "s1"],
    )
    elapsed = perf_counter() - before
    assert elapsed < 1.5, f"Bulk patch blocked for {elapsed:.3f}s"
    assert [track["id"] for track in patched] == ["s2", "s1"]
    assert len(patched[0]["clips"]) == 12_000 and not patched[1]["clips"]
    assert len(current[0]["clips"]) == 12_000 and not current[1]["clips"]
    assert all(clip["track_id"] == "s2" for clip in patched[0]["clips"])


@pytest.mark.parametrize("metadata", [
    {"unknown": {"cue_index": 1, "payload_revision": 2}},
    {"c": {"track_id": "s2"}},
    {"c": {"cue_index": True, "payload_revision": 2}},
    {"c": {"cue_index": 0, "payload_revision": 2}},
])
def test_invalid_clip_metadata_cannot_partially_change_snapshot(metadata):
    tracks = [{"id": "s1", "clips": [{"id": "c", "track_id": "s1"}]}]
    original = deepcopy(tracks)
    with pytest.raises(ValueError, match="metadata"):
        ProjectStateModel._patched_tracks(tracks, track_upserts=[], removed_track_ids=[],
            clip_upserts=[], removed_clip_ids=[], clip_metadata=metadata)
    assert tracks == original


def _large_subtitle_editor(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model._set_backend_ready(True)
    model.project_state.replace({"id": "large-edit", "name": "Large", "revision": 1,
        "source_cues": [{"id": f"c{i}", "index": i + 1, "text": "文字",
                         "range": {"start_us": i * 1000000, "end_us": (i+1) * 1000000}}
                        for i in range(1000)]})
    requests, projections = [], []
    monkeypatch.setattr(model.runtime, "request",
        lambda action, params=None, context=None: requests.append((action, params, context)))
    monkeypatch.setattr(model.runtime, "project_timeline_patch",
        lambda snapshot, patch, context: projections.append((snapshot, patch, context)))
    return model, requests, projections


def test_large_add_projection_serializes_background_saves_without_losing_cue(monkeypatch):
    model, requests, projections = _large_subtitle_editor(monkeypatch)
    model.updateSubtitleText("c0", "Đã sửa", "source")
    model._send_subtitle_command("add", values={"text": "Mới"}, needs_selection=False)
    assert len(requests) == 1 and requests[0][2]["background_subtitle_save"]
    updated = {**model.project_state._canonical_snapshot["source_cues"][0], "text": "Đã sửa"}
    model._action_succeeded("subtitle_command", {"_subtitle_patch": True,
        "id": "large-edit", "revision": 2, "source_cue_upserts": [updated]}, requests[0][2])
    QCoreApplication.instance().processEvents()
    assert len(requests) == 2 and requests[-1][1]["command"] == "add"
    context = requests[-1][2]
    added = {"id": "new", "index": 1001, "text": "Mới",
             "range": {"start_us": 1000000000, "end_us": 1001000000}}
    patch = {"_subtitle_patch": True, "id": "large-edit", "revision": 3,
             "source_cue_upserts": [added]}
    model._action_succeeded("subtitle_command", patch, context)
    assert model.subtitleBusy and len(projections) == 1
    model.updateSubtitleText("c1", "Sau thêm", "source")
    model._dispatch_background_subtitle_save()
    assert len(requests) == 2  # Do not apply an N+2 delta before its N+1 base.
    canonical, display = ProjectStateModel.prepare_subtitle_patch(projections[0][0], patch)
    model._timeline_patch_projected(True,
        {"canonical": canonical, "projected": display, "patch": patch}, projections[0][2])
    model._dispatch_background_subtitle_save()
    assert len(requests) == 3 and not model.subtitleBusy
    updated = {**canonical["source_cues"][1], "text": "Sau thêm"}
    model._action_succeeded("subtitle_command", {"_subtitle_patch": True,
        "id": "large-edit", "revision": 4, "source_cue_upserts": [updated]}, requests[-1][2])
    assert model.project_state.revision == 4
    assert model.project_state.subtitle_cues.find("new")["source_text"] == "Mới"
    assert model.project_state.subtitle_cues.find("c1")["source_text"] == "Sau thêm"


def test_project_refresh_does_not_retire_active_subtitle_delete(monkeypatch):
    model, requests, projections = _large_subtitle_editor(monkeypatch)
    model._send_subtitle_command("delete", cue_id="c0")
    context = requests[-1][2]
    model._action_succeeded("get_project", deepcopy(model.project_state._canonical_snapshot),
                           {"project_id": "large-edit"})
    assert model.subtitleBusy and model._subtitle_command_context == context
    patch = {"_subtitle_patch": True, "id": "large-edit", "revision": 2,
             "removed_source_cue_ids": ["c0"]}
    model._action_succeeded("subtitle_command", patch, context)
    canonical, display = ProjectStateModel.prepare_subtitle_patch(projections[0][0], patch)
    model._timeline_patch_projected(True,
        {"canonical": canonical, "projected": display, "patch": patch}, projections[0][2])
    assert not model.subtitleBusy and not model.project_state.subtitle_cues.find("c0")


@pytest.mark.parametrize("failure", ["projection", "disconnect", "watchdog", "send"])
def test_large_delete_failure_recovers_and_ignores_late_projection(monkeypatch, failure):
    model, requests, projections = _large_subtitle_editor(monkeypatch)
    if failure == "send":
        def closed(*_args, **_kwargs):
            raise OSError("closed socket")
        monkeypatch.setattr(model.runtime, "request", closed)
    model._send_subtitle_command("delete", cue_id="c0")
    if failure == "send":
        assert not model.subtitleBusy and model._timeline_reconcile_pending
        return
    patch = {"_subtitle_patch": True, "id": "large-edit", "revision": 2,
             "removed_source_cue_ids": ["c0"]}
    model._action_succeeded("subtitle_command", patch, requests[-1][2])
    deferred_delete = []
    assert model._defer_until_subtitle_saves_flushed(
        "second-delete", lambda: deferred_delete.append(True), "Chờ thao tác trước")
    canonical, display = ProjectStateModel.prepare_subtitle_patch(projections[0][0], patch)
    context = projections[0][2]
    model.updateSubtitleText("c1", "Chưa lưu", "source")
    model._send_effect_command("add_blur", overlay_id="pending-blur")
    assert model._subtitle_save_queue and model._pending_effect_commands
    if failure == "projection":
        model._timeline_patch_projected(False, {"message": "invalid patch"}, context)
    elif failure == "disconnect":
        model._set_backend_ready(False)
    else:
        model._subtitle_command_timed_out()
    assert not model.subtitleBusy and not model._subtitle_command_context
    assert model._timeline_reconcile_pending
    assert not model._subtitle_flush_callbacks and not deferred_delete
    model._dispatch_background_subtitle_save()
    assert not model._subtitle_save_queue and not model._pending_effect_commands
    assert model.project_state.subtitle_cues.find("c1")["source_text"] == "文字"
    model._timeline_patch_projected(True,
        {"canonical": canonical, "projected": display, "patch": patch}, context)
    assert model.project_state.revision == 1 and model.project_state.subtitle_cues.find("c0")


def test_multi_detail_waits_for_subtitle_projection_and_switch_clears_old_context(monkeypatch):
    model, requests, projections = _large_subtitle_editor(monkeypatch)
    model._send_subtitle_command("delete", cue_id="c0")
    context = requests[-1][2]
    model.openMultiVideoDetail("other")
    assert len(requests) == 1 and "multi-detail" in model._subtitle_flush_callbacks
    patch = {"_subtitle_patch": True, "id": "large-edit", "revision": 2,
             "removed_source_cue_ids": ["c0"]}
    model._action_succeeded("subtitle_command", patch, context)
    canonical, display = ProjectStateModel.prepare_subtitle_patch(projections[0][0], patch)
    model._timeline_patch_projected(True,
        {"canonical": canonical, "projected": display, "patch": patch}, projections[0][2])
    QCoreApplication.instance().processEvents()
    assert any(a == "get_project" and p == {"project_id": "other"} for a,p,_ in requests)
    model._apply_project({"id": "other", "name": "Other", "revision": 1})
    model._subtitle_command_timed_out()
    assert not model.subtitleBusy and not model._subtitle_command_context
    assert not model._pending_effect_commands and not model._subtitle_save_queue
    model._timeline_patch_projected(True,
        {"canonical": canonical, "projected": display, "patch": patch}, projections[0][2])
    assert model.project_state.projectId == "other"


def test_cancelled_coalesced_overlay_drag_restores_saved_geometry_and_settles_preview(monkeypatch):
    model, requests, _ = _large_subtitle_editor(monkeypatch)
    project = deepcopy(model.project_state._canonical_snapshot)
    project["overlays"] = [{"id": "blur", "kind": "blur", "x": .1, "y": .1,
                            "width": .2, "height": .2, "enabled": True,
                            "range": {"start_us": 0, "end_us": 1_000_000}}]
    model.project_state.replace(project)
    model._send_subtitle_command("delete", cue_id="c0")
    context = requests[-1][2]
    settled = []
    model.overlayGeometrySettled.connect(
        lambda identity, success: settled.append((identity, success)))
    model._send_effect_command("update", overlay_id="blur", values={"x": .2})
    model._send_effect_command("update", overlay_id="blur", values={"x": .3})
    assert len(model._pending_effect_commands) == 1
    model._action_failed("subtitle_command", {"message": "write failed"}, context)
    assert model.project_state.overlays[0]["x"] == .1
    assert settled == [("blur", False)]


def test_timeline_cut_projection_blocks_and_rolls_back_optimistic_subtitle_edits(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(_cuttable_project_snapshot())
    requests: list[tuple[str, dict, dict]] = []
    projected: list[tuple[dict, dict, dict]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, params=None, *, context=None: requests.append(
            (action, params or {}, context or {})
        ),
    )
    monkeypatch.setattr(
        view_model.runtime,
        "project_timeline_patch",
        lambda snapshot, patch, context: projected.append((snapshot, patch, context)),
    )
    view_model.applyTimelineCut("remove_range", 2_000_000, 4_000_000)
    context = dict(view_model._timeline_cut_context)
    patch = {
        "_timeline_patch": True,
        "id": "project-cut",
        "revision": 2,
        "timeline_edit": {
            "retained_ranges": [
                {"start_us": 0, "end_us": 2_000_000},
                {"start_us": 4_000_000, "end_us": 10_000_000},
            ]
        },
        "track_upserts": [],
        "removed_track_ids": [],
        "clip_upserts": [],
        "removed_clip_ids": [],
    }

    view_model._action_succeeded("timeline_command", patch, context)
    view_model.updateSubtitleText("cue-1", "\u5df2\u7f16\u8f91", "source")

    assert [action for action, _params, _context in requests] == ["timeline_command"]
    assert view_model._subtitle_save_queue == []
    assert view_model.project_state.subtitle_cues.get(0)["source_text"] == "\u4e00"
    assert view_model.project_state.revision == 1
    canonical, display = ProjectStateModel.prepare_timeline_patch(projected[0][0], patch)
    view_model._timeline_patch_projected(
        True,
        {"canonical": canonical, "projected": display, "patch": patch},
        context,
    )
    assert view_model.project_state.revision == 2
    assert view_model.project_state.durationUs == 8_000_000
    assert view_model.timelineBusy is False


def test_timeline_cut_timeout_reconciles_committed_head_before_next_cut(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(_cuttable_project_snapshot())
    requests: list[tuple[str, dict, dict]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, *, context=None: requests.append(
            (action, payload or {}, context or {})
        ),
    )
    view_model.applyTimelineCut("remove_range", 2_000_000, 4_000_000)

    view_model._timeline_cut_timed_out()

    reconcile = requests[-1]
    assert reconcile[0] == "get_project"
    assert reconcile[2]["timeline_reconcile"] is True
    assert view_model.timelineBusy is True
    committed = _cuttable_project_snapshot()
    committed["revision"] = 2
    committed["timeline_edit"] = {
        "retained_ranges": [
            {"start_us": 0, "end_us": 2_000_000},
            {"start_us": 4_000_000, "end_us": 10_000_000},
        ]
    }
    view_model._action_succeeded("get_project", committed, reconcile[2])

    assert view_model.project_state.revision == 2
    assert view_model.timelineBusy is False
    view_model.applyTimelineCut("keep_range", 0, 1_000_000)
    assert requests[-1][1]["base_revision"] == 2


def test_timeline_cut_failure_disconnect_and_stale_ack_release_only_current_request(
    monkeypatch,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(_cuttable_project_snapshot())
    monkeypatch.setattr(view_model.runtime, "request", lambda *_args, **_kwargs: "ipc")
    view_model.setPlayheadMs(7_000)
    view_model.applyTimelineCut("remove_range", 2_000_000, 4_000_000)
    context = dict(view_model._timeline_cut_context)

    view_model._action_succeeded(
        "timeline_command",
        {"_timeline_patch": True, "id": "project-cut", "revision": 99},
        {**context, "request_id": "stale"},
    )
    assert view_model.project_state.revision == 1
    assert view_model.timelineBusy is True

    view_model._set_backend_ready(False)
    assert view_model.timelineBusy is True
    assert view_model._timeline_reconcile_pending is True
    assert view_model.playheadUs == 7_000_000


def test_timeline_cut_waits_for_background_subtitle_save(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    view_model = ApplicationViewModel()
    view_model._set_backend_ready(True)
    view_model.project_state.replace(_cuttable_project_snapshot())
    calls: list[str] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, _params=None, *, context=None: calls.append(action),
    )
    view_model._subtitle_save_queue.append(
        {
            "params": {
                "project_id": "project-cut",
                "command": "update_text",
                "language": "source",
                "values": {"text": "Đã sửa"},
                "cue_id": "cue-1",
            },
            "context": {
                "background_subtitle_save": True,
                "project_id": "project-cut",
                "optimistic_changes": [],
            },
        }
    )

    view_model.applyTimelineCut("keep_range", 1_000_000, 8_000_000)

    assert calls == ["subtitle_command"]
    assert "timeline-cut" in view_model._subtitle_flush_callbacks
    callback = view_model._subtitle_flush_callbacks["timeline-cut"]
    view_model._subtitle_save_in_flight = False
    view_model._subtitle_flush_callbacks.clear()
    callback()
    assert calls == ["subtitle_command", "timeline_command"]


def _cue_role_model(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    MemorySettings.values = {}
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model._set_backend_ready(True)
    source = [
        {"id": "c1", "index": 1, "text": "原文", "role": "dialogue",
         "subtitle_track_id": "S1", "range": {"start_us": 0, "end_us": 1_000_000}},
        {"id": "c2", "index": 2, "text": "山河", "role": "scene_text",
         "subtitle_track_id": "S3", "range": {"start_us": 1_000_000, "end_us": 2_000_000}},
    ]
    model.project_state.replace({"id": "roles", "name": "Roles", "revision": 1,
        "source_cues": source, "title_cues": [{"id": "title", "index": 1,
            "text": "Kênh", "role": "title_text", "subtitle_track_id": "T1",
            "range": {"start_us": 0, "end_us": 2_000_000}}],
        "tracks": [{"id": cue["subtitle_track_id"], "kind": "subtitle", "name": "Text",
                    "clips": [{"id": "clip-" + cue["id"], "track_id": cue["subtitle_track_id"],
                               "cue_id": cue["id"], "timeline_range": cue["range"]}]}
                   for cue in source]})
    requests = []
    model.runtime.request = lambda action, params=None, context=None: requests.append(
        (action, params, context))
    return model, requests


def test_cue_classification_is_aggregate_and_conversion_preserves_selection_playhead(monkeypatch):
    model, requests = _cue_role_model(monkeypatch)
    assert not model.selectedCueClassification["can_convert"]
    model.setPlayheadMs(1250)
    model.selectTimelineCue("c1")
    assert model.selectedCueClassification == {
        "count": 1, "role": "dialogue", "track": "S1", "can_convert": True}
    model.selectTimelineCue("c2", True)
    assert model.selectedCueClassification == {
        "count": 2, "role": "mixed", "track": "", "can_convert": True}
    model.setSelectedCueRole("dialogue")
    assert requests[-1][1]["command"] == "set_cue_role"
    assert requests[-1][1]["values"] == {"cue_ids": ["c1", "c2"], "role": "dialogue"}
    assert model.selectedCueIds == ["c1", "c2"]
    assert model.playheadUs == 1_250_000


def test_cue_conversion_rejects_empty_title_mixed_title_and_invalid_role(monkeypatch):
    model, requests = _cue_role_model(monkeypatch)
    model.setSelectedCueRole("scene_text")
    model.selectTimelineCue("title")
    assert model.selectedCueClassification["role"] == "title_text"
    assert not model.selectedCueClassification["can_convert"]
    model.setSelectedCueRole("dialogue")
    model.selectTimelineCue("c1", True)
    assert model.selectedCueIds == ["c1"]  # Timeline keeps title/subtitle groups separate.
    model._selected_cue_ids = ["title", "c1"]
    assert not model.selectedCueClassification["can_convert"]
    model.setSelectedCueRole("scene_text")
    model.selectTimelineCue("c1")
    model.setSelectedCueRole("title_text")
    assert requests == []


def test_cue_role_conversion_flushes_text_and_captures_original_selection(monkeypatch):
    model, requests = _cue_role_model(monkeypatch)
    model.selectTimelineCue("c1")
    model.updateSubtitleText("c1", "Đã sửa chữ", "source")
    model.setSelectedCueRole("scene_text")
    assert len(requests) == 1
    assert requests[0][1]["command"] == "update_text"
    callback = model._subtitle_flush_callbacks["subtitle-command-set_cue_role"]
    model.selectTimelineCue("c2")
    model._subtitle_save_in_flight = False
    model._subtitle_flush_callbacks.clear()
    callback()
    assert requests[-1][1]["command"] == "set_cue_role"
    assert requests[-1][1]["values"]["cue_ids"] == ["c1"]
