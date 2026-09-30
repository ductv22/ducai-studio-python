from __future__ import annotations

import time
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


@pytest.mark.parametrize("response", [{}, [], "invalid", None])
def test_malformed_translation_preferences_response_keeps_current_project(
    monkeypatch, response,
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
    view_model.project_state.replace(
        {"id": "project-whisper", "name": "Phim", "revision": 1}
    )
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
        role
        for role, name in view_model.multi_videos.roleNames().items()
        if name == b"selected"
    )
    assert view_model.multiSelectedCount == 2
    assert all(
        view_model.multi_videos.data(view_model.multi_videos.index(row, 0), selected_role)
        is True
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
        role
        for role, name in view_model.multi_videos.roleNames().items()
        if name == b"selected"
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
    assert model.data(model.index(0, 0), roles[b"statusText"]) == (
        "Thủ công · Đang dịch 25%"
    )
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
    assert model.data(first, roles[b"thumbnailUrl"]).toString().startswith(
        "image://multi-thumbnail/"
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
    assert model.data(needs_edit, roles[b"statusText"]) == (
        "Còn chữ Trung · 10 cue · đã bỏ qua"
    )
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
    assert view_model.multi_videos.data(card, roles[b"videoUrl"]).toString().endswith(
        "/source.mp4"
    )
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
    assert "Đang tạm dừng" in view_model.multi_videos.data(
        card, roles[b"statusText"]
    )


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
    runtime.actionSucceeded.connect(
        lambda action, _result, _context: succeeded.append(action)
    )

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
    assert model.data(model.index(0, 0), roles[b"statusText"]) == (
        "Đã tạm dừng · dịch 50%"
    )
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
        "Đang khôi phục cache · lồng tiếng 40%"
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
    assert (
        view_model.project_state.subtitle_cues.get(0)["translated_text"]
        == "Đây là chiến hạm"
    )
    assert requests[-1][0] == "subtitle_command"
    assert requests[-1][1]["language"] == "translated"
    assert requests[-1][1]["compact"] is True
    assert requests[-1][1]["values"]["replacement"] == " hạm"

    view_model._action_failed(
        "subtitle_command",
        {"message": "disk full"},
        requests[-1][2],
    )

    assert (
        view_model.project_state.subtitle_cues.get(0)["translated_text"]
        == "Đây là chiếnhạm"
    )
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
    }
    context = requests[-1][2]
    assert isinstance(context, dict)
    assert [
        item["previous_start_us"] for item in context["optimistic_changes"]
    ] == [1_000_000, 3_000_000]

    view_model._action_failed(
        "subtitle_command",
        {"message": "Không thể lưu"},
        context,
    )

    assert view_model.project_state.subtitle_cues.find("scene-1")["start_us"] == 1_000_000
    assert view_model.project_state.subtitle_cues.find("scene-2")["start_us"] == 3_000_000


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
            "subtitle_track_upserts": [
                {"id": "S3", "kind": "subtitle", "name": "Chữ trong cảnh"}
            ],
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

    assert model.subtitleCueIdsInRange(900_000, 2_100_000, 1, 1) == ["scene-1"]
    assert model.subtitleCueIdsInRange(0, 5_000_000, 1, 2) == [
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
    assert (
        view_model.project_state.subtitle_cues.get(0)["translated_text"]
        == "Đây là 朋友 của tôi"
    )


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
        "effects": 6,
        "mixer": 6,
        "export": 6,
        "queue": 6,
        "settings": 7,
    }

    for workspace, expected_index in expected_indexes.items():
        view_model.navigate(workspace)
        assert view_model.currentWorkspace == workspace
        assert view_model.currentIndex == expected_index
        assert "sẽ được nối" not in view_model.statusMessage


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
    view_model.project_state.replace(
        {"id": "project-export", "name": "Phim", "revision": 8}
    )
    requests: list[tuple[str, dict, object]] = []
    monkeypatch.setattr(
        view_model.runtime,
        "request",
        lambda action, payload=None, context=None: requests.append(
            (action, payload or {}, context)
        ),
    )
    output = tmp_path / "edited.srt"

    view_model.exportSubtitleSrt(
        "translated", QUrl.fromLocalFile(str(output)).toString()
    )

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


def test_failed_translation_with_different_provider_starts_a_new_job(monkeypatch) -> None:
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
    view_model._translation_job_state = "failed"
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

    assert requests[0][0] == "create_translation_job"
    assert requests[0][1]["provider_id"] == "gemini-api"
    assert requests[0][1]["model_id"] == "gemini-3.5-flash-lite"


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

    assert (
        view_model.project_state.overlay("overlay-default-blur")["concealment_mode"]
        == "cover"
    )
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
    assert model.trackModel.count == 1
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
