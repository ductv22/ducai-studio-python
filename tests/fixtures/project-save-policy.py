from copy import deepcopy

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest

from ducai_studio.ui.viewmodels import application_view_model as application_module
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel, JobRuntime


class MemorySettings:
    def __init__(self, _parent=None):
        self.values = {}

    def value(self, key, default=None, *, type=None):
        value = self.values.get(key, default)
        return type(value) if type is not None else value

    def setValue(self, key, value):
        self.values[key] = value


@pytest.fixture
def editor(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(application_module, "QSettings", MemorySettings)
    model = ApplicationViewModel()
    model._apply_project({"id": "p", "name": "Save", "revision": 1})
    model._set_backend_ready(True)
    requests = []
    monkeypatch.setattr(model.runtime, "request", lambda action, params=None, context=None:
                        requests.append((action, params, deepcopy(context))))
    yield model, requests
    model._project_autosave_timer.stop()
    model._project_save_watchdog.stop()


def dirty(model, revision=2):
    model._apply_project({"id": "p", "name": "Save", "revision": revision,
                          "_project_dirty": True, "_saved_revision": 1})


def ack(model, requests, revision):
    context = requests[-1][2]
    model._action_succeeded("save_project_edits", {"id": "p", "revision": revision,
                           "_saved_revision": revision, "_project_dirty": False}, context)


def test_editor_rpc_defers_persistence_but_not_jobs(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._ready = True
    calls = []
    monkeypatch.setattr(runtime, "_request", lambda a, p=None, context=None: calls.append((a, p)))
    for action in ("subtitle_command", "timeline_command", "effect_command"):
        runtime.request(action, {"project_id": "p"})
        assert calls[-1][1]["deferred"] is True
    runtime.request("create_ocr_job", {"project_id": "p"})
    assert "deferred" not in calls[-1][1]


def test_manual_and_five_minute_save_do_not_restart_timer_on_each_edit(editor):
    model, requests = editor
    dirty(model)
    assert model.projectDirty and model._project_autosave_timer.interval() == 300_000
    remaining = model._project_autosave_timer.remainingTime()
    QTest.qWait(30)
    dirty(model, 3)
    assert model._project_autosave_timer.remainingTime() < remaining
    model.projectAutosaveRequested.connect(model.saveProject)
    model._project_autosave_timer.timeout.emit()
    assert requests[-1][0] == "save_project_edits"
    assert not model.subtitleBusy and not model.timelineBusy
    ack(model, requests, 3)
    assert not model.projectDirty and not model.projectSaveBusy
    assert not model._project_autosave_timer.isActive()
    assert model.saveState == "Đã lưu"


def test_clean_or_saving_project_does_not_request_autosave(editor):
    model, requests = editor
    events = []
    model.projectAutosaveRequested.connect(lambda: events.append(True))
    model._project_autosave_timer.timeout.emit()
    assert not events
    dirty(model)
    assert model._project_autosave_timer.isSingleShot()
    model.saveProject()
    assert not model._project_autosave_timer.isActive()
    model._project_autosave_timer.timeout.emit()
    assert not events
    ack(model, requests, 2)
    model._project_autosave_timer.timeout.emit()
    assert not events
    dirty(model, 3)
    assert model._project_autosave_timer.isActive()
    model._project_autosave_timer.timeout.emit()
    assert events == [True]


@pytest.mark.parametrize("timeout", [True, False])
def test_failed_save_retains_draft_releases_gate_and_cancels_switch(editor, timeout):
    model, requests = editor
    dirty(model)
    model.openProject("other")
    assert requests[-1][0] == "save_project_edits"
    if timeout:
        model._project_save_timed_out()
    else:
        model._action_failed("save_project_edits", {"message": "disk full"}, requests[-1][2])
    QCoreApplication.processEvents()
    assert model.projectDirty and not model.projectSaveBusy
    assert not model.subtitleBusy and not model.timelineBusy
    assert not any(action == "get_project" for action, _, _ in requests)
    model.saveProject()
    ack(model, requests, 2)
    assert not model.projectDirty


def test_late_save_ack_does_not_mark_new_edits_saved_or_switch_early(editor):
    model, requests = editor
    dirty(model)
    model.openProject("other")
    first_context = deepcopy(requests[-1][2])
    dirty(model, 3)
    ack(model, requests, 2)
    QCoreApplication.processEvents()
    assert model.projectDirty and requests[-1][0] == "save_project_edits"
    assert not any(action == "get_project" for action, _, _ in requests)
    model._action_succeeded("save_project_edits", {"id": "p", "revision": 2,
                           "_saved_revision": 2, "_project_dirty": False}, first_context)
    assert model.projectSaveBusy
    ack(model, requests, 3)
    QCoreApplication.processEvents()
    assert requests[-1][0] == "get_project" and requests[-1][1]["project_id"] == "other"
    model._apply_project({"id": "other", "name": "Other", "revision": 1})
    assert not model.projectDirty and model.saveState == "Đã lưu"


def test_save_waits_for_effect_and_timeline_ack(editor):
    model, requests = editor
    model._set_timeline_busy(True)
    model.saveProject()
    assert not requests
    model._set_timeline_busy(False)
    dirty(model)
    model._run_subtitle_flush_callbacks()
    QCoreApplication.processEvents()
    assert requests[-1][0] == "save_project_edits"
