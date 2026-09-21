from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ducai_studio.domain.multi_video import (
    MultiBatchState,
    MultiItemState,
    MultiStage,
    stage_progress,
)

from .database import Database
from .job_repository import utc_now_text

PRIMARY_BATCH_ID = "multi-primary"


def _next_millisecond(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed + timedelta(milliseconds=1)).astimezone(UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


class MultiVideoRepository:
    """Durable membership and orchestration state for the Multi video tab."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_primary(self) -> None:
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO multi_batches(
                    id, state, config_json, retry_pass, current_project_id,
                    created_at, updated_at
                ) VALUES (?, ?, '{}', 0, '', ?, ?)
                """,
                (PRIMARY_BATCH_ID, MultiBatchState.IDLE.value, now, now),
            )

    def list_items(self) -> list[dict[str, object]]:
        self.ensure_primary()
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT project_id, ordinal, selected, stage, state, progress,
                       child_job_id, retry_count, final_pass, error_code,
                       error_json, updated_at
                FROM multi_batch_items
                WHERE batch_id = ?
                ORDER BY ordinal, project_id
                """,
                (PRIMARY_BATCH_ID,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["selected"] = bool(item["selected"])
            item["final_pass"] = bool(item["final_pass"])
            try:
                item["error_payload"] = json.loads(str(item.pop("error_json") or "{}"))
            except json.JSONDecodeError:
                item["error_payload"] = {}
            result.append(item)
        return result

    def get_batch(self) -> dict[str, object]:
        self.ensure_primary()
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM multi_batches WHERE id = ?", (PRIMARY_BATCH_ID,)
            ).fetchone()
        if row is None:
            raise LookupError("Multi video batch is unavailable")
        result = dict(row)
        try:
            result["config"] = json.loads(str(result.pop("config_json") or "{}"))
        except json.JSONDecodeError:
            result["config"] = {}
        return result

    def add_projects(
        self,
        project_ids: list[str],
        *,
        selected: bool = True,
        restore_hidden: bool = True,
    ) -> list[dict[str, object]]:
        ids = [value.strip() for value in project_ids if isinstance(value, str) and value.strip()]
        if not ids:
            return self.list_items()
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            if restore_hidden:
                connection.executemany(
                    "DELETE FROM multi_project_exclusions WHERE project_id = ?",
                    ((project_id,) for project_id in ids),
                )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), -1) AS max_ordinal
                FROM multi_batch_items WHERE batch_id = ?
                """,
                (PRIMARY_BATCH_ID,),
            ).fetchone()
            ordinal = int(row["max_ordinal"]) + 1 if row else 0
            for project_id in ids:
                existing = connection.execute(
                    "SELECT 1 FROM multi_batch_items WHERE batch_id = ? AND project_id = ?",
                    (PRIMARY_BATCH_ID, project_id),
                ).fetchone()
                if existing is not None:
                    if selected:
                        connection.execute(
                            """
                            UPDATE multi_batch_items SET selected = 1, updated_at = ?
                            WHERE batch_id = ? AND project_id = ?
                            """,
                            (now, PRIMARY_BATCH_ID, project_id),
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO multi_batch_items(
                        batch_id, project_id, ordinal, selected, stage, state,
                        progress, child_job_id, retry_count, final_pass,
                        error_code, error_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, '', 0, 0, '', '{}', ?)
                    """,
                    (
                        PRIMARY_BATCH_ID,
                        project_id,
                        ordinal,
                        int(bool(selected)),
                        MultiStage.OCR.value,
                        MultiItemState.WAITING.value,
                        now,
                    ),
                )
                ordinal += 1
        return self.list_items()

    def sync_projects(self, project_ids: list[str]) -> list[dict[str, object]]:
        """Append newly discovered video projects without selecting or unhiding them."""

        normalized = list(
            dict.fromkeys(
                value.strip()
                for value in project_ids
                if isinstance(value, str) and value.strip()
            )
        )
        if not normalized:
            return self.list_items()
        with self.database.connect(read_only=True) as connection:
            placeholders = ", ".join("?" for _value in normalized)
            hidden = {
                str(row["project_id"])
                for row in connection.execute(
                    f"""
                    SELECT project_id FROM multi_project_exclusions
                    WHERE project_id IN ({placeholders})
                    """,
                    tuple(normalized),
                ).fetchall()
            }
        visible = [project_id for project_id in normalized if project_id not in hidden]
        return self.add_projects(visible, selected=False, restore_hidden=False)

    def remove_project(self, project_id: str) -> list[dict[str, object]]:
        value = project_id.strip()
        if not value:
            return self.list_items()
        with self.database.write_transaction() as connection:
            project_exists = connection.execute(
                "SELECT 1 FROM projects WHERE id = ?", (value,)
            ).fetchone()
            if project_exists is not None:
                connection.execute(
                    """
                    INSERT INTO multi_project_exclusions(project_id, hidden_at)
                    VALUES (?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET hidden_at = excluded.hidden_at
                    """,
                    (value, utc_now_text()),
                )
            connection.execute(
                "DELETE FROM multi_batch_items WHERE batch_id = ? AND project_id = ?",
                (PRIMARY_BATCH_ID, value),
            )
            connection.execute(
                """
                UPDATE multi_batches SET current_project_id = '', updated_at = ?
                WHERE id = ? AND current_project_id = ?
                """,
                (utc_now_text(), PRIMARY_BATCH_ID, value),
            )
            self._compact_ordinals(connection)
        return self.list_items()

    def hidden_project_ids(self) -> tuple[str, ...]:
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT project_id FROM multi_project_exclusions
                ORDER BY hidden_at DESC, project_id
                """
            ).fetchall()
        return tuple(str(row["project_id"]) for row in rows)

    def restore_hidden_projects(self) -> list[dict[str, object]]:
        project_ids = list(self.hidden_project_ids())
        if not project_ids:
            return self.list_items()
        return self.add_projects(project_ids, selected=True, restore_hidden=True)

    def reorder_projects(self, project_ids: list[str]) -> list[dict[str, object]]:
        """Persist an exact membership order without changing selection or cache state."""

        normalized = [
            value.strip()
            for value in project_ids
            if isinstance(value, str) and value.strip()
        ]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Danh sách Multi video có project bị trùng")
        with self.database.write_transaction() as connection:
            rows = connection.execute(
                """
                SELECT project_id FROM multi_batch_items
                WHERE batch_id = ? ORDER BY ordinal, project_id
                """,
                (PRIMARY_BATCH_ID,),
            ).fetchall()
            current = [str(row["project_id"]) for row in rows]
            if set(normalized) != set(current) or len(normalized) != len(current):
                raise ValueError("Thứ tự mới phải chứa đúng toàn bộ video trong Multi")
            # UNIQUE(batch_id, ordinal) forbids in-place swaps. Move rows to a
            # disjoint ordinal range first, then assign their final positions.
            offset = len(current) + 1
            connection.execute(
                "UPDATE multi_batch_items SET ordinal = ordinal + ? WHERE batch_id = ?",
                (offset, PRIMARY_BATCH_ID),
            )
            for ordinal, project_id in enumerate(normalized):
                connection.execute(
                    """
                    UPDATE multi_batch_items SET ordinal = ?, updated_at = ?
                    WHERE batch_id = ? AND project_id = ?
                    """,
                    (ordinal, utc_now_text(), PRIMARY_BATCH_ID, project_id),
                )
        return self.list_items()

    def set_selected(self, project_id: str, selected: bool) -> list[dict[str, object]]:
        with self.database.write_transaction() as connection:
            connection.execute(
                """
                UPDATE multi_batch_items SET selected = ?, updated_at = ?
                WHERE batch_id = ? AND project_id = ?
                """,
                (int(bool(selected)), utc_now_text(), PRIMARY_BATCH_ID, project_id.strip()),
            )
        return self.list_items()

    def set_all_selected(self, selected: bool) -> list[dict[str, object]]:
        with self.database.write_transaction() as connection:
            connection.execute(
                "UPDATE multi_batch_items SET selected = ?, updated_at = ? WHERE batch_id = ?",
                (int(bool(selected)), utc_now_text(), PRIMARY_BATCH_ID),
            )
        return self.list_items()

    def update_batch(
        self,
        *,
        state: str | None = None,
        config: Mapping[str, object] | None = None,
        retry_pass: int | None = None,
        current_project_id: str | None = None,
    ) -> dict[str, object]:
        fields: list[str] = []
        values: list[object] = []
        if state is not None:
            MultiBatchState(state)
            fields.append("state = ?")
            values.append(state)
            if state in {
                MultiBatchState.COMPLETED.value,
                MultiBatchState.COMPLETED_WITH_ERRORS.value,
            }:
                fields.append("finished_at = ?")
                values.append(utc_now_text())
            elif state == MultiBatchState.RUNNING.value:
                fields.append("finished_at = NULL")
        if config is not None:
            fields.append("config_json = ?")
            values.append(json.dumps(dict(config), ensure_ascii=False, sort_keys=True))
        if retry_pass is not None:
            fields.append("retry_pass = ?")
            values.append(int(retry_pass))
        if current_project_id is not None:
            fields.append("current_project_id = ?")
            values.append(current_project_id)
        fields.append("updated_at = ?")
        values.append(utc_now_text())
        values.append(PRIMARY_BATCH_ID)
        with self.database.write_transaction() as connection:
            connection.execute(
                f"UPDATE multi_batches SET {', '.join(fields)} WHERE id = ?", tuple(values)
            )
        return self.get_batch()

    def reset_for_start(self, config: Mapping[str, object]) -> None:
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            latest_attempt = connection.execute(
                """
                SELECT MAX(created_at) AS latest FROM multi_stage_attempts
                WHERE batch_id = ? AND project_id IN (
                    SELECT project_id FROM multi_batch_items
                    WHERE batch_id = ? AND selected = 1
                )
                """,
                (PRIMARY_BATCH_ID, PRIMARY_BATCH_ID),
            ).fetchone()
            latest_created_at = str(latest_attempt["latest"] or "") if latest_attempt else ""
            # ``multi_stage_attempts`` remains a full audit history.  Scope the
            # new retry budget with a monotonically later run boundary instead
            # of deleting previous attempts.  The +1 ms handles two resets in
            # the same clock tick without a schema migration.
            run_started_at = (
                _next_millisecond(latest_created_at)
                if latest_created_at and latest_created_at >= now
                else now
            )
            connection.execute(
                """
                UPDATE multi_batches
                SET state = ?, config_json = ?, retry_pass = 0,
                    current_project_id = '', started_at = ?, finished_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    MultiBatchState.RUNNING.value,
                    json.dumps(dict(config), ensure_ascii=False, sort_keys=True),
                    run_started_at,
                    now,
                    PRIMARY_BATCH_ID,
                ),
            )
            connection.execute(
                """
                UPDATE multi_batch_items
                SET stage = ?, state = ?, progress = 0, child_job_id = '',
                    retry_count = 0, final_pass = 0, error_code = '',
                    error_json = '{}', updated_at = ?
                WHERE batch_id = ? AND selected = 1
                """,
                (MultiStage.OCR.value, MultiItemState.WAITING.value, now, PRIMARY_BATCH_ID),
            )

    def reset_for_retry_start(self, config: Mapping[str, object]) -> None:
        """Start a new Multi pass without rewinding completed stages.

        ``completed_with_errors`` means the user explicitly chose
        “Chạy lại video lỗi”.  Keep each failed item's current stage and child
        job/checkpoint so a repaired translation goes directly to TTS and a
        failed OCR/TTS item resumes its own cache.  Completed cards remain
        untouched and are never processed again in this pass.
        """

        now = utc_now_text()
        with self.database.write_transaction() as connection:
            latest_attempt = connection.execute(
                """
                SELECT MAX(created_at) AS latest FROM multi_stage_attempts
                WHERE batch_id = ? AND project_id IN (
                    SELECT project_id FROM multi_batch_items
                    WHERE batch_id = ? AND selected = 1
                )
                """,
                (PRIMARY_BATCH_ID, PRIMARY_BATCH_ID),
            ).fetchone()
            latest_created_at = str(latest_attempt["latest"] or "") if latest_attempt else ""
            run_started_at = (
                _next_millisecond(latest_created_at)
                if latest_created_at and latest_created_at >= now
                else now
            )
            connection.execute(
                """
                UPDATE multi_batches
                SET state = ?, config_json = ?, retry_pass = 0,
                    current_project_id = '', started_at = ?, finished_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    MultiBatchState.RUNNING.value,
                    json.dumps(dict(config), ensure_ascii=False, sort_keys=True),
                    run_started_at,
                    now,
                    PRIMARY_BATCH_ID,
                ),
            )
            # Requeue only cards which are not already done.  Preserve stage
            # and child_job_id so the coordinator can retry the exact failed
            # job or promote a manually fixed translation review to TTS.
            connection.execute(
                """
                UPDATE multi_batch_items
                SET state = ?, progress = CASE stage
                        WHEN 'ocr' THEN 0
                        WHEN 'translate' THEN 0.3333333333333333
                        WHEN 'tts' THEN 0.6666666666666666
                        ELSE progress
                    END,
                    retry_count = 0, final_pass = 0, error_code = '',
                    error_json = '{}', updated_at = ?
                WHERE batch_id = ? AND selected = 1 AND state != ?
                """,
                (
                    MultiItemState.WAITING.value,
                    now,
                    PRIMARY_BATCH_ID,
                    MultiItemState.COMPLETED.value,
                ),
            )

    def promote_deferred_final_pass(self) -> int:
        """Atomically open the one final pass and requeue every deferred item."""

        now = utc_now_text()
        with self.database.write_transaction() as connection:
            connection.execute(
                """
                UPDATE multi_batches
                SET retry_pass = 1, current_project_id = '', updated_at = ?
                WHERE id = ?
                """,
                (now, PRIMARY_BATCH_ID),
            )
            updated = connection.execute(
                """
                UPDATE multi_batch_items
                SET state = ?, retry_count = 0, final_pass = 1, updated_at = ?
                WHERE batch_id = ? AND selected = 1 AND state = ?
                """,
                (
                    MultiItemState.WAITING.value,
                    now,
                    PRIMARY_BATCH_ID,
                    MultiItemState.DEFERRED.value,
                ),
            )
        return int(updated.rowcount)

    def recover_software_failed_stage(
        self,
        *,
        project_id: str,
        stage: str,
        job_id: str,
        error_code: str,
    ) -> bool:
        """Restore retry budget consumed by a deterministic application defect."""

        MultiStage(stage)
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            item = connection.execute(
                """
                SELECT state, child_job_id, error_code
                FROM multi_batch_items
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                """,
                (PRIMARY_BATCH_ID, project_id, stage),
            ).fetchone()
            if (
                item is None
                or str(item["child_job_id"]) != job_id
                or str(item["error_code"]) != error_code
                or str(item["state"])
                not in {MultiItemState.DEFERRED.value, MultiItemState.FAILED.value}
            ):
                return False
            connection.execute(
                """
                UPDATE multi_stage_attempts
                SET state = 'superseded', finished_at = COALESCE(finished_at, ?)
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                    AND job_id = ? AND state = 'failed' AND error_code = ?
                """,
                (now, PRIMARY_BATCH_ID, project_id, stage, job_id, error_code),
            )
            connection.execute(
                """
                UPDATE multi_batch_items
                SET state = ?, retry_count = 0, final_pass = 0,
                    error_code = '', error_json = '{}', updated_at = ?
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                """,
                (
                    MultiItemState.WAITING.value,
                    now,
                    PRIMARY_BATCH_ID,
                    project_id,
                    stage,
                ),
            )
        return True

    def record_stage_failure(
        self,
        *,
        project_id: str,
        stage: str,
        job_id: str,
        error_code: str,
        error_payload: Mapping[str, object],
        retryable: bool = True,
    ) -> dict[str, object]:
        """Persist one failed attempt and its retry decision in one transaction.

        ``attempt_number`` is the durable failure count for a stage/pass.  Using
        it instead of blindly incrementing the item makes recovery idempotent
        even if the process stopped after the worker reached ``failed``.
        Deterministic review results can opt out of immediate retries while
        retaining the same atomic attempt and deferred-pass history.
        """

        MultiStage(stage)
        now = utc_now_text()
        encoded_error = json.dumps(dict(error_payload), ensure_ascii=False)
        with self.database.write_transaction() as connection:
            item = connection.execute(
                """
                SELECT retry_count, final_pass, child_job_id
                FROM multi_batch_items
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                """,
                (PRIMARY_BATCH_ID, project_id, stage),
            ).fetchone()
            batch = connection.execute(
                "SELECT retry_pass, current_project_id FROM multi_batches WHERE id = ?",
                (PRIMARY_BATCH_ID,),
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT id, pass_number, attempt_number, state
                FROM multi_stage_attempts
                WHERE batch_id = ? AND project_id = ? AND stage = ? AND job_id = ?
                ORDER BY
                    CASE state WHEN 'running' THEN 0 WHEN 'interrupted' THEN 1 ELSE 2 END,
                    pass_number DESC, attempt_number DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (PRIMARY_BATCH_ID, project_id, stage, job_id),
            ).fetchone()
            if item is None or batch is None or attempt is None:
                return {"applied": False}
            if str(item["child_job_id"]) != job_id:
                return {"applied": False}
            attempt_state = str(attempt["state"])
            current_project = str(batch["current_project_id"])
            recoverable_partial = attempt_state == "failed" and current_project == project_id
            if attempt_state not in {"running", "interrupted"} and not recoverable_partial:
                return {"applied": False}

            failed_row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM multi_stage_attempts
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                    AND pass_number = ? AND state = 'failed'
                    AND created_at >= (
                        SELECT started_at FROM multi_batches WHERE id = ?
                    )
                """,
                (
                    PRIMARY_BATCH_ID,
                    project_id,
                    stage,
                    int(attempt["pass_number"]),
                    PRIMARY_BATCH_ID,
                ),
            ).fetchone()
            durable_failures = int(failed_row["total"] if failed_row is not None else 0)
            if attempt_state != "failed":
                durable_failures += 1
            retry_count = max(int(item["retry_count"]), durable_failures)
            retry_pass = int(batch["retry_pass"])
            final_pass = bool(item["final_pass"])
            if not retryable:
                item_state = (
                    MultiItemState.FAILED.value
                    if final_pass or retry_pass > 0
                    else MultiItemState.DEFERRED.value
                )
            elif final_pass or (retry_count > 5 and retry_pass > 0):
                item_state = MultiItemState.FAILED.value
            elif retry_count > 5:
                item_state = MultiItemState.DEFERRED.value
            else:
                item_state = MultiItemState.RETRYING.value

            connection.execute(
                """
                UPDATE multi_stage_attempts
                SET state = 'failed', error_code = ?, error_json = ?, finished_at = ?
                WHERE id = ?
                """,
                (error_code, encoded_error, now, attempt["id"]),
            )
            connection.execute(
                """
                UPDATE multi_batch_items
                SET state = ?, retry_count = ?, error_code = ?, error_json = ?, updated_at = ?
                WHERE batch_id = ? AND project_id = ? AND stage = ? AND child_job_id = ?
                """,
                (
                    item_state,
                    retry_count,
                    error_code,
                    encoded_error,
                    now,
                    PRIMARY_BATCH_ID,
                    project_id,
                    stage,
                    job_id,
                ),
            )
            connection.execute(
                """
                UPDATE multi_batches SET current_project_id = '', updated_at = ?
                WHERE id = ? AND current_project_id = ?
                """,
                (now, PRIMARY_BATCH_ID, project_id),
            )
        return {
            "applied": True,
            "state": item_state,
            "retry_count": retry_count,
        }

    def update_item(self, project_id: str, **updates: object) -> dict[str, object]:
        allowed = {
            "stage": "stage",
            "state": "state",
            "progress": "progress",
            "child_job_id": "child_job_id",
            "retry_count": "retry_count",
            "final_pass": "final_pass",
            "error_code": "error_code",
            "error_payload": "error_json",
        }
        fields: list[str] = []
        values: list[object] = []
        for key, value in updates.items():
            column = allowed.get(key)
            if column is None:
                raise ValueError(f"Unknown Multi item field: {key}")
            if key == "stage":
                MultiStage(str(value))
            elif key == "state":
                MultiItemState(str(value))
            elif key == "error_payload":
                value = json.dumps(
                    dict(value) if isinstance(value, Mapping) else {}, ensure_ascii=False
                )
            fields.append(f"{column} = ?")
            values.append(value)
        if not fields:
            return next(
                (item for item in self.list_items() if item["project_id"] == project_id), {}
            )
        fields.append("updated_at = ?")
        values.extend((utc_now_text(), PRIMARY_BATCH_ID, project_id.strip()))
        with self.database.write_transaction() as connection:
            connection.execute(
                f"""UPDATE multi_batch_items SET {", ".join(fields)}
                WHERE batch_id = ? AND project_id = ?""",
                tuple(values),
            )
        return next((item for item in self.list_items() if item["project_id"] == project_id), {})

    def accept_manual_translation_review(
        self,
        *,
        project_id: str,
        job_id: str = "",
    ) -> bool:
        """Record a user-accepted CJK review and advance the item to TTS."""

        now = utc_now_text()
        with self.database.write_transaction() as connection:
            item = connection.execute(
                """
                SELECT stage, state, child_job_id
                FROM multi_batch_items
                WHERE batch_id = ? AND project_id = ?
                """,
                (PRIMARY_BATCH_ID, project_id.strip()),
            ).fetchone()
            if item is None or str(item["stage"]) != MultiStage.TRANSLATE.value:
                return False
            current_job_id = str(item["child_job_id"] or "")
            if job_id and current_job_id and job_id != current_job_id:
                return False
            if current_job_id:
                connection.execute(
                    """
                    UPDATE multi_stage_attempts
                    SET state = 'manual_override', error_code = 'manual_translation_accepted',
                        error_json = ?, finished_at = COALESCE(finished_at, ?)
                    WHERE batch_id = ? AND project_id = ? AND stage = ?
                        AND job_id = ? AND state IN ('running', 'failed', 'interrupted')
                    """,
                    (
                        json.dumps(
                            {
                                "message": (
                                    "Người dùng đã sửa hết chữ Trung trong bản dịch review; "
                                    "Multi chuyển thẳng sang lồng tiếng."
                                )
                            },
                            ensure_ascii=False,
                        ),
                        now,
                        PRIMARY_BATCH_ID,
                        project_id.strip(),
                        MultiStage.TRANSLATE.value,
                        current_job_id,
                    ),
                )
            connection.execute(
                """
                UPDATE multi_batch_items
                SET stage = ?, state = ?, progress = ?, child_job_id = '',
                    retry_count = 0, final_pass = 0, error_code = '',
                    error_json = '{}', updated_at = ?
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                """,
                (
                    MultiStage.TTS.value,
                    MultiItemState.WAITING.value,
                    stage_progress(MultiStage.TRANSLATE.value, 1.0),
                    now,
                    PRIMARY_BATCH_ID,
                    project_id.strip(),
                    MultiStage.TRANSLATE.value,
                ),
            )
            return connection.total_changes > 0

    def add_attempt(
        self, project_id: str, stage: str, pass_number: int, attempt: int, job_id: str
    ) -> str:
        MultiStage(stage)
        identifier = str(uuid4())
        with self.database.write_transaction() as connection:
            running = connection.execute(
                """
                SELECT id FROM multi_stage_attempts
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                    AND pass_number = ? AND job_id = ? AND state = 'running'
                ORDER BY created_at DESC, attempt_number DESC, id DESC
                """,
                (
                    PRIMARY_BATCH_ID,
                    project_id,
                    stage,
                    int(pass_number),
                    job_id,
                ),
            ).fetchall()
            if running:
                # Reconcile rows left by the old interruption loop and reuse
                # the newest live attempt. A single child launch must never
                # manufacture another visible retry merely because _advance was
                # scheduled more than once.
                keep_id = str(running[0]["id"])
                stale_ids = [str(row["id"]) for row in running[1:]]
                if stale_ids:
                    now = utc_now_text()
                    placeholders = ", ".join("?" for _value in stale_ids)
                    connection.execute(
                        f"""
                        UPDATE multi_stage_attempts
                        SET state = 'superseded',
                            error_code = 'duplicate_attempt_reconciled',
                            error_json = ?, finished_at = ?
                        WHERE id IN ({placeholders})
                        """,
                        (
                            json.dumps(
                                {
                                    "message": (
                                        "Bản ghi retry trùng từ phiên bản cũ đã được hợp nhất; "
                                        "checkpoint hiện tại vẫn được giữ."
                                    )
                                },
                                ensure_ascii=False,
                            ),
                            now,
                            *stale_ids,
                        ),
                    )
                return keep_id
            batch = connection.execute(
                "SELECT started_at FROM multi_batches WHERE id = ?",
                (PRIMARY_BATCH_ID,),
            ).fetchone()
            run_started_at = str(batch["started_at"] or "") if batch is not None else ""
            created_at = utc_now_text()
            if run_started_at and created_at < run_started_at:
                # reset_for_start may advance the boundary by 1 ms to separate
                # attempts created in the same clock tick.  Never let the new
                # attempt fall behind its own run boundary.
                created_at = run_started_at
            connection.execute(
                """
                INSERT INTO multi_stage_attempts(
                    id, batch_id, project_id, stage, pass_number, attempt_number,
                    job_id, state, error_code, error_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', '', '{}', ?)
                """,
                (
                    identifier,
                    PRIMARY_BATCH_ID,
                    project_id,
                    stage,
                    pass_number,
                    attempt,
                    job_id,
                    created_at,
                ),
            )
        return identifier

    def record_stage_interruption(
        self,
        *,
        project_id: str,
        stage: str,
        job_id: str,
        pause_requested: bool,
        error_payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Close a child interruption once and stop automatic relaunch loops."""

        MultiStage(stage)
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            item = connection.execute(
                """
                SELECT child_job_id FROM multi_batch_items
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                """,
                (PRIMARY_BATCH_ID, project_id, stage),
            ).fetchone()
            batch = connection.execute(
                "SELECT state, current_project_id FROM multi_batches WHERE id = ?",
                (PRIMARY_BATCH_ID,),
            ).fetchone()
            if (
                item is None
                or batch is None
                or str(item["child_job_id"]) != job_id
            ):
                return {"applied": False}
            batch_state = str(batch["state"])
            if pause_requested:
                if batch_state not in {
                    MultiBatchState.PAUSE_REQUESTED.value,
                    MultiBatchState.PAUSED.value,
                }:
                    return {"applied": False}
                next_batch_state = MultiBatchState.PAUSED.value
                error_code = ""
                payload: dict[str, object] = {}
            else:
                if batch_state not in {
                    MultiBatchState.RUNNING.value,
                    MultiBatchState.INTERRUPTED.value,
                }:
                    return {"applied": False}
                next_batch_state = MultiBatchState.INTERRUPTED.value
                error_code = "child_interrupted"
                payload = dict(error_payload or {})
            connection.execute(
                """
                UPDATE multi_stage_attempts
                SET state = 'interrupted', error_code = ?, error_json = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                    AND job_id = ? AND state = 'running'
                """,
                (
                    "multi_pause_checkpoint" if pause_requested else error_code,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    PRIMARY_BATCH_ID,
                    project_id,
                    stage,
                    job_id,
                ),
            )
            connection.execute(
                """
                UPDATE multi_batch_items
                SET error_code = ?, error_json = ?, updated_at = ?
                WHERE batch_id = ? AND project_id = ? AND stage = ?
                    AND child_job_id = ?
                """,
                (
                    error_code,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    PRIMARY_BATCH_ID,
                    project_id,
                    stage,
                    job_id,
                ),
            )
            connection.execute(
                """
                UPDATE multi_batches SET state = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_batch_state, now, PRIMARY_BATCH_ID),
            )
        return {"applied": True, "batch_state": next_batch_state}

    def next_attempt_number(self, project_id: str, stage: str, pass_number: int) -> int:
        """Return the next durable attempt ordinal for one stage/pass.

        Retry budget is tracked separately on the batch item.  Attempt ordinals
        are an audit trail, so an app interruption must never reuse an ordinal
        that already belongs to a closed attempt.
        """

        MultiStage(stage)
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) AS maximum
                FROM multi_stage_attempts
                WHERE batch_id = ? AND project_id = ? AND stage = ? AND pass_number = ?
                    AND created_at >= (
                        SELECT started_at FROM multi_batches WHERE id = ?
                    )
                """,
                (
                    PRIMARY_BATCH_ID,
                    project_id.strip(),
                    stage,
                    int(pass_number),
                    PRIMARY_BATCH_ID,
                ),
            ).fetchone()
        return int(row["maximum"] if row is not None else 0) + 1

    def finish_attempt(
        self,
        job_id: str,
        *,
        state: str,
        error_code: str = "",
        error_payload: Mapping[str, object] | None = None,
    ) -> None:
        with self.database.write_transaction() as connection:
            connection.execute(
                """
                UPDATE multi_stage_attempts
                SET state = ?, error_code = ?, error_json = ?, finished_at = ?
                WHERE batch_id = ? AND job_id = ? AND state = 'running'
                """,
                (
                    state,
                    error_code,
                    json.dumps(dict(error_payload or {}), ensure_ascii=False),
                    utc_now_text(),
                    PRIMARY_BATCH_ID,
                    job_id,
                ),
            )

    def mark_interrupted(self) -> None:
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            connection.execute(
                """
                UPDATE multi_batches SET state = ?, updated_at = ?
                WHERE id = ? AND state IN (?, ?)
                """,
                (
                    MultiBatchState.INTERRUPTED.value,
                    now,
                    PRIMARY_BATCH_ID,
                    MultiBatchState.RUNNING.value,
                    MultiBatchState.PAUSE_REQUESTED.value,
                ),
            )
            connection.execute(
                """
                UPDATE multi_stage_attempts
                SET state = 'interrupted', error_code = 'app_interrupted',
                    error_json = ?, finished_at = ?
                WHERE batch_id = ? AND state = 'running'
                """,
                (
                    json.dumps(
                        {
                            "message": (
                                "Ứng dụng đã đóng khi tác vụ đang chạy; checkpoint vẫn được giữ."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    now,
                    PRIMARY_BATCH_ID,
                ),
            )

    @staticmethod
    def _compact_ordinals(connection) -> None:
        rows = connection.execute(
            """
            SELECT project_id FROM multi_batch_items
            WHERE batch_id = ? ORDER BY ordinal, project_id
            """,
            (PRIMARY_BATCH_ID,),
        ).fetchall()
        for ordinal, row in enumerate(rows):
            connection.execute(
                "UPDATE multi_batch_items SET ordinal = ? WHERE batch_id = ? AND project_id = ?",
                (ordinal, PRIMARY_BATCH_ID, row["project_id"]),
            )
