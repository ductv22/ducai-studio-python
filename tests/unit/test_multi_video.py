from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QTimer

from ducai_studio.domain.jobs import JobState
from ducai_studio.domain.multi_video import (
    MultiBatchState,
    MultiItemState,
    MultiStage,
    stage_local_progress,
    stage_progress,
)
from ducai_studio.domain.project import MediaAsset, MediaKind, ProjectSnapshot
from ducai_studio.infrastructure.database import Database
from ducai_studio.infrastructure.multi_video_repository import MultiVideoRepository
from ducai_studio.job_manager.multi_video_coordinator import MultiVideoCoordinator
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.media_probe import MediaProbeResult
from ducai_studio.ui.viewmodels.project_job_models import MultiVideoListModel


class MemoryMultiRepository:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items
        self.batch: dict[str, object] = {
            "state": "running",
            "retry_pass": 0,
            "current_project_id": "",
            "config": {},
        }
        self.attempts: list[tuple[object, ...]] = []
        self.finished_attempts: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.reset_calls: list[str] = []

    def get_batch(self) -> dict[str, object]:
        return dict(self.batch)

    def list_items(self) -> list[dict[str, object]]:
        return [dict(item) for item in self.items]

    def update_batch(self, **values: object) -> dict[str, object]:
        self.batch.update(values)
        return self.get_batch()

    def update_item(self, project_id: str, **values: object) -> dict[str, object]:
        item = next(item for item in self.items if item["project_id"] == project_id)
        item.update(values)
        return dict(item)

    def add_attempt(self, *values: object) -> str:
        self.attempts.append(values)
        return "attempt"

    def finish_attempt(self, *values: object, **kwargs: object) -> None:
        self.finished_attempts.append((values, dict(kwargs)))

    def remove_project(self, project_id: str) -> None:
        self.items = [item for item in self.items if item["project_id"] != project_id]

    def reset_for_start(self, config: object) -> None:
        del config
        self.reset_calls.append("fresh")
        self.batch.update(state="running", current_project_id="")
        for item in self.items:
            if item.get("selected") is True:
                item.update(
                    stage="ocr",
                    state="waiting",
                    progress=0.0,
                    child_job_id="",
                    retry_count=0,
                    error_code="",
                    error_payload={},
                )

    def reset_for_retry_start(self, config: object) -> None:
        del config
        self.reset_calls.append("retry")
        self.batch.update(state="running", current_project_id="")
        for item in self.items:
            if item.get("selected") is True and item.get("state") != "completed":
                item.update(
                    state="waiting",
                    retry_count=0,
                    error_code="",
                    error_payload={},
                )


class FakeServer(QObject):
    def __init__(self, items: list[dict[str, object]], jobs: dict[str, object]) -> None:
        super().__init__()
        repository = MemoryMultiRepository(items)
        self.service = SimpleNamespace(
            multi_videos=repository,
            jobs=SimpleNamespace(get=lambda job_id: jobs[job_id]),
            refresh_translation_multi_policy=self._refresh_translation_multi_policy,
            list_multi_videos=lambda: {
                "batch": repository.get_batch(),
                "items": repository.list_items(),
            },
            remove_multi_project=lambda project_id: (
                repository.remove_project(project_id)
                or {
                    "batch": repository.get_batch(),
                    "items": repository.list_items(),
                }
            ),
        )
        self.jobs = jobs
        self.started: list[dict[str, object]] = []
        self.resumed: list[str] = []
        self.paused: list[str] = []
        self.cancelled: list[str] = []
        self.multi_status_broadcasts = 0

    def _refresh_translation_multi_policy(self, *, job_id: str) -> dict[str, object]:
        job = self.jobs[job_id]
        config = dict(job.spec.provider_config)
        config.update(MultiVideoCoordinator.MULTI_TRANSLATION_POLICY)
        config["headed"] = False
        job.spec.provider_config = config
        return {"id": job_id}

    def _retry_job(self, job_id: str) -> None:
        self.jobs[job_id].state = JobState.QUEUED

    def _start_job(self, params: dict[str, object]) -> None:
        self.started.append(dict(params))

    def _resume_job(self, job_id: str) -> None:
        self.resumed.append(job_id)

    def _pause_job(self, job_id: str) -> None:
        self.paused.append(job_id)
        self.jobs[job_id].state = JobState.PAUSE_REQUESTED

    def _cancel_job(self, job_id: str) -> None:
        self.cancelled.append(job_id)
        self.jobs[job_id].state = JobState.CANCELLED

    def _broadcast_multi_status(self) -> None:
        self.multi_status_broadcasts += 1


class AsyncCancelServer(FakeServer):
    def _cancel_job(self, job_id: str) -> None:
        self.cancelled.append(job_id)
        self.jobs[job_id].state = JobState.CANCEL_REQUESTED


class _PauseDeadlineJobs:
    def __init__(self, job: object) -> None:
        self.job = job
        self.events: list[dict[str, object]] = []

    def get(self, _job_id: str) -> object:
        return self.job

    def append_worker_event(self, **values: object) -> tuple[int, object]:
        self.events.append(dict(values))
        transitions = values.get("transitions", {})
        if isinstance(transitions, dict):
            target = transitions.get(self.job.state)
            if target is not None:
                self.job.state = target
        return len(self.events), self.job


class _PauseDeadlineWorkers:
    def __init__(self) -> None:
        self.stops: list[tuple[str, int]] = []

    def has_worker(self, _job_id: str) -> bool:
        return True

    def request_stop(self, job_id: str, *, grace_ms: int) -> None:
        self.stops.append((job_id, grace_ms))


def test_stage_progress_maps_each_child_stage_to_one_third() -> None:
    assert stage_progress("ocr", 0.5) == 1 / 6
    assert stage_progress("translate", 0.5) == 0.5
    assert stage_progress("tts", 0.5) == 5 / 6
    assert stage_progress("done", 0) == 1.0


def test_stage_local_progress_recovers_each_child_percentage() -> None:
    assert stage_local_progress("ocr", 0.2) == pytest.approx(0.6)
    assert stage_local_progress("translate", 0.5) == pytest.approx(0.5)
    assert stage_local_progress("tts", 0.75) == pytest.approx(0.25)
    assert stage_local_progress("done", 0.0) == 1.0
    assert stage_local_progress("translate", stage_progress("translate", 0.0)) == 0.0


def test_multi_card_shows_whisper_child_phase_with_stage_local_percent() -> None:
    text = MultiVideoListModel._state_text(
        {
            "project_id": "project-1",
            "stage": "ocr",
            "state": "running",
            "batch_state": "running",
            "child_phase": "transcribe",
            "progress": stage_progress("ocr", 0.42),
        }
    )

    assert text == "Whisper đang nhận dạng · 42%"


def test_multi_catalog_sync_keeps_auto_cards_unselected_and_remove_hidden(tmp_path: Path) -> None:
    database = Database(tmp_path / "multi-catalog.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        for project_id in ("manual", "discovered"):
            connection.execute(
                """
                INSERT INTO projects(id, path, name, schema_version, head_revision, updated_at)
                VALUES (?, ?, ?, 1, 1, '2026-08-29T00:00:00Z')
                """,
                (project_id, str(tmp_path / f"{project_id}.ducai"), project_id),
            )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["manual"], selected=True)
    repository.remove_project("manual")
    repository.sync_projects(["manual", "discovered"])

    assert [item["project_id"] for item in repository.list_items()] == ["discovered"]
    assert repository.list_items()[0]["selected"] is False
    assert repository.hidden_project_ids() == ("manual",)

    repository.add_projects(["manual"])
    restored = {item["project_id"]: item for item in repository.list_items()}
    assert restored["manual"]["selected"] is True
    assert repository.hidden_project_ids() == ()


