import json
import sqlite3
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import ducai_studio.infrastructure.checkpoint_repository as checkpoint_module
import ducai_studio.infrastructure.database as database_module
from ducai_studio.domain.artifacts import ArtifactState
from ducai_studio.domain.jobs import JobKind, JobSpec, JobState
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    Overlay,
    OverlayKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
    TranslationPreferences,
    VoiceConformState,
)
from ducai_studio.domain.timebase import SpeedRatio, TimeRange
from ducai_studio.domain.voice_conform import (
    VoiceConformCueInput,
    VoiceConformSettings,
    build_voice_conform_plan,
)
from ducai_studio.infrastructure.checkpoint_repository import CheckpointRepository
from ducai_studio.infrastructure.database import SCHEMA_SQL, Database
from ducai_studio.infrastructure.job_repository import JobRepository
from ducai_studio.infrastructure.multi_video_repository import MultiVideoRepository
from ducai_studio.infrastructure.project_repository import (
    ProjectRepository,
    decode_project_snapshot,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    result = Database(tmp_path / "state.db")
    result.initialize(applied_at="2026-08-24T00:00:00Z")
    return result


def make_spec(**overrides) -> JobSpec:
    values = {
        "kind": JobKind.OCR,
        "project_id": "project-1",
        "input_revision": 1,
        "provider_id": "paddle-fast",
        "provider_version": "3.2.0",
        "model_id": "PP-OCRv5-server",
        "input_fingerprint": "fingerprint-1",
        "settings_fingerprint": "settings-1",
        "provider_config": {"mode": "fast"},
    }
    values.update(overrides)
    return JobSpec(**values)


def test_job_state_and_idempotent_event_survive_reopen(database: Database) -> None:
    first = JobRepository(database)
    job = first.create(make_spec(), job_id="job-1")
    first.transition(job.id, JobState.START_REQUESTED)
    first.transition(job.id, JobState.RUNNING)
    sequence = first.append_event(
        event_id="worker-progress-1",
        job_id=job.id,
        kind="progress",
        payload={"completed": 10},
    )
    assert (
        first.append_event(
            event_id="worker-progress-1",
            job_id=job.id,
            kind="progress",
            payload={"completed": 99},
        )
        == sequence
    )

    reopened = JobRepository(Database(database.path))
    assert reopened.get(job.id).state is JobState.RUNNING
    assert reopened.list_events(job.id, after_sequence=sequence - 1)[0].payload == {"completed": 10}
    assert reopened.reconcile_interrupted() == 1
    assert reopened.get(job.id).state is JobState.INTERRUPTED
    reopened.transition(job.id, JobState.QUEUED)
    assert reopened.get(job.id).state is JobState.QUEUED


def test_queued_job_is_reconciled_after_restart_instead_of_blocking_a_new_stage(
    database: Database,
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="queued-before-worker-start")

    assert job.state is JobState.QUEUED
    assert jobs.reconcile_interrupted() == 1
    assert jobs.get(job.id).state is JobState.INTERRUPTED


def test_repeated_job_reads_reuse_spec_but_refresh_lifecycle(database: Database) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec())
    first = jobs.get(job.id)

    assert jobs.get(job.id).spec is first.spec
    requested = jobs.transition(job.id, JobState.START_REQUESTED)
    assert requested.spec is first.spec
    assert requested.state is JobState.START_REQUESTED
    assert first.state is JobState.QUEUED
    running = jobs.transition(job.id, JobState.RUNNING)
    assert running.spec is first.spec
    assert running.started_at is not None
    failed = jobs.transition(
        job.id, JobState.FAILED, error_code="provider_timeout", error_payload={"attempt": 2}
    )
    assert failed.spec == first.spec
    assert failed.state is JobState.FAILED
    assert failed.finished_at is not None
    assert failed.error_code == "provider_timeout"
    assert failed.error_payload == {"attempt": 2}
    # Finished jobs need not retain a potentially huge input in the repository.
    assert jobs.get(job.id).spec is not failed.spec


@pytest.mark.parametrize(
    ("column", "value", "attribute", "expected"),
    [
        ("kind", "translate", "kind", JobKind.TRANSLATE),
        ("project_id", "project-2", "project_id", "project-2"),
        ("input_revision", 2, "input_revision", 2),
        ("provider_id", "openai-compatible", "provider_id", "openai-compatible"),
        ("provider_version", "v2", "provider_version", "v2"),
        ("model_id", "other-model", "model_id", "other-model"),
        ("input_fingerprint", "new-input", "input_fingerprint", "new-input"),
        ("settings_fingerprint", "new-settings", "settings_fingerprint", "new-settings"),
        ("config_json", '{"mode":"quality"}', "provider_config", {"mode": "quality"}),
    ],
)
def test_job_spec_reuse_invalidates_for_every_persisted_input(
    database: Database, column: str, value: object, attribute: str, expected: object
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec())
    before = jobs.get(job.id)
    with database.write_transaction() as connection:
        connection.execute(f"UPDATE jobs SET {column} = ? WHERE id = ?", (value, job.id))

    after = jobs.get(job.id)

    assert after.spec is not before.spec
    assert getattr(after.spec, attribute) == expected
    assert jobs.get(job.id).spec is after.spec


def test_job_spec_reuse_observes_retry_credentials_without_new_fingerprint(
    database: Database,
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(provider_config={"credential_references": ["old-ref"]}))
    before = jobs.get(job.id)
    jobs.transition(job.id, JobState.START_REQUESTED)
    jobs.transition(job.id, JobState.FAILED)
    after = jobs.replace_retry_credential_references(job.id, ("new-ref",))

    assert after.spec.provider_config["credential_references"] == ("new-ref",)
    assert before.spec.provider_config["credential_references"] == ("old-ref",)
    assert after.spec.resume_fingerprint == before.spec.resume_fingerprint
    assert jobs.get(job.id).spec == after.spec


def test_job_spec_reuse_revalidates_changed_input_and_cannot_return_deleted_job(
    database: Database,
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec())
    before = jobs.get(job.id)
    for invalid in ('{"api_key":"unsafe"}', "not-json"):
        with database.write_transaction() as connection:
            connection.execute("UPDATE jobs SET config_json = ? WHERE id = ?", (invalid, job.id))
        with pytest.raises(ValueError):
            jobs.get(job.id)
    with database.write_transaction() as connection:
        connection.execute(
            "UPDATE jobs SET config_json = ? WHERE id = ?", (json.dumps({"mode": "fast"}), job.id)
        )
    assert jobs.get(job.id).spec == before.spec
    with database.write_transaction() as connection:
        connection.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
    with pytest.raises(LookupError, match="Unknown job"):
        jobs.get(job.id)


