from __future__ import annotations

import errno
from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer

from ducai_studio.domain.jobs import JobKind, JobState, assert_no_secret_fields
from ducai_studio.domain.multi_video import (
    STAGES,
    MultiBatchState,
    MultiItemState,
    MultiStage,
    stage_progress,
    worker_progress_fraction,
)
from ducai_studio.infrastructure.runtime_program import runtime_program_path
from ducai_studio.infrastructure.whisper_runtime import whisper_runtime_is_ready

from .service import JobManagerService


class MultiVideoCoordinator(QObject):
    """Runs selected projects through OCR, translation and TTS in order.

    The coordinator owns sequencing and retry policy; child jobs remain the
    normal Job Manager jobs, so their checkpoints and cache contracts are
    unchanged.  It intentionally never starts two Multi children at once.
    """

    RETRY_DELAYS_MS = (2_000, 4_000, 8_000, 16_000, 30_000)
    PAUSE_ACK_DEADLINE_MS = 1_500
    PAUSE_STOP_GRACE_MS = 250
    NON_BLOCKING_STAGE_REVIEWS = frozenset(
        {(MultiStage.OCR.value, "ocr_review_required")}
    )
    REMOVAL_PENDING_CODE = "membership_removal_pending"
    DESELECTION_PENDING_CODE = "child_deselection_pending"
    TRANSLATION_POLICY_UPGRADE_PENDING_CODE = "translation_policy_upgrade_pending"
    TRANSLATION_LEDGER_CACHE_ERROR = "Translation QA-pass chunk has missing cached text"
    CHILD_INTERRUPTED_CODE = "child_interrupted"
    MULTI_TRANSLATION_POLICY = {
        "qa_policy": "multi_auto",
        "cjk_is_blocker": True,
        "auto_compact_overlong_once": True,
        "max_overlong_compaction_passes": 1,
    }

    def __init__(self, server: QObject) -> None:
        super().__init__(server)
        self.server = server
        self.service: JobManagerService = server.service  # type: ignore[attr-defined]
        self._settings: dict[str, Any] = {}
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._advance)
        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._advance)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(100)
        self._status_timer.timeout.connect(
            self.server._broadcast_multi_status  # type: ignore[attr-defined]
        )
        self._handled_terminal_jobs: set[str] = set()
        # Recovery may have converted a child to INTERRUPTED while a durable
        # membership-removal request was waiting for cancellation.  Finishing
        # that cleanup is safe and is not the same as auto-resuming the batch.
        QTimer.singleShot(0, self._advance)

    def _publish_status(self) -> None:
        # Coalesce noisy worker progress into at most ten UI updates/second.
        if not self._status_timer.isActive():
            self._status_timer.start()

    def _list_multi_status(self) -> dict[str, object]:
        """Return a lightweight status snapshot with legacy-test fallback."""

        status = getattr(self.service, "list_multi_video_status", None)
        if callable(status):
            return status()
        return self.service.list_multi_videos()

    def start(self, settings: Mapping[str, object]) -> dict[str, object]:
        self.server.licensing.require_active()
        if self._drain_pending_removals():
            raise ValueError(
                "Multi video đang chờ tác vụ cũ dừng an toàn; hãy đợi video được xóa "
                "khỏi danh sách rồi chạy lại."
            )
        batch = self.service.multi_videos.get_batch()
        if batch["state"] in {
            MultiBatchState.RUNNING.value,
            MultiBatchState.PAUSE_REQUESTED.value,
        }:
            raise ValueError("Multi video đang chạy; hãy tạm dừng trước khi chạy lại")
        if batch["state"] in {
            MultiBatchState.PAUSED.value,
            MultiBatchState.INTERRUPTED.value,
        }:
            raise ValueError(
                "Lượt Multi video hiện tại còn checkpoint hoặc tác vụ con; "
                "hãy dùng Tiếp tục Multi video thay vì tạo lượt mới."
            )
        selected = [item for item in self.service.multi_videos.list_items() if item["selected"]]
        if not selected:
            raise ValueError("Hãy chọn ít nhất một video trong Multi video")
        config = self._unattended_settings(settings)
        config["translation_preferences_by_project"] = self._validate_settings(
            config, selected
        )
        casting_configs = self._capture_dialogue_casting_settings(selected)
        if casting_configs:
            config["dialogue_casting_by_project"] = casting_configs
        assert_no_secret_fields(config)
        self._settings = config
        self._handled_terminal_jobs.clear()
        # A completed-with-errors run is resumed from its durable stage
        # checkpoints.  Rewinding those cards to OCR made “Chạy lại video lỗi”
        # repeat successful work and ignored a manually fixed translation.
        if batch["state"] == MultiBatchState.COMPLETED_WITH_ERRORS.value:
            retry_reset = getattr(self.service.multi_videos, "reset_for_retry_start", None)
            if callable(retry_reset):
                retry_reset(config)
            else:
                # Compatibility for older in-memory repositories used by
                # integrations; production repositories expose the precise
                # stage-preserving method above.
                self.service.multi_videos.reset_for_start(config)
        else:
            self.service.multi_videos.reset_for_start(config)
        self._advance()
        self._publish_status()
        # Do not decode every project snapshot before acknowledging the
        # button.  The catalog is refreshed by the normal terminal event; the
        # lightweight status payload keeps start responsive and preserves the
        # existing card model.
        return self._list_multi_status()

    def pause(self) -> dict[str, object]:
        batch = self.service.multi_videos.get_batch()
        if batch["state"] != MultiBatchState.RUNNING.value:
            return self._list_multi_status()
        self.service.multi_videos.update_batch(state=MultiBatchState.PAUSE_REQUESTED.value)
        current = str(batch.get("current_project_id", ""))
        pause_delivered = False
        if current:
            item = next(
                (x for x in self.service.multi_videos.list_items() if x["project_id"] == current),
                None,
            )
            job_id = str(item.get("child_job_id", "")) if item else ""
            if job_id:
                try:
                    job = self.service.jobs.get(job_id)
                    if job.state in {
                        JobState.RUNNING,
                        JobState.START_REQUESTED,
                        JobState.RESUME_REQUESTED,
                    }:
                        self.server._pause_job(job_id)  # type: ignore[attr-defined]
                        pause_delivered = True
                        QTimer.singleShot(
                            self.PAUSE_ACK_DEADLINE_MS,
                            lambda child_job_id=job_id, project_id=current: (
                                self._interrupt_slow_pause(project_id, child_job_id)
                            ),
                        )
                except (LookupError, ValueError):
                    # The child completed between the state read and pause.
                    # In that boundary, pausing before the next stage is safe.
                    pass
        if not pause_delivered:
            self.service.multi_videos.update_batch(state=MultiBatchState.PAUSED.value)
        self._publish_status()
        return self._list_multi_status()

    def _interrupt_slow_pause(self, project_id: str, job_id: str) -> None:
        """Bound Multi pause latency while preserving the last checkpoint.

        OCR, translation and TTS workers process controls cooperatively between
        provider calls.  A single blocking provider call can therefore delay a
        normal pause for tens of seconds.  Multi has a stronger UX contract:
        after a short acknowledgement deadline, stop only the owned child
        process and leave its job INTERRUPTED so Resume restarts that exact job
        from its latest durable checkpoint/cache.
        """

        batch = self.service.multi_videos.get_batch()
        if (
            batch.get("state") != MultiBatchState.PAUSE_REQUESTED.value
            or str(batch.get("current_project_id", "")) != project_id
        ):
            return
        item = next(
            (
                value
                for value in self.service.multi_videos.list_items()
                if str(value.get("project_id", "")) == project_id
            ),
            None,
        )
        if item is None or str(item.get("child_job_id", "")) != job_id:
            return
        try:
            job = self.service.jobs.get(job_id)
        except LookupError:
            self.service.multi_videos.update_batch(
                state=MultiBatchState.PAUSED.value,
            )
            self._publish_status()
            return
        if job.state is not JobState.PAUSE_REQUESTED:
            return

        _sequence, interrupted = self.service.jobs.append_worker_event(
            event_id=str(uuid4()),
            job_id=job_id,
            kind="multi_pause_deadline_reached",
            payload={
                "deadline_ms": self.PAUSE_ACK_DEADLINE_MS,
                "resume_from_checkpoint": True,
            },
            transitions={JobState.PAUSE_REQUESTED: JobState.INTERRUPTED},
        )
        if interrupted.state is not JobState.INTERRUPTED:
            return
        workers = getattr(self.server, "workers", None)
        if workers is not None and workers.has_worker(job_id):
            workers.request_stop(
                job_id,
                grace_ms=self.PAUSE_STOP_GRACE_MS,
            )
            # WorkerSupervisor emits workerFinished after the owned process
            # tree exits.  _advance then publishes PAUSED; until that boundary
            # the Resume button stays locked and cannot race a still-owned PID.
            self._publish_status()
            return
        self.service.multi_videos.update_batch(state=MultiBatchState.PAUSED.value)
        self._publish_status()

    def resume(self) -> dict[str, object]:
        self.server.licensing.require_active()
        batch = self.service.multi_videos.get_batch()
        if batch["state"] not in {
            MultiBatchState.PAUSED.value,
            MultiBatchState.INTERRUPTED.value,
        }:
            if batch["state"] == MultiBatchState.RUNNING.value:
                return self._list_multi_status()
            raise ValueError("Multi video chưa ở trạng thái có thể tiếp tục")
        stored_config = batch.get("config", {})
        if not isinstance(stored_config, Mapping):
            raise ValueError("Snapshot cấu hình Multi video không hợp lệ")
        self._settings = self._unattended_settings(stored_config)
        assert_no_secret_fields(self._settings)
        # The interruption was already made durable before the user chose
        # Resume. Mark it handled so _advance relaunches that exact job once
        # instead of interpreting the historical terminal state as a fresh
        # interruption and pausing the batch again.
        for item in self.service.multi_videos.list_items():
            job_id = str(item.get("child_job_id", ""))
            if not job_id:
                continue
            try:
                child = self.service.jobs.get(job_id)
            except LookupError:
                continue
            if child.state is JobState.INTERRUPTED:
                self._handled_terminal_jobs.add(job_id)
        self.service.multi_videos.update_batch(state=MultiBatchState.RUNNING.value)
        self._advance()
        self._publish_status()
        return self._list_multi_status()

    @staticmethod
    def _unattended_settings(settings: Mapping[str, object]) -> dict[str, Any]:
        """Apply settings that are invariant for every unattended Multi run."""

        config = deepcopy(dict(settings))
        translation = config.get("translation", {})
        if not isinstance(translation, Mapping):
            return config
        translation = dict(translation)
        # Multi is intentionally unattended: only untranslated CJK is a
        # content blocker, while overlong text gets one automatic compaction
        # pass.  Reapply the invariant when resuming snapshots written by
        # releases that did not persist these fields yet.
        translation.update(MultiVideoCoordinator.MULTI_TRANSLATION_POLICY)
        if str(translation.get("provider_id", "")).strip().casefold() == "gemini-web":
            provider_options = translation.get("provider_options", {})
            if isinstance(provider_options, Mapping):
                provider_options = dict(provider_options)
            else:
                provider_options = {}
            provider_options["headed"] = False
            translation["provider_options"] = provider_options
        config["translation"] = translation
        return config

    def _capture_dialogue_casting_settings(
        self, selected_items: list[dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        getter = getattr(self.service, "get_dialogue_casting", None)
        if not callable(getter):
            return {}
        captured = {}
        for item in selected_items:
            project_id = str(item.get("project_id", ""))
            result = getter(project_id)
            raw = result.get("config") if isinstance(result, Mapping) else None
            if not isinstance(raw, Mapping):
                raise ValueError("Không thể đóng băng cấu hình Phân vai thoại của Multi video")
            frozen = {
                key: deepcopy(raw.get(key))
                for key in ("enabled", "male_voice", "female_voice", "fallback_voice")
            }
            overrides = raw.get("overrides", [])
            if not isinstance(overrides, list):
                raise ValueError("Nhãn phân vai của project Multi video không hợp lệ")
            frozen["overrides"] = deepcopy(overrides)
            captured[project_id] = frozen
        return captured

    def on_worker_event(self, job_id: str, kind: str, payload: Mapping[str, object]) -> bool:
        items = self.service.multi_videos.list_items()
        item = next((x for x in items if str(x.get("child_job_id", "")) == job_id), None)
        if item is None:
            return False
        stage = str(item.get("stage", MultiStage.OCR.value))
        if kind == "progress":
            fraction = self._progress_fraction(payload)
            self.service.multi_videos.update_item(
                str(item["project_id"]), progress=stage_progress(stage, fraction)
            )
            self._publish_status()
            return True
        if kind in {"succeeded", "failed", "paused", "stopped"}:
            self._schedule_advance(0)
        return True

    def on_worker_finished(self, job_id: str) -> None:
        self._schedule_advance(0)

    @staticmethod
    def _progress_fraction(payload: Mapping[str, object]) -> float:
        """Decode the worker progress contract without guessing in QML."""
        return worker_progress_fraction(dict(payload))

    def _next_attempt_number(self, project_id: str, stage: str, pass_number: int) -> int:
        resolver = getattr(self.service.multi_videos, "next_attempt_number", None)
        if callable(resolver):
            return int(resolver(project_id, stage, pass_number))
        # Small in-memory repositories used by integrations predating schema
        # v4 do not expose the audit query; preserve their old behavior while
        # the durable repository remains authoritative in production.
        item = next(
            (
                value
                for value in self.service.multi_videos.list_items()
                if str(value.get("project_id", "")) == project_id
            ),
            {},
        )
        return int(item.get("retry_count", 0)) + 1

    def _schedule_advance(self, delay_ms: int = 0) -> None:
        if delay_ms:
            self._retry_timer.start(delay_ms)
        elif not self._advance_timer.isActive():
            self._advance_timer.start(0)
        self._publish_status()

    def _advance(self) -> None:
        if self._drain_pending_removals():
            return
        batch = self.service.multi_videos.get_batch()
        if batch["state"] == MultiBatchState.PAUSE_REQUESTED.value:
            items = self.service.multi_videos.list_items()
            current_project = str(batch.get("current_project_id", ""))
            current = next(
                (x for x in items if x["project_id"] == current_project),
                None,
            )
            # A deadline pause marks the child job INTERRUPTED before asking
            # WorkerSupervisor to terminate its process tree.  Keep the batch
            # in pause_requested until workerFinished removes that handle;
            # otherwise this branch can publish PAUSED while the old worker is
            # still alive, allowing Resume to start a duplicate child job.
            workers = getattr(self.server, "workers", None)
            child_job_id = str(current.get("child_job_id", "")) if current else ""
            if child_job_id:
                try:
                    child_job = self.service.jobs.get(child_job_id)
                except LookupError:
                    child_job = None
                # A cooperative pause intentionally keeps the worker process
                # alive so Resume can reuse the same job/checkpoint.  Let
                # _check_child publish PAUSED immediately for that case.  The
                # worker-handle guard is only needed after the deadline path,
                # where the job was marked INTERRUPTED and its process tree is
                # still being terminated.
                if (
                    child_job is not None
                    and child_job.state is JobState.PAUSED
                ):
                    self._check_child(current)
                    return
                if (
                    child_job is not None
                    and child_job.state is JobState.INTERRUPTED
                    and workers is not None
                    and workers.has_worker(child_job_id)
                ):
                    return
            if (
                current is not None
                and child_job_id
                and self._check_child(current)
            ):
                return
            self.service.multi_videos.update_batch(
                state=MultiBatchState.PAUSED.value,
                current_project_id="",
            )
            return
        if batch["state"] != MultiBatchState.RUNNING.value:
            return
        if not self.server.licensing.status()["active"]:
            # Do not admit another child; pause current work at its normal safe checkpoint.
            self.pause()
            return
        items = self.service.multi_videos.list_items()
        current_project = str(batch.get("current_project_id", ""))
        if current_project:
            current = next((x for x in items if x["project_id"] == current_project), None)
            if current is None or not current.get("selected"):
                # Selection may change only after the batch reached a safe
                # paused checkpoint.  Cancel its paused child before detaching
                # it; otherwise that active job blocks a later run forever.
                if current is not None and self._cancel_deselected_child(current):
                    return
                if current is not None:
                    self.service.multi_videos.update_item(
                        str(current["project_id"]),
                        state=MultiItemState.WAITING.value,
                        child_job_id="",
                        error_code="",
                        error_payload={},
                    )
                self.service.multi_videos.update_batch(current_project_id="")
                current_project = ""
                current = None
            if (
                current is not None
                and str(current.get("child_job_id", ""))
                and self._check_child(current)
            ):
                return
        selected = [x for x in items if x.get("selected")]
        if not selected:
            self._finish_batch(items)
            return
        if self._advance_legacy_nonblocking_review(selected):
            return
        if self._advance_recoverable_translation_ledger_failure(selected):
            return
        if self._advance_manual_translation_reviews(selected):
            return
        retry_pass = int(batch.get("retry_pass", 0))
        pending = [
            x
            for x in selected
            if x.get("state")
            not in {
                MultiItemState.COMPLETED.value,
                MultiItemState.FAILED.value,
                # Deferred items must wait until every item in the first pass
                # has been visited.  Including them here immediately made the
                # "final pass" branch unreachable.
                MultiItemState.DEFERRED.value,
            }
        ]
        if not pending:
            deferred = [x for x in selected if x.get("state") == MultiItemState.DEFERRED.value]
            if deferred and retry_pass <= 1:
                promote = getattr(
                    self.service.multi_videos, "promote_deferred_final_pass", None
                )
                if callable(promote):
                    promote()
                else:
                    self.service.multi_videos.update_batch(retry_pass=1, current_project_id="")
                    for item in deferred:
                        self.service.multi_videos.update_item(
                            str(item["project_id"]),
                            state=MultiItemState.WAITING.value,
                            retry_count=0,
                            final_pass=True,
                        )
                self._advance()
                return
            self._finish_batch(items)
            return
        item = pending[0]
        self.service.multi_videos.update_batch(current_project_id=str(item["project_id"]))
        self._launch_or_skip(item)

    def _advance_manual_translation_reviews(
        self, selected: list[dict[str, object]]
    ) -> bool:
        """Promote a hand-fixed Multi translation review to the next stage.

        A failed translation job may have imported its immutable review SRT
        before stopping on CJK.  Once the user edits that SRT in the project,
        the project is the authoritative input for TTS; do not spend another
        provider attempt or retranslate the already reviewed cues.
        """

        checker = getattr(self.service, "get_manual_translation_review_state", None)
        accepter = getattr(self.service.multi_videos, "accept_manual_translation_review", None)
        if not callable(checker):
            return False
        for item in selected:
            if str(item.get("stage", "")) != MultiStage.TRANSLATE.value:
                continue
            if str(item.get("state", "")) not in {
                MultiItemState.DEFERRED.value,
                MultiItemState.FAILED.value,
                MultiItemState.WAITING.value,
            }:
                continue
            project_id = str(item.get("project_id", ""))
            job_id = str(item.get("child_job_id", ""))
            try:
                review = checker(project_id, job_id or None)
            except (LookupError, OSError, RuntimeError, TypeError, ValueError):
                continue
            if not isinstance(review, Mapping) or review.get("ready") is not True:
                continue
            if callable(accepter):
                accepted = bool(accepter(project_id=project_id, job_id=job_id))
                if not accepted:
                    continue
            else:
                self.service.multi_videos.update_item(
                    project_id,
                    stage=MultiStage.TTS.value,
                    state=MultiItemState.WAITING.value,
                    progress=stage_progress(MultiStage.TRANSLATE.value, 1.0),
                    child_job_id="",
                    retry_count=0,
                    final_pass=False,
                    error_code="",
                    error_payload={},
                )
            self._schedule_advance(0)
            return True
        return False

    def _check_child(self, item: Mapping[str, object]) -> bool:
        job_id = str(item.get("child_job_id", ""))
        if not job_id:
            return False
        try:
            job = self.service.jobs.get(job_id)
        except LookupError:
            self.service.multi_videos.finish_attempt(
                job_id,
                state="cancelled",
                error_code="child_missing_after_deselection",
                error_payload={
                    "message": "Không còn tìm thấy tác vụ sau khi video được bỏ chọn."
                },
            )
            return False
        stage = MultiStage(str(item.get("stage", MultiStage.OCR.value)))
        if (
            stage is MultiStage.TRANSLATE
            and job.state not in {JobState.SUCCEEDED, JobState.CANCELLED}
            and not self._translation_job_uses_multi_policy(job)
        ):
            self._replace_legacy_translation_child(item, job_id, job)
            return True
        if job.state is JobState.SUCCEEDED:
            if job_id in self._handled_terminal_jobs:
                return True
            self._handled_terminal_jobs.add(job_id)
            self.service.multi_videos.finish_attempt(job_id, state="succeeded")
            self._stage_succeeded(item)
            return True
        if job.state is JobState.FAILED:
            if job_id in self._handled_terminal_jobs:
                return True
            self._handled_terminal_jobs.add(job_id)
            code = str(job.error_code or "worker_failed")
            stage_value = stage.value
            payload = self._multi_failure_payload(stage_value, code, job.error_payload)
            if (stage_value, code) in self.NON_BLOCKING_STAGE_REVIEWS:
                # The Job Manager imports and validates the immutable OCR REVIEW
                # SRT before it persists this terminal event.  Multi therefore
                # treats the QA result as a completed stage while manual OCR
                # keeps its existing review workflow.
                self.service.multi_videos.finish_attempt(job_id, state="succeeded")
                self._stage_succeeded(item)
                return True
            if (
                stage_value == MultiStage.TRANSLATE.value
                and code == "translation_review_required"
                and payload.get("warnings_only") is True
            ):
                self.service.multi_videos.finish_attempt(job_id, state="succeeded")
                self._stage_succeeded(item)
                return True
            retryable = self._failure_is_retryable(stage_value, code, payload)
            record_failure = getattr(
                self.service.multi_videos, "record_stage_failure", None
            )
            if callable(record_failure):
                result = record_failure(
                    project_id=str(item["project_id"]),
                    stage=stage_value,
                    job_id=job_id,
                    error_code=code,
                    error_payload=payload,
                    retryable=retryable,
                )
                if result.get("applied"):
                    retry_count = int(result.get("retry_count", 0))
                    state = str(result.get("state", ""))
                    delay = (
                        self.RETRY_DELAYS_MS[
                            min(retry_count - 1, len(self.RETRY_DELAYS_MS) - 1)
                        ]
                        if state == MultiItemState.RETRYING.value and retry_count > 0
                        else 0
                    )
                    self._schedule_advance(delay)
            else:
                self.service.multi_videos.finish_attempt(
                    job_id, state="failed", error_code=code, error_payload=payload
                )
                self._stage_failed(item, code, payload, retryable=retryable)
            return True
        if job.state is JobState.INTERRUPTED:
            batch = self.service.multi_videos.get_batch()
            batch_state = str(batch.get("state", MultiBatchState.IDLE.value))
            if job_id in self._handled_terminal_jobs:
                # After an explicit Resume, returning False lets _advance call
                # _launch_or_skip and restart this exact durable child. While
                # paused/interrupted, keep the item stable and never auto-loop.
                return batch_state != MultiBatchState.RUNNING.value
            self._handled_terminal_jobs.add(job_id)
            pause_requested = batch_state == MultiBatchState.PAUSE_REQUESTED.value
            payload = {
                "message": (
                    "Tác vụ đã bị gián đoạn trước khi bộ máy xác nhận trạng thái. "
                    "Cache/checkpoint hiện có vẫn an toàn."
                ),
                "action": (
                    "Bấm Tiếp tục Multi video để chạy lại đúng công đoạn từ cache; "
                    "tool sẽ không tự lặp retry."
                ),
                "stage": stage.value,
                "job_id": job_id,
            }
            recorder = getattr(
                self.service.multi_videos,
                "record_stage_interruption",
                None,
            )
            if callable(recorder):
                result = recorder(
                    project_id=str(item["project_id"]),
                    stage=stage.value,
                    job_id=job_id,
                    pause_requested=pause_requested,
                    error_payload=payload,
                )
                if not result.get("applied"):
                    return True
            else:
                self.service.multi_videos.finish_attempt(
                    job_id,
                    state="interrupted",
                    error_code=(
                        "multi_pause_checkpoint"
                        if pause_requested
                        else self.CHILD_INTERRUPTED_CODE
                    ),
                    error_payload={} if pause_requested else payload,
                )
                self.service.multi_videos.update_item(
                    str(item["project_id"]),
                    error_code="" if pause_requested else self.CHILD_INTERRUPTED_CODE,
                    error_payload={} if pause_requested else payload,
                )
                self.service.multi_videos.update_batch(
                    state=(
                        MultiBatchState.PAUSED.value
                        if pause_requested
                        else MultiBatchState.INTERRUPTED.value
                    )
                )
            self._publish_status()
            return True
        if job.state is JobState.CANCELLED:
            if job_id in self._handled_terminal_jobs:
                return True
            self._handled_terminal_jobs.add(job_id)
            if (
                str(item.get("error_code", ""))
                == self.TRANSLATION_POLICY_UPGRADE_PENDING_CODE
            ):
                self._finish_translation_policy_upgrade(item, job_id)
                return True
            self.service.multi_videos.finish_attempt(
                job_id,
                state="cancelled",
                error_code="child_cancelled",
                error_payload={
                    "message": (
                        "Tác vụ con đã bị người dùng hủy; video này được bỏ qua "
                        "trong lượt Multi hiện tại."
                    ),
                },
            )
            self._stage_cancelled(item)
            return True
        if job.state is JobState.PAUSED:
            batch = self.service.multi_videos.get_batch()
            if batch["state"] == MultiBatchState.PAUSE_REQUESTED.value:
                self.service.multi_videos.update_batch(state=MultiBatchState.PAUSED.value)
            elif batch["state"] == MultiBatchState.RUNNING.value:
                self.server._resume_job(job_id)  # type: ignore[attr-defined]
            return True
        return job.state in {
            JobState.RUNNING,
            JobState.START_REQUESTED,
            JobState.RESUME_REQUESTED,
            JobState.PAUSE_REQUESTED,
            JobState.CANCEL_REQUESTED,
        }

    @staticmethod
    def _failure_is_retryable(
        stage: str, code: str, payload: Mapping[str, object]
    ) -> bool:
        """Only CJK review remains a content blocker for new Multi jobs."""

        if stage == MultiStage.TRANSLATE.value and code == "translation_review_required":
            raw_ids = payload.get("cjk_cue_ids", [])
            return isinstance(raw_ids, list) and bool(raw_ids)
        return True

    def _advance_legacy_nonblocking_review(
        self, selected: list[dict[str, object]]
    ) -> bool:
        """Adopt OCR REVIEW items deferred by releases before this policy.

        Such an item could only be persisted after Job Manager had validated and
        imported the immutable review SRT.  Advancing it avoids re-running OCR
        merely to reinterpret the same QA evidence after an application update.
        """

        item = next(
            (
                value
                for value in selected
                if value.get("stage") == MultiStage.OCR.value
                and value.get("state")
                in {MultiItemState.DEFERRED.value, MultiItemState.FAILED.value}
                and value.get("error_code") == "ocr_review_required"
            ),
            None,
        )
        if item is None:
            return False
        self._stage_succeeded(item)
        return True

    def _advance_recoverable_translation_ledger_failure(
        self, selected: list[dict[str, object]]
    ) -> bool:
        """Resume cache rejected by the old QA-policy normalization defect."""

        item = next(
            (
                value
                for value in selected
                if value.get("stage") == MultiStage.TRANSLATE.value
                and value.get("state")
                in {MultiItemState.DEFERRED.value, MultiItemState.FAILED.value}
                and value.get("error_code") == "ValueError"
                and isinstance(value.get("error_payload"), Mapping)
                and value["error_payload"].get("message")
                == self.TRANSLATION_LEDGER_CACHE_ERROR
            ),
            None,
        )
        if item is None:
            return False
        project_id = str(item["project_id"])
        job_id = str(item.get("child_job_id", ""))
        if not job_id:
            return False
        try:
            job = self.service.jobs.get(job_id)
        except LookupError:
            return False
        if job.state not in {JobState.FAILED, JobState.INTERRUPTED}:
            return False
        recover = getattr(
            self.service.multi_videos,
            "recover_software_failed_stage",
            None,
        )
        if callable(recover):
            recovered = bool(
                recover(
                    project_id=project_id,
                    stage=MultiStage.TRANSLATE.value,
                    job_id=job_id,
                    error_code="ValueError",
                )
            )
            if not recovered:
                return False
        else:
            self.service.multi_videos.update_item(
                project_id,
                state=MultiItemState.WAITING.value,
                retry_count=0,
                final_pass=False,
                error_code="",
                error_payload={},
            )
        self.service.refresh_translation_multi_policy(job_id=job_id)
        self.server._retry_job(job_id)  # type: ignore[attr-defined]
        self.service.multi_videos.update_batch(current_project_id=project_id)
        self._start_existing_child(
            project_id,
            MultiStage.TRANSLATE,
            job_id,
            counts_as_retry=False,
        )
        return True

    def _launch_or_skip(self, item: Mapping[str, object]) -> None:
        project_id = str(item["project_id"])
        stage = MultiStage(str(item.get("stage", MultiStage.OCR.value)))
        job_id = str(item.get("child_job_id", ""))
        if job_id:
            try:
                job = self.service.jobs.get(job_id)
            except LookupError:
                job = None
            if (
                job is not None
                and stage is MultiStage.TRANSLATE
                and job.state not in {JobState.SUCCEEDED, JobState.CANCELLED}
                and not self._translation_job_uses_multi_policy(job)
            ):
                self._replace_legacy_translation_child(item, job_id, job)
                return
            if job is not None and job.state is JobState.PAUSED:
                self.server._resume_job(job_id)  # type: ignore[attr-defined]
                return
            if job is not None and job.state in {
                JobState.FAILED,
                JobState.INTERRUPTED,
            }:
                # Retry the exact durable job so its resume fingerprint and
                # checkpoint remain authoritative.  Creating another job here
                # silently discarded the cache that Multi promises to reuse.
                counts_as_retry = job.state is JobState.FAILED
                self.server._retry_job(job_id)  # type: ignore[attr-defined]
                self._start_existing_child(
                    project_id,
                    stage,
                    job_id,
                    counts_as_retry=counts_as_retry,
                )
                return
            if job is not None and job.state is JobState.QUEUED:
                self._start_existing_child(
                    project_id,
                    stage,
                    job_id,
                    counts_as_retry=int(item.get("retry_count", 0)) > 0,
                )
                return
            if job is not None and job.state in {
                JobState.RUNNING,
                JobState.START_REQUESTED,
                JobState.RESUME_REQUESTED,
            }:
                return
        if stage is MultiStage.TRANSLATE:
            checker = getattr(self.service, "get_manual_translation_review_state", None)
            if callable(checker):
                try:
                    review = checker(project_id, job_id or None)
                except (LookupError, OSError, RuntimeError, TypeError, ValueError):
                    review = {}
                if isinstance(review, Mapping) and review.get("ready") is True:
                    accepter = getattr(
                        self.service.multi_videos,
                        "accept_manual_translation_review",
                        None,
                    )
                    if callable(accepter) and not accepter(
                        project_id=project_id, job_id=job_id
                    ):
                        return
                    self._stage_succeeded({**dict(item), "stage": stage.value})
                    return
        # Let the normal job factory calculate the immutable input/settings
        # fingerprints.  It returns a matching successful job without starting
        # a worker; a changed setting creates a new job only for this stage.
        # Merely seeing SRT/D1 in the project is insufficient because it does
        # not prove that output was made with this batch snapshot.
        self._start_child(project_id, stage)

    def _start_child(self, project_id: str, stage: MultiStage) -> None:
        self.server.licensing.require_active()
        config = self._settings
        try:
            self._ensure_stage_capacity(stage)
            if stage is MultiStage.OCR:
                options = dict(config.get("ocr", {}))
                result = self.service.create_ocr_job(
                    project_id=project_id,
                    sample_interval_us=int(options.get("sample_interval_us", 800_000)),
                    scene_text_enabled=options.get("scene_text_enabled", True),
                    engine_python=str(options.get("engine_python", runtime_program_path())),
                    device=str(options.get("device", "gpu:0")),
                    extraction_method=str(
                        options.get("extraction_method", "frame_ocr")
                    ),
                    language=str(options.get("language", "zh")),
                    model_id=str(options.get("model_id", "large-v3")),
                    model_path=(
                        str(options["model_path"])
                        if isinstance(options.get("model_path"), str)
                        and str(options["model_path"]).strip()
                        else None
                    ),
                    reuse_completed=True,
                )
                worker_module = "ducai_studio.workers.ocr_worker"
                stage_key = "ocr"
            elif stage is MultiStage.TRANSLATE:
                options = dict(config.get("translation", {}))
                provider_options = dict(options.get("provider_options", {}))
                frozen_preferences = config.get("translation_preferences_by_project", {})
                if isinstance(frozen_preferences, Mapping):
                    project_preferences = frozen_preferences.get(project_id, {})
                    if isinstance(project_preferences, Mapping):
                        provider_options["translation_style_preset"] = str(
                            project_preferences.get("preset", "auto")
                        )
                        provider_options["translation_custom_rules"] = str(
                            project_preferences.get("custom_rules", "")
                        )
                provider_options.update(
                    {
                        key: options[key]
                        for key in self.MULTI_TRANSLATION_POLICY
                        if key in options
                    }
                )
                result = self.service.create_translation_job(
                    project_id=project_id,
                    provider_id=str(options.get("provider_id", "gemini-cli")),
                    model_id=str(options.get("model_id", "gemini-2.5-flash")),
                    provider_options=provider_options,
                    reuse_completed=True,
                )
                worker_module = "ducai_studio.workers.translation_worker"
                stage_key = "translation"
            else:
                options = dict(config.get("tts", {}))
                provider_options = dict(options.get("provider_options", {}))
                frozen_by_project = config.get("dialogue_casting_by_project", {})
                frozen_casting = (
                    frozen_by_project.get(project_id)
                    if isinstance(frozen_by_project, Mapping)
                    else None
                )
                if isinstance(frozen_casting, Mapping):
                    provider_options["dialogue_casting"] = deepcopy(dict(frozen_casting))
                prepare_voice_generation = getattr(
                    self.service, "prepare_voice_generation", None
                )
                factory = prepare_voice_generation if callable(prepare_voice_generation) else (
                    self.service.create_tts_job
                )
                result = factory(
                    project_id=project_id,
                    provider_id=str(options.get("provider_id", "piper")),
                    voice_id=str(options.get("voice_id", "")),
                    provider_options=provider_options,
                    reuse_completed=True,
                )
                worker_module = "ducai_studio.workers.tts_worker"
                stage_key = "tts"
        except Exception as exc:
            # Job creation validates media/provider/model/voice before any
            # worker starts.  A preflight error is not a provider attempt and
            # must not consume the five retry slots.
            self._preflight_failed(project_id, stage, exc)
            return
        job_id = str(result.get("id", ""))
        if not job_id:
            self._stage_failed(
                item={
                    "project_id": project_id,
                    "stage": stage.value,
                    "final_pass": False,
                    "retry_count": 0,
                },
                code="job_creation_failed",
                payload={"message": "Job Manager không trả về mã tác vụ"},
            )
            return
        if result.get("reused") is True:
            current = next(
                (
                    x
                    for x in self.service.multi_videos.list_items()
                    if x["project_id"] == project_id
                ),
                {"project_id": project_id, "stage": stage.value},
            )
            self._stage_succeeded(current)
            return
        spec = result.get("spec")
        job_kind = str(spec.get("kind", "")) if isinstance(spec, Mapping) else ""
        is_dialogue_casting = job_kind == JobKind.DIALOGUE_CASTING.value
        if is_dialogue_casting:
            worker_module = "ducai_studio.workers.dialogue_casting_worker"
            stage_key = JobKind.DIALOGUE_CASTING.value
        pass_number = int(self.service.multi_videos.get_batch().get("retry_pass", 0))
        attempt = self._next_attempt_number(
            project_id,
            stage.value,
            pass_number,
        )
        current_item = next(
            (
                item
                for item in self.service.multi_videos.list_items()
                if str(item.get("project_id", "")) == project_id
            ),
            {},
        )
        is_retry = int(current_item.get("retry_count", 0) or 0) > 0
        self.service.multi_videos.update_item(
            project_id,
            state=MultiItemState.RETRYING.value if is_retry else MultiItemState.RUNNING.value,
            progress=stage_progress(stage.value, 0.0),
            child_job_id=job_id,
            error_code="",
            error_payload={},
        )
        self.service.multi_videos.add_attempt(
            project_id,
            stage.value,
            pass_number,
            attempt,
            job_id,
        )
        self._handled_terminal_jobs.discard(job_id)
        try:
            self.server._start_job(  # type: ignore[attr-defined]
                {
                    "job_id": job_id,
                    "program": str(runtime_program_path()),
                    "arguments": ["-m", worker_module, job_id],
                    "resume_stage_key": stage_key,
                    "resume_checkpoint_key": "latest",
                }
            )
        except Exception as exc:
            self._handle_worker_start_failure(project_id, stage, job_id, exc)

    @classmethod
    def _translation_job_uses_multi_policy(cls, job: object) -> bool:
        spec = getattr(job, "spec", None)
        provider_config = getattr(spec, "provider_config", None)
        # Lightweight test doubles and integrations predating JobSpec do not
        # expose provider_config.  Production JobRecord always does.
        if not isinstance(provider_config, Mapping):
            return True
        return all(
            provider_config.get(key) == expected
            for key, expected in cls.MULTI_TRANSLATION_POLICY.items()
        )

    def _replace_legacy_translation_child(
        self,
        item: Mapping[str, object],
        job_id: str,
        job: object,
    ) -> None:
        """Restart a legacy child with CJK-only QA at its existing checkpoint.

        Job specs are immutable, so resuming a child created with ``legacy``
        QA would silently restore the old blocking behavior.  Cancellation is
        checkpoint-safe and does not count as a provider retry; its bounded QA
        policy is refreshed only after the worker has stopped.
        """

        project_id = str(item["project_id"])
        self.service.multi_videos.update_item(
            project_id,
            error_code=self.TRANSLATION_POLICY_UPGRADE_PENDING_CODE,
            error_payload={
                "message": "Đang nâng cấp tác vụ Dịch sang chính sách chỉ chặn chữ Trung."
            },
        )
        state = getattr(job, "state", None)
        if state in {JobState.FAILED, JobState.INTERRUPTED}:
            self._handled_terminal_jobs.add(job_id)
            self._finish_translation_policy_upgrade(item, job_id)
            return
        if state is not JobState.CANCEL_REQUESTED:
            self.server._cancel_job(job_id)  # type: ignore[attr-defined]
        try:
            refreshed = self.service.jobs.get(job_id)
        except LookupError:
            refreshed = None
        if refreshed is not None and refreshed.state is JobState.CANCELLED:
            self._handled_terminal_jobs.add(job_id)
            self._finish_translation_policy_upgrade(item, job_id)
            return
        self._schedule_advance(250)

    def _finish_translation_policy_upgrade(
        self, item: Mapping[str, object], job_id: str
    ) -> None:
        project_id = str(item["project_id"])
        self.service.multi_videos.finish_attempt(
            job_id,
            state="cancelled",
            error_code=self.TRANSLATION_POLICY_UPGRADE_PENDING_CODE,
            error_payload={
                "message": "Tác vụ cũ đã dừng; Multi sẽ tiếp tục từ cache với chính sách mới."
            },
        )
        self.service.refresh_translation_multi_policy(job_id=job_id)
        self.server._retry_job(job_id)  # type: ignore[attr-defined]
        self.service.multi_videos.update_item(
            project_id, error_code="", error_payload={}
        )
        self._start_existing_child(
            project_id,
            MultiStage.TRANSLATE,
            job_id,
            counts_as_retry=False,
        )

    def _start_existing_child(
        self,
        project_id: str,
        stage: MultiStage,
        job_id: str,
        *,
        counts_as_retry: bool,
    ) -> None:
        self.server.licensing.require_active()
        try:
            self._ensure_stage_capacity(stage)
        except OSError as exc:
            self._preflight_failed(
                project_id,
                stage,
                exc,
                child_job_id=job_id,
            )
            return
        try:
            job = self.service.jobs.get(job_id)
        except LookupError:
            job = None
        job_kind = getattr(getattr(job, "spec", None), "kind", None)
        if job_kind is JobKind.DIALOGUE_CASTING or job_kind == JobKind.DIALOGUE_CASTING.value:
            worker_module = "ducai_studio.workers.dialogue_casting_worker"
            stage_key = JobKind.DIALOGUE_CASTING.value
        else:
            worker_module = {
                MultiStage.OCR: "ducai_studio.workers.ocr_worker",
                MultiStage.TRANSLATE: "ducai_studio.workers.translation_worker",
                MultiStage.TTS: "ducai_studio.workers.tts_worker",
            }[stage]
            stage_key = {
                MultiStage.OCR: "ocr",
                MultiStage.TRANSLATE: "translation",
                MultiStage.TTS: "tts",
            }[stage]
        pass_number = int(self.service.multi_videos.get_batch().get("retry_pass", 0))
        attempt = self._next_attempt_number(
            project_id,
            stage.value,
            pass_number,
        )
        self.service.multi_videos.update_item(
            project_id,
            state=(
                MultiItemState.RETRYING.value if counts_as_retry else MultiItemState.RUNNING.value
            ),
            child_job_id=job_id,
            error_code="",
            error_payload={},
        )
        self.service.multi_videos.add_attempt(
            project_id,
            stage.value,
            pass_number,
            attempt,
            job_id,
        )
        self._handled_terminal_jobs.discard(job_id)
        try:
            self.server._start_job(  # type: ignore[attr-defined]
                {
                    "job_id": job_id,
                    "program": str(runtime_program_path()),
                    "arguments": ["-m", worker_module, job_id],
                    "resume_stage_key": stage_key,
                    "resume_checkpoint_key": "latest",
                }
            )
        except Exception as exc:
            self._handle_worker_start_failure(project_id, stage, job_id, exc)

    def _handle_worker_start_failure(
        self, project_id: str, stage: MultiStage, job_id: str, exc: Exception
    ) -> None:
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
            self._preflight_failed(
                project_id,
                stage,
                exc,
                child_job_id=job_id,
            )
            return
        current = next(
            (
                item
                for item in self.service.multi_videos.list_items()
                if str(item.get("project_id", "")) == project_id
            ),
            {"project_id": project_id, "stage": stage.value, "child_job_id": job_id},
        )
        try:
            job = self.service.jobs.get(job_id)
        except LookupError:
            job = None
        if job is not None and job.state is JobState.FAILED:
            self._check_child(current)
            return
        self._stage_failed(
            current,
            "worker_start_failed",
            {"message": str(exc), "action": "Tool sẽ tự thử lại từ checkpoint hiện có."},
        )

    def _cancel_deselected_child(self, item: Mapping[str, object]) -> bool:
        job_id = str(item.get("child_job_id", ""))
        if not job_id:
            return False
        try:
            job = self.service.jobs.get(job_id)
        except LookupError:
            return False
        active_states = {
            JobState.QUEUED,
            JobState.START_REQUESTED,
            JobState.RUNNING,
            JobState.PAUSE_REQUESTED,
            JobState.PAUSED,
            JobState.RESUME_REQUESTED,
            JobState.CANCEL_REQUESTED,
            JobState.INTERRUPTED,
        }
        if job.state in active_states:
            self.service.multi_videos.update_item(
                str(item["project_id"]),
                error_code=self.DESELECTION_PENDING_CODE,
                error_payload={
                    "message": "Đang dừng tác vụ sau khi video được bỏ chọn."
                },
            )
            if job.state is not JobState.CANCEL_REQUESTED:
                self.server._cancel_job(job_id)  # type: ignore[attr-defined]
            try:
                refreshed = self.service.jobs.get(job_id)
            except LookupError:
                refreshed = None
            if refreshed is not None and refreshed.state in active_states:
                return True
            job = refreshed
            if job is None:
                return True
        if job.state is JobState.SUCCEEDED:
            self.service.multi_videos.finish_attempt(job_id, state="succeeded")
        elif job.state is JobState.FAILED:
            self.service.multi_videos.finish_attempt(
                job_id,
                state="failed",
                error_code=str(getattr(job, "error_code", "") or "worker_failed"),
                error_payload=dict(getattr(job, "error_payload", {}) or {}),
            )
        elif job.state is JobState.CANCELLED:
            self.service.multi_videos.finish_attempt(
                job_id,
                state="cancelled",
                error_code="child_deselected",
                error_payload={"message": "Video đã được bỏ chọn sau checkpoint an toàn."},
            )
        else:
            return True
        if str(item.get("error_code", "")) == self.DESELECTION_PENDING_CODE:
            self._schedule_advance(0)
        return False

    def remove_project(self, project_id: str) -> dict[str, object]:
        """Remove membership while ensuring its paused child cannot be orphaned."""

        item = next(
            (
                value
                for value in self.service.multi_videos.list_items()
                if str(value.get("project_id", "")) == project_id
            ),
            None,
        )
        if item is not None:
            job_id = str(item.get("child_job_id", ""))
            if job_id:
                try:
                    job = self.service.jobs.get(job_id)
                except LookupError:
                    job = None
                active_states = {
                    JobState.QUEUED,
                    JobState.START_REQUESTED,
                    JobState.RUNNING,
                    JobState.PAUSE_REQUESTED,
                    JobState.PAUSED,
                    JobState.RESUME_REQUESTED,
                    JobState.CANCEL_REQUESTED,
                }
                # Some job backends update the in-memory object to CANCELLED
                # synchronously.  Preserve the pre-cancel state so the durable
                # Multi attempt is always closed before membership disappears.
                was_active = job is not None and job.state in active_states
                self.service.multi_videos.update_item(
                    project_id,
                    error_code=self.REMOVAL_PENDING_CODE,
                    error_payload={
                        "message": "Đang chờ tác vụ dừng an toàn trước khi xóa khỏi Multi."
                    },
                )
                if was_active and job.state is not JobState.CANCEL_REQUESTED:
                    self.server._cancel_job(job_id)  # type: ignore[attr-defined]
                if self._drain_pending_removals():
                    self._publish_status()
                    return self.service.list_multi_videos()
                return self.service.list_multi_videos()
        return self.service.remove_multi_project(project_id)

    def _drain_pending_removals(self) -> bool:
        """Finish durable membership removals only after their child is terminal.

        ``_cancel_job`` is synchronous for a queued/missing worker but merely
        changes a live worker to ``cancel_requested``.  Keeping the item until
        the terminal worker event preserves the job-to-item link and prevents
        an invisible active child from blocking a later run.
        """

        repository = getattr(self.service, "multi_videos", None)
        if repository is None:
            return False
        pending = False
        for item in tuple(repository.list_items()):
            if str(item.get("error_code", "")) != self.REMOVAL_PENDING_CODE:
                continue
            project_id = str(item.get("project_id", ""))
            job_id = str(item.get("child_job_id", ""))
            job = None
            if job_id:
                try:
                    job = self.service.jobs.get(job_id)
                except LookupError:
                    job = None
            if job is not None and job.state in {
                JobState.QUEUED,
                JobState.START_REQUESTED,
                JobState.RUNNING,
                JobState.PAUSE_REQUESTED,
                JobState.PAUSED,
                JobState.RESUME_REQUESTED,
                JobState.CANCEL_REQUESTED,
                JobState.INTERRUPTED,
            }:
                if job.state is not JobState.CANCEL_REQUESTED:
                    with suppress(LookupError, ValueError):
                        self.server._cancel_job(job_id)  # type: ignore[attr-defined]
                    try:
                        job = self.service.jobs.get(job_id)
                    except LookupError:
                        job = None
                if job is not None and job.state in {
                    JobState.QUEUED,
                    JobState.START_REQUESTED,
                    JobState.RUNNING,
                    JobState.PAUSE_REQUESTED,
                    JobState.PAUSED,
                    JobState.RESUME_REQUESTED,
                    JobState.CANCEL_REQUESTED,
                    JobState.INTERRUPTED,
                }:
                    pending = True
                    continue
            if job_id:
                if job is not None and job.state is JobState.SUCCEEDED:
                    self.service.multi_videos.finish_attempt(job_id, state="succeeded")
                elif job is not None and job.state is JobState.FAILED:
                    self.service.multi_videos.finish_attempt(
                        job_id,
                        state="failed",
                        error_code=str(getattr(job, "error_code", "") or "worker_failed"),
                        error_payload=dict(getattr(job, "error_payload", {}) or {}),
                    )
                else:
                    self.service.multi_videos.finish_attempt(
                        job_id,
                        state="cancelled",
                        error_code="membership_removed",
                        error_payload={
                            "message": "Video đã được xóa khỏi danh sách Multi video."
                        },
                    )
            self.service.remove_multi_project(project_id)
            self._handled_terminal_jobs.discard(job_id)
            self._publish_status()
        return pending

    def _ensure_stage_capacity(self, stage: MultiStage) -> None:
        if stage not in {MultiStage.OCR, MultiStage.TTS}:
            return
        checkpoints = getattr(self.service, "checkpoints", None)
        checker = getattr(checkpoints, "ensure_worker_capacity", None)
        if callable(checker):
            checker(
                stage_label=(
                    "trích xuất SRT" if stage is MultiStage.OCR else "lồng tiếng"
                )
            )

    def _preflight_failed(
        self,
        project_id: str,
        stage: MultiStage,
        error: BaseException | str,
        *,
        child_job_id: str = "",
    ) -> None:
        no_space = isinstance(error, OSError) and getattr(error, "errno", None) == errno.ENOSPC
        if no_space:
            error_code = "insufficient_disk_space"
            action = (
                "Dọn đủ dung lượng được nêu trên ổ chứa dữ liệu DUCAI, rồi bấm "
                "Chạy lại video lỗi hoặc Tiếp tục Multi video."
            )
        else:
            error_code = "preflight_failed"
            action = (
                "Kiểm tra media, model, API key và giọng đã chọn rồi bấm "
                "Tiếp tục Multi video."
            )
        self.service.multi_videos.update_item(
            project_id,
            state=MultiItemState.FAILED.value,
            child_job_id=child_job_id,
            error_code=error_code,
            error_payload={
                "message": str(error),
                "action": action,
            },
        )
        self.service.multi_videos.update_batch(current_project_id="")
        self._schedule_advance(0)

    def _stage_succeeded(self, item: Mapping[str, object]) -> None:
        project_id = str(item["project_id"])
        stage = MultiStage(str(item.get("stage", MultiStage.OCR.value)))
        child_job_id = str(item.get("child_job_id", ""))
        if stage is MultiStage.TTS and child_job_id:
            try:
                child = self.service.jobs.get(child_job_id)
            except LookupError:
                child = None
            child_kind = getattr(getattr(child, "spec", None), "kind", None)
            if (
                child_kind is JobKind.DIALOGUE_CASTING
                or child_kind == JobKind.DIALOGUE_CASTING.value
            ):
                self.service.multi_videos.update_item(
                    project_id,
                    state=MultiItemState.WAITING.value,
                    progress=stage_progress(MultiStage.TTS.value, 0.0),
                    child_job_id="",
                    retry_count=0,
                    final_pass=False,
                    error_code="",
                    error_payload={},
                )
                self._schedule_advance(0)
                return
        next_stage = (
            STAGES[STAGES.index(stage) + 1] if stage is not MultiStage.TTS else MultiStage.DONE
        )
        if next_stage is MultiStage.DONE:
            self.service.multi_videos.update_item(
                project_id,
                stage=next_stage.value,
                state=MultiItemState.COMPLETED.value,
                progress=1.0,
                child_job_id="",
                retry_count=0,
                final_pass=False,
                error_code="",
                error_payload={},
            )
        else:
            self.service.multi_videos.update_item(
                project_id,
                stage=next_stage.value,
                state=MultiItemState.WAITING.value,
                progress=stage_progress(stage.value, 1.0),
                child_job_id="",
                retry_count=0,
                final_pass=False,
                error_code="",
                error_payload={},
            )
        self._schedule_advance(0)

    @staticmethod
    def _multi_failure_payload(
        stage: str,
        code: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        result = dict(payload)
        if (stage, code) == (MultiStage.TRANSLATE.value, "translation_review_required"):
            cjk_count = max(0, int(result.get("cjk_blocker_count", 0) or 0))
            if cjk_count:
                result.setdefault(
                    "message",
                    f"Bản dịch còn {cjk_count} cue chứa chữ Trung; Multi sẽ thử lại từ cache.",
                )
                result.setdefault(
                    "action",
                    "Kiểm tra cue chữ Trung sau khi hết lượt thử; cache khác vẫn được giữ.",
                )
            elif result.get("warnings_only") is True:
                result.setdefault(
                    "message",
                    "Bản dịch chỉ còn cảnh báo; Multi sẽ tự chuyển sang lồng tiếng.",
                )
            return result
        if (stage, code) != (MultiStage.OCR.value, "ocr_review_required"):
            return result
        blocker_count = max(0, int(result.get("blocker_count", 0) or 0))
        blocker_text = (
            f" Còn {blocker_count} mục QA cần xem lại."
            if blocker_count
            else " Còn kết quả QA cần xem lại."
        )
        result.setdefault(
            "message",
            "OCR đã quét xong và lưu bản SRT cần rà soát."
            f"{blocker_text} Multi sẽ chuyển sang video tiếp theo thay vì quét lại. "
            "Mở dự án và rà soát các mục OCR được đánh dấu nếu muốn xử lý video này.",
        )
        result.setdefault(
            "action",
            "Mở dự án, rà soát các mục OCR được đánh dấu rồi chạy lại từ cache nếu cần.",
        )
        return result

    def _stage_failed(
        self,
        item: Mapping[str, object],
        code: str,
        payload: Mapping[str, object],
        *,
        retryable: bool = True,
    ) -> None:
        project_id = str(item["project_id"])
        retry_count = int(item.get("retry_count", 0)) + 1
        final_pass = bool(item.get("final_pass", False))
        retry_pass = int(self.service.multi_videos.get_batch().get("retry_pass", 0))
        if not retryable:
            state = (
                MultiItemState.FAILED.value
                if final_pass or retry_pass > 0
                else MultiItemState.DEFERRED.value
            )
        elif final_pass or (retry_count > 5 and retry_pass > 0):
            state = MultiItemState.FAILED.value
        elif retry_count > 5:
            state = MultiItemState.DEFERRED.value
        else:
            state = ""
        if state:
            self.service.multi_videos.update_item(
                project_id,
                state=state,
                retry_count=retry_count,
                error_code=code,
                error_payload=payload,
            )
            self.service.multi_videos.update_batch(current_project_id="")
            self._schedule_advance(0)
            return
        self.service.multi_videos.update_item(
            project_id,
            state=MultiItemState.RETRYING.value,
            retry_count=retry_count,
            error_code=code,
            error_payload=payload,
        )
        self.service.multi_videos.update_batch(current_project_id="")
        self._schedule_advance(
            self.RETRY_DELAYS_MS[min(retry_count - 1, len(self.RETRY_DELAYS_MS) - 1)]
        )

    def _stage_cancelled(self, item: Mapping[str, object]) -> None:
        project_id = str(item["project_id"])
        self.service.multi_videos.update_item(
            project_id,
            state=MultiItemState.FAILED.value,
            error_code="child_cancelled",
            error_payload={
                "message": (
                    "Tác vụ con đã bị hủy. Bấm Chạy lại video lỗi để xử lý video "
                    "này trong lượt mới."
                ),
            },
        )
        self.service.multi_videos.update_batch(current_project_id="")
        self._schedule_advance(0)

    def _finish_batch(self, items: list[dict[str, object]]) -> None:
        selected = [item for item in items if item.get("selected")]
        errors = any(item.get("state") == MultiItemState.FAILED.value for item in selected)
        self.service.multi_videos.update_batch(
            state=(
                MultiBatchState.COMPLETED_WITH_ERRORS.value
                if errors
                else MultiBatchState.COMPLETED.value
            ),
            current_project_id="",
        )

    def _validate_settings(
        self,
        settings: Mapping[str, object],
        selected_items: list[dict[str, object]],
    ) -> dict[str, dict[str, str]]:
        for section in ("ocr", "translation", "tts"):
            value = settings.get(section, {})
            if not isinstance(value, Mapping):
                raise ValueError(f"Cấu hình {section} của Multi video không hợp lệ")
        ocr = dict(settings.get("ocr", {}))
        extraction_method = str(ocr.get("extraction_method", "frame_ocr")).casefold()
        if extraction_method not in {"frame_ocr", "whisper_audio"}:
            raise ValueError("Phương thức trích xuất Multi video không hợp lệ.")
        engine_python = Path(str(ocr.get("engine_python", runtime_program_path()))).expanduser()
        if not engine_python.is_file():
            raise ValueError(
                "Runtime Whisper không tồn tại; hãy chạy "
                "scripts/setup-whisper-runtime.ps1 trước khi chạy Multi."
                if extraction_method == "whisper_audio"
                else "Python Paddle OCR không tồn tại; hãy sửa cấu hình OCR "
                "trước khi chạy Multi."
            )
        if extraction_method == "whisper_audio":
            if not whisper_runtime_is_ready(engine_python):
                raise ValueError(
                    "Runtime Whisper chưa đầy đủ hoặc sai phiên bản; hãy chạy "
                    "scripts/setup-whisper-runtime.ps1 trước khi chạy Multi."
                )
            model_path = Path(str(ocr.get("model_path", ""))).expanduser()
            if not (
                model_path.is_dir()
                and (model_path / "model.bin").is_file()
                and (model_path / "config.json").is_file()
            ):
                raise ValueError(
                    "Model Whisper large-v3 chưa được cài đầy đủ trước khi chạy Multi."
                )
        translation = dict(settings.get("translation", {}))
        provider_id = str(translation.get("provider_id", "")).strip().casefold()
        model_id = str(translation.get("model_id", "")).strip()
        if provider_id not in {
            "gemini-web",
            "gemini-cli",
            "gemini-api",
            "deepseek-api",
            "tokenhub-api",
            "qwen-api",
            "glm-api",
            "openai-compatible",
        }:
            raise ValueError("Nguồn dịch Multi video không được hỗ trợ.")
        if not model_id:
            raise ValueError("Chưa chọn model dịch cho Multi video.")
        provider_options = translation.get("provider_options", {})
        provider_options = dict(provider_options) if isinstance(provider_options, Mapping) else {}
        references = provider_options.get("credential_references", [])
        if provider_id in {
            "gemini-api",
            "deepseek-api",
            "tokenhub-api",
            "qwen-api",
            "glm-api",
            "openai-compatible",
        } and (not isinstance(references, list) or not references):
            raise ValueError(
                "Chưa có API key tham chiếu cho nguồn dịch đã chọn; key phải được lưu "
                "trong Windows Credential Manager trước khi chạy Multi."
            )
        if isinstance(references, list) and references:
            stored = self.service.list_credentials().get("references", [])
            stored_names = {
                str(value).strip().casefold()
                for value in stored
                if isinstance(value, str) and value.strip()
            }
            missing = [
                str(value).strip()
                for value in references
                if isinstance(value, str)
                and str(value).strip().casefold() not in stored_names
            ]
            if missing:
                raise ValueError(
                    "API key tham chiếu không còn trong Windows Credential Manager; "
                    "hãy lưu lại key trước khi chạy Multi video."
                )
        tts = dict(settings.get("tts", {}))
        if str(tts.get("provider_id", "")).strip().casefold() not in {
            "piper",
            "vieneu-3.8",
        }:
            raise ValueError("Chưa chọn bộ máy giọng hợp lệ cho Multi video.")
        if not str(tts.get("voice_id", "")).strip():
            raise ValueError("Chưa chọn giọng đọc cho Multi video.")
        translation_preferences: dict[str, dict[str, str]] = {}
        for item in selected_items:
            project_id = str(item.get("project_id", ""))
            try:
                snapshot = self.service.projects.load(project_id)
            except LookupError as exc:
                raise ValueError("Một project Multi video không còn tồn tại.") from exc
            videos = [media for media in snapshot.media if media.kind.value == "video"]
            if not videos or not Path(videos[0].path).is_file():
                raise ValueError(f"Video nguồn của project “{snapshot.name}” không còn tồn tại.")
            translation_preferences[project_id] = {
                "preset": snapshot.translation_preferences.preset,
                "custom_rules": snapshot.translation_preferences.custom_rules,
            }
        return translation_preferences
