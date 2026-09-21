from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

SCHEMA_VERSION = 5
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
    head_revision INTEGER NOT NULL CHECK(head_revision > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_revisions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision > 0),
    snapshot_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, revision)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    project_id TEXT NOT NULL,
    input_revision INTEGER NOT NULL CHECK(input_revision > 0),
    provider_id TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    error_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS jobs_state_created_idx ON jobs(state, created_at);
CREATE INDEX IF NOT EXISTS jobs_project_idx ON jobs(project_id, created_at);

CREATE TABLE IF NOT EXISTS job_stages (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    state TEXT NOT NULL,
    weight REAL NOT NULL CHECK(weight > 0),
    completed_units INTEGER NOT NULL DEFAULT 0 CHECK(completed_units >= 0),
    total_units INTEGER CHECK(total_units IS NULL OR total_units >= 0),
    message TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(job_id, stage_key)
);

CREATE TABLE IF NOT EXISTS job_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS job_events_job_sequence_idx ON job_events(job_id, sequence);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    state TEXT NOT NULL CHECK(state IN ('technical', 'validated', 'final')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_checkpoints (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage_key TEXT NOT NULL,
    checkpoint_key TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
    input_fingerprint TEXT NOT NULL,
    cursor_us INTEGER CHECK(cursor_us IS NULL OR cursor_us >= 0),
    artifact_id TEXT REFERENCES artifacts(id),
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, stage_key, checkpoint_key)
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""

MIGRATION_2_SQL = """
ALTER TABLE jobs ADD COLUMN provider_version TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE jobs ADD COLUMN model_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE jobs ADD COLUMN settings_fingerprint TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE job_checkpoints ADD COLUMN resume_fingerprint TEXT NOT NULL DEFAULT '';
"""

MIGRATION_3_SQL = """
ALTER TABLE jobs ADD COLUMN purpose TEXT NOT NULL DEFAULT '';
UPDATE jobs
SET purpose = json_extract(config_json, '$.purpose')
WHERE kind = 'tts' AND json_type(config_json, '$.purpose') = 'text';
CREATE INDEX IF NOT EXISTS job_events_job_kind_sequence_idx
ON job_events(job_id, kind, sequence DESC);
"""

MIGRATION_4_SQL = """
CREATE TABLE IF NOT EXISTS multi_batches (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    retry_pass INTEGER NOT NULL DEFAULT 0 CHECK(retry_pass >= 0),
    current_project_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS multi_batch_items (
    batch_id TEXT NOT NULL REFERENCES multi_batches(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    selected INTEGER NOT NULL DEFAULT 1 CHECK(selected IN (0, 1)),
    stage TEXT NOT NULL DEFAULT 'ocr',
    state TEXT NOT NULL DEFAULT 'waiting',
    progress REAL NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 1),
    child_job_id TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    final_pass INTEGER NOT NULL DEFAULT 0 CHECK(final_pass IN (0, 1)),
    error_code TEXT NOT NULL DEFAULT '',
    error_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(batch_id, project_id),
    UNIQUE(batch_id, ordinal)
);

CREATE INDEX IF NOT EXISTS multi_batch_items_order_idx
ON multi_batch_items(batch_id, ordinal);

CREATE TABLE IF NOT EXISTS multi_stage_attempts (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES multi_batches(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    pass_number INTEGER NOT NULL CHECK(pass_number >= 0),
    attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
    job_id TEXT NOT NULL,
    state TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS multi_stage_attempts_job_idx
ON multi_stage_attempts(batch_id, job_id);
"""

MIGRATION_5_SQL = """
CREATE TABLE IF NOT EXISTS multi_project_exclusions (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    hidden_at TEXT NOT NULL
);
"""

MIGRATIONS = (
    (1, SCHEMA_SQL),
    (2, MIGRATION_2_SQL),
    (3, MIGRATION_3_SQL),
    (4, MIGRATION_4_SQL),
    (5, MIGRATION_5_SQL),
)


def iter_sql_statements(script: str) -> Iterator[str]:
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    if buffer.strip():
        raise RuntimeError("Incomplete SQL migration statement")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def initialize(self, *, applied_at: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            # journal_mode persists in the database file. Reapplying it on
            # every connection needlessly takes a journal lock and is brittle
            # on Windows after hard-killed sibling processes.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    checksum TEXT NOT NULL
                )
                """
                )
            connection.commit()
            for version, statement in MIGRATIONS:
                checksum = sha256(statement.encode("utf-8")).hexdigest()
                existing = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if existing is not None:
                    if existing["checksum"] != checksum:
                        raise RuntimeError("Database migration checksum drift detected")
                    continue
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for migration_statement in iter_sql_statements(statement):
                        connection.execute(migration_statement)
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(version, applied_at, checksum)
                        VALUES (?, ?, ?)
                        """,
                        (version, applied_at, checksum),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