def test_job_spec_reuse_is_bounded_and_compact_activity_keeps_full_input(
    database: Database,
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(provider_config={"source_cues": [{"id": 1, "text": "Original"}]}))
    before = jobs.get(job.id)
    compact = jobs.list_recent_activity()[0][0]
    assert "source_cues" not in compact.spec.provider_config
    assert jobs.get(job.id).spec is before.spec
    assert jobs.get(job.id).spec.provider_config == before.spec.provider_config
    other = jobs.create(make_spec(), job_id="other-job")
    jobs.get(other.id)
    reread = jobs.get(job.id)
    assert reread.spec is not before.spec
    assert reread.spec == before.spec


def test_compact_progress_events_and_recent_activity_do_not_return_full_config(
    database: Database,
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(
        make_spec(
            kind=JobKind.TTS,
            provider_config={
                "purpose": "production",
                "cues": [{"id": f"cue-{index}", "text": "Dài" * 100} for index in range(50)],
                "credential_references": ["secret-reference"],
            },
        ),
        job_id="compact-activity",
    )
    first = jobs.append_progress_event(
        event_id="progress-compact",
        job_id=job.id,
        payload={"phase": "final_master_apply", "phase_current": 5, "phase_total": 10},
    )
    duplicate = jobs.append_progress_event(
        event_id="progress-compact",
        job_id=job.id,
        payload={"phase_current": 9},
    )
    jobs.append_event(
        event_id="succeeded-compact",
        job_id=job.id,
        kind="succeeded",
        payload={"status": "PASS", "purpose": "production"},
    )

    record, progress, result = jobs.list_recent_activity(limit=10)[0]

    assert duplicate == first
    assert record.spec.provider_config == {"purpose": "production"}
    assert progress == {
        "phase": "final_master_apply",
        "phase_current": 5,
        "phase_total": 10,
    }
    assert result == {"purpose": "production", "status": "PASS"}


def test_compact_translation_activity_preserves_style_identity(database: Database) -> None:
    jobs = JobRepository(database)
    config = {"translation_style_preset": "xianxia", "translation_custom_rules": "Giữ nghĩa.",
              "base_url": "http://127.0.0.1:8045/v1", "credential_references": ["local"],
              "source_cues": [{"index": 1, "text": "你好"}]}
    jobs.create(make_spec(kind=JobKind.TRANSLATE, provider_config=config))
    record = jobs.list_recent_activity()[0][0]
    assert dict(record.spec.provider_config) == {k: v if k != "credential_references" else tuple(v)
                                              for k, v in config.items() if k != "source_cues"}


def test_latest_project_activity_prefers_current_revision_then_latest_finish(
    database: Database, tmp_path: Path
) -> None:
    ProjectRepository(database).save(
        project_path=tmp_path / "Project.ducai",
        snapshot=ProjectSnapshot("project-1", "Project", 2),
    )
    jobs = JobRepository(database)
    jobs.create(make_spec(input_revision=2), job_id="current-long")
    jobs.create(make_spec(input_revision=2), job_id="current-short")
    jobs.create(make_spec(input_revision=1), job_id="stale-last")
    with database.write_transaction() as connection:
        connection.execute(
            """
            UPDATE jobs SET state = 'failed', created_at = ?, finished_at = ?,
                            error_code = 'failed', error_json = '{}'
            WHERE id = ?
            """,
            ("2026-08-29T10:00:00Z", "2026-08-29T12:00:00Z", "current-long"),
        )
        connection.execute(
            """
            UPDATE jobs SET state = 'failed', created_at = ?, finished_at = ?,
                            error_code = 'failed', error_json = '{}'
            WHERE id = ?
            """,
            ("2026-08-29T11:00:00Z", "2026-08-29T11:30:00Z", "current-short"),
        )
        connection.execute(
            """
            UPDATE jobs SET state = 'failed', created_at = ?, finished_at = ?,
                            error_code = 'failed', error_json = '{}'
            WHERE id = ?
            """,
            ("2026-08-29T09:00:00Z", "2026-08-29T13:00:00Z", "stale-last"),
        )

    activity = jobs.latest_project_activity(["project-1"])["project-1"]

    assert activity["id"] == "current-long"
    assert activity["input_revision"] == 2


def test_project_summaries_can_list_the_complete_catalog(
    database: Database, tmp_path: Path
) -> None:
    projects = ProjectRepository(database)
    for index in range(3):
        projects.save(
            project_path=tmp_path / f"Project-{index}.ducai",
            snapshot=ProjectSnapshot(f"project-{index}", f"Project {index}", 1),
        )

    assert len(projects.list_summaries(limit=2)) == 2
    assert len(projects.list_summaries(limit=None)) == 3


def test_job_event_kind_filter_returns_only_unseen_translation_deltas(
    database: Database,
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="translation-events")
    progress_sequence = jobs.append_event(
        event_id="progress",
        job_id=job.id,
        kind="progress",
        payload={"current": 1},
    )
    first_delta = jobs.append_event(
        event_id="delta-1",
        job_id=job.id,
        kind="translation_delta",
        payload={"values": [{"id": "cue-1", "index": 1, "text": "Một."}]},
    )
    jobs.append_event(
        event_id="delta-2",
        job_id=job.id,
        kind="translation_delta",
        payload={"values": [{"id": "cue-2", "index": 2, "text": "Hai."}]},
    )

    events = jobs.list_events(
        job.id,
        after_sequence=progress_sequence,
        kind="translation_delta",
    )
    unseen = jobs.list_events(
        job.id,
        after_sequence=first_delta,
        kind="translation_delta",
    )

    assert [event.event_id for event in events] == ["delta-1", "delta-2"]
    assert [event.event_id for event in unseen] == ["delta-2"]


def test_worker_event_secrets_are_redacted_before_sqlite(database: Database) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="redacted-event")
    sequence = jobs.append_event(
        event_id="redacted-event-1",
        job_id=job.id,
        kind="provider_log",
        payload={
            "apiKey": "secret",
            "nested": {"oauth_token": "secret-2"},
            "message": "retry failed with api_key=secret-3 token=secret-4 secret=secret-5",
        },
    )
    event = jobs.list_events(job.id, after_sequence=sequence - 1)[0]
    assert event.payload == {
        "apiKey": "[REDACTED]",
        "nested": {"oauth_token": "[REDACTED]"},
        "message": ("retry failed with api_key=[REDACTED] token=[REDACTED] secret=[REDACTED]"),
    }


