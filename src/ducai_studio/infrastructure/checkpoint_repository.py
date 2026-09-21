from __future__ import annotations

import errno
import json
import os
import shutil
from collections.abc import Callable
from contextlib import closing, suppress
from hashlib import sha256
from pathlib import Path
from time import sleep
from typing import Any
from uuid import uuid4

from ducai_studio.domain.artifacts import ArtifactRecord, ArtifactState, CheckpointRecord
from ducai_studio.domain.jobs import redact_secret_fields

from .database import Database
from .job_repository import utc_now_text

_MIN_WORKER_FREE_BYTES = 512 * 1024 * 1024
_CHECKPOINT_COPY_RESERVE_BYTES = 256 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CheckpointRepository:
    def __init__(
        self,
        database: Database,
        artifact_root: str | Path,
        *,
        phase_observer: Callable[[str], None] | None = None,
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.staging_root = self.artifact_root / "staging"
        self.immutable_root = self.artifact_root / "immutable"
        self.staging_root.mkdir(exist_ok=True)
        self.immutable_root.mkdir(exist_ok=True)
        self._phase_observer = phase_observer
        self._cleanup_stale_temporaries()

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = max(0, int(value))
        if amount >= 1024**3:
            return f"{amount / 1024**3:.2f} GiB"
        return f"{amount / 1024**2:.0f} MiB"

    def _ensure_free_space(self, *, required_bytes: int, stage_label: str) -> None:
        available = int(shutil.disk_usage(self.artifact_root).free)
        required = max(1, int(required_bytes))
        if available >= required:
            return
        raise OSError(
            errno.ENOSPC,
            (
                f"Không đủ dung lượng để {stage_label}. Cần ít nhất "
                f"{self._format_bytes(required)} nhưng ổ đĩa chỉ còn "
                f"{self._format_bytes(available)}. Hãy dọn dung lượng trong thư mục "
                "dữ liệu DUCAI rồi tiếp tục từ cache/checkpoint hiện có."
            ),
        )

    def ensure_worker_capacity(self, *, stage_label: str) -> None:
        """Reject a large media stage before it can fail midway through a checkpoint."""

        self._ensure_free_space(
            required_bytes=_MIN_WORKER_FREE_BYTES,
            stage_label=stage_label,
        )

    def commit(
        self,
        *,
        job_id: str,
        stage_key: str,
        checkpoint_key: str,
        schema_version: int,
        resume_fingerprint: str,
        cursor_us: int | None,
        payload: dict[str, Any],
        artifact_path: str | Path | None = None,
        artifact_kind: str = "checkpoint",
        artifact_state: ArtifactState = ArtifactState.TECHNICAL,
        artifact_id: str | None = None,
        expected_sha256: str | None = None,
        event_id: str | None = None,
        event_kind: str = "checkpoint",
        event_payload: dict[str, Any] | None = None,
    ) -> CheckpointRecord:
        if schema_version <= 0 or not resume_fingerprint.strip():
            raise ValueError("Checkpoint schema version and fingerprint are required")
        if cursor_us is not None and cursor_us < 0:
            raise ValueError("Checkpoint cursor cannot be negative")

        if event_id is not None:
            # This is a write/replay path. A writable connection can perform
            # any SQLite recovery needed after a killed worker; opening mode=ro
            # before the write adds a weaker, unnecessary failure boundary.
            # sqlite3.Connection's context manager commits or rolls back but
            # deliberately does not close the handle.  Close this replay
            # probe before the potentially long artifact copy so a hard-killed
            # worker cannot leave a live WAL/SHM handle at the copy boundary.
            with closing(self.database.connect()) as connection:
                existing_event = connection.execute(
                    "SELECT job_id FROM job_events WHERE event_id = ?", (event_id,)
                ).fetchone()
            if existing_event is not None:
                if existing_event["job_id"] != job_id:
                    raise RuntimeError("Checkpoint event id belongs to another job")
                existing_checkpoint = self.load(
                    job_id=job_id,
                    stage_key=stage_key,
                    checkpoint_key=checkpoint_key,
                    resume_fingerprint=resume_fingerprint,
                )
                if existing_checkpoint is None:
                    raise RuntimeError("Checkpoint event exists without its durable checkpoint")
                if artifact_path is not None:
                    stale_source = Path(artifact_path).expanduser().resolve(strict=False)
                    if stale_source.is_relative_to(self.staging_root):
                        with suppress(OSError):
                            stale_source.unlink(missing_ok=True)
                return existing_checkpoint

        now = utc_now_text()
        artifact: ArtifactRecord | None = None
        staging_source: Path | None = None
        if artifact_path is not None:
            path = Path(artifact_path).expanduser().resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(self.staging_root):
                raise ValueError("Checkpoint artifact is outside the managed staging root")
            staging_source = path
            identifier = artifact_id or (
                f"evt-{sha256(event_id.encode('utf-8')).hexdigest()[:32]}"
                if event_id is not None
                else str(uuid4())
            )
            suffix_source = path.with_suffix("") if path.suffix == ".partial" else path
            suffix = suffix_source.suffix or ".bin"
            target_root = self.immutable_root / job_id
            target_root.mkdir(parents=True, exist_ok=True)
            temporary = target_root / f".{identifier}-{uuid4().hex}.tmp"
            digest = sha256()
            size_bytes = 0
            source_stat_before = path.stat()
            self._ensure_free_space(
                required_bytes=source_stat_before.st_size + _CHECKPOINT_COPY_RESERVE_BYTES,
                stage_label="lưu checkpoint",
            )
            try:
                with path.open("rb") as source, temporary.open("xb") as destination:
                    self._notify_phase("copy_started")
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        destination.write(block)
                        digest.update(block)
                        size_bytes += len(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                source_stat_after = path.stat()
                if (
                    source_stat_after.st_size != source_stat_before.st_size
                    or source_stat_after.st_mtime_ns != source_stat_before.st_mtime_ns
                ):
                    raise RuntimeError("Checkpoint artifact changed while being snapshotted")
                actual_sha256 = digest.hexdigest()
                if expected_sha256 is not None and actual_sha256 != expected_sha256:
                    raise ValueError("Checkpoint artifact hash does not match")
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            target = target_root / f"{identifier}-{actual_sha256[:16]}{suffix}"
            if target.exists():
                if target.stat().st_size != size_bytes or sha256_file(target) != actual_sha256:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError("Immutable checkpoint target hash collision")
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(target)
            if target.stat().st_size != size_bytes or sha256_file(target) != actual_sha256:
                target.unlink(missing_ok=True)
                raise RuntimeError("Promoted checkpoint artifact failed verification")
            target.chmod(0o444)
            self._notify_phase("promoted")
            artifact = ArtifactRecord(
                id=identifier,
                job_id=job_id,
                kind=artifact_kind,
                uri=str(target),
                sha256=actual_sha256,
                size_bytes=size_bytes,
                state=artifact_state,
                created_at=now,
            )

        encoded_payload = json.dumps(
            redact_secret_fields(payload), ensure_ascii=False, sort_keys=True
        )
        replaced_artifact_id: str | None = None
        with self.database.write_transaction() as connection:
            existing_checkpoint = connection.execute(
                """
                SELECT artifact_id FROM job_checkpoints
                WHERE job_id = ? AND stage_key = ? AND checkpoint_key = ?
                """,
                (job_id, stage_key, checkpoint_key),
            ).fetchone()
            if existing_checkpoint is not None and existing_checkpoint["artifact_id"] is not None:
                replaced_artifact_id = str(existing_checkpoint["artifact_id"])
            if event_id is not None:
                connection.execute(
                    """
                    INSERT INTO job_events(event_id, job_id, kind, occurred_at, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        job_id,
                        event_kind,
                        now,
                        json.dumps(
                            redact_secret_fields(event_payload or {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
            if artifact is not None:
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        id, job_id, kind, uri, sha256, size_bytes, state, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        artifact.job_id,
                        artifact.kind,
                        artifact.uri,
                        artifact.sha256,
                        artifact.size_bytes,
                        artifact.state.value,
                        artifact.created_at,
                    ),
                )
            connection.execute(
                """
                INSERT INTO job_checkpoints(
                    job_id, stage_key, checkpoint_key, schema_version, input_fingerprint,
                    resume_fingerprint, cursor_us, artifact_id, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, stage_key, checkpoint_key) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    input_fingerprint = excluded.input_fingerprint,
                    resume_fingerprint = excluded.resume_fingerprint,
                    cursor_us = excluded.cursor_us,
                    artifact_id = excluded.artifact_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    stage_key,
                    checkpoint_key,
                    schema_version,
                    resume_fingerprint,
                    resume_fingerprint,
                    cursor_us,
                    artifact.id if artifact else None,
                    encoded_payload,
                    now,
                ),
            )
        self._notify_phase("committed")
        current_artifact_id = artifact.id if artifact else None
        if replaced_artifact_id and replaced_artifact_id != current_artifact_id:
            self._delete_unreferenced_technical_artifact(replaced_artifact_id)
        if staging_source is not None:
            with suppress(OSError):
                staging_source.unlink(missing_ok=True)
        self._cleanup_stale_temporaries()
        return CheckpointRecord(
            job_id=job_id,
            stage_key=stage_key,
            checkpoint_key=checkpoint_key,
            schema_version=schema_version,
            resume_fingerprint=resume_fingerprint,
            cursor_us=cursor_us,
            artifact_id=artifact.id if artifact else None,
            payload=payload,
            updated_at=now,
        )

    def _delete_unreferenced_technical_artifact(self, artifact_id: str) -> None:
        """Remove a superseded technical snapshot without touching review/final data."""

        path: Path | None = None
        with self.database.write_transaction() as connection:
            row = connection.execute(
                "SELECT uri, state FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None or str(row["state"]) != ArtifactState.TECHNICAL.value:
                return
            referenced = connection.execute(
                "SELECT 1 FROM job_checkpoints WHERE artifact_id = ? LIMIT 1",
                (artifact_id,),
            ).fetchone()
            if referenced is not None:
                return
            candidate = Path(str(row["uri"])).expanduser().resolve(strict=False)
            if not candidate.is_relative_to(self.immutable_root):
                return
            connection.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
            path = candidate
        if path is None:
            return
        # Delete the database record first. A crash may then leave an
        # unreferenced file (safe and recoverable), but can never leave a live
        # artifact row pointing at a file that was already removed.
        with suppress(OSError):
            if path.exists():
                path.chmod(0o666)
            path.unlink(missing_ok=True)
        parent = path.parent
        if parent != self.immutable_root:
            with suppress(OSError):
                parent.rmdir()

    def _notify_phase(self, phase: str) -> None:
        if self._phase_observer is not None:
            self._phase_observer(phase)

    def _cleanup_stale_temporaries(self) -> None:
        for attempt in range(5):
            remaining: list[Path] = []
            for temporary in self.immutable_root.rglob(".*.tmp"):
                if not temporary.is_file():
                    continue
                try:
                    temporary.unlink()
                except OSError:
                    remaining.append(temporary)
            if not remaining:
                return
            if attempt < 4:
                sleep(0.01)

    def load(
        self,
        *,
        job_id: str,
        stage_key: str,
        checkpoint_key: str,
        resume_fingerprint: str,
    ) -> CheckpointRecord | None:
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM job_checkpoints
                WHERE job_id = ? AND stage_key = ? AND checkpoint_key = ?
                """,
                (job_id, stage_key, checkpoint_key),
            ).fetchone()
        if row is None or row["resume_fingerprint"] != resume_fingerprint:
            return None
        if row["artifact_id"] is not None:
            with self.database.connect(read_only=True) as connection:
                artifact = connection.execute(
                    "SELECT uri, sha256, size_bytes FROM artifacts WHERE id = ?",
                    (row["artifact_id"],),
                ).fetchone()
            if artifact is None:
                raise RuntimeError("Checkpoint artifact record is missing")
            artifact_path = Path(artifact["uri"]).expanduser().resolve(strict=True)
            if not artifact_path.is_relative_to(self.immutable_root):
                raise RuntimeError("Checkpoint artifact escaped immutable storage")
            if (
                artifact_path.stat().st_size != int(artifact["size_bytes"])
                or sha256_file(artifact_path) != artifact["sha256"]
            ):
                raise RuntimeError("Checkpoint artifact hash or size does not match")
        return CheckpointRecord(
            job_id=row["job_id"],
            stage_key=row["stage_key"],
            checkpoint_key=row["checkpoint_key"],
            schema_version=int(row["schema_version"]),
            resume_fingerprint=row["resume_fingerprint"],
            cursor_us=row["cursor_us"],
            artifact_id=row["artifact_id"],
            payload=json.loads(row["payload_json"]),
            updated_at=row["updated_at"],
        )

    def resolve_artifact(self, artifact_id: str) -> ArtifactRecord:
        if not artifact_id.strip():
            raise ValueError("Artifact id is required")
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise LookupError(f"Unknown artifact: {artifact_id}")
        path = Path(row["uri"]).expanduser().resolve(strict=True)
        if not path.is_relative_to(self.immutable_root):
            raise RuntimeError("Checkpoint artifact escaped immutable storage")
        if path.stat().st_size != int(row["size_bytes"]) or sha256_file(path) != row["sha256"]:
            raise RuntimeError("Checkpoint artifact hash or size does not match")
        return ArtifactRecord(
            id=row["id"],
            job_id=row["job_id"],
            kind=row["kind"],
            uri=str(path),
            sha256=row["sha256"],
            size_bytes=int(row["size_bytes"]),
            state=ArtifactState(row["state"]),
            created_at=row["created_at"],
        )
