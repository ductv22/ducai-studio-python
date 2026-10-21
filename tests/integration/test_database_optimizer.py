from __future__ import annotations

import json
import zlib
from hashlib import sha256
from threading import Event

import pytest

from ducai_studio.infrastructure.database import Database
from ducai_studio.infrastructure.database_optimizer import (
    DatabaseOptimizationCancelled,
    finalize_verified_optimizer_backup,
    optimize_project_database,
)


def test_database_optimizer_compresses_legacy_revisions_and_keeps_backup(tmp_path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    database.initialize(applied_at="2026-10-01T00:00:00Z")
    payload = json.dumps(
        {"id": "project", "revision": 1, "cues": ["字幕"] * 2_000},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw = payload.encode("utf-8")
    digest = sha256(raw).hexdigest()
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(id, path, name, schema_version, head_revision, updated_at)
            VALUES ('project', ?, 'Project', 1, 1, '2026-10-01T00:00:00Z')
            """,
            (str(tmp_path / "project.ducai"),),
        )
        connection.execute(
            """
            INSERT INTO project_revisions(
                project_id, revision, snapshot_json, snapshot_sha256, created_at,
                snapshot_encoding, uncompressed_size, compressed_size
            ) VALUES ('project', 1, ?, ?, '2026-10-01T00:00:00Z',
                      'json-v1', ?, ?)
            """,
            (payload, digest, len(raw), len(raw)),
        )

    result = optimize_project_database(database)

    assert result["status"] == "completed"
    assert result["compressed_revisions"] == 1
    backup = path.with_name(path.name + ".a121-backup")
    assert backup.is_file()
    with database.connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM project_revisions WHERE project_id='project' AND revision=1"
        ).fetchone()
        assert row is not None
        assert row["snapshot_encoding"] == "zlib-json-v1"
        assert row["snapshot_json"] == ""
        assert zlib.decompress(row["snapshot_blob"]) == raw
        assert row["snapshot_sha256"] == digest
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    finalize_verified_optimizer_backup(path)
    assert not backup.exists()


def test_database_optimizer_refuses_to_overwrite_unverified_backup(tmp_path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    database.initialize(applied_at="2026-10-01T00:00:00Z")
    backup = path.with_name(path.name + ".a121-backup")
    backup.write_bytes(b"pending verification")

    try:
        optimize_project_database(database)
    except RuntimeError as exc:
        assert "bản sao an toàn" in str(exc)
    else:
        raise AssertionError("optimizer must preserve an unverified backup")

    assert backup.read_bytes() == b"pending verification"


def test_database_optimizer_cancelled_before_swap_keeps_active_database(tmp_path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    database.initialize(applied_at="2026-10-01T00:00:00Z")
    before = path.read_bytes()
    cancel_event = Event()
    cancel_event.set()

    with pytest.raises(DatabaseOptimizationCancelled, match="không bị thay đổi"):
        optimize_project_database(database, cancel_event=cancel_event)

    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".a121-backup").exists()
    assert not tuple(tmp_path.glob("state.db.optimize-*.tmp"))