def test_nested_frozen_provider_config_round_trips_and_error_code_is_safe(
    database: Database,
) -> None:
    jobs = JobRepository(database)
    spec = make_spec(provider_config={"profiles": [{"credentialRef": "gemini-account-1"}]})
    created = jobs.create(spec, job_id="nested-provider-config")
    assert jobs.get(created.id).spec.provider_config["profiles"][0]["credentialRef"] == (
        "gemini-account-1"
    )
    with pytest.raises(ValueError, match="error code"):
        jobs.transition(
            created.id,
            JobState.CANCEL_REQUESTED,
            error_code="sk-AAAAAAAAAAAAAAAAAAAA12345678",
        )
    assert jobs.get(created.id).state is JobState.QUEUED


def test_worker_event_and_acknowledged_state_transition_are_atomic(database: Database) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="atomic-worker-event")
    jobs.transition(job.id, JobState.START_REQUESTED)
    jobs.transition(job.id, JobState.RUNNING)
    command_id, requested = jobs.request_control(
        job.id,
        action="pause",
        target=JobState.PAUSE_REQUESTED,
    )
    assert requested.state is JobState.PAUSE_REQUESTED
    with pytest.raises(RuntimeError, match="no matching control request"):
        jobs.append_worker_event(
            event_id="worker-paused-invalid",
            job_id=job.id,
            kind="paused",
            payload={"status": "paused", "command_id": "missing-command"},
            transitions={JobState.PAUSE_REQUESTED: JobState.PAUSED},
            acknowledges_command_id="missing-command",
            expected_control_action="pause",
        )
    assert jobs.get(job.id).state is JobState.PAUSE_REQUESTED
    assert not jobs.event_exists("worker-paused-invalid")

    sequence, paused = jobs.append_worker_event(
        event_id="worker-paused-1",
        job_id=job.id,
        kind="paused",
        payload={"status": "paused", "command_id": command_id},
        transitions={JobState.PAUSE_REQUESTED: JobState.PAUSED},
        acknowledges_command_id=command_id,
        expected_control_action="pause",
    )
    assert paused.state is JobState.PAUSED
    duplicate_sequence, duplicate = jobs.append_worker_event(
        event_id="worker-paused-1",
        job_id=job.id,
        kind="paused",
        payload={"status": "wrong-duplicate"},
        transitions={JobState.PAUSED: JobState.FAILED},
        acknowledges_command_id=command_id,
        expected_control_action="pause",
    )
    assert duplicate_sequence == sequence
    assert duplicate.state is JobState.PAUSED
    assert jobs.list_events(job.id, after_sequence=sequence - 1)[0].payload == {
        "status": "paused",
        "command_id": command_id,
    }
    assert jobs.reconcile_interrupted() == 1
    assert jobs.get(job.id).state is JobState.INTERRUPTED


def test_checkpoint_requires_managed_path_and_matching_fingerprint(
    database: Database, tmp_path: Path
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="job-checkpoint")
    root = tmp_path / "artifacts"
    root.mkdir()
    staging = root / "staging"
    staging.mkdir()
    artifact = staging / "ocr-state.partial"
    artifact.write_bytes(b'{"cursor":42}')
    checkpoints = CheckpointRepository(database, root)
    record = checkpoints.commit(
        job_id=job.id,
        stage_key="ocr",
        checkpoint_key="primary",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=42_000_000,
        payload={"rows": 12},
        artifact_path=artifact,
        artifact_state=ArtifactState.VALIDATED,
    )
    assert record.artifact_id
    resolved = checkpoints.resolve_artifact(record.artifact_id)
    assert Path(resolved.uri).read_bytes() == b'{"cursor":42}'
    assert resolved.state is ArtifactState.VALIDATED
    assert (
        checkpoints.load(
            job_id=job.id,
            stage_key="ocr",
            checkpoint_key="primary",
            resume_fingerprint=job.spec.resume_fingerprint,
        )
        == record
    )
    assert (
        checkpoints.load(
            job_id=job.id,
            stage_key="ocr",
            checkpoint_key="primary",
            resume_fingerprint="changed-execution",
        )
        is None
    )

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        checkpoints.commit(
            job_id=job.id,
            stage_key="ocr",
            checkpoint_key="outside",
            schema_version=1,
            resume_fingerprint=job.spec.resume_fingerprint,
            cursor_us=None,
            payload={},
            artifact_path=outside,
        )


def test_checkpoint_promotes_staging_artifact_and_detects_mutation(
    database: Database, tmp_path: Path
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="job-immutable-checkpoint")
    root = tmp_path / "artifacts"
    staging = root / "staging"
    staging.mkdir(parents=True)
    source = staging / "state.partial"
    source.write_bytes(b"immutable checkpoint")
    checkpoints = CheckpointRepository(database, root)

    checkpoints.commit(
        job_id=job.id,
        stage_key="ocr",
        checkpoint_key="primary",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=1,
        payload={},
        artifact_path=source,
    )
    assert not source.exists()
    with database.connect(read_only=True) as connection:
        row = connection.execute("SELECT uri FROM artifacts WHERE job_id = ?", (job.id,)).fetchone()
    promoted = Path(row["uri"])
    assert promoted.is_file()
    promoted.chmod(0o666)
    promoted.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="hash"):
        checkpoints.load(
            job_id=job.id,
            stage_key="ocr",
            checkpoint_key="primary",
            resume_fingerprint=job.spec.resume_fingerprint,
        )