def test_multi_catalog_discovery_requests_the_complete_project_catalog(
    monkeypatch, tmp_path: Path
) -> None:
    """Multi must not silently omit videos after an arbitrary project-count cap."""

    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    requested_limits: list[int | None] = []

    def list_summaries(*, limit: int | None = 100) -> tuple[dict[str, object], ...]:
        requested_limits.append(limit)
        return ()

    monkeypatch.setattr(service.projects, "list_summaries", list_summaries)

    assert service.list_multi_videos()["items"] == []
    assert requested_limits == [None]


def test_multi_selection_returns_lightweight_status_without_reloading_catalog(
    monkeypatch, tmp_path: Path
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    calls: list[tuple[str, object]] = []
    expected = {
        "batch": {"state": "idle"},
        "items": [{"project_id": "first", "selected": True}],
        "partial": True,
    }
    monkeypatch.setattr(service, "_require_multi_membership_editable", lambda: None)
    monkeypatch.setattr(
        service.multi_videos,
        "set_selected",
        lambda project_id, selected: calls.append((project_id, selected)),
    )
    monkeypatch.setattr(
        service.multi_videos,
        "set_all_selected",
        lambda selected: calls.append(("all", selected)),
    )
    monkeypatch.setattr(service, "list_multi_video_status", lambda: expected)
    monkeypatch.setattr(
        service,
        "list_multi_videos",
        lambda: pytest.fail("Selection must not decode the full project catalog"),
    )

    assert service.select_multi_project("first", True) is expected
    assert service.select_all_multi_projects(False) is expected
    assert calls == [("first", True), ("all", False)]


def test_retry_reuses_the_exact_failed_job_and_checkpoint() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RETRYING.value,
        "child_job_id": "translation-job",
        "retry_count": 1,
        "final_pass": False,
    }
    job = SimpleNamespace(state=JobState.FAILED)
    server = FakeServer([item], {"translation-job": job})
    coordinator = MultiVideoCoordinator(server)

    coordinator._launch_or_skip(item)

    assert server.started[0]["job_id"] == "translation-job"
    assert server.started[0]["resume_checkpoint_key"] == "latest"
    assert server.service.multi_videos.items[0]["child_job_id"] == "translation-job"
    assert server.service.multi_videos.items[0]["state"] == MultiItemState.RETRYING.value


def test_queued_child_left_by_interruption_is_started_instead_of_recreated() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RETRYING.value,
        "child_job_id": "translation-job",
        "retry_count": 2,
        "final_pass": False,
    }
    server = FakeServer(
        [item],
        {"translation-job": SimpleNamespace(state=JobState.QUEUED)},
    )
    coordinator = MultiVideoCoordinator(server)

    coordinator._launch_or_skip(item)

    assert [request["job_id"] for request in server.started] == ["translation-job"]
    assert server.service.multi_videos.items[0]["state"] == MultiItemState.RETRYING.value


def test_interrupted_child_waits_for_explicit_resume_and_restarts_once() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TTS.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "tts-job",
        "retry_count": 0,
        "final_pass": False,
    }
    job = SimpleNamespace(state=JobState.INTERRUPTED)
    server = FakeServer([item], {"tts-job": job})
    server.service.multi_videos.batch.update(
        state=MultiBatchState.RUNNING.value,
        current_project_id="project-1",
        config={},
    )
    coordinator = MultiVideoCoordinator(server)

    assert coordinator._check_child(item) is True
    assert server.started == []
    assert server.service.multi_videos.batch["state"] == MultiBatchState.INTERRUPTED.value
    assert server.service.multi_videos.items[0]["error_code"] == "child_interrupted"

    coordinator.resume()

    assert [request["job_id"] for request in server.started] == ["tts-job"]
    assert server.service.multi_videos.batch["state"] == MultiBatchState.RUNNING.value
    assert len(server.service.multi_videos.attempts) == 1
    assert server.service.multi_videos.finished_attempts[0][1]["state"] == "interrupted"


def test_synchronous_worker_start_failure_enters_normal_retry_policy() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RETRYING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
    }
    job = SimpleNamespace(
        state=JobState.QUEUED,
        error_code="",
        error_payload={},
    )
    server = FakeServer([item], {"translation-job": job})

    def fail_start(_params: dict[str, object]) -> None:
        job.state = JobState.FAILED
        job.error_code = "worker_start_failed"
        job.error_payload = {"message": "cannot spawn"}
        raise RuntimeError("cannot spawn")

    server._start_job = fail_start  # type: ignore[method-assign]
    coordinator = MultiVideoCoordinator(server)
    delays: list[int] = []
    coordinator._schedule_advance = delays.append  # type: ignore[method-assign]

    coordinator._launch_or_skip(item)

    stored = server.service.multi_videos.items[0]
    assert stored["state"] == MultiItemState.RETRYING.value
    assert stored["retry_count"] == 1
    assert delays == [MultiVideoCoordinator.RETRY_DELAYS_MS[0]]


def test_multi_disk_preflight_reports_specific_action_without_starting_worker() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TTS.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "tts-job",
        "retry_count": 0,
        "final_pass": False,
    }
    job = SimpleNamespace(state=JobState.INTERRUPTED)
    server = FakeServer([item], {"tts-job": job})
    coordinator = MultiVideoCoordinator(server)

    def reject_stage(_stage: MultiStage) -> None:
        raise OSError(errno.ENOSPC, "Không đủ dung lượng để lồng tiếng")

    coordinator._ensure_stage_capacity = reject_stage  # type: ignore[method-assign]
    coordinator._schedule_advance = lambda _delay=0: None  # type: ignore[method-assign]

    coordinator._start_existing_child(
        "project-1",
        MultiStage.TTS,
        "tts-job",
        counts_as_retry=False,
    )

    stored = server.service.multi_videos.items[0]
    assert server.started == []
    assert stored["error_code"] == "insufficient_disk_space"
    assert "Dọn đủ dung lượng" in stored["error_payload"]["action"]
    assert "API key" not in stored["error_payload"]["action"]


def test_deferred_items_wait_until_final_pass() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.DEFERRED.value,
        "child_job_id": "",
        "retry_count": 6,
        "final_pass": False,
    }
    server = FakeServer([item], {})
    coordinator = MultiVideoCoordinator(server)
    launched: list[dict[str, object]] = []
    coordinator._launch_or_skip = lambda value: launched.append(dict(value))  # type: ignore[method-assign]

    coordinator._advance()

    assert server.service.multi_videos.batch["retry_pass"] == 1
    assert launched[0]["state"] == MultiItemState.WAITING.value
    assert launched[0]["final_pass"] is True


