"""Replay immutable OCR review artifacts in an isolated DB; live DB is read-only."""
from __future__ import annotations

import json
import sqlite3
import sys
import zlib
from pathlib import Path

from ducai_studio.domain.jobs import JobKind, JobSpec
from ducai_studio.domain.timeline_edit import project_for_timeline
from ducai_studio.infrastructure.project_repository import (
    decode_project_snapshot,
    encode_project_snapshot,
)
from ducai_studio.job_manager.service import JobManagerService, _apply_ocr_cue_regions


def main():
    database, job_id, output = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]).resolve()
    assert not output.exists(), "Use a fresh verification directory"
    output.mkdir(parents=True)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        job = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        row = connection.execute("SELECT * FROM project_revisions "
                                 "WHERE project_id=? AND revision=?",
                                 (job["project_id"], job["input_revision"])).fetchone()
        raw = json.loads(zlib.decompress(row["snapshot_blob"])
                         if row["snapshot_encoding"] == "zlib-json-v1" else row["snapshot_json"])
        artifacts = {r["kind"]: dict(r) for r in connection.execute(
            "SELECT * FROM artifacts WHERE job_id=?", (job_id,))}
    original = decode_project_snapshot(raw)
    config = json.loads(job["config_json"])
    (output / "input-project.json").write_text(encode_project_snapshot(original)[0],
                                               encoding="utf-8")
    (output / "probe-config.json").write_text(json.dumps(config), encoding="utf-8")
    service = JobManagerService(database_path=output / "state.db",
                                artifact_root=output / "artifacts")
    service.projects.save(project_path=output / "verification.ducai", snapshot=original)
    service.jobs.create(JobSpec(JobKind.OCR, original.id, original.revision,
        job["provider_id"], job["provider_version"], job["model_id"], job["input_fingerprint"],
        job["settings_fingerprint"], config), job_id=job_id)
    checkpoints = [{"payload": {"stage": "review"}, "artifact": artifacts[k]}
                   for k in ("ocr_review_srt", "ocr_qa_json")]
    imported = service.import_review_ocr_srt(job_id, *checkpoints)
    reopened = service.projects.load(original.id)
    # The persistence encoder intentionally omits empty optional marker arrays.
    # Compare canonical snapshots, not the service's raw dataclass transport.
    assert encode_project_snapshot(reopened)[0] == encode_project_snapshot(
        decode_project_snapshot(imported)
    )[0]
    qa = json.loads(Path(artifacts["ocr_qa_json"]["uri"]).read_text(encoding="utf-8"))
    expected = _apply_ocr_cue_regions(service.media_imports.import_srt(
        original, artifacts["ocr_review_srt"]["uri"], language="zh"), qa)
    projected = project_for_timeline(reopened)
    actual_rows = {(c.text, c.range, c.role) for c in projected.source_cues}
    expected_rows = {(c.text, c.range, c.role) for c in expected.source_cues}
    assert actual_rows == expected_rows, (actual_rows - expected_rows, expected_rows - actual_rows)
    assert len(projected.source_cues) == len(expected.source_cues)
    (output / "result-project.json").write_text(encode_project_snapshot(reopened)[0],
                                                encoding="utf-8")
    checks = [[int((c.range.start_us + c.range.end_us) / 2000), c.text]
              for c in projected.source_cues if c.is_dialogue][:5]
    (output / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
    report = {"status": "PASS", "cue_count": len(actual_rows), "all_timestamp_parity": True,
              "save_reopen": True, "retained_start_us": config["scan_plan"]["segments"][0]
              ["base_start_us"], "scenes": [{"text": c.text, "start_us": c.range.start_us,
              "end_us": c.range.end_us, "state": c.review_state} for c in projected.source_cues
              if c.is_scene_text]}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
