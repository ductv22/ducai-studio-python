"""Read-only real-project load benchmark; compare defensive copy and IPC ownership."""
from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import statistics
import zlib
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QCoreApplication

from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("project_id")
    args = parser.parse_args()
    app = QCoreApplication([])
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("""
            SELECT r.snapshot_json, r.snapshot_encoding, r.snapshot_blob
            FROM project_revisions r JOIN projects p
                ON p.id = r.project_id AND p.head_revision = r.revision
            WHERE p.id = ?
        """, (args.project_id,)).fetchone()
    assert row is not None
    data = zlib.decompress(row[2]) if row[1] == "zlib-json-v1" else row[0]
    durations = {}
    identities = []
    for ownership in (False, True):
        timings = []
        for _ in range(3):
            snapshot = json.loads(data)
            model = ProjectStateModel()
            start = perf_counter()
            model.replace(snapshot, take_ownership=ownership)
            timings.append((perf_counter() - start) * 1000)
            identities.append((model.subtitle_cues.count, model.durationUs,
                               tuple(item["id"] for rows, _, _ in model.clips._track_indexes
                                     for item in rows)))
            assert ownership == (model._canonical_snapshot is snapshot)
            model.deleteLater()
            app.processEvents()
            gc.collect()
        durations["owned" if ownership else "defensive"] = timings
    assert all(identity == identities[0] for identity in identities)
    print(json.dumps({"status": "PASS", "cue_count": identities[0][0],
                      "duration_us": identities[0][1], "timings_ms": durations,
                      "median_ms": {k: statistics.median(v) for k, v in durations.items()}}))


if __name__ == "__main__":
    main()