def test_first_pass_has_one_initial_attempt_and_five_retries() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "job",
        "retry_count": 5,
        "final_pass": False,
    }
    server = FakeServer([item], {})
    coordinator = MultiVideoCoordinator(server)
    coordinator._schedule_advance = lambda _delay=0: None  # type: ignore[method-assign]

    coordinator._stage_failed(item, "worker_failed", {"message": "failed"})

    stored = server.service.multi_videos.items[0]
    assert stored["retry_count"] == 6
    assert stored["state"] == MultiItemState.DEFERRED.value


def test_multi_ocr_review_advances_to_translation_without_retry() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "ocr-job",
        "retry_count": 0,
        "final_pass": False,
    }
    job = SimpleNamespace(
        state=JobState.FAILED,
        error_code="ocr_review_required",
        error_payload={"blocker_count": 2, "coverage_ratio": 0.968},
    )
    server = FakeServer([item], {"ocr-job": job})
    server.service.multi_videos.batch["current_project_id"] = "project-1"
    coordinator = MultiVideoCoordinator(server)
    delays: list[int] = []
    coordinator._schedule_advance = delays.append  # type: ignore[method-assign]

    assert coordinator._check_child(item) is True

    stored = server.service.multi_videos.items[0]
    assert stored["stage"] == MultiStage.TRANSLATE.value
    assert stored["state"] == MultiItemState.WAITING.value
    assert stored["retry_count"] == 0
    assert stored["error_code"] == ""
    assert server.service.multi_videos.finished_attempts[-1][1]["state"] == "succeeded"
    assert delays == [0]


def test_resume_adopts_ocr_review_deferred_by_an_older_release() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.DEFERRED.value,
        "child_job_id": "ocr-job",
        "retry_count": 6,
        "final_pass": False,
        "error_code": "ocr_review_required",
    }
    server = FakeServer([item], {})
    coordinator = MultiVideoCoordinator(server)
    delays: list[int] = []
    coordinator._schedule_advance = delays.append  # type: ignore[method-assign]

    coordinator._advance()

    stored = server.service.multi_videos.items[0]
    assert stored["stage"] == MultiStage.TRANSLATE.value
    assert stored["state"] == MultiItemState.WAITING.value
    assert stored["retry_count"] == 0
    assert stored["error_code"] == ""
    assert delays == [0]


def test_resume_adopts_old_ocr_review_while_another_project_remains_current() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    legacy = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.FAILED.value,
        "child_job_id": "old-ocr-job",
        "retry_count": 1,
        "final_pass": True,
        "error_code": "ocr_review_required",
    }
    current = {
        "project_id": "project-2",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.WAITING.value,
        "child_job_id": "",
        "retry_count": 0,
        "final_pass": True,
        "error_code": "",
    }
    server = FakeServer([legacy, current], {})
    server.service.multi_videos.batch["current_project_id"] = "project-2"
    coordinator = MultiVideoCoordinator(server)
    delays: list[int] = []
    coordinator._schedule_advance = delays.append  # type: ignore[method-assign]

    coordinator._advance()

    stored = server.service.multi_videos.items[0]
    assert stored["stage"] == MultiStage.TRANSLATE.value
    assert stored["state"] == MultiItemState.WAITING.value
    assert stored["final_pass"] is False
    assert stored["error_code"] == ""
    assert delays == [0]


def test_resume_recovers_retry_budget_consumed_by_translation_ledger_defect() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.FAILED.value,
        "child_job_id": "translation-job",
        "retry_count": 6,
        "final_pass": False,
        "error_code": "ValueError",
        "error_payload": {
            "message": MultiVideoCoordinator.TRANSLATION_LEDGER_CACHE_ERROR,
        },
    }
    job = SimpleNamespace(
        state=JobState.FAILED,
        spec=SimpleNamespace(
            provider_config={
                "qa_policy": "multi_auto",
                "headed": False,
            }
        ),
    )
    server = FakeServer([item], {"translation-job": job})
    coordinator = MultiVideoCoordinator(server)

    coordinator._advance()

    stored = server.service.multi_videos.items[0]
    assert stored["state"] == MultiItemState.RUNNING.value
    assert stored["retry_count"] == 0
    assert stored["final_pass"] is False
    assert stored["error_code"] == ""
    assert server.service.multi_videos.batch["current_project_id"] == "project-1"
    assert server.started[-1]["job_id"] == "translation-job"
    assert server.started[-1]["resume_checkpoint_key"] == "latest"
    assert server.service.multi_videos.attempts[-1][0:2] == (
        "project-1",
        MultiStage.TRANSLATE.value,
    )


def test_resume_forces_legacy_gemini_web_batch_to_background_mode() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    server = FakeServer([], {})
    server.service.multi_videos.batch.update(
        {
            "state": "interrupted",
            "config": {
                "translation": {
                    "provider_id": "gemini-web",
                    "provider_options": {"headed": True, "guest": True},
                }
            },
        }
    )
    coordinator = MultiVideoCoordinator(server)
    coordinator._advance = lambda: None  # type: ignore[method-assign]

    coordinator.resume()

    options = coordinator._settings["translation"]["provider_options"]
    assert options["headed"] is False
    assert options["guest"] is True
    translation = coordinator._settings["translation"]
    assert translation["qa_policy"] == "multi_auto"
    assert translation["cjk_is_blocker"] is True
    assert translation["auto_compact_overlong_once"] is True
    assert translation["max_overlong_compaction_passes"] == 1


def test_multi_translation_job_receives_cjk_only_qa_policy() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.WAITING.value,
        "child_job_id": "",
        "retry_count": 0,
        "final_pass": False,
    }
    server = FakeServer([item], {})
    captured: dict[str, object] = {}

    def create_translation_job(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"id": "translation-job", "reused": False}

    server.service.create_translation_job = create_translation_job
    coordinator = MultiVideoCoordinator(server)
    coordinator._settings = coordinator._unattended_settings(
        {
            "translation": {
                "provider_id": "gemini-web",
                "model_id": "selected-on-web",
                "provider_options": {
                    "profile_path": "gemini-web-profile",
                    "headed": True,
                    "guest": True,
                },
            }
        }
    )

    coordinator._start_child("project-1", MultiStage.TRANSLATE)

    provider_options = captured["provider_options"]
    assert isinstance(provider_options, dict)
    assert provider_options["headed"] is False
    assert provider_options["qa_policy"] == "multi_auto"
    assert provider_options["cjk_is_blocker"] is True
    assert provider_options["auto_compact_overlong_once"] is True
    assert provider_options["max_overlong_compaction_passes"] == 1


@pytest.mark.parametrize(
    ("ocr_options", "expected_method"),
    [
        ({}, "frame_ocr"),
        (
            {
                "extraction_method": "whisper_audio",
                "engine_python": "whisper-python.exe",
                "device": "auto",
                "language": "zh",
                "model_id": "large-v3",
                "model_path": "large-v3-model",
            },
            "whisper_audio",
        ),
    ],
)
def test_multi_ocr_snapshot_defaults_legacy_to_frame_and_preserves_whisper(
    ocr_options: dict[str, object],
    expected_method: str,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.WAITING.value,
        "child_job_id": "",
        "retry_count": 0,
        "final_pass": False,
    }
    server = FakeServer([item], {})
    captured: dict[str, object] = {}

    def create_ocr_job(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"id": "ocr-job", "reused": False}

    server.service.create_ocr_job = create_ocr_job
    coordinator = MultiVideoCoordinator(server)
    coordinator._settings = {"ocr": ocr_options}

    coordinator._start_child("project-1", MultiStage.OCR)

    assert captured["extraction_method"] == expected_method
    if expected_method == "whisper_audio":
        assert captured["engine_python"] == "whisper-python.exe"
        assert captured["model_path"] == "large-v3-model"