def test_checkpoint_replacement_removes_only_superseded_technical_artifact(
    database: Database, tmp_path: Path
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="checkpoint-gc")
    checkpoints = CheckpointRepository(database, tmp_path / "artifacts")

    first_source = checkpoints.staging_root / "first.partial"
    first_source.write_bytes(b"first technical ledger")
    first = checkpoints.commit(
        job_id=job.id,
        stage_key="ocr",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=1,
        payload={"cursor": 1},
        artifact_path=first_source,
    )
    assert first.artifact_id is not None
    first_path = Path(checkpoints.resolve_artifact(first.artifact_id).uri)

    review_source = checkpoints.staging_root / "review.partial"
    review_source.write_bytes(b"validated review")
    review = checkpoints.commit(
        job_id=job.id,
        stage_key="ocr",
        checkpoint_key="review",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=1,
        payload={"status": "review"},
        artifact_path=review_source,
        artifact_state=ArtifactState.VALIDATED,
    )
    assert review.artifact_id is not None
    review_path = Path(checkpoints.resolve_artifact(review.artifact_id).uri)

    second_source = checkpoints.staging_root / "second.partial"
    second_source.write_bytes(b"second technical ledger")
    second = checkpoints.commit(
        job_id=job.id,
        stage_key="ocr",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=2,
        payload={"cursor": 2},
        artifact_path=second_source,
    )

    assert second.artifact_id is not None and second.artifact_id != first.artifact_id
    assert not first_path.exists()
    with database.connect(read_only=True) as connection:
        artifact_ids = {
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM artifacts WHERE job_id = ?", (job.id,)
            ).fetchall()
        }
    assert first.artifact_id not in artifact_ids
    assert second.artifact_id in artifact_ids
    assert review.artifact_id in artifact_ids
    assert review_path.is_file()


def test_checkpoint_capacity_error_explains_how_to_resume(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints = CheckpointRepository(database, tmp_path / "artifacts")
    monkeypatch.setattr(
        checkpoint_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=0),
    )

    with pytest.raises(OSError, match="Không đủ dung lượng.*tiếp tục từ cache/checkpoint"):
        checkpoints.ensure_worker_capacity(stage_label="lồng tiếng")


def test_checkpoint_worker_event_replay_is_idempotent_after_staging_cleanup(
    database: Database, tmp_path: Path
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="checkpoint-replay")
    root = tmp_path / "artifacts"
    staging = root / "staging"
    staging.mkdir(parents=True)
    source = staging / "state.partial"
    source.write_bytes(b"checkpoint snapshot")
    checkpoints = CheckpointRepository(database, root)
    arguments = {
        "job_id": job.id,
        "stage_key": "ocr",
        "checkpoint_key": "primary",
        "schema_version": 1,
        "resume_fingerprint": job.spec.resume_fingerprint,
        "cursor_us": 12,
        "payload": {"rows": 3},
        "artifact_path": source,
        "event_id": "checkpoint-event-1",
        "event_payload": {"cursor_us": 12},
    }

    first = checkpoints.commit(**arguments)
    assert not source.exists()
    replayed = checkpoints.commit(**arguments)
    assert replayed == first
    assert [event.kind for event in jobs.list_events(job.id)].count("checkpoint") == 1


def test_checkpoint_replay_opens_writable_connection_for_wal_recovery(
    database: Database, tmp_path: Path, monkeypatch
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="checkpoint-wal-recovery")
    checkpoints = CheckpointRepository(database, tmp_path / "artifacts")
    source = checkpoints.staging_root / "wal-recovery.partial"
    source.write_bytes(b"recover me")
    calls: list[bool] = []
    replay_probe_closed = False
    original_connect = database.connect

    def observe_connect(*, read_only: bool = False):
        nonlocal replay_probe_closed
        calls.append(read_only)
        connection = original_connect(read_only=read_only)
        if len(calls) != 1:
            return connection

        class ReplayProbe:
            def execute(self, *args, **kwargs):
                return connection.execute(*args, **kwargs)

            def close(self) -> None:
                nonlocal replay_probe_closed
                replay_probe_closed = True
                connection.close()

        return ReplayProbe()

    monkeypatch.setattr(database, "connect", observe_connect)

    checkpoints.commit(
        job_id=job.id,
        stage_key="translation",
        checkpoint_key="primary",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=1_000_000,
        payload={"rows": 1},
        artifact_path=source,
        event_id="checkpoint-wal-recovery-event",
    )

    assert calls[0] is False
    assert replay_probe_closed is True


def test_normal_connections_reuse_persistent_wal_mode_without_reapplying_pragma(
    database: Database, monkeypatch
) -> None:
    statements: list[str] = []
    original_connect = database_module.sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", traced_connect)

    with database.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    normalized = [statement.casefold().replace(" ", "") for statement in statements]
    assert normalized.count("pragmajournal_mode=wal") == 0


@pytest.mark.parametrize("crash_phase", ["copy_started", "promoted", "committed"])
def test_checkpoint_replay_recovers_each_hard_kill_boundary(
    database: Database, tmp_path: Path, crash_phase: str
) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id=f"checkpoint-crash-{crash_phase}")
    root = tmp_path / "artifacts"
    source = root / "staging" / "state.partial"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"checkpoint crash boundary")
    marker = tmp_path / f"{crash_phase}.marker"
    event_id = f"checkpoint-event-{crash_phase}"
    fixture = Path(__file__).parents[1] / "fixtures" / "checkpoint_crash_worker.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(fixture),
            "--database",
            str(database.path),
            "--artifacts",
            str(root),
            "--source",
            str(source),
            "--marker",
            str(marker),
            "--phase",
            crash_phase,
            "--job-id",
            job.id,
            "--fingerprint",
            job.spec.resume_fingerprint,
            "--event-id",
            event_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not marker.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    f"Crash fixture exited before {crash_phase}: {stdout}\n{stderr}"
                )
            time.sleep(0.02)
        assert marker.read_text(encoding="utf-8") == crash_phase
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    recovered = CheckpointRepository(database, root)
    record = recovered.commit(
        job_id=job.id,
        stage_key="ocr",
        checkpoint_key="primary",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=42_000_000,
        payload={"rows": 12},
        artifact_path=source,
        event_id=event_id,
        event_payload={"cursor_us": 42_000_000},
    )

    assert (
        recovered.load(
            job_id=job.id,
            stage_key="ocr",
            checkpoint_key="primary",
            resume_fingerprint=job.spec.resume_fingerprint,
        )
        == record
    )
    assert not source.exists()
    assert not list(root.rglob(".*.tmp"))
    with database.connect(read_only=True) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM job_checkpoints WHERE job_id = ?", (job.id,)
        ).fetchone()[0]
        artifacts = connection.execute(
            "SELECT uri, sha256 FROM artifacts WHERE job_id = ?", (job.id,)
        ).fetchall()
    assert event_count == 1
    assert checkpoint_count == 1
    assert len(artifacts) == 1
    promoted = Path(artifacts[0]["uri"])
    assert promoted.read_bytes() == b"checkpoint crash boundary"
    assert artifacts[0]["sha256"] == sha256(promoted.read_bytes()).hexdigest()


