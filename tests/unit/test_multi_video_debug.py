from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject

from ducai_studio.domain.jobs import JobState
from ducai_studio.domain.multi_video import MultiItemState, MultiStage
from ducai_studio.job_manager.multi_video_coordinator import MultiVideoCoordinator


class _Repository:
    def __init__(self, item: dict[str, object], *, retry_pass: int = 0) -> None:
        self.item = item
        self.batch: dict[str, object] = {
            "state": "running",
            "retry_pass": retry_pass,
            "current_project_id": item["project_id"],
            "config": {},
        }

    def get_batch(self) -> dict[str, object]:
        return dict(self.batch)

    def list_items(self) -> list[dict[str, object]]:
        return [dict(self.item)]

    def update_batch(self, **values: object) -> dict[str, object]:
        self.batch.update(values)
        return self.get_batch()

    def update_item(self, project_id: str, **values: object) -> dict[str, object]:
        assert project_id == self.item["project_id"]
        self.item.update(values)
        return dict(self.item)


class _Server(QObject):
    def __init__(self, repository: _Repository, job: object | None = None) -> None:
        super().__init__()
        self.service = SimpleNamespace(
            multi_videos=repository,
            jobs=SimpleNamespace(get=lambda _job_id: job),
        )

    def _broadcast_multi_status(self) -> None:
        return

    def _resume_job(self, _job_id: str) -> None:
        return


def _coordinator(repository: _Repository, job: object | None = None) -> MultiVideoCoordinator:
    QCoreApplication.instance() or QCoreApplication([])
    result = MultiVideoCoordinator(_Server(repository, job))
    result._schedule_advance = lambda _delay=0: None  # type: ignore[method-assign]
    return result


def test_paused_child_completes_batch_pause_handshake_without_spending_retry() -> None:
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "translation-job",
        "retry_count": 3,
        "final_pass": False,
    }
    repository = _Repository(item)
    repository.batch["state"] = "pause_requested"
    coordinator = _coordinator(repository, SimpleNamespace(state=JobState.PAUSED))

    coordinator._advance()

    assert repository.batch["state"] == "paused"
    assert repository.item["retry_count"] == 3


def test_successful_stage_starts_the_next_stage_with_a_fresh_retry_budget() -> None:
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.OCR.value,
        "state": MultiItemState.RUNNING.value,
        "child_job_id": "ocr-job",
        "retry_count": 5,
        "final_pass": True,
    }
    repository = _Repository(item, retry_pass=1)
    coordinator = _coordinator(repository)

    coordinator._stage_succeeded(item)

    assert repository.item["stage"] == MultiStage.TRANSLATE.value
    assert repository.item["retry_count"] == 0
    assert repository.item["final_pass"] is False


def test_stage_exhausted_during_deferred_pass_finishes_without_another_pass() -> None:
    item = {
        "project_id": "project-1",
        "selected": True,
        "stage": MultiStage.TRANSLATE.value,
        "state": MultiItemState.RETRYING.value,
        "child_job_id": "translation-job",
        "retry_count": 5,
        "final_pass": False,
    }
    repository = _Repository(item, retry_pass=1)
    coordinator = _coordinator(repository)

    coordinator._stage_failed(item, "worker_failed", {"message": "failed"})

    assert repository.item["retry_count"] == 6
    assert repository.item["state"] == MultiItemState.FAILED.value
