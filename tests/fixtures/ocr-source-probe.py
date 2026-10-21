"""Run the real OCR pipeline on source excerpts without writing to the live DB."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from itertools import count
from pathlib import Path

from ducai_studio.application.media_commands import parse_srt
from ducai_studio.application.ocr_pipeline import group_visual_evidence
from ducai_studio.application.subtitle_finalizer import finalize_ocr_candidates
from ducai_studio.workers.ocr_worker import OcrWorker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("job_id")
    parser.add_argument("output", type=Path)
    parser.add_argument("--inspect-evidence", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT config_json FROM jobs WHERE id=?",
                                 (args.job_id,)).fetchone()
    original = json.loads(row[0])
    original = original.get("provider_config", original)
    segment = original["scan_plan"]["segments"][0]
    assert len(original["scan_plan"]["segments"]) == 1, "fixture requires one retained segment"
    os.environ["DUCAI_ARTIFACT_STAGING_ROOT"] = str(root / "staging")
    reports = []
    for name, start, end, expected in (
        ("complete-family", 0, 6, "我在嘉元城见到了墨老的家人"),
        ("split-row", 33, 39, "多亏了他们那个在七玄门的儿子"),
        ("fast-missing", 117, 124, "快来吃饭了"),
        ("row-order", 164, 173, "现在的我即使出现在你们面前"),
        ("wedding-partial", 180, 187, "今日见你出嫁"),
    ):
        start_us, end_us = start * 1_000_000, end * 1_000_000
        duration = end_us - start_us
        output = root / f"{name}_ZH_FINAL.srt"
        plan_segment = {**segment, "timeline_start_us": 0, "timeline_end_us": duration,
                        "base_start_us": 0, "base_end_us": duration,
                        "source_start_us": segment["source_start_us"] + start_us,
                        "source_end_us": segment["source_start_us"] + end_us}
        config = {**original, "scan_plan": {"schema_version": 1, "duration_us": duration,
                                           "segments": [plan_segment]},
                  "duration_us": duration, "output_path": str(output),
                  "staging_output_path": str(output),
                  "engine_host_path": str(Path(__file__).resolve().parents[2] /
                                           "src/ducai_studio/engine_hosts/ocr_engine_host.py")}
        worker = OcrWorker(name)
        events = []
        worker.send = lambda kind, payload, sink=events, **_kwargs: sink.append((kind, payload))
        spec = {"kind": "ocr", "provider_id": "paddle-fast-quality",
                "provider_config": config, "input_fingerprint": name,
                "settings_fingerprint": name}
        if args.inspect_evidence:
            worker.spec, worker.config = spec, config
            worker._configure_scan_plan()
            worker.evidence_path = max(worker.job_staging.glob("checkpoint-*.ndjson.partial"),
                                       key=lambda path: path.stat().st_mtime)
            fast, windows = worker._fast_candidates(duration, 400_000)
            final = worker._final_candidates(fast, windows, duration, 400_000)
            resolved = finalize_ocr_candidates(final, video_duration_us=duration,
                                               evidence=worker._load_evidence())
            def readings(candidates):
                return [{"text": item.text, "start_us": item.range.start_us,
                         "end_us": item.range.end_us, "source": item.source,
                         "review": list(item.review_reasons)}
                        for item in candidates]
            print(json.dumps({"excerpt": name, "fast": readings(fast),
                              "final": readings(final), "status": str(resolved.status),
                              "blockers": list(resolved.blockers),
                              "resolved_texts": [cue.text for cue in resolved.cues]},
                              ensure_ascii=False), flush=True)
            if name == "fast-missing":
                print(json.dumps({band: readings(group_visual_evidence(worker._load_evidence(),
                    band=band, duration_us=duration, sample_interval_us=400_000,
                    provider_prefix="paddle-fast")) for band in ("upper", "lower")},
                    ensure_ascii=False), flush=True)
            continue
        worker._configure({"job": spec})
        began = time.perf_counter()
        try:
            code = worker._execute()
        finally:
            if worker.engine:
                worker.engine.close()
        published = output if output.exists() else worker.job_staging / "ocr-review.srt.partial"
        identities = count()
        cues = parse_srt(published.read_text(encoding="utf-8-sig"), language="zh",
                         id_factory=lambda values=identities: str(next(values)))
        qa_path = (output.with_name(output.stem + "_QA.json") if output.exists()
                   else worker.job_staging / "ocr-review_QA.json.partial")
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        texts = [cue.text for cue in cues]
        report = {"excerpt": name, "timeline_start_us": start_us,
                  "source_start_us": plan_segment["source_start_us"],
                  "seconds": round(time.perf_counter() - began, 3), "worker_exit": code,
                  "expected": expected, "expected_found": expected in texts,
                  "cues": [{"start_us": cue.range.start_us + start_us,
                            "end_us": cue.range.end_us + start_us, "text": cue.text}
                           for cue in cues], "blockers": qa["blockers"],
                  "warnings": qa.get("warnings", [])}
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    (root / "source-probe.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