def test_retry_resets_attempt_start_time(database: Database) -> None:
    jobs = JobRepository(database)
    job = jobs.create(make_spec(), job_id="retry-job")
    jobs.transition(job.id, JobState.START_REQUESTED)
    first_started = jobs.transition(job.id, JobState.RUNNING).started_at
    jobs.transition(job.id, JobState.FAILED)
    jobs.transition(job.id, JobState.QUEUED)
    time.sleep(0.01)
    jobs.transition(job.id, JobState.START_REQUESTED)
    second_started = jobs.transition(job.id, JobState.RUNNING).started_at
    assert second_started != first_started


def test_project_revision_is_immutable(database: Database, tmp_path: Path) -> None:
    projects = ProjectRepository(database)
    snapshot = ProjectSnapshot("project-1", "Demo", 1)
    digest = projects.save(project_path=tmp_path / "Demo.ducai", snapshot=snapshot)
    assert len(digest) == 64
    assert projects.load_json(snapshot.id)["name"] == "Demo"

    changed_same_revision = ProjectSnapshot("project-1", "Changed", 1)
    with pytest.raises(RuntimeError, match="immutable"):
        projects.save(project_path=tmp_path / "Demo.ducai", snapshot=changed_same_revision)
    assert projects.load_json(snapshot.id)["name"] == "Demo"


def test_project_snapshot_round_trips_timeline_links_cues_and_overlays(
    database: Database, tmp_path: Path
) -> None:
    media = MediaAsset("media", "C:/Phim/movie.mp4", MediaKind.VIDEO, 10_000_000)
    source = TimeRange(0, 8_500_000)
    clip = TimelineClip(
        "clip",
        "V1",
        TimeRange(0, 10_000_000),
        source,
        "media",
        "link-av",
        SpeedRatio(17, 20),
        0.75,
    )
    snapshot = ProjectSnapshot(
        "project-complete",
        "Đạo gia",
        3,
        media=(media,),
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (clip,)),),
        source_cues=(
            SubtitleCue(
                "cue-1",
                1,
                TimeRange(1_000_000, 2_000_000),
                "你好",
                "zh",
                confidence=0.91,
                evidence_ids=("ocr-1",),
                visual_box=(0.2, 0.8, 0.6, 0.1),
                visual_box_source="ocr",
            ),
        ),
        overlays=(
            Overlay(
                "logo",
                OverlayKind.LOGO,
                TimeRange(0, 10_000_000),
                0.8,
                0.05,
                0.15,
                0.1,
                "media",
            ),
            Overlay(
                "source-blur",
                OverlayKind.BLUR,
                TimeRange(0, 10_000_000),
                0.04,
                0.82,
                0.92,
                0.15,
                tracking_mode="source_subtitle",
                padding_x=0.012,
                padding_y=0.008,
                concealment_mode="cover",
            ),
            Overlay(
                "overlay-ocr-watermark-1",
                OverlayKind.BLUR,
                TimeRange(0, 10_000_000),
                0.72,
                0.08,
                0.26,
                0.09,
                tracking_mode="ocr_watermark",
                padding_x=0.0,
                padding_y=0.0,
                concealment_mode="cover",
            ),
        ),
        translation_preferences=TranslationPreferences(
            "xianxia", "Tên đứng trước hậu tố thân mật."
        ),
    )
    projects = ProjectRepository(database)
    projects.save(project_path=tmp_path / "Đạo gia.ducai", snapshot=snapshot)

    assert projects.load(snapshot.id) == snapshot


def test_project_snapshot_round_trips_scene_text_metadata_and_legacy_defaults(
    database: Database, tmp_path: Path
) -> None:
    scene = SubtitleCue(
        "scene-1",
        1,
        TimeRange(500_000, 2_500_000),
        "海王兽",
        "zh",
        confidence=0.94,
        evidence_ids=("scene-frame-1", "scene-frame-2"),
        visual_box=(0.78, 0.24, 0.08, 0.31),
        visual_box_source="ocr",
        role="scene_text",
        orientation="vertical",
        style_hint="calligraphy",
        panel_box=(0.73, 0.18, 0.18, 0.43),
        background_color="#101010",
        background_style="ink_splash",
        review_state="accepted",
        scene_font_scale=0.85,
        text_font_size=23,
        subtitle_track_id="S2",
        background_padding_x=24,
        background_padding_y=12,
        fade_enabled=True,
        fade_duration_ms=450,
    )
    snapshot = ProjectSnapshot(
        "scene-project",
        "Scene",
        1,
        source_cues=(scene,),
        ocr_dialogue_region=(0.08, 0.74, 0.84, 0.22),
        ocr_exclusion_regions=((0.82, 0.02, 0.16, 0.08),),
    )
    projects = ProjectRepository(database)
    projects.save(project_path=tmp_path / "Scene.ducai", snapshot=snapshot)

    assert projects.load(snapshot.id).source_cues == (scene,)

    legacy = decode_project_snapshot(
        {
            "id": "legacy-cue-project",
            "name": "Legacy cue",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [
                {
                    "id": "legacy",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_000_000},
                    "text": "你好",
                    "language": "zh",
                }
            ],
            "translated_cues": [],
            "overlays": [],
            "schema_version": 1,
        }
    )
    assert legacy.source_cues[0].role == "dialogue"
    assert legacy.source_cues[0].orientation == "horizontal"
    assert legacy.source_cues[0].scene_font_scale == 1.0
    assert legacy.source_cues[0].text_font_size == 12
    assert legacy.source_cues[0].subtitle_track_id == ""
    assert legacy.source_cues[0].background_padding_x == 20
    assert legacy.source_cues[0].background_padding_y == 10
    assert legacy.source_cues[0].background_style == "solid"
    assert legacy.source_cues[0].fade_enabled is False
    assert legacy.source_cues[0].fade_duration_ms == 300
    assert legacy.source_cues[0].panel_box is None
    assert legacy.ocr_dialogue_region is None
    assert legacy.ocr_exclusion_regions == ()
    assert legacy.translation_preferences.saved_styles == ()
    assert legacy.translation_preferences.active_style_name == ""

    named_style = decode_project_snapshot(
        {
            "id": "named-style-project",
            "name": "Named style",
            "revision": 1,
            "media": [],
            "tracks": [],
            "source_cues": [],
            "translated_cues": [],
            "overlays": [],
            "translation_preferences": {
                "preset": "auto",
                "custom_rules": "",
                "saved_styles": {
                    "Bản cũ": {
                        "preset": "modern",
                        "custom_rules": "Dùng anh/em.",
                    }
                },
            },
        }
    )
    assert named_style.translation_preferences.saved_styles[0].name == "Bản cũ"
    assert named_style.translation_preferences.saved_styles[0].preset == "modern"


