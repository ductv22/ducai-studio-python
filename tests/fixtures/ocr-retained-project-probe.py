"""Real retained-video OCR with frozen read-only inputs and isolated artifacts/DB."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import zlib
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ducai_studio.application.media_commands import parse_srt
from ducai_studio.application.ocr_pipeline import OCR_AGGREGATION_VERSION
from ducai_studio.infrastructure.project_repository import (
    decode_project_snapshot,
    encode_project_snapshot,
)
from ducai_studio.job_manager.service import (
    JobManagerService,
    _apply_ocr_cue_regions,
    _merge_preserved_manual_subtitle_cues,
    build_scan_plan,
)
from ducai_studio.providers.ocr import DEFAULT_SUBTITLE_BANDS
from ducai_studio.workers.ocr_worker import OcrWorker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("job_id")
    parser.add_argument("output", type=Path)
    parser.add_argument("--collect-existing", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    assert args.collect_existing or not root.exists(), "Use a new isolated output directory"
    root.mkdir(parents=True, exist_ok=args.collect_existing)
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as live:
        live.row_factory = sqlite3.Row
        job = live.execute(
            "SELECT project_id,config_json FROM jobs WHERE id=?", (args.job_id,)
        ).fetchone()
        row = live.execute(
            "SELECT r.* FROM project_revisions r JOIN projects p ON p.id=r.project_id "
            "AND p.head_revision=r.revision WHERE p.id=?",
            (job["project_id"],),
        ).fetchone()
        raw = (
            zlib.decompress(row["snapshot_blob"])
            if row["snapshot_encoding"] == "zlib-json-v1"
            else row["snapshot_json"].encode("utf-8")
        )
        assert sha256(raw).hexdigest() == row["snapshot_sha256"]
    original = (
        decode_project_snapshot(
            json.loads((root / "input-project.json").read_text(encoding="utf-8"))
        )
        if args.collect_existing
        else decode_project_snapshot(json.loads(raw))
    )
    frozen, checksum = encode_project_snapshot(original)
    if not args.collect_existing:
        (root / "input-project.json").write_text(frozen, encoding="utf-8")
    clone = replace(
        original, id=str(uuid4()), revision=1, name=original.name + " — OCR verification"
    )
    service = JobManagerService(database_path=root / "state.db", artifact_root=root / "artifacts")
    if args.collect_existing:
        clone = service.projects.load(service.projects.list_summaries()[0]["id"])
    else:
        service.projects.save(project_path=root / "verification.ducai", snapshot=clone)
    config = json.loads(job["config_json"])
    config = config.get("provider_config", config)
    config.pop("engine_host_sha256", None)
    if original.ocr_dialogue_region is None:
        config["bands"] = [asdict(item) for item in DEFAULT_SUBTITLE_BANDS]
    else:
        x, y, width, height = original.ocr_dialogue_region
        config["bands"] = [
            {
                "name": "upper",
                "x1": x,
                "y1": y,
                "x2": x + width,
                "y2": y + height,
                "scale": 1.0,
                "reading_mode": "dialogue",
            }
        ]
    config.update(
        {
            "scan_plan": build_scan_plan(original),
            "ocr_aggregation_version": OCR_AGGREGATION_VERSION,
            "exclusion_regions": [list(item) for item in original.ocr_exclusion_regions],
            "engine_host_path": str(
                Path(__file__).resolve().parents[2]
                / "src/ducai_studio/engine_hosts/ocr_engine_host.py"
            ),
            "output_path": str(root / "retained_ZH_FINAL.srt"),
            "staging_output_path": str(root / "retained_ZH_FINAL.srt"),
        }
    )
    (root / "probe-config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fingerprint = sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    spec = {
        "kind": "ocr",
        "provider_id": "paddle-fast-quality",
        "provider_config": config,
        "input_fingerprint": checksum,
        "settings_fingerprint": fingerprint,
    }
    os.environ["DUCAI_ARTIFACT_STAGING_ROOT"] = str(root / "staging")
    worker = OcrWorker("full-retained-probe")
    began = time.perf_counter()

    def record(kind, payload, **_kwargs):
        event = {"seconds": round(time.perf_counter() - began, 3), "kind": kind, "payload": payload}
        with (root / "events.ndjson").open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(event, ensure_ascii=False) + "\n")
        if kind != "checkpoint":
            print(json.dumps(event, ensure_ascii=False), flush=True)

    worker.send = record
    if args.collect_existing:
        config = json.loads((root / "probe-config.json").read_text(encoding="utf-8"))
        worker.config, worker.spec = config, spec
        worker._configure_scan_plan()
        worker.stage = "quality"
        worker.cursor_us = config["scan_plan"]["duration_us"]
        worker.evidence_path = max(
            worker.job_staging.glob("checkpoint-*.ndjson.partial"),
            key=lambda path: path.stat().st_mtime,
        )
        code = worker._execute()
    else:
        worker._configure({"job": spec})
        try:
            code = worker._execute()
        finally:
            if worker.engine is not None:
                worker.engine.close()
    output = root / "retained_ZH_FINAL.srt"
    if not output.exists():
        output = worker.job_staging / "ocr-review.srt.partial"
        qa_path = worker.job_staging / "ocr-review_QA.json.partial"
    else:
        qa_path = output.with_name(output.stem + "_QA.json")
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    result = service.media_imports.import_srt(clone, str(output), language="zh")
    result = _apply_ocr_cue_regions(result, qa)
    result = service._canonicalize_ocr_result(result, config, watermarks_in_timeline=True)
    result = _merge_preserved_manual_subtitle_cues(clone, result)
    service.projects.save(project_path=root / "verification.ducai", snapshot=result)
    reopened = service.projects.load(clone.id)
    assert reopened == result
    (root / "result-project.json").write_text(
        encode_project_snapshot(reopened)[0], encoding="utf-8"
    )
    cues = parse_srt(
        output.read_text(encoding="utf-8-sig"), language="zh", id_factory=lambda: uuid4().hex
    )
    checks = []
    for timestamp, expected, bad in (
        (4_200_000, "在去太南小会的路上", "会的路H"),
        (23_300_000, "这里是散修们聚集的一处秘境", "这里住的"),
        (26_100_000, "交换了不少有用之物", "田物"),
    ):
        nearby = [
            cue
            for cue in cues
            if cue.range.start_us < timestamp + 1_000_000
            and cue.range.end_us > timestamp - 1_000_000
        ]
        checks.append(
            {
                "timestamp_us": timestamp,
                "expected_found": any(cue.text == expected for cue in nearby),
                "bad_absent": all(cue.text != bad for cue in nearby),
                "nearby": [asdict(cue) for cue in nearby],
            }
        )
    duplicate_pairs = [
        (a.index, b.index)
        for a, b in zip(cues, cues[1:], strict=False)
        if a.range.end_us > b.range.start_us
    ]
    elapsed_scan = [
        json.loads(line)["seconds"]
        for line in (root / "events.ndjson").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "succeeded"
    ]
    report = {
        "worker_exit": code,
        "seconds": max(elapsed_scan, default=round(time.perf_counter() - began, 3)),
        "input_revision": original.revision,
        "input_sha256": checksum,
        "duration_us": config["scan_plan"]["duration_us"],
        "status": qa["status"],
        "cue_count": len(cues),
        "scene_cues": sum(not cue.is_dialogue for cue in reopened.source_cues),
        "blockers": qa["blockers"],
        "warnings": qa.get("warnings", []),
        "coverage_ratio": qa["coverage_ratio"],
        "overlapping_pairs": duplicate_pairs,
        "checks": checks,
        "save_reopen": "PASS",
        "isolated_project_id": clone.id,
        "output_path": str(output),
        "qa_path": str(qa_path),
    }
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str(root / "report.json"),
                "status": qa["status"],
                "seconds": report["seconds"],
                "checks_pass": all(
                    item["expected_found"] and item["bad_absent"] for item in checks
                ),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
