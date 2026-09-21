from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ducai_studio.domain.jobs import (
    TERMINAL_JOB_STATES,
    JobEvent,
    JobKind,
    JobRecord,
    JobSpec,
    JobState,
    assert_job_transition,
    assert_no_secret_fields,
    assert_safe_error_code,
    redact_secret_fields,
    thaw_json,
)

from .database import Database


def utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JobRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, spec: JobSpec, *, job_id: str | None = None) -> JobRecord:
        identifier = job_id or str(uuid4())
        created_at = utc_now_text()
        assert_no_secret_fields(spec.provider_config)
        purpose = ""
        if spec.kind is JobKind.TTS:
            raw_purpose = spec.provider_config.get("purpose")
            if isinstance(raw_purpose, str):
                purpose = raw_purpose.strip().casefold()
        elif spec.kind is JobKind.TRANSLATE:
            raw_purpose = spec.provider_config.get("translation_purpose")
            if isinstance(raw_purpose, str):
                purpose = raw_purpose.strip().casefold()
        with self.database.write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id, kind, state, project_id, input_revision, provider_id,
                    provider_version, model_id, input_fingerprint, settings_fingerprint,
                    config_json, purpose, created_at, error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
                """,
                (
                    identifier,
                    spec.kind.value,
                    JobState.QUEUED.value,
                    spec.project_id,
                    spec.input_revision,
                    spec.provider_id,
                    spec.provider_version,
                    spec.model_id,
                    spec.input_fingerprint,
                    spec.settings_fingerprint,
                    json.dumps(
                        thaw_json(spec.provider_config), ensure_ascii=False, sort_keys=True
                    ),
                    purpose,
                    created_at,
                ),
            )
        return JobRecord(identifier, spec, JobState.QUEUED, created_at)

    def get(self, job_id: str) -> JobRecord:
        with self.database.connect(read_only=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(f"Unknown job: {job_id}")
        return self._record_from_row(row)

    def replace_retry_credential_references(
        self,
        job_id: str,
        credential_references: tuple[str, ...],
    ) -> JobRecord:
        """Refresh transport credentials without invalidating content checkpoints."""
        if not credential_references or not all(
            isinstance(value, str) and value.strip() for value in credential_references
        ):
            raise ValueError("Retry credential references must be non-empty strings")
        with self.database.write_transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise LookupError(f"Unknown job: {job_id}")
            state = JobState(row["state"])
            if state not in {
                JobState.FAILED,
                JobState.INTERRUPTED,
                JobState.CANCELLED,
            }:
                raise ValueError("Credentials can only be refreshed for a retryable job")
            config = json.loads(row["config_json"])
            config["credential_references"] = list(credential_references)
            assert_no_secret_fields(config)
            connection.execute(
                "UPDATE jobs SET config_json = ? WHERE id = ? AND state = ?",
                (
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    job_id,
                    state.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
                VALUES (?, ?, 'credential_pool_refreshed', ?, ?)
                """,
                (
                    str(uuid4()),
                    job_id,
                    utc_now_text(),
                    json.dumps({"reference_count": len(credential_references)}),
                ),
            )
        return self.get(job_id)

    def replace_retry_web_translation_options(
        self,
        job_id: str,
        options: Mapping[str, Any],
    ) -> JobRecord:
        """Refresh resumable Web transport settings without rebinding the ledger."""
        if not options:
            raise ValueError("Retry Web translation options cannot be empty")
        with self.database.write_transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise LookupError(f"Unknown job: {job_id}")
            state = JobState(row["state"])
            if state not in {
                JobState.FAILED,
                JobState.INTERRUPTED,
                JobState.CANCELLED,
            }:
                raise ValueError("Web options can only be refreshed for a retryable job")
            if row["kind"] != JobKind.TRANSLATE.value or row["provider_id"] != "gemini-web":
                raise ValueError("Only Gemini Web translation jobs accept Web retry options")
            config = json.loads(row["config_json"])
            config.update(dict(options))
            assert_no_secret_fields(config)
            connection.execute(
                "UPDATE jobs SET config_json = ? WHERE id = ? AND state = ?",
                (
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    job_id,
                    state.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
                VALUES (?, ?, 'web_transport_refreshed', ?, ?)
                """,
                (
                    str(uuid4()),
                    job_id,
                    utc_now_text(),
                    json.dumps({"updated_fields": sorted(options)}),
                ),
            )
        return self.get(job_id)

    def replace_retry_translation_policy(
        self,
        job_id: str,
        policy: Mapping[str, Any],
    ) -> JobRecord:
        """Upgrade resumable translation QA policy without discarding its ledger."""

        if not policy:
            raise ValueError("Retry translation policy cannot be empty")
        with self.database.write_transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise LookupError(f"Unknown job: {job_id}")
            state = JobState(row["state"])
            if state not in {
                JobState.FAILED,
                JobState.INTERRUPTED,
                JobState.CANCELLED,
            }:
                raise ValueError("Translation policy can only be refreshed for a retryable job")
            if row["kind"] != JobKind.TRANSLATE.value:
                raise ValueError("Only translation jobs accept a refreshed QA policy")
            config = json.loads(row["config_json"])
            config.update(dict(policy))
            if row["provider_id"] == "gemini-web":
                config["headed"] = False
            assert_no_secret_fields(config)
            connection.execute(
                "UPDATE jobs SET config_json = ? WHERE id = ? AND state = ?",
                (
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    job_id,
                    state.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
                VALUES (?, ?, 'translation_policy_refreshed', ?, ?)
                """,
                (
                    str(uuid4()),
                    job_id,
                    utc_now_text(),
                    json.dumps({"updated_fields": sorted(policy)}),
                ),
            )
        return self.get(job_id)

    def transition(
        self,
        job_id: str,
        target: JobState,
        *,
        error_code: str | None = None,
        error_payload: dict[str, Any] | None = None,
    ) -> JobRecord:
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise LookupError(f"Unknown job: {job_id}")
            current = JobState(row["state"])
            self._transition_in_transaction(
                connection,
                row=row,
                current=current,
                target=target,
                occurred_at=now,
                state_event_id=str(uuid4()),
                error_code=error_code,
                error_payload=error_payload,
            )
        return self.get(job_id)

    def request_control(
        self,
        job_id: str,
        *,
        action: str,
        target: JobState,
    ) -> tuple[str, JobRecord]:
        """Commit a control intent before it is delivered to a worker.

        The `control_requested` event is the durable outbox item. Its event ID
        is sent as the command ID and must be echoed by the worker acknowledgement.
        """
        command_id = str(uuid4())
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise LookupError(f"Unknown job: {job_id}")
            current = JobState(row["state"])
            self._transition_in_transaction(
                connection,
                row=row,
                current=current,
                target=target,
                occurred_at=now,
                state_event_id=command_id,
                state_event_kind="control_requested",
                state_event_payload={"action": action, "command_id": command_id},
                error_code=None,
                error_payload=None,
            )
        return command_id, self.get(job_id)

    def append_worker_event(
        self,
        *,
        event_id: str,
        job_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        occurred_at: str | None = None,
        transitions: Mapping[JobState, JobState] | None = None,
        error_code: str | None = None,
        error_payload: dict[str, Any] | None = None,
        acknowledges_command_id: str | None = None,
        expected_control_action: str | None = None,
    ) -> tuple[int, JobRecord]:
        """Persist a worker acknowledgement and its state change atomically.

        Duplicate worker events are a no-op, including the transition. This is
        the durable boundary used when a worker retries delivery after a manager
        restart.
        """
        timestamp = occurred_at or utc_now_text()
        encoded = json.dumps(
            redact_secret_fields(payload or {}), ensure_ascii=False, sort_keys=True
        )
        with self.database.write_transaction() as connection:
            if acknowledges_command_id is not None:
                command_row = connection.execute(
                    """
                    SELECT kind, payload_json FROM job_events
                    WHERE event_id = ? AND job_id = ?
                    """,
                    (acknowledges_command_id, job_id),
                ).fetchone()
                if command_row is None or command_row["kind"] != "control_requested":
                    raise RuntimeError("Worker acknowledgement has no matching control request")
                command_payload = json.loads(command_row["payload_json"])
                if (
                    expected_control_action is not None
                    and command_payload.get("action") != expected_control_action
                ):
                    raise RuntimeError("Worker acknowledgement action does not match request")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, job_id, kind, timestamp, encoded),
            )
            event_row = connection.execute(
                "SELECT sequence, job_id FROM job_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if event_row is None:
                raise RuntimeError("Worker event was not persisted")
            if event_row["job_id"] != job_id:
                raise RuntimeError("Worker event id belongs to another job")

            job_row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job_row is None:
                raise LookupError(f"Unknown job: {job_id}")
            if inserted.rowcount == 1 and transitions:
                current = JobState(job_row["state"])
                target = transitions.get(current)
                if target is not None:
                    self._transition_in_transaction(
                        connection,
                        row=job_row,
                        current=current,
                        target=target,
                        occurred_at=timestamp,
                        state_event_id=f"{event_id}:state",
                        error_code=error_code,
                        error_payload=error_payload,
                    )

        return int(event_row["sequence"]), self.get(job_id)

    def event_exists(self, event_id: str, *, job_id: str | None = None) -> bool:
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT job_id FROM job_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is not None and job_id is not None and row["job_id"] != job_id:
            raise RuntimeError("Worker event id belongs to another job")
        return row is not None

    def append_event(
        self,
        *,
        event_id: str,
        job_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> int:
        timestamp = occurred_at or utc_now_text()
        encoded = json.dumps(
            redact_secret_fields(payload or {}), ensure_ascii=False, sort_keys=True
        )
        with self.database.write_transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, job_id, kind, timestamp, encoded),
            )
            row = connection.execute(
                "SELECT sequence FROM job_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Event was not persisted")
        return int(row["sequence"])

    def append_progress_event(
        self,
        *,
        event_id: str,
        job_id: str,
        payload: dict[str, Any],
        occurred_at: str | None = None,
    ) -> int:
        """Persist high-frequency telemetry without loading the job configuration."""
        timestamp = occurred_at or utc_now_text()
        encoded = json.dumps(
            redact_secret_fields(payload), ensure_ascii=False, sort_keys=True
        )
        with self.database.write_transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO job_events(
                    event_id, job_id, kind, occurred_at, payload_json
                ) VALUES (?, ?, 'progress', ?, ?)
                """,
                (event_id, job_id, timestamp, encoded),
            )
            row = connection.execute(
                "SELECT sequence, job_id FROM job_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Progress event was not persisted")
            if row["job_id"] != job_id:
                raise RuntimeError("Progress event id belongs to another job")
        return int(row["sequence"])

    def list_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        kind: str | None = None,
    ) -> tuple[JobEvent, ...]:
        if kind is not None and not kind.strip():
            raise ValueError("Event kind filter must be non-empty")
        with self.database.connect(read_only=True) as connection:
            if kind is None:
                rows = connection.execute(
                    """
                    SELECT sequence, event_id, job_id, kind, occurred_at, payload_json
                    FROM job_events
                    WHERE job_id = ? AND sequence > ?
                    ORDER BY sequence
                    """,
                    (job_id, after_sequence),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT sequence, event_id, job_id, kind, occurred_at, payload_json
                    FROM job_events
                    WHERE job_id = ? AND sequence > ? AND kind = ?
                    ORDER BY sequence
                    """,
                    (job_id, after_sequence, kind),
                ).fetchall()
        return tuple(
            JobEvent(
                protocol_version=1,
                sequence=int(row["sequence"]),
                event_id=row["event_id"],
                job_id=row["job_id"],
                kind=row["kind"],
                occurred_at=row["occurred_at"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        )

    def list_recent(self, *, limit: int = 100) -> tuple[JobRecord, ...]:
        if limit <= 0:
            raise ValueError("Job list limit must be positive")
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def list_project_jobs(
        self,
        *,
        project_id: str,
        kind: JobKind | None = None,
        input_fingerprint: str | None = None,
    ) -> tuple[JobRecord, ...]:
        """Return every matching project job without a global-history cutoff."""

        if not project_id.strip():
            raise ValueError("Project id is required")
        clauses = ["project_id = ?"]
        parameters: list[object] = [project_id]
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind.value)
        if input_fingerprint is not None:
            if not input_fingerprint.strip():
                raise ValueError("Input fingerprint must be non-empty")
            clauses.append("input_fingerprint = ?")
            parameters.append(input_fingerprint)
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def list_recent_activity(
        self, *, limit: int = 100
    ) -> tuple[
        tuple[JobRecord, dict[str, Any] | None, dict[str, Any] | None], ...
    ]:
        if limit <= 0:
            raise ValueError("Job list limit must be positive")
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                WITH recent AS (
                    SELECT
                        id, kind, state, project_id, input_revision, provider_id,
                        provider_version, model_id, input_fingerprint,
                        settings_fingerprint,
                        CASE
                            WHEN purpose <> ''
                            THEN json_object(
                                'purpose', purpose,
                                'parent_tts_job_id', json_extract(
                                    config_json, '$.parent_tts_job_id'
                                ),
                                'timing_repair_cycle', json_extract(
                                    config_json, '$.timing_repair_cycle'
                                ),
                                'max_timing_repair_cycles', json_extract(
                                    config_json, '$.max_timing_repair_cycles'
                                ),
                                'timing_repair_job_id', json_extract(
                                    config_json, '$.timing_repair_job_id'
                                )
                            )
                            ELSE '{}'
                        END AS config_json,
                        created_at, started_at, finished_at, error_code, error_json
                    FROM jobs
                    ORDER BY created_at DESC, id
                    LIMIT ?
                )
                SELECT
                    recent.*,
                    (
                        SELECT payload_json
                        FROM job_events AS progress
                        WHERE progress.job_id = recent.id AND progress.kind = 'progress'
                        ORDER BY progress.sequence DESC
                        LIMIT 1
                    ) AS progress_json,
                    (
                        SELECT payload_json
                        FROM job_events AS succeeded
                        WHERE succeeded.job_id = recent.id AND succeeded.kind = 'succeeded'
                        ORDER BY succeeded.sequence DESC
                        LIMIT 1
                    ) AS result_json
                FROM recent
                ORDER BY created_at DESC, id
                """,
                (limit,),
            ).fetchall()
        activity = []
        for row in rows:
            compact_row = dict(row)
            compact_config = json.loads(compact_row["config_json"])
            compact_row["config_json"] = json.dumps(
                {key: value for key, value in compact_config.items() if value is not None}
            )
            activity.append(
                (
                    self._record_from_row(compact_row),
                    json.loads(row["progress_json"]) if row["progress_json"] else None,
                    json.loads(row["result_json"]) if row["result_json"] else None,
                )
            )
        return tuple(activity)

    def latest_project_activity(
        self, project_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Return one lightweight active-or-latest job per project.

        This intentionally avoids decoding the full provider config, which can
        contain thousands of cue records for translation and TTS jobs.
        """

        ids = list(
            dict.fromkeys(
                value.strip()
                for value in project_ids
                if isinstance(value, str) and value.strip()
            )
        )
        if not ids:
            return {}
        placeholders = ", ".join("?" for _value in ids)
        active_states = (
            JobState.QUEUED.value,
            JobState.START_REQUESTED.value,
            JobState.RUNNING.value,
            JobState.PAUSE_REQUESTED.value,
            JobState.PAUSED.value,
            JobState.RESUME_REQUESTED.value,
            JobState.CANCEL_REQUESTED.value,
        )
        active_placeholders = ", ".join("?" for _value in active_states)
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT jobs.id, jobs.project_id, jobs.kind, jobs.state,
                           jobs.input_revision, jobs.created_at, jobs.finished_at,
                           jobs.error_code, jobs.error_json,
                           projects.head_revision AS project_head_revision,
                           ROW_NUMBER() OVER (
                               PARTITION BY jobs.project_id
                               ORDER BY CASE WHEN jobs.state IN ({active_placeholders})
                                             THEN 0 ELSE 1 END,
                                        CASE
                                            WHEN jobs.state IN ({active_placeholders}) THEN 0
                                            WHEN jobs.input_revision = projects.head_revision THEN 0
                                            ELSE 1
                                        END,
                                        CASE WHEN jobs.state IN ({active_placeholders})
                                             THEN jobs.created_at
                                             ELSE COALESCE(jobs.finished_at, jobs.created_at)
                                        END DESC,
                                        jobs.created_at DESC, jobs.id DESC
                           ) AS rank
                    FROM jobs
                    LEFT JOIN projects ON projects.id = jobs.project_id
                    WHERE jobs.project_id IN ({placeholders})
                )
                SELECT ranked.*,
                       (
                           SELECT payload_json FROM job_events AS progress
                           WHERE progress.job_id = ranked.id
                             AND progress.kind = 'progress'
                           ORDER BY progress.sequence DESC LIMIT 1
                       ) AS progress_json
                FROM ranked WHERE rank = 1
                """,
                (*active_states, *active_states, *active_states, *ids),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                progress = json.loads(str(row["progress_json"] or "{}"))
            except json.JSONDecodeError:
                progress = {}
            try:
                error_payload = json.loads(str(row["error_json"] or "{}"))
            except json.JSONDecodeError:
                error_payload = {}
            result[str(row["project_id"])] = {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "state": str(row["state"]),
                "input_revision": int(row["input_revision"]),
                "project_head_revision": int(row["project_head_revision"] or 0),
                "created_at": str(row["created_at"]),
                "finished_at": str(row["finished_at"] or ""),
                "error_code": str(row["error_code"] or ""),
                "error_payload": error_payload,
                "progress": progress,
            }
        return result

    def terminal_ids(
        self,
        *,
        project_id: str,
        kinds: tuple[JobKind, ...] | None = None,
    ) -> tuple[str, ...]:
        if not project_id.strip():
            raise ValueError("Project id is required")
        states = tuple(state.value for state in TERMINAL_JOB_STATES)
        placeholders = ", ".join("?" for _value in states)
        parameters: list[object] = [project_id, *states]
        kind_filter = ""
        if kinds:
            kind_values = tuple(kind.value for kind in kinds)
            kind_filter = f" AND kind IN ({', '.join('?' for _value in kind_values)})"
            parameters.extend(kind_values)
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM jobs
                WHERE project_id = ? AND state IN ({placeholders}){kind_filter}
                ORDER BY created_at, id
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    def project_ids(
        self,
        *,
        project_id: str,
        kinds: tuple[JobKind, ...] | None = None,
    ) -> tuple[str, ...]:
        if not project_id.strip():
            raise ValueError("Project id is required")
        parameters: list[object] = [project_id]
        kind_filter = ""
        if kinds:
            kind_values = tuple(kind.value for kind in kinds)
            kind_filter = f" AND kind IN ({', '.join('?' for _value in kind_values)})"
            parameters.extend(kind_values)
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM jobs
                WHERE project_id = ?{kind_filter}
                ORDER BY created_at, id
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    def active_id(
        self,
        *,
        project_id: str,
        kind: JobKind,
        exclude_job_id: str = "",
    ) -> str:
        if not project_id.strip():
            raise ValueError("Project id is required")
        active_states = tuple(
            state.value
            for state in (
                JobState.QUEUED,
                JobState.START_REQUESTED,
                JobState.RUNNING,
                JobState.PAUSE_REQUESTED,
                JobState.PAUSED,
                JobState.RESUME_REQUESTED,
                JobState.CANCEL_REQUESTED,
            )
        )
        placeholders = ", ".join("?" for _value in active_states)
        exclusion = " AND id <> ?" if exclude_job_id else ""
        parameters: tuple[object, ...] = (project_id, kind.value, *active_states)
        if exclude_job_id:
            parameters = (*parameters, exclude_job_id)
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                f"""
                SELECT id FROM jobs
                WHERE project_id = ? AND kind = ? AND state IN ({placeholders})
                {exclusion}
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                parameters,
            ).fetchone()
        return str(row["id"]) if row is not None else ""

    def delete_terminal(self, job_id: str) -> None:
        if not job_id.strip():
            raise ValueError("Job id is required")
        states = tuple(state.value for state in TERMINAL_JOB_STATES)
        placeholders = ", ".join("?" for _value in states)
        with self.database.write_transaction() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"Unknown job: {job_id}")
            if row["state"] not in states:
                raise ValueError("Chỉ có thể xóa lịch sử của tác vụ đã kết thúc.")
            deleted = connection.execute(
                f"DELETE FROM jobs WHERE id = ? AND state IN ({placeholders})",
                (job_id, *states),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("Job history changed during deletion")

    def latest_event(self, job_id: str, *, kind: str) -> JobEvent | None:
        if not job_id.strip() or not kind.strip():
            raise ValueError("Job id and event kind are required")
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT sequence, event_id, job_id, kind, occurred_at, payload_json
                FROM job_events
                WHERE job_id = ? AND kind = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (job_id, kind),
            ).fetchone()
        if row is None:
            return None
        return JobEvent(
            protocol_version=1,
            sequence=int(row["sequence"]),
            event_id=row["event_id"],
            job_id=row["job_id"],
            kind=row["kind"],
            occurred_at=row["occurred_at"],
            payload=json.loads(row["payload_json"]),
        )

    def reconcile_interrupted(self) -> int:
        interrupted_from = (
            JobState.QUEUED.value,
            JobState.START_REQUESTED.value,
            JobState.RUNNING.value,
            JobState.PAUSE_REQUESTED.value,
            JobState.PAUSED.value,
            JobState.RESUME_REQUESTED.value,
            JobState.CANCEL_REQUESTED.value,
        )
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            placeholders = ", ".join("?" for _state in interrupted_from)
            rows = connection.execute(
                f"SELECT id, state FROM jobs WHERE state IN ({placeholders})",
                interrupted_from,
            ).fetchall()
            for row in rows:
                control = connection.execute(
                    """
                    SELECT event_id, payload_json FROM job_events
                    WHERE job_id = ? AND kind = 'control_requested'
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (row["id"],),
                ).fetchone()
                connection.execute(
                    "UPDATE jobs SET state = ?, finished_at = NULL WHERE id = ? AND state = ?",
                    (JobState.INTERRUPTED.value, row["id"], row["state"]),
                )
                connection.execute(
                    """
                    INSERT INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
                    VALUES (?, ?, 'recovered_interrupted', ?, ?)
                    """,
                    (
                        str(uuid4()),
                        row["id"],
                        now,
                        json.dumps({"from": row["state"]}, sort_keys=True),
                    ),
                )
                if control is not None and row["state"].endswith("_requested"):
                    control_payload = json.loads(control["payload_json"])
                    connection.execute(
                        """
                        INSERT INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
                        VALUES (?, ?, 'control_abandoned', ?, ?)
                        """,
                        (
                            str(uuid4()),
                            row["id"],
                            now,
                            json.dumps(
                                {
                                    "command_id": control["event_id"],
                                    "action": control_payload.get("action"),
                                    "reason": "worker_process_lost_on_restart",
                                },
                                sort_keys=True,
                            ),
                        ),
                    )
        return len(rows)

    @staticmethod
    def _transition_in_transaction(
        connection: sqlite3.Connection,
        *,
        row: Any,
        current: JobState,
        target: JobState,
        occurred_at: str,
        state_event_id: str,
        state_event_kind: str = "state_changed",
        state_event_payload: dict[str, Any] | None = None,
        error_code: str | None,
        error_payload: dict[str, Any] | None,
    ) -> None:
        assert_job_transition(current, target)
        if error_code is not None:
            assert_safe_error_code(error_code)
        started_at = occurred_at if target is JobState.RUNNING else row["started_at"]
        finished_at = occurred_at if target in TERMINAL_JOB_STATES else None
        encoded_error = json.dumps(
            redact_secret_fields(error_payload or {}), ensure_ascii=False, sort_keys=True
        )
        cursor = connection.execute(
            """
            UPDATE jobs
            SET state = ?, started_at = ?, finished_at = ?, error_code = ?, error_json = ?
            WHERE id = ? AND state = ?
            """,
            (
                target.value,
                started_at,
                finished_at,
                error_code,
                encoded_error,
                row["id"],
                current.value,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Job state changed concurrently")
        connection.execute(
            """
            INSERT INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                state_event_id,
                row["id"],
                state_event_kind,
                occurred_at,
                json.dumps(
                    {
                        "from": current.value,
                        "to": target.value,
                        **(state_event_payload or {}),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )

    @staticmethod
    def _record_from_row(row: Any) -> JobRecord:
        spec = JobSpec(
            kind=JobKind(row["kind"]),
            project_id=row["project_id"],
            input_revision=int(row["input_revision"]),
            provider_id=row["provider_id"],
            provider_version=row["provider_version"],
            model_id=row["model_id"],
            input_fingerprint=row["input_fingerprint"],
            settings_fingerprint=row["settings_fingerprint"],
            provider_config=json.loads(row["config_json"]),
        )
        return JobRecord(
            id=row["id"],
            spec=spec,
            state=JobState(row["state"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_code=row["error_code"],
            error_payload=json.loads(row["error_json"]),
        )