def test_project_decoder_splits_legacy_overlapping_scene_text_into_visible_lanes() -> None:
    def cue(
        cue_id: str,
        index: int,
        text: str,
        start_us: int,
        end_us: int,
        *,
        source: str = "ocr_review",
        track_id: str = "",
        language: str = "zh",
    ) -> dict[str, object]:
        return {
            "id": cue_id,
            "index": index,
            "range": {"start_us": start_us, "end_us": end_us},
            "text": text,
            "language": language,
            "source": source,
            "role": "scene_text",
            "subtitle_track_id": track_id,
        }

    automatic = [
        cue("scene-a", 1, "甲", 1_000_000, 2_000_000),
        cue("scene-b", 2, "乙", 1_000_000, 2_000_000),
        cue("scene-c", 3, "丙", 1_000_000, 2_000_000),
        cue("scene-d", 4, "丁", 1_000_000, 2_000_000),
        cue("scene-e", 5, "戊", 3_000_000, 4_000_000),
    ]
    manual = cue(
        "manual-scene",
        6,
        "Tên thêm tay",
        1_000_000,
        2_000_000,
        source="manual_scene_text",
        track_id="S3",
        language="vi",
    )
    translated = [
        {**item, "id": item["id"] + "-vi", "text": f"VI {item['text']}", "language": "vi"}
        for item in automatic
    ] + [dict(manual)]

    def clip(cue_id: str, cue_index: int, track_id: str) -> dict[str, object]:
        return {
            "id": f"clip-{cue_id}",
            "track_id": track_id,
            "timeline_range": {"start_us": 1_000_000, "end_us": 2_000_000},
            "cue_id": cue_id,
            "cue_index": cue_index,
        }

    decoded = decode_project_snapshot(
        {
            "id": "legacy-overlap-project",
            "name": "Legacy overlap",
            "revision": 7,
            "media": [],
            "tracks": [
                {
                    "id": "S2",
                    "kind": "subtitle",
                    "name": "Chữ trong cảnh",
                    "clips": [
                        clip(str(item["id"]), int(item["index"]), "S2")
                        for item in automatic[:4]
                    ],
                },
                {
                    "id": "S3",
                    "kind": "subtitle",
                    "name": "Chữ thêm tay",
                    "clips": [clip("manual-scene", 6, "S3")],
                },
            ],
            "source_cues": [*automatic, manual],
            "translated_cues": translated,
            "overlays": [],
            "schema_version": 1,
        }
    )

    source_lanes = {item.id: item.subtitle_track_id for item in decoded.source_cues}
    assert source_lanes == {
        "scene-a": "S2",
        "scene-b": "S4",
        "scene-c": "S5",
        "scene-d": "S6",
        "scene-e": "S2",
        "manual-scene": "S3",
    }
    translated_lanes = {
        item.index: item.subtitle_track_id for item in decoded.translated_cues
    }
    assert translated_lanes == {
        item.index: item.subtitle_track_id for item in decoded.source_cues
    }
    tracks = {track.id: track for track in decoded.tracks}
    assert set(tracks) == {"S2", "S3", "S4", "S5", "S6"}
    assert [clip.cue_id for clip in tracks["S2"].clips] == ["scene-a", "scene-e"]
    assert tracks["S3"].clips[0].cue_id == "manual-scene"
    assert tracks["S4"].clips[0].cue_id == "scene-b"
    assert tracks["S5"].clips[0].cue_id == "scene-c"
    assert tracks["S6"].clips[0].cue_id == "scene-d"


def test_project_decoder_preserves_valid_scene_text_on_s1_and_repairs_collision() -> None:
    def cue(
        cue_id: str,
        index: int,
        start_us: int,
        end_us: int,
        *,
        role: str,
        track_id: str,
    ) -> dict[str, object]:
        return {
            "id": cue_id,
            "index": index,
            "range": {"start_us": start_us, "end_us": end_us},
            "text": cue_id,
            "language": "vi",
            "source": "manual_scene_text" if role == "scene_text" else "ocr",
            "role": role,
            "subtitle_track_id": track_id,
        }

    decoded = decode_project_snapshot(
        {
            "id": "explicit-s1-scene-project",
            "name": "Explicit S1 scene",
            "revision": 3,
            "media": [],
            "tracks": [],
            "source_cues": [
                cue("dialogue", 1, 0, 1_000_000, role="dialogue", track_id="S1"),
                cue(
                    "scene-safe",
                    2,
                    1_000_000,
                    2_000_000,
                    role="scene_text",
                    track_id="S1",
                ),
                cue(
                    "scene-collision",
                    3,
                    500_000,
                    900_000,
                    role="scene_text",
                    track_id="S1",
                ),
            ],
            "translated_cues": [],
            "overlays": [],
            "schema_version": 1,
        }
    )

    lanes = {item.id: item.subtitle_track_id for item in decoded.source_cues}
    assert lanes["dialogue"] == "S1"
    assert lanes["scene-safe"] == "S1"
    assert lanes["scene-collision"] == "S2"
    assert decoded.source_cues[1].role == "scene_text"
    assert decoded.source_cues[2].role == "scene_text"


def test_project_decoder_defaults_legacy_blur_to_blur_only_concealment() -> None:
    payload = {
        "id": "legacy-project",
        "name": "Legacy",
        "revision": 1,
        "media": [],
        "tracks": [],
        "source_cues": [],
        "translated_cues": [],
        "overlays": [
            {
                "id": "legacy-blur",
                "kind": "blur",
                "range": {"start_us": 0, "end_us": 1_000_000},
                "x": 0.1,
                "y": 0.8,
                "width": 0.8,
                "height": 0.1,
                "enabled": True,
                "opacity": 1.0,
                "blur_radius": 22,
                "tracking_mode": "source_subtitle",
                "padding_x": 0.012,
                "padding_y": 0.008,
            }
        ],
        "schema_version": 1,
    }

    decoded = decode_project_snapshot(payload)

    assert decoded.overlays[0].concealment_mode == "blur"