def test_resume_replaces_paused_legacy_translation_child_without_retry_penalty() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "legacy-translation-job",
        "retry_count": 2,
        "final_pass": False,
        "error_code": "",
    }
    legacy_job = SimpleNamespace(
        state=JobState.PAUSED,
        spec=SimpleNamespace(provider_config={"qa_policy": "legacy"}),
    )
    server = FakeServer([item], {"legacy-translation-job": legacy_job})
    coordinator = MultiVideoCoordinator(server)
    delays: list[int] = []
    coordinator._schedule_advance = delays.append  # type: ignore[method-assign]

    assert coordinator._check_child(item) is True

    stored = server.service.multi_videos.items[0]
    assert server.cancelled == ["legacy-translation-job"]
    assert server.resumed == []
    assert stored["state"] == MultiItemState.RUNNING.value
    assert stored["child_job_id"] == "legacy-translation-job"
    assert stored["retry_count"] == 2
    assert stored["error_code"] == ""
    assert legacy_job.spec.provider_config["qa_policy"] == "multi_auto"
    assert legacy_job.spec.provider_config["headed"] is False
    assert server.started[-1]["job_id"] == "legacy-translation-job"
    assert server.started[-1]["resume_checkpoint_key"] == "latest"
    assert delays == []


def test_resume_upgrades_failed_legacy_translation_without_invalid_cancel() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RETRYING.value,
        "child_job_id": "legacy-translation-job",
        "retry_count": 3,
        "final_pass": False,
        "error_code": "translation_review_required",
    }
    legacy_job = SimpleNamespace(
        state=JobState.FAILED,
        spec=SimpleNamespace(provider_config={"qa_policy": "legacy"}),
    )
    server = FakeServer([item], {"legacy-translation-job": legacy_job})
    coordinator = MultiVideoCoordinator(server)

    coordinator._launch_or_skip(item)

    assert server.cancelled == []
    assert server.started[-1]["job_id"] == "legacy-translation-job"
    assert legacy_job.spec.provider_config["qa_policy"] == "multi_auto"
    assert server.service.multi_videos.items[0]["retry_count"] == 3


def test_final_pass_failure_is_not_retried_again() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TTS.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "job",
        "retry_count": 0,
        "final_pass": True,
    }
    server = FakeServer([item], {})
    coordinator = MultiVideoCoordinator(server)
    coordinator._schedule_advance = lambda _delay=0: None  # type: ignore[method-assign]

    coordinator._stage_failed(item, "worker_failed", {"message": "failed"})

    assert server.service.multi_videos.items[0]["state"] == MultiItemState.FAILED.value


def test_pause_requested_between_stages_prevents_the_next_child_from_starting() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "ocr-job",
        "retry_count": 0,
        "final_pass": False,
    }
    server = FakeServer([item], {})
    server.service.multi_videos.batch["current_project_id"] = "project-1"
    coordinator = MultiVideoCoordinator(server)
    launched: list[dict[str, object]] = []
    coordinator._launch_or_skip = lambda value: launched.append(dict(value))  # type: ignore[method-assign]

    coordinator._stage_succeeded(item)
    coordinator.pause()
    coordinator._advance_timer.stop()
    coordinator._advance()

    stored = server.service.multi_videos.items[0]
    assert stored["stage"] == MultiStage.TRANSLATE.value
    assert stored["state"] == MultiItemState.WAITING.value
    assert stored["child_job_id"] == ""
    assert server.service.multi_videos.batch["state"] == "paused"
    assert launched == []


def test_slow_multi_pause_interrupts_owned_worker_and_keeps_checkpoint_resume() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "ocr-job",
    }
    job = SimpleNamespace(state=JobState.PAUSE_REQUESTED)
    server = FakeServer([item], {"ocr-job": job})
    jobs = _PauseDeadlineJobs(job)
    workers = _PauseDeadlineWorkers()
    server.service.jobs = jobs
    server.workers = workers  # type: ignore[attr-defined]
    server.service.multi_videos.batch.update(
        state=MultiBatchState.PAUSE_REQUESTED.value,
        current_project_id="project-1",
    )
    coordinator = MultiVideoCoordinator(server)

    coordinator._interrupt_slow_pause("project-1", "ocr-job")

    assert job.state is JobState.INTERRUPTED
    assert workers.stops == [
        ("ocr-job", MultiVideoCoordinator.PAUSE_STOP_GRACE_MS)
    ]
    assert jobs.events[0]["kind"] == "multi_pause_deadline_reached"
    assert jobs.events[0]["payload"] == {
        "deadline_ms": MultiVideoCoordinator.PAUSE_ACK_DEADLINE_MS,
        "resume_from_checkpoint": True,
    }
    # The batch stays locked until WorkerSupervisor confirms the owned process
    # tree has exited; this prevents Resume from racing the old PID.
    assert server.service.multi_videos.batch["state"] == "pause_requested"


def test_multi_pause_schedules_a_bounded_worker_ack_deadline(monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "ocr-job",
    }
    job = SimpleNamespace(state=JobState.RUNNING)
    server = FakeServer([item], {"ocr-job": job})
    server.service.multi_videos.batch.update(
        state=MultiBatchState.RUNNING.value,
        current_project_id="project-1",
    )
    coordinator = MultiVideoCoordinator(server)
    scheduled: list[tuple[int, object]] = []
    monkeypatch.setattr(
        QTimer,
        "singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )

    coordinator.pause()

    assert server.paused == ["ocr-job"]
    assert job.state is JobState.PAUSE_REQUESTED
    assert server.service.multi_videos.batch["state"] == "pause_requested"
    assert scheduled[0][0] == MultiVideoCoordinator.PAUSE_ACK_DEADLINE_MS


def test_pause_deadline_does_nothing_after_worker_acknowledges() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "ocr-job",
    }
    job = SimpleNamespace(state=JobState.PAUSED)
    server = FakeServer([item], {"ocr-job": job})
    jobs = _PauseDeadlineJobs(job)
    workers = _PauseDeadlineWorkers()
    server.service.jobs = jobs
    server.workers = workers  # type: ignore[attr-defined]
    server.service.multi_videos.batch.update(
        state=MultiBatchState.PAUSED.value,
        current_project_id="project-1",
    )
    coordinator = MultiVideoCoordinator(server)

    coordinator._interrupt_slow_pause("project-1", "ocr-job")

    assert jobs.events == []
    assert workers.stops == []


def test_pause_advance_waits_for_worker_finished_after_deadline_interrupt() -> None:
    """A paused batch must not become resumable while its old PID is alive."""

    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "ocr-job",
    }
    job = SimpleNamespace(state=JobState.INTERRUPTED)
    server = FakeServer([item], {"ocr-job": job})
    server.workers = _PauseDeadlineWorkers()  # type: ignore[attr-defined]
    server.service.multi_videos.batch.update(
        state=MultiBatchState.PAUSE_REQUESTED.value,
        current_project_id="project-1",
    )
    coordinator = MultiVideoCoordinator(server)

    coordinator._advance()

    assert server.service.multi_videos.batch["state"] == MultiBatchState.PAUSE_REQUESTED.value


