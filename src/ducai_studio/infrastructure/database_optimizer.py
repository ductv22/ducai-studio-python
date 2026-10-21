from __future__ import annotations

import os
import shutil
import sqlite3
import zlib
from collections.abc import Callable
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import uuid4

from .database import Database


class DatabaseOptimizationCancelled(RuntimeError):
    """Raised before the active database is swapped when cancellation wins."""


def _report_progress(
    callback: Callable[[dict[str, int | str]], None] | None,
    *,
    stage: str,
    current: int = 0,
    total: int = 0,
) -> None:
    if callback is not None:
        callback({"stage": stage, "current": current, "total": total})


def _cancel_checkpoint(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DatabaseOptimizationCancelled(
            "Đã hủy tối ưu trước khi đổi database; dữ liệu hiện tại không bị thay đổi."
        )


def _integrity_ok(path: Path) -> bool:
    database = Database(path)
    with closing(database.connect(read_only=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    return bool(integrity and str(integrity[0]).casefold() == "ok" and not foreign_keys)


def finalize_verified_optimizer_backup(database_path: Path) -> None:
    """Remove the prior database only after a later successful health check."""

    backup = database_path.with_name(database_path.name + ".a121-backup")
    if not backup.is_file() or not database_path.is_file():
        return
    if _integrity_ok(database_path):
        backup.unlink(missing_ok=True)


def optimize_project_database(
    database: Database,
    *,
    cancel_event: Event | None = None,
    progress_callback: Callable[[dict[str, int | str]], None] | None = None,
) -> dict[str, int | str]:
    """Rebuild the database and compress every legacy project revision.

    SQLite backup creates the candidate without mutating the active file. The
    candidate is fully checked before the atomic swap; the old file remains as
    a backup until the next service startup verifies the replacement again.
    """

    source_path = database.path
    if not source_path.is_file():
        raise FileNotFoundError("Không tìm thấy cơ sở dữ liệu dự án để tối ưu")
    backup_path = source_path.with_name(source_path.name + ".a121-backup")
    if backup_path.exists():
        raise RuntimeError(
            "Đang giữ bản sao an toàn từ lần tối ưu trước; hãy khởi động lại tool "
            "để health-check hoàn tất trước khi tối ưu lần nữa."
        )
    source_size = source_path.stat().st_size
    required_free = max(64 * 1024 * 1024, source_size * 2)
    if shutil.disk_usage(source_path.parent).free < required_free:
        raise OSError(
            "Không đủ dung lượng trống để tạo và kiểm tra database tối ưu an toàn."
        )

    candidate_path = source_path.with_name(
        f"{source_path.name}.optimize-{uuid4().hex}.tmp"
    )
    compressed_revisions = 0
    try:
        _cancel_checkpoint(cancel_event)
        _report_progress(progress_callback, stage="copying")

        def backup_progress(_status: int, remaining: int, total: int) -> None:
            _cancel_checkpoint(cancel_event)
            _report_progress(
                progress_callback,
                stage="copying",
                current=max(0, total - remaining),
                total=max(0, total),
            )

        with (
            closing(database.connect(read_only=True)) as source,
            closing(sqlite3.connect(candidate_path)) as candidate,
        ):
            source.backup(candidate, pages=1_024, progress=backup_progress, sleep=0.05)
        _cancel_checkpoint(cancel_event)
        candidate = sqlite3.connect(candidate_path)
        candidate.row_factory = sqlite3.Row
        candidate.execute("PRAGMA foreign_keys = ON")
        try:
            tables = tuple(
                str(row[0])
                for row in candidate.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            with closing(database.connect(read_only=True)) as source:
                source_counts = {
                    table: int(source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                    for table in tables
                }
            rows = candidate.execute(
                """
                SELECT project_id, revision, snapshot_json, snapshot_sha256
                FROM project_revisions
                WHERE snapshot_encoding = 'json-v1'
                ORDER BY project_id, revision
                """
            ).fetchall()
            _report_progress(
                progress_callback,
                stage="compressing",
                current=0,
                total=len(rows),
            )
            for offset in range(0, len(rows), 100):
                _cancel_checkpoint(cancel_event)
                candidate.execute("BEGIN IMMEDIATE")
                try:
                    for row in rows[offset : offset + 100]:
                        raw = str(row["snapshot_json"]).encode("utf-8")
                        if sha256(raw).hexdigest() != str(row["snapshot_sha256"]):
                            raise RuntimeError(
                                "Revision project cũ không khớp SHA-256; đã hủy tối ưu."
                            )
                        blob = zlib.compress(raw, level=3)
                        candidate.execute(
                            """
                            UPDATE project_revisions
                            SET snapshot_json = '', snapshot_encoding = 'zlib-json-v1',
                                snapshot_blob = ?, uncompressed_size = ?, compressed_size = ?
                            WHERE project_id = ? AND revision = ?
                            """,
                            (
                                blob,
                                len(raw),
                                len(blob),
                                row["project_id"],
                                row["revision"],
                            ),
                        )
                        compressed_revisions += 1
                    candidate.commit()
                    _report_progress(
                        progress_callback,
                        stage="compressing",
                        current=min(offset + 100, len(rows)),
                        total=len(rows),
                    )
                except Exception:
                    candidate.rollback()
                    raise
            _cancel_checkpoint(cancel_event)
            _report_progress(progress_callback, stage="vacuuming")
            candidate.set_progress_handler(
                lambda: 1
                if cancel_event is not None and cancel_event.is_set()
                else 0,
                1_000,
            )
            try:
                candidate.execute("VACUUM")
            except sqlite3.OperationalError as exc:
                if cancel_event is not None and cancel_event.is_set():
                    raise DatabaseOptimizationCancelled(
                        "Đã hủy tối ưu trước khi đổi database; dữ liệu hiện tại không bị thay đổi."
                    ) from exc
                raise
            finally:
                candidate.set_progress_handler(None, 0)
            _cancel_checkpoint(cancel_event)
            _report_progress(progress_callback, stage="verifying")
            target_counts = {
                table: int(candidate.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in tables
            }
            if target_counts != source_counts:
                raise RuntimeError("Số hàng thay đổi trong lúc tối ưu; đã hủy trước khi swap.")
            for row in candidate.execute(
                """
                SELECT snapshot_blob, uncompressed_size, snapshot_sha256
                FROM project_revisions WHERE snapshot_encoding = 'zlib-json-v1'
                """
            ):
                blob = row["snapshot_blob"]
                if not isinstance(blob, bytes):
                    raise RuntimeError("Revision nén thiếu payload")
                raw = zlib.decompress(blob)
                if len(raw) != int(row["uncompressed_size"]):
                    raise RuntimeError("Revision nén sai kích thước")
                if sha256(raw).hexdigest() != str(row["snapshot_sha256"]):
                    raise RuntimeError("Revision nén sai SHA-256")
            integrity = candidate.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                raise RuntimeError("SQLite integrity_check không đạt")
            if candidate.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("SQLite foreign_key_check không đạt")
        finally:
            candidate.close()

        with closing(database.connect()) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _cancel_checkpoint(cancel_event)
        _report_progress(progress_callback, stage="swapping")
        for suffix in ("-wal", "-shm"):
            source_path.with_name(source_path.name + suffix).unlink(missing_ok=True)
        os.replace(source_path, backup_path)
        try:
            os.replace(candidate_path, source_path)
            if not _integrity_ok(source_path):
                raise RuntimeError("Database mới không vượt qua health-check sau swap")
        except Exception:
            source_path.unlink(missing_ok=True)
            os.replace(backup_path, source_path)
            raise
        return {
            "status": "completed",
            "compressed_revisions": compressed_revisions,
            "before_bytes": source_size,
            "after_bytes": source_path.stat().st_size,
            "backup_path": str(backup_path),
        }
    finally:
        candidate_path.unlink(missing_ok=True)