def test_project_snapshot_round_trips_voice_conform_state(
    database: Database, tmp_path: Path
) -> None:
    video = MediaAsset("video", "C:/Phim/movie.mp4", MediaKind.VIDEO, 2_000_000)
    base = MediaAsset("voice-base", "C:/Phim/base.wav", MediaKind.AUDIO, 4_000_000)
    conformed = MediaAsset("voice-fit", "C:/Phim/fit.wav", MediaKind.AUDIO, 3_200_000)
    plan = build_voice_conform_plan(
        (VoiceConformCueInput("cue-1", 1, 0, 2_000_000, 4_000_000),),
        source_duration_us=2_000_000,
        settings=VoiceConformSettings(),
    )
    snapshot = ProjectSnapshot(
        "project-conform",
        "Đồng bộ",
        2,
        media=(video, base, conformed),
        tracks=(
            TimelineTrack(
                "D1",
                TrackKind.VOICE,
                "Thuyết minh",
                (
                    TimelineClip(
                        "voice-clip",
                        "D1",
                        TimeRange(0, 3_200_000),
                        TimeRange(0, 3_200_000),
                        "voice-fit",
                    ),
                ),
            ),
        ),
        voice_conform=VoiceConformState(plan, "voice-base", "voice-fit", "tts-parent"),
    )
    projects = ProjectRepository(database)
    projects.save(project_path=tmp_path / "Đồng bộ.ducai", snapshot=snapshot)

    assert projects.load(snapshot.id) == snapshot


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("revision", 1.9, "must be an integer"),
        ("revision", None, "must be an integer"),
    ],
)
def test_project_decoder_rejects_coerced_scalar_types(field, value, message) -> None:
    payload = {
        "id": "project",
        "name": "Project",
        "revision": 1,
        "media": [],
        "tracks": [],
        "source_cues": [],
        "translated_cues": [],
        "overlays": [],
        "schema_version": 1,
    }
    payload[field] = value
    with pytest.raises(TypeError, match=message):
        decode_project_snapshot(payload)


def test_database_migrates_existing_v1_state_without_deleting_jobs(tmp_path: Path) -> None:
    path = tmp_path / "legacy-state.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES (1, ?, ?)",
            ("2026-08-24T00:00:00Z", sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()),
        )
        connection.execute(
            """
            INSERT INTO jobs(
                id, kind, state, project_id, input_revision, provider_id,
                input_fingerprint, config_json, created_at, error_json
            ) VALUES ('legacy-job', 'ocr', 'queued', 'project', 1, 'paddle-fast',
                      'input', '{}', '2026-08-24T00:00:00Z', '{}')
            """
        )

    migrated = Database(path)
    migrated.initialize(applied_at="2026-08-24T01:00:00Z")
    job = JobRepository(migrated).get("legacy-job")
    assert job.spec.provider_version == "legacy"
    assert job.spec.model_id == "default"
    with migrated.connect(read_only=True) as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    assert versions == [1, 2, 3, 4, 5, 6]


def test_v6_migration_does_not_rewrite_legacy_project_snapshots(tmp_path: Path) -> None:
    path = tmp_path / "large-v5-state.db"
    legacy_snapshot = '{"payload":"' + ("x" * 2_000_000) + '"}'
    with sqlite3.connect(path) as connection:
        for version, statement in database_module.MIGRATIONS[:5]:
            connection.executescript(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES (?, ?, ?)",
                (
                    version,
                    "2026-09-30T00:00:00Z",
                    sha256(statement.encode("utf-8")).hexdigest(),
                ),
            )
        connection.execute(
            """
            INSERT INTO projects(id, path, name, schema_version, head_revision, updated_at)
            VALUES ('legacy-large', 'legacy-large.ducai', 'Legacy large', 1, 1, ?)
            """,
            ("2026-09-30T00:00:00Z",),
        )
        connection.execute(
            """
            INSERT INTO project_revisions(
                project_id, revision, snapshot_json, snapshot_sha256, created_at
            ) VALUES ('legacy-large', 1, ?, ?, ?)
            """,
            (
                legacy_snapshot,
                sha256(legacy_snapshot.encode("utf-8")).hexdigest(),
                "2026-09-30T00:00:00Z",
            ),
        )
        # This makes any accidental v6 full-table backfill fail deterministically
        # without allocating a multi-gigabyte test database.
        connection.execute(
            """
            CREATE TRIGGER reject_revision_backfill
            BEFORE UPDATE ON project_revisions
            BEGIN
                SELECT RAISE(ABORT, 'legacy snapshot rewrite attempted');
            END
            """
        )

    Database(path).initialize(applied_at="2026-10-01T00:00:00Z")

    with Database(path).connect(read_only=True) as connection:
        row = connection.execute(
            """
            SELECT snapshot_json, snapshot_encoding, snapshot_blob,
                   uncompressed_size, compressed_size
            FROM project_revisions
            WHERE project_id = 'legacy-large' AND revision = 1
            """
        ).fetchone()
        marker = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 6"
        ).fetchone()

    assert row is not None
    assert row["snapshot_json"] == legacy_snapshot
    assert row["snapshot_encoding"] == "json-v1"
    assert row["snapshot_blob"] is None
    assert row["uncompressed_size"] == 0
    assert row["compressed_size"] == 0
    assert marker is not None
    assert marker["checksum"] == sha256(
        database_module.MIGRATION_6_SQL.encode("utf-8")
    ).hexdigest()