def test_normal_pause_ack_publishes_paused_even_when_worker_stays_alive() -> None:
    """Cooperative pause keeps the worker for resume and must not hang UI."""

    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "ocr-job",
    }
    job = SimpleNamespace(state=JobState.PAUSED)
    server = FakeServer([item], {"ocr-job": job})
    server.workers = _PauseDeadlineWorkers()  # type: ignore[attr-defined]
    server.service.multi_videos.batch.update(
        state=MultiBatchState.PAUSE_REQUESTED.value,
        current_project_id="project-1",
    )
    coordinator = MultiVideoCoordinator(server)

    coordinator._advance()

    assert server.service.multi_videos.batch["state"] == MultiBatchState.PAUSED.value


def test_progress_updates_are_coalesced_before_multi_status_publication() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
    }
    server = FakeServer([item], {})
    coordinator = MultiVideoCoordinator(server)

    for progress in (10, 50, 75):
        coordinator.on_worker_event("translation-job", "progress", {"progress": progress})

    assert coordinator._status_timer.interval() == 100
    assert coordinator._status_timer.isActive() is True
    assert server.multi_status_broadcasts == 0
    assert server.service.multi_videos.items[0]["progress"] == stage_progress("translate", 0.75)

    # Model one timer expiry without sleeping; all progress updates above share
    # this single publication window.
    coordinator._status_timer.stop()
    coordinator._status_timer.timeout.emit()

    assert server.multi_status_broadcasts == 1


def test_progress_uses_current_and_total_from_worker_event() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
    }
    server = FakeServer([item], {})
    coordinator = MultiVideoCoordinator(server)

    coordinator.on_worker_event(
        "translation-job", "progress", {"current": 40, "total": 100}
    )

    assert server.service.multi_videos.items[0]["progress"] == stage_progress(
        "translate", 0.4
    )


def test_deselecting_paused_current_card_does_not_resume_child() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": False,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
    }
    job = SimpleNamespace(state=JobState.PAUSED)
    server = FakeServer([item], {"translation-job": job})
    server.service.multi_videos.batch["current_project_id"] = "project-1"
    coordinator = MultiVideoCoordinator(server)

    coordinator._advance()
    coordinator._advance_timer.stop()
    coordinator._advance()

    assert server.resumed == []
    assert server.cancelled == ["translation-job"]
    assert server.service.multi_videos.finished_attempts[-1][1]["state"] == "cancelled"
    assert server.service.multi_videos.batch["current_project_id"] == ""
    assert server.service.multi_videos.items[0]["state"] == MultiItemState.WAITING.value
    assert server.service.multi_videos.items[0]["child_job_id"] == ""


def test_deselecting_card_waits_for_async_child_cancellation() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": False,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
    }
    job = SimpleNamespace(state=JobState.PAUSED)
    server = AsyncCancelServer([item], {"translation-job": job})
    server.service.multi_videos.batch["current_project_id"] = "project-1"
    coordinator = MultiVideoCoordinator(server)

    coordinator._advance()
    assert job.state is JobState.CANCEL_REQUESTED
    assert server.service.multi_videos.items[0]["child_job_id"] == "translation-job"
    assert server.service.multi_videos.finished_attempts == []

    job.state = JobState.CANCELLED
    coordinator.on_worker_finished("translation-job")
    coordinator._advance_timer.stop()
    coordinator._advance()

    assert server.service.multi_videos.items[0]["child_job_id"] == ""
    assert server.service.multi_videos.items[0]["state"] == MultiItemState.WAITING.value
    assert server.service.multi_videos.finished_attempts[-1][1]["state"] == "cancelled"


def test_removing_paused_current_card_cancels_child_before_membership_is_deleted() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
    }
    server = FakeServer(
        [item],
        {"translation-job": SimpleNamespace(state=JobState.PAUSED)},
    )
    coordinator = MultiVideoCoordinator(server)

    result = coordinator.remove_project("project-1")

    assert server.cancelled == ["translation-job"]
    assert server.service.multi_videos.finished_attempts[-1][1]["error_code"] == (
        "membership_removed"
    )
    assert result["items"] == []


def test_removing_paused_card_waits_for_async_child_cancellation() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
    }
    job = SimpleNamespace(state=JobState.PAUSED)
    server = AsyncCancelServer([item], {"translation-job": job})
    coordinator = MultiVideoCoordinator(server)

    result = coordinator.remove_project("project-1")

    assert server.cancelled == ["translation-job"]
    assert job.state is JobState.CANCEL_REQUESTED
    assert [value["project_id"] for value in result["items"]] == ["project-1"]
    assert server.service.multi_videos.items[0]["error_code"] == (
        "membership_removal_pending"
    )
    assert server.service.multi_videos.finished_attempts == []

    job.state = JobState.CANCELLED
    coordinator.on_worker_finished("translation-job")
    coordinator._advance_timer.stop()
    coordinator._advance()

    assert server.service.multi_videos.items == []
    assert server.service.multi_videos.finished_attempts[-1][1]["state"] == "cancelled"


def test_new_batch_cannot_orphan_a_paused_child_or_pending_removal() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
        "error_code": "membership_removal_pending",
    }
    job = SimpleNamespace(state=JobState.CANCEL_REQUESTED)
    server = AsyncCancelServer([item], {"translation-job": job})
    server.service.multi_videos.batch["state"] = "paused"
    coordinator = MultiVideoCoordinator(server)

    with pytest.raises(ValueError, match="đang chờ tác vụ cũ dừng an toàn"):
        coordinator.start({})

    assert job.state is JobState.CANCEL_REQUESTED
    assert server.started == []
    assert server.service.multi_videos.items[0]["child_job_id"] == "translation-job"
    assert server.service.multi_videos.items[0]["error_code"] == (
        "membership_removal_pending"
    )


