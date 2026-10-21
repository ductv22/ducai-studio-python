"""Inspect saved cue overlaps without opening a writable production service."""

from __future__ import annotations

import json
import sqlite3
import sys
import zlib
from pathlib import Path

from ducai_studio.domain.timeline_edit import project_for_timeline
from ducai_studio.infrastructure.project_repository import decode_project_snapshot


def main() -> None:
    database = Path(sys.argv[1]).resolve()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        projects = connection.execute(
            "SELECT project_id, MAX(revision) AS revision FROM project_revisions "
            "GROUP BY project_id"
        ).fetchall()
        for project in projects:
            row = connection.execute(
                "SELECT * FROM project_revisions WHERE project_id=? AND revision=?",
                (project["project_id"], project["revision"]),
            ).fetchone()
            raw = json.loads(
                zlib.decompress(row["snapshot_blob"])
                if row["snapshot_encoding"] == "zlib-json-v1"
                else row["snapshot_json"]
            )
            snapshot = project_for_timeline(decode_project_snapshot(raw))
            pairs = []
            active = []
            for cue in sorted(snapshot.source_cues, key=lambda c: c.range.start_us):
                active = [c for c in active if c.range.end_us > cue.range.start_us]
                for other in active:
                    if other.role == cue.role == "dialogue" or (
                        other.role == cue.role == "scene_text"
                        and other.visual_box == cue.visual_box
                    ):
                        pairs.append([
                            {"id": c.id, "text": c.text,
                             "range": [c.range.start_us, c.range.end_us],
                             "box": c.visual_box, "source": c.source,
                             "evidence_count": len(c.evidence_ids)}
                            for c in (other, cue)
                        ])
                active.append(cue)
            print(json.dumps({
                "project": snapshot.id, "name": snapshot.name,
                "revision": snapshot.revision, "cue_count": len(snapshot.source_cues),
                "overlaps": pairs[:30], "overlap_count": len(pairs),
                "translated": [
                    {"id": c.id, "text": c.text,
                     "range": [c.range.start_us, c.range.end_us],
                     "role": c.role,
                     "box": c.visual_box, "track": c.subtitle_track_id}
                    for c in snapshot.translated_cues
                    if c.is_scene_text
                ],
            }, ensure_ascii=False))
            if len(sys.argv) > 2:
                output = Path(sys.argv[2]).resolve()
                output.mkdir(parents=True, exist_ok=True)
                (output / f"{snapshot.id}.json").write_text(
                    json.dumps(raw, ensure_ascii=False), encoding="utf-8"
                )
        jobs = connection.execute(
            "SELECT id, project_id, kind, input_revision FROM jobs "
            "ORDER BY rowid DESC LIMIT 12"
        ).fetchall()
        print(json.dumps([dict(job) for job in jobs], ensure_ascii=False))
        for job in jobs:
            if job["kind"] == "ocr":
                artifacts = connection.execute(
                    "SELECT kind, uri FROM artifacts WHERE job_id=?", (job["id"],)
                ).fetchall()
                print(json.dumps({"job": job["id"], "artifacts": [dict(a) for a in artifacts]},
                                 ensure_ascii=False))


if __name__ == "__main__":
    main()