def test_database_v3_backfills_only_tts_purpose_and_adds_activity_index(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v2-state.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.executescript(database_module.MIGRATION_2_SQL)
        for version, statement in (
            (1, SCHEMA_SQL),
            (2, database_module.MIGRATION_2_SQL),
        ):
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES (?, ?, ?)",
                (
                    version,
                    "2026-08-24T00:00:00Z",
                    sha256(statement.encode("utf-8")).hexdigest(),
                ),
            )
        for job_id, kind, config_json in (
            ("tts-preview", "tts", '{"purpose":"preview","cues":[1,2,3]}'),
            ("export-purpose-noise", "export", '{"purpose":"preview"}'),
        ):
            connection.execute(
                """
                INSERT INTO jobs(
                    id, kind, state, project_id, input_revision, provider_id,
                    input_fingerprint, config_json, created_at, error_json
                ) VALUES (?, ?, 'queued', 'project', 1, 'provider',
                          'input', ?, '2026-08-24T00:00:00Z', '{}')
                """,
                (job_id, kind, config_json),
            )

    Database(path).initialize(applied_at="2026-08-24T01:00:00Z")

    with Database(path).connect(read_only=True) as connection:
        purposes = {
            row["id"]: row["purpose"]
            for row in connection.execute("SELECT id, purpose FROM jobs ORDER BY id")
        }
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'job_events_job_kind_sequence_idx'"
        ).fetchone()
        versions = [
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]

    assert purposes == {"export-purpose-noise": "", "tts-preview": "preview"}
    assert index_sql is not None
    assert "job_id, kind, sequence DESC" in index_sql["sql"]
    assert versions == [1, 2, 3, 4, 5, 6]


def test_v3_to_v4_multi_membership_survives_reopen_and_removal_keeps_project_cache(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v3-state.db"
    project_path = tmp_path / "kept-project.ducai"
    cache_path = tmp_path / "cache" / "ocr-checkpoint.bin"
    project_path.write_bytes(b"project fixture")
    cache_path.parent.mkdir()
    cache_path.write_bytes(b"durable checkpoint")
    snapshot_json = "{}"
    with sqlite3.connect(path) as connection:
        for statement in (
            SCHEMA_SQL,
            database_module.MIGRATION_2_SQL,
            database_module.MIGRATION_3_SQL,
        ):
            connection.executescript(statement)
        for version, statement in (
            (1, SCHEMA_SQL),
            (2, database_module.MIGRATION_2_SQL),
            (3, database_module.MIGRATION_3_SQL),
        ):
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES (?, ?, ?)",
                (
                    version,
                    "2026-08-29T00:00:00Z",
                    sha256(statement.encode("utf-8")).hexdigest(),
                ),
            )
        connection.execute(
            """
            INSERT INTO projects(id, path, name, schema_version, head_revision, updated_at)
            VALUES ('project-kept', ?, 'Kept', 1, 1, '2026-08-29T00:00:00Z')
            """,
            (str(project_path),),
        )
        connection.execute(
            """
            INSERT INTO project_revisions(
                project_id, revision, snapshot_json, snapshot_sha256, created_at
            ) VALUES ('project-kept', 1, ?, ?, '2026-08-29T00:00:00Z')
            """,
            (snapshot_json, sha256(snapshot_json.encode("utf-8")).hexdigest()),
        )
        connection.execute(
            """
            INSERT INTO jobs(
                id, kind, state, project_id, input_revision, provider_id,
                input_fingerprint, config_json, created_at, error_json,
                provider_version, model_id, settings_fingerprint, purpose
            ) VALUES (
                'ocr-kept', 'ocr', 'succeeded', 'project-kept', 1, 'paddle',
                'input', '{}', '2026-08-29T00:00:00Z', '{}',
                '1.0', 'model', 'settings', ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                id, job_id, kind, uri, sha256, size_bytes, state, created_at
            ) VALUES (
                'artifact-kept', 'ocr-kept', 'ocr_cache', ?, ?, ?, 'validated',
                '2026-08-29T00:00:00Z'
            )
            """,
            (
                str(cache_path),
                sha256(cache_path.read_bytes()).hexdigest(),
                cache_path.stat().st_size,
            ),
        )
        connection.execute(
            """
            INSERT INTO job_checkpoints(
                job_id, stage_key, checkpoint_key, schema_version,
                input_fingerprint, cursor_us, artifact_id, payload_json,
                updated_at, resume_fingerprint
            ) VALUES (
                'ocr-kept', 'ocr', 'latest', 1, 'input', 1000000,
                'artifact-kept', '{}', '2026-08-29T00:00:00Z', 'resume'
            )
            """
        )

    database = Database(path)
    database.initialize(applied_at="2026-08-29T01:00:00Z")
    repository = MultiVideoRepository(database)
    repository.ensure_primary()
    repository.add_projects(["project-kept"])
    reopened = MultiVideoRepository(Database(path))

    assert [item["project_id"] for item in reopened.list_items()] == ["project-kept"]
    reopened.remove_project("project-kept")

    with database.connect(read_only=True) as connection:
        versions = [
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        assert connection.execute("SELECT 1 FROM projects WHERE id = 'project-kept'").fetchone()
        assert connection.execute(
            "SELECT 1 FROM project_revisions WHERE project_id = 'project-kept'"
        ).fetchone()
        assert connection.execute("SELECT 1 FROM jobs WHERE id = 'ocr-kept'").fetchone()
        assert connection.execute(
            "SELECT 1 FROM job_checkpoints WHERE job_id = 'ocr-kept'"
        ).fetchone()
        assert connection.execute("SELECT 1 FROM artifacts WHERE id = 'artifact-kept'").fetchone()
        assert connection.execute(
            "SELECT 1 FROM multi_project_exclusions WHERE project_id = 'project-kept'"
        ).fetchone()
    assert versions == [1, 2, 3, 4, 5, 6]
    assert reopened.list_items() == []
    assert project_path.read_bytes() == b"project fixture"
    assert cache_path.read_bytes() == b"durable checkpoint"

    with database.write_transaction() as connection:
        connection.execute("DELETE FROM projects WHERE id = 'project-kept'")
    with database.connect(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM multi_project_exclusions WHERE project_id = 'project-kept'"
        ).fetchone() is None


def test_failed_migration_rolls_back_schema_and_version_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "atomic-migration.db")
    database.initialize(applied_at="2026-08-24T00:00:00Z")
    broken = """
    CREATE TABLE migration_must_rollback(id INTEGER PRIMARY KEY);
    THIS IS NOT VALID SQL;
    """
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (*database_module.MIGRATIONS, (database_module.SCHEMA_VERSION + 1, broken)),
    )

    with pytest.raises(sqlite3.OperationalError):
        database.initialize(applied_at="2026-08-24T01:00:00Z")

    with database.connect(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_must_rollback'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (database_module.SCHEMA_VERSION + 1,),
            ).fetchone()
            is None
        )