def test_new_batch_does_not_inherit_previous_retry_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "ducai_studio.infrastructure.multi_video_repository.utc_now_text",
        lambda: "2026-08-29T00:00:00.000Z",
    )
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(id, path, name, schema_version, head_revision, updated_at)
            VALUES ('project-1', ?, 'Project', 1, 1, '2026-08-29T00:00:00Z')
            """,
            (str(tmp_path / "project-1.ducai"),),
        )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-1"])
    for attempt in range(1, 7):
        job_id = f"old-job-{attempt}"
        repository.add_attempt("project-1", MultiStage.OCR.value, 0, attempt, job_id)
        repository.finish_attempt(job_id, state="failed", error_code="old_failure")

    repository.reset_for_start({"ocr": {}, "translation": {}, "tts": {}})

    assert repository.next_attempt_number("project-1", MultiStage.OCR.value, 0) == 1
    with database.connect(read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM multi_stage_attempts WHERE project_id = ?",
            ("project-1",),
        ).fetchone()["total"] == 6
    repository.update_item(
        "project-1",
        state=MultiItemState.RUNNING.value,
        child_job_id="new-job",
    )
    repository.add_attempt("project-1", MultiStage.OCR.value, 0, 1, "new-job")
    result = repository.record_stage_failure(
        project_id="project-1",
        stage=MultiStage.OCR.value,
        job_id="new-job",
        error_code="worker_failed",
        error_payload={"message": "new run failure"},
    )

    assert result["retry_count"] == 1
    assert result["state"] == MultiItemState.RETRYING.value
    repository.update_item(
        "project-1",
        state=MultiItemState.RUNNING.value,
        child_job_id="new-job-2",
    )
    assert repository.next_attempt_number("project-1", MultiStage.OCR.value, 0) == 2
    repository.add_attempt("project-1", MultiStage.OCR.value, 0, 2, "new-job-2")
    second = repository.record_stage_failure(
        project_id="project-1",
        stage=MultiStage.OCR.value,
        job_id="new-job-2",
        error_code="worker_failed",
        error_payload={"message": "second new run failure"},
    )
    assert second["retry_count"] == 2


def test_retry_batch_reset_preserves_failed_stage_and_child_checkpoint(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-30T00:00:00Z")
    with database.write_transaction() as connection:
        for project_id in ("needs-review", "finished"):
            connection.execute(
                """
                INSERT INTO projects(id, path, name, schema_version, head_revision, updated_at)
                VALUES (?, ?, ?, 1, 1, '2026-08-30T00:00:00Z')
                """,
                (project_id, str(tmp_path / f"{project_id}.ducai"), project_id),
            )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["needs-review", "finished"])
    repository.update_batch(state=MultiBatchState.COMPLETED_WITH_ERRORS.value)
    repository.update_item(
        "needs-review",
        stage=MultiStage.TRANSLATE.value,
        state=MultiItemState.FAILED.value,
        progress=0.5,
        child_job_id="translation-checkpoint-job",
        retry_count=6,
        final_pass=True,
        error_code="translation_review_required",
        error_payload={"cjk_blocker_count": 3},
    )
    repository.update_item(
        "finished",
        stage=MultiStage.DONE.value,
        state=MultiItemState.COMPLETED.value,
        progress=1.0,
        child_job_id="tts-finished-job",
    )

    repository.reset_for_retry_start({"ocr": {}, "translation": {}, "tts": {}})

    items = {item["project_id"]: item for item in repository.list_items()}
    review = items["needs-review"]
    assert review["stage"] == MultiStage.TRANSLATE.value
    assert review["child_job_id"] == "translation-checkpoint-job"
    assert review["state"] == MultiItemState.WAITING.value
    assert review["retry_count"] == 0
    assert review["error_code"] == ""
    assert items["finished"]["stage"] == MultiStage.DONE.value
    assert items["finished"]["state"] == MultiItemState.COMPLETED.value


def test_completed_with_errors_start_uses_stage_preserving_retry_reset() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.FAILED.value,
        "child_job_id": "translation-job",
        "retry_count": 6,
        "error_code": "translation_review_required",
    }
    server = FakeServer([item], {})
    server.service.multi_videos.batch["state"] = (
        MultiBatchState.COMPLETED_WITH_ERRORS.value
    )
    coordinator = MultiVideoCoordinator(server)
    coordinator._validate_settings = lambda *_args: None  # type: ignore[method-assign]
    coordinator._advance = lambda: None  # type: ignore[method-assign]

    coordinator.start({"ocr": {}, "translation": {}, "tts": {}})

    stored = server.service.multi_videos.items[0]
    assert server.service.multi_videos.reset_calls == ["retry"]
    assert stored["stage"] == MultiStage.TRANSLATE.value
    assert stored["child_job_id"] == "translation-job"


def test_multi_membership_is_persistent_and_removal_keeps_project(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        for index in range(2):
            connection.execute(
                """
                INSERT INTO projects(
                    id, path, name, schema_version, head_revision, updated_at
                ) VALUES (?, ?, ?, 1, 1, ?)
                """,
                (
                    f"project-{index}",
                    str(tmp_path / f"project-{index}.ducai"),
                    f"Project {index}",
                    "2026-08-29T00:00:00Z",
                ),
            )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-0", "project-1"])

    reopened = MultiVideoRepository(database)
    assert [item["project_id"] for item in reopened.list_items()] == [
        "project-0",
        "project-1",
    ]
    reopened.remove_project("project-0")

    with database.connect(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM projects WHERE id = 'project-0'").fetchone()
    assert [item["project_id"] for item in reopened.list_items()] == ["project-1"]


def test_final_pass_promotion_recovers_atomically_from_retry_pass_marker(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        for index in range(2):
            connection.execute(
                """
                INSERT INTO projects(
                    id, path, name, schema_version, head_revision, updated_at
                ) VALUES (?, ?, ?, 1, 1, ?)
                """,
                (
                    f"project-{index}",
                    str(tmp_path / f"project-{index}.ducai"),
                    f"Project {index}",
                    "2026-08-29T00:00:00Z",
                ),
            )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-0", "project-1"])
    repository.update_batch(state="running", retry_pass=1)
    for index in range(2):
        repository.update_item(
            f"project-{index}",
            state=MultiItemState.DEFERRED.value,
            retry_count=6,
        )

    assert repository.promote_deferred_final_pass() == 2

    items = repository.list_items()
    assert repository.get_batch()["retry_pass"] == 1
    assert all(item["state"] == MultiItemState.WAITING.value for item in items)
    assert all(item["final_pass"] is True for item in items)
    assert all(item["retry_count"] == 0 for item in items)


def test_software_failure_recovery_supersedes_attempts_and_resets_budget(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(id, path, name, schema_version, head_revision, updated_at)
            VALUES ('project-1', ?, 'Project', 1, 1, '2026-08-29T00:00:00Z')
            """,
            (str(tmp_path / "project-1.ducai"),),
        )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-1"])
    repository.update_item(
        "project-1",
        stage=MultiStage.TRANSLATE.value,
        state=MultiItemState.FAILED.value,
        child_job_id="translation-job",
        retry_count=6,
        error_code="ValueError",
        error_payload={
            "message": MultiVideoCoordinator.TRANSLATION_LEDGER_CACHE_ERROR,
        },
    )
    for attempt in range(1, 7):
        repository.add_attempt(
            "project-1",
            MultiStage.TRANSLATE.value,
            1,
            attempt,
            "translation-job",
        )
        repository.finish_attempt(
            "translation-job",
            state="failed",
            error_code="ValueError",
            error_payload={
                "message": MultiVideoCoordinator.TRANSLATION_LEDGER_CACHE_ERROR,
            },
        )

    assert repository.recover_software_failed_stage(
        project_id="project-1",
        stage=MultiStage.TRANSLATE.value,
        job_id="translation-job",
        error_code="ValueError",
    )

    item = repository.list_items()[0]
    assert item["state"] == MultiItemState.WAITING.value
    assert item["retry_count"] == 0
    assert item["error_code"] == ""
    with database.connect(read_only=True) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) AS total FROM multi_stage_attempts
            WHERE project_id = 'project-1' AND state = 'failed'
            """
        ).fetchone()["total"] == 0
        assert connection.execute(
            """
            SELECT COUNT(*) AS total FROM multi_stage_attempts
            WHERE project_id = 'project-1' AND state = 'superseded'
            """
        ).fetchone()["total"] == 6


def test_failed_attempt_recording_is_atomic_and_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(
                id, path, name, schema_version, head_revision, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?)
            """,
            (
                "project-1",
                str(tmp_path / "project-1.ducai"),
                "Project 1",
                "2026-08-29T00:00:00Z",
            ),
        )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-1"])
    repository.update_batch(state="running", current_project_id="project-1")
    repository.update_item(
        "project-1",
        stage=MultiStage.TRANSLATE.value,
        state=MultiItemState.RUNNING.value,
        child_job_id="translation-job",
    )
    repository.add_attempt("project-1", MultiStage.TRANSLATE.value, 0, 1, "translation-job")

    first = repository.record_stage_failure(
        project_id="project-1",
        stage=MultiStage.TRANSLATE.value,
        job_id="translation-job",
        error_code="worker_failed",
        error_payload={"message": "failed"},
    )
    second = repository.record_stage_failure(
        project_id="project-1",
        stage=MultiStage.TRANSLATE.value,
        job_id="translation-job",
        error_code="worker_failed",
        error_payload={"message": "failed"},
    )

    stored = repository.list_items()[0]
    assert first == {"applied": True, "state": "retrying", "retry_count": 1}
    assert second == {"applied": False}
    assert stored["retry_count"] == 1
    assert repository.get_batch()["current_project_id"] == ""


