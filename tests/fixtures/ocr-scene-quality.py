"""Run the real OCR pipeline from frozen inputs, never open the production DB."""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ducai_studio.application.ocr_pipeline import OCR_AGGREGATION_VERSION
from ducai_studio.domain.timeline_edit import project_for_timeline
from ducai_studio.infrastructure.project_repository import (
    decode_project_snapshot,
    encode_project_snapshot,
)
from ducai_studio.job_manager.service import JobManagerService, _apply_ocr_cue_regions
from ducai_studio.workers.ocr_worker import OcrWorker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--reuse-evidence", type=Path)
    parser.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--raw-profile", action="store_true")
    parser.add_argument("--checks-json", type=Path)
    parser.add_argument("--report-review", action="store_true")
    args = parser.parse_args()
    baseline, root = args.baseline.resolve(), args.output.resolve()
    assert not root.exists(), "Use a fresh isolated directory"
    root.mkdir(parents=True)
    original = decode_project_snapshot(json.loads(
        (baseline / "input-project.json").read_text(encoding="utf-8")))
    config = json.loads((baseline / "probe-config.json").read_text(encoding="utf-8"))
    config.update({
        "device": args.device, "ocr_aggregation_version": OCR_AGGREGATION_VERSION,
        "output_path": str(root / "retained_ZH_FINAL.srt"),
        "staging_output_path": str(root / "retained_ZH_FINAL.srt"),
    })
    if args.profile:
        config["engine_host_path"] = str(Path(__file__).with_name("ocr-trace-host.py"))
        os.environ["DUCAI_OCR_PROFILE_PATH"] = str(root / "host-profile.ndjson")
        if args.raw_profile:
            os.environ["DUCAI_OCR_RAW_PROFILE_PATH"] = str(root / "raw-readings.ndjson")
    if args.duration:
        assert len(config["scan_plan"]["segments"]) == 1
        offset = int(args.start * 1_000_000)
        duration = min(int(args.duration * 1_000_000),
                       config["scan_plan"]["duration_us"] - offset)
        assert duration > 0 and offset >= 0
        config["scan_plan"]["duration_us"] = duration
        segment = config["scan_plan"]["segments"][0]
        ratio = (segment["source_end_us"] - segment["source_start_us"]) / (
            segment["timeline_end_us"] - segment["timeline_start_us"])
        segment["source_start_us"] += int(offset * ratio)
        segment["source_end_us"] = segment["source_start_us"] + int(duration * ratio)
        for key in ("timeline_end_us", "base_end_us", "uncut_end_us"):
            segment[key] = segment[key.replace("end", "start")] + duration
    (root / "probe-config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (root / "input-project.json").write_text(encode_project_snapshot(original)[0], encoding="utf-8")
    os.environ["DUCAI_ARTIFACT_STAGING_ROOT"] = str(root / "staging")
    worker = OcrWorker("scene-quality-verification")
    if args.profile:
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()
    began = time.perf_counter()

    def record(kind, payload, **kwargs):
        event = {"seconds": round(time.perf_counter() - began, 3), "kind": kind, "payload": payload}
        with (root / "events.ndjson").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        if kind != "checkpoint":
            print(json.dumps(event, ensure_ascii=False), flush=True)

    worker.send = record
    worker._configure({"job": {"kind": "ocr", "provider_config": config}})
    if args.reuse_evidence:
        working = (args.reuse_evidence
                   / "staging/scene-quality-verification/ocr-evidence-working.ndjson")
        rows = [json.loads(line) for line in working.open(encoding="utf-8")]
        rows = [row for row in rows if args.finalize_only or not (
            "quality" in str(row.get("provider_id", ""))
            and (row.get("evidence_kind") == "scene" or row.get("record_kind") == "scene_probe")
        )]
        worker._append_evidence(rows)
        worker.stage = "scene_quality" if args.finalize_only else "quality"
        worker.cursor_us = config["scan_plan"]["duration_us"]
    try:
        code = worker._execute()
    finally:
        if worker.engine is not None:
            worker.engine.close()
        if args.profile:
            profiler.disable()
            profiler.dump_stats(str(root / "worker-profile.pstats"))
    source = root / "retained_ZH_FINAL.srt"
    qa_path = source.with_name(source.stem + "_QA.json")
    if not source.exists():
        source = worker.job_staging / "ocr-review.srt.partial"
        qa_path = worker.job_staging / "ocr-review_QA.json.partial"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    service = JobManagerService(database_path=root / "state.db", artifact_root=root / "artifacts")
    clone = replace(original, id=str(uuid4()), revision=1)
    service.projects.save(project_path=root / "verification.ducai", snapshot=clone)
    result = _apply_ocr_cue_regions(service.media_imports.import_srt(
        clone, str(source), language="zh"), qa)
    result = service._canonicalize_ocr_result(result, config, watermarks_in_timeline=True)
    service.projects.save(project_path=root / "verification.ducai", snapshot=result)
    assert service.projects.load(clone.id) == result
    (root / "result-project.json").write_text(encode_project_snapshot(result)[0], encoding="utf-8")
    projected = project_for_timeline(result)
    dialogue = tuple(c for c in projected.source_cues if c.is_dialogue)
    scenes = tuple(c for c in projected.source_cues if c.is_scene_text)
    assert all(c.text_font_size == 40 for c in scenes)
    overlaps = [(a.index, b.index) for i, a in enumerate(dialogue) for b in dialogue[i + 1:]
                if a.range.start_us < b.range.end_us and b.range.start_us < a.range.end_us]
    if overlaps:
        assert args.report_review and qa["status"] == "REVIEW", overlaps
        assert any(b.startswith("cue_overlap:") for b in qa["blockers"])
    checks = {}
    expected_checks = [(4_200_000, "在去太南小会的路上"),
                       (23_300_000, "这里是散修们聚集的一处秘境"),
                       (26_100_000, "交换了不少有用之物")]
    if args.checks_json:
        expected_checks = [(t * 1000, text) for t, text in json.loads(
            args.checks_json.read_text(encoding="utf-8"))]
    for timestamp, expected in expected_checks:
        checks[str(timestamp)] = [c.text for c in dialogue if
                                  c.range.start_us <= timestamp < c.range.end_us]
        assert checks[str(timestamp)] == [expected], checks
    report = {"exit_code": code, "status": qa["status"], "dialogue_checks": checks,
              "duration_us": config["scan_plan"]["duration_us"],
              "seconds": round(time.perf_counter() - began, 3),
              "dialogue_cues": len(dialogue), "scene_cues": len(scenes),
              "save_reopen": "PASS", "scene_sizes": [40],
              "blockers": qa["blockers"], "dialogue_overlaps": overlaps,
              "scenes": [{"text": c.text, "start_us": c.range.start_us,
                          "end_us": c.range.end_us, "box": c.visual_box,
                          "review_state": c.review_state} for c in scenes]}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