@pytest.mark.parametrize(("prior_failures", "expected_retry_count"), [(0, 1), (5, 6)])
def test_non_retryable_review_failure_is_deferred_atomically(
    tmp_path: Path,
    prior_failures: int,
    expected_retry_count: int,
) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(
                id, path, name, schema_version, head_revision, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?)
            """,
            (
                "project-1",
                str(tmp_path / "project-1.ducai"),
                "Project 1",
                "2026-08-29T00:00:00Z",
            ),
        )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-1"])
    repository.reset_for_start({"ocr": {}, "translation": {}, "tts": {}})
    repository.update_batch(current_project_id="project-1")
    for attempt_number in range(1, prior_failures + 1):
        old_job_id = f"old-ocr-job-{attempt_number}"
        repository.add_attempt(
            "project-1",
            MultiStage.OCR.value,
            0,
            attempt_number,
            old_job_id,
        )
        repository.finish_attempt(
            old_job_id,
            state="failed",
            error_code="ocr_review_required",
        )
    repository.update_item(
        "project-1",
        stage=MultiStage.OCR.value,
        state=MultiItemState.RUNNING.value,
        child_job_id="ocr-job",
        retry_count=prior_failures,
    )
    repository.add_attempt(
        "project-1",
        MultiStage.OCR.value,
        0,
        prior_failures + 1,
        "ocr-job",
    )

    result = repository.record_stage_failure(
        project_id="project-1",
        stage=MultiStage.OCR.value,
        job_id="ocr-job",
        error_code="ocr_review_required",
        error_payload={"blocker_count": 2},
        retryable=False,
    )

    stored = repository.list_items()[0]
    assert result == {
        "applied": True,
        "state": "deferred",
        "retry_count": expected_retry_count,
    }
    assert stored["state"] == MultiItemState.DEFERRED.value
    assert stored["retry_count"] == expected_retry_count
    assert repository.get_batch()["current_project_id"] == ""


def test_interrupted_attempt_does_not_spend_retry_budget_when_next_launch_fails(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(
                id, path, name, schema_version, head_revision, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?)
            """,
            (
                "project-1",
                str(tmp_path / "project-1.ducai"),
                "Project 1",
                "2026-08-29T00:00:00Z",
            ),
        )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-1"])
    repository.update_batch(state="running", current_project_id="project-1")
    repository.update_item(
        "project-1",
        stage=MultiStage.TRANSLATE.value,
        state=MultiItemState.RUNNING.value,
        child_job_id="translation-job",
    )
    repository.add_attempt("project-1", MultiStage.TRANSLATE.value, 0, 1, "translation-job")
    repository.mark_interrupted()
    repository.update_batch(state="running", current_project_id="project-1")
    repository.add_attempt("project-1", MultiStage.TRANSLATE.value, 0, 2, "translation-job")

    result = repository.record_stage_failure(
        project_id="project-1",
        stage=MultiStage.TRANSLATE.value,
        job_id="translation-job",
        error_code="worker_start_failed",
        error_payload={"message": "failed after resume"},
    )

    assert result["retry_count"] == 1
    assert repository.list_items()[0]["retry_count"] == 1


def test_multi_reorder_is_exact_and_preserves_item_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        for index in range(3):
            connection.execute(
                """
                INSERT INTO projects(
                    id, path, name, schema_version, head_revision, updated_at
                ) VALUES (?, ?, ?, 1, 1, ?)
                """,
                (
                    f"project-{index}",
                    str(tmp_path / f"project-{index}.ducai"),
                    f"Project {index}",
                    "2026-08-29T00:00:00Z",
                ),
            )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-0", "project-1", "project-2"])
    repository.update_item("project-1", progress=0.5, stage="translate")

    reordered = repository.reorder_projects(["project-2", "project-1", "project-0"])

    assert [item["project_id"] for item in reordered] == [
        "project-2",
        "project-1",
        "project-0",
    ]
    assert reordered[1]["progress"] == 0.5
    assert reordered[1]["stage"] == "translate"
    with pytest.raises(ValueError, match="đúng toàn bộ"):
        repository.reorder_projects(["project-2", "project-1"])


def test_interrupted_batch_closes_running_attempt_and_requires_explicit_resume(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-29T00:00:00Z")
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(
                id, path, name, schema_version, head_revision, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?)
            """,
            (
                "project-1",
                str(tmp_path / "project-1.ducai"),
                "Project 1",
                "2026-08-29T00:00:00Z",
            ),
        )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-1"])
    repository.update_batch(state="running", current_project_id="project-1")
    repository.update_item("project-1", child_job_id="ocr-job", state="running")
    repository.add_attempt("project-1", MultiStage.OCR.value, 0, 1, "ocr-job")

    repository.mark_interrupted()

    assert repository.get_batch()["state"] == "interrupted"
    with database.connect(read_only=True) as connection:
        attempt = connection.execute(
            "SELECT state, error_code FROM multi_stage_attempts WHERE job_id = ?",
            ("ocr-job",),
        ).fetchone()
    assert attempt is not None
    assert (attempt["state"], attempt["error_code"]) == ("interrupted", "app_interrupted")


def test_attempt_insert_reuses_one_running_row_and_reconciles_legacy_duplicates(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize(applied_at="2026-08-31T00:00:00Z")
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(
                id, path, name, schema_version, head_revision, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?)
            """,
            (
                "project-1",
                str(tmp_path / "project-1.ducai"),
                "Project 1",
                "2026-08-31T00:00:00Z",
            ),
        )
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-1"])
    repository.update_batch(state="running", current_project_id="project-1")
    first_id = repository.add_attempt(
        "project-1", MultiStage.TTS.value, 0, 1, "tts-job"
    )
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO multi_stage_attempts(
                id, batch_id, project_id, stage, pass_number, attempt_number,
                job_id, state, error_code, error_json, created_at
            ) VALUES (?, 'multi-primary', ?, ?, 0, 2, ?, 'running', '', '{}', ?)
            """,
            (
                "legacy-duplicate",
                "project-1",
                MultiStage.TTS.value,
                "tts-job",
                "2099-08-31T00:00:01Z",
            ),
        )

    reused_id = repository.add_attempt(
        "project-1", MultiStage.TTS.value, 0, 3, "tts-job"
    )

    assert reused_id == "legacy-duplicate"
    with database.connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT id, state FROM multi_stage_attempts
            WHERE project_id = ? AND stage = ? AND job_id = ?
            ORDER BY attempt_number
            """,
            ("project-1", MultiStage.TTS.value, "tts-job"),
        ).fetchall()
    assert [(row["id"], row["state"]) for row in rows] == [
        (first_id, "superseded"),
        ("legacy-duplicate", "running"),
    ]


def test_cancelled_child_is_terminal_and_is_not_retried() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 2,
        "final_pass": False,
    }
    server = FakeServer([item], {"translation-job": SimpleNamespace(state=JobState.CANCELLED)})
    server.service.multi_videos.batch["current_project_id"] = "project-1"
    coordinator = MultiVideoCoordinator(server)
    coordinator._schedule_advance = lambda _delay=0: None  # type: ignore[method-assign]

    coordinator._check_child(item)

    stored = server.service.multi_videos.items[0]
    assert stored["state"] == MultiItemState.FAILED.value
    assert stored["error_code"] == "child_cancelled"
    assert stored["retry_count"] == 2
    assert server.service.multi_videos.batch["current_project_id"] == ""


def test_multi_translation_warning_only_advances_directly_to_tts() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 0,
        "final_pass": False,
    }
    job = SimpleNamespace(
        state=JobState.FAILED,
        error_code="translation_review_required",
        error_payload={
            "warnings_only": True,
            "cjk_cue_ids": [],
            "message": "Bản dịch chỉ còn cảnh báo.",
        },
    )
    server = FakeServer([item], {"translation-job": job})
    server.service.multi_videos.batch["current_project_id"] = "project-1"
    coordinator = MultiVideoCoordinator(server)
    coordinator._schedule_advance = lambda _delay=0: None  # type: ignore[method-assign]

    coordinator._check_child(item)

    stored = server.service.multi_videos.items[0]
    assert stored["stage"] == MultiStage.TTS.value
    assert stored["state"] == MultiItemState.WAITING.value
    assert stored["child_job_id"] == ""
    assert coordinator._failure_is_retryable(
        MultiStage.TRANSLATE.value,
        "translation_review_required",
        {"warnings_only": True, "cjk_cue_ids": []},
    ) is False
    assert coordinator._failure_is_retryable(
        MultiStage.TRANSLATE.value,
        "translation_review_required",
        {"warnings_only": False, "cjk_cue_ids": [2]},
    ) is True


def test_manual_translation_review_advances_without_restarting_provider() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.WAITING.value,
        "child_job_id": "",
        "retry_count": 0,
        "final_pass": False,
    }
    server = FakeServer([item], {})
    server.service.get_manual_translation_review_state = lambda project_id, job_id=None: {
        "available": True,
        "ready": True,
        "cjk_cue_ids": [],
    }
    coordinator = MultiVideoCoordinator(server)
    coordinator._schedule_advance = lambda _delay=0: None  # type: ignore[method-assign]

    coordinator._launch_or_skip(item)

    stored = server.service.multi_videos.items[0]
    assert stored["stage"] == MultiStage.TTS.value
    assert stored["state"] == MultiItemState.WAITING.value
    assert server.started == []


@pytest.mark.parametrize(
    ("section", "value", "message"),
    [
        ("ocr", {"engine_python": "missing-python.exe"}, "Python Paddle OCR"),
        (
            "ocr",
            {
                "extraction_method": "whisper_audio",
                "engine_python": "missing-whisper-python.exe",
            },
            "Runtime Whisper",
        ),
        ("translation", {"provider_id": "unsupported", "model_id": "model"}, "không được hỗ trợ"),
        ("translation", {"provider_id": "gemini-api", "model_id": "model"}, "API key tham chiếu"),
        ("tts", {"provider_id": "unsupported", "voice_id": "voice"}, "bộ máy giọng"),
        ("tts", {"provider_id": "piper", "voice_id": ""}, "giọng đọc"),
    ],
)
def test_multi_start_rejects_invalid_stage_configuration(
    tmp_path: Path, section: str, value: dict[str, object], message: str
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    snapshot = ProjectSnapshot(
        "project-1",
        "Project 1",
        1,
        media=(MediaAsset("video", str(video), MediaKind.VIDEO, 1_000),),
    )
    server = FakeServer([{"project_id": "project-1", "selected": True}], {})
    server.service.projects = SimpleNamespace(load=lambda _project_id: snapshot)
    server.service.list_credentials = lambda: {"references": []}
    coordinator = MultiVideoCoordinator(server)
    settings: dict[str, object] = {
        "ocr": {"engine_python": str(tmp_path / "python.exe")},
        "translation": {"provider_id": "gemini-web", "model_id": "model"},
        "tts": {"provider_id": "piper", "voice_id": "voice"},
    }
    engine = tmp_path / "python.exe"
    engine.write_bytes(b"runtime")
    settings[section] = value

    with pytest.raises(ValueError, match=message):
        coordinator._validate_settings(settings, server.service.multi_videos.list_items())


def test_multi_import_same_media_twice_creates_independent_projects(tmp_path: Path) -> None:
    class StaticMediaProbe:
        def probe(self, _media_path: str) -> MediaProbeResult:
            return MediaProbeResult(1_000, True, True, 16, 16, "mp4")

    media = tmp_path / "source.mp4"
    media.write_bytes(b"video")
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    first = service.create_project_with_media(
        project_path=str(tmp_path / "first.ducai"), name="First", media_path=str(media)
    )
    first_id = str(first["project"]["id"])
    second = service.create_project_with_media(
        project_path=str(tmp_path / "second.ducai"),
        name="Second",
        media_path=str(media),
        excluded_project_ids=[first_id],
    )
    second_id = str(second["project"]["id"])

    assert first_id != second_id
    assert first["reused"] is False
    assert second["reused"] is False
    assert [asset.path for asset in service.projects.load(first_id).media] == [str(media)]
    assert [asset.path for asset in service.projects.load(second_id).media] == [str(media)]


def test_multi_project_status_ignores_terminal_job_from_stale_project_revision() -> None:
    status = JobManagerService._multi_project_status(
        {
            "id": "old-failure",
            "kind": "translate",
            "state": "failed",
            "input_revision": 1,
            "progress": {"phase_current": 1, "phase_total": 2},
            "error_payload": {"message": "Lỗi của revision cũ"},
        },
        has_source=True,
        project_revision=2,
    )

    assert status["project_status_override"] is False
    assert status["project_status_text"] == "Đã trích xuất SRT"


def test_partial_multi_status_ignores_stale_terminal_job(
    monkeypatch, tmp_path: Path
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    monkeypatch.setattr(
        service.multi_videos,
        "get_batch",
        lambda: {"state": "idle", "current_project_id": ""},
    )
    monkeypatch.setattr(
        service.multi_videos,
        "list_items",
        lambda: [{"project_id": "project-1", "state": "waiting"}],
    )
    monkeypatch.setattr(
        service.jobs,
        "latest_project_activity",
        lambda _project_ids: {
            "project-1": {
                "id": "old-failure",
                "kind": "translate",
                "state": "failed",
                "input_revision": 1,
                "project_head_revision": 2,
                "progress": {"phase_current": 1, "phase_total": 2},
                "error_payload": {"message": "Lỗi của revision cũ"},
            }
        },
    )

    item = service.list_multi_video_status()["items"][0]

    assert item["project_status_override"] is False
