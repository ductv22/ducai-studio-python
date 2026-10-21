"""Measure the real OCR worker on frozen input, with optional serial comparison.

All outputs are isolated. This fixture never connects to the live database and
never supplies synthetic OCR, lowers sampling or hides REVIEW output.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from hashlib import file_digest, sha256
from itertools import zip_longest
from math import isfinite
from pathlib import Path

from ducai_studio.workers.ocr_worker import OcrWorker


def _numeric_deltas(before, after, sizes, dimensions_known, audit, changes, group):
    geometry_delta = confidence_delta = 0.0
    for key in ("confidence", "scene_confidence"):
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        audit[f"{key}_changed_regions"] += 1
        if (type(a) in (int, float) and type(b) in (int, float)
                and isfinite(a) and isfinite(b)):
            confidence_delta = max(confidence_delta, abs(a - b))
        else:
            changes.append(f"{group}.{key}")
    for key in ("box", "panel_box"):
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        audit[f"{key}_changed_regions"] += 1
        if (not dimensions_known or not isinstance(a, list) or not isinstance(b, list)
                or len(a) != 4 or len(b) != 4
                or not all(type(v) in (int, float) and isfinite(v) for v in (*a, *b))):
            changes.append(f"{group}.{key}")
            continue
        width, height = sizes[0]
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        geometry_delta = max(geometry_delta, abs(ax - bx) * width,
            abs(ay - by) * height, abs((ax + aw) - (bx + bw)) * width,
            abs((ay + ah) - (by + bh)) * height)
    return geometry_delta, confidence_delta


def _evidence_path(root: Path, report: dict) -> Path | None:
    if report.get("raw_evidence_path"):
        path = Path(report["raw_evidence_path"])
        return path if path.is_file() else None
    # Earlier isolated runs did not record this path. Never substitute a
    # checkpoint subset for the complete working evidence stream.
    paths = list((root / "staging").glob("*/ocr-evidence-working.ndjson"))
    return paths[0] if len(paths) == 1 else None


def _evidence_rows(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("raw_evidence_row_must_be_object")
            yield row


def _compare_evidence(root, current, baseline, previous, sizes, dimensions_known):
    paths = (_evidence_path(baseline, previous), _evidence_path(root, current))
    counts = {"baseline": 0, "candidate": 0}
    changes = []
    audit = Counter()
    geometry_delta = confidence_delta = 0.0
    available = all(path is not None for path in paths)
    error = None
    if available:
        try:
            for before, after in zip_longest(*(_evidence_rows(path) for path in paths)):
                counts["baseline"] += before is not None
                counts["candidate"] += after is not None
                if before is None or after is None:
                    continue
                # Text, identity, source, kind and sequence are exact contracts.
                # Compare by row order; sorting would conceal reordered evidence.
                semantic_keys = (before.keys() | after.keys()) - {
                    "box", "panel_box", "confidence", "scene_confidence",
                }
                changes.extend(k for k in semantic_keys if before.get(k) != after.get(k))
                geometry, confidence = _numeric_deltas(before, after, sizes,
                    dimensions_known, audit, changes, "raw_evidence")
                geometry_delta = max(geometry_delta, geometry)
                confidence_delta = max(confidence_delta, confidence)
        except (OSError, ValueError) as exc:
            available = False
            error = f"{type(exc).__name__}: {exc}"
    return {"raw_evidence_available": available, "raw_evidence_error": error,
            "raw_evidence_count_equal": available and counts["baseline"] == counts["candidate"],
            "raw_evidence_rows": counts, "semantic_evidence_changes": sorted(set(changes)),
            "numeric_evidence_audit": {
                **{key.replace("_changed_regions", "_changed_rows"): value
                   for key, value in audit.items()},
                "maximum_confidence_delta": confidence_delta},
            "maximum_evidence_edge_delta_pixels": geometry_delta,
            "raw_evidence_geometry_within_two_pixels": geometry_delta <= 2.0}


def compare_runs(root: Path, baseline: Path) -> dict:
    current = json.loads((root / "report.json").read_text(encoding="utf-8"))
    previous = json.loads((baseline / "report.json").read_text(encoding="utf-8"))
    current_qa = json.loads(Path(current["qa_path"]).read_text(encoding="utf-8"))
    previous_qa = json.loads(Path(previous["qa_path"]).read_text(encoding="utf-8"))
    run_metadata = {"job_id", "output_path", "output_sha256", "output_size", "output_mtime_ns"}
    region_keys = ("cue_regions", "watermark_regions")
    decision_keys = (current_qa.keys() | previous_qa.keys()) - run_metadata - set(region_keys)
    decision_changes = sorted(k for k in decision_keys if current_qa.get(k) != previous_qa.get(k))
    decisions_equal = not decision_changes
    changes = []
    geometry_delta = 0.0
    numeric_audit = Counter()
    confidence_delta = 0.0
    sizes = current["frame_sizes"]
    dimensions_known = len(sizes) == 1 and sizes == previous["frame_sizes"]
    region_count_equal = True
    for group in region_keys:
        previous_regions = previous_qa.get(group, [])
        current_regions = current_qa.get(group, [])
        region_count_equal &= len(current_regions) == len(previous_regions)
        for before, after in zip(previous_regions, current_regions, strict=False):
            semantic_keys = (before.keys() | after.keys()) - {"box", "panel_box", "confidence",
                                                           "scene_confidence"}
            changes.extend(f"{group}.{k}" for k in semantic_keys if before.get(k) != after.get(k))
            geometry, confidence = _numeric_deltas(before, after, sizes,
                dimensions_known, numeric_audit, changes, group)
            geometry_delta = max(geometry_delta, geometry)
            confidence_delta = max(confidence_delta, confidence)
    evidence = _compare_evidence(root, current, baseline, previous, sizes, dimensions_known)
    same_input = current["configuration_fingerprint"] == previous["configuration_fingerprint"]
    same_srt = current["srt_sha256"] == previous["srt_sha256"]
    geometry_within_tolerance = geometry_delta <= 2.0
    parity = (same_input and same_srt and decisions_equal and region_count_equal
              and not changes and geometry_within_tolerance
              and evidence["raw_evidence_available"] and evidence["raw_evidence_count_equal"]
              and not evidence["semantic_evidence_changes"]
              and evidence["raw_evidence_geometry_within_two_pixels"])
    return {"same_input": same_input, "same_srt": same_srt,
            "qa_decisions_equal": decisions_equal, "region_count_equal": region_count_equal,
            "qa_decision_changes": decision_changes,
            "semantic_region_changes": sorted(set(changes)),
            "numeric_region_audit": {**numeric_audit, "maximum_confidence_delta": confidence_delta},
            "maximum_region_edge_delta_pixels": geometry_delta,
            "geometry_within_two_pixels": geometry_within_tolerance,
            **evidence,
            "parity": parity, "baseline_seconds": previous["seconds"],
            "candidate_seconds": current["seconds"],
            "speedup": previous["seconds"] / current["seconds"],
            "time_saved_percent": 100 * (1 - current["seconds"] / previous["seconds"])}


def compare_exact_runs(root: Path, baseline: Path) -> dict:
    """Stricter execution-only gate: no confidence/geometry tolerance."""
    comparison = compare_runs(root, baseline)
    reports = [json.loads((directory / "report.json").read_text(encoding="utf-8"))
               for directory in (baseline, root)]
    metadata = {"job_id", "output_path", "output_sha256", "output_size", "output_mtime_ns"}
    qa_hashes, evidence_hashes, srt_hashes = [], [], []
    for directory, report in zip((baseline, root), reports, strict=True):
        qa = json.loads(Path(report["qa_path"]).read_text(encoding="utf-8"))
        normalized = {key: value for key, value in qa.items() if key not in metadata}
        qa_hashes.append(sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")).encode()).hexdigest())
        evidence_path = _evidence_path(directory, report)
        srt_path = Path(report["srt_path"]) if report.get("srt_path") else None
        for path, hashes in ((evidence_path, evidence_hashes), (srt_path, srt_hashes)):
            if path is None or not path.is_file():
                hashes.append(None)
            else:
                with path.open("rb") as stream:
                    hashes.append(file_digest(stream, "sha256").hexdigest())
    evidence_equal = evidence_hashes[0] is not None and evidence_hashes[0] == evidence_hashes[1]
    srt_equal = srt_hashes[0] is not None and srt_hashes[0] == srt_hashes[1]
    report_hashes_valid = all(report.get("srt_sha256") == digest and digest is not None
                              for report, digest in zip(reports, srt_hashes, strict=True))
    exact = (comparison["parity"] and evidence_equal and srt_equal and report_hashes_valid
             and qa_hashes[0] == qa_hashes[1]
             and comparison["raw_evidence_rows"]["baseline"] > 0)
    return {**comparison, "tolerant_parity": comparison["parity"], "parity": exact,
            "exact_raw_evidence": evidence_equal,
            "exact_normalized_qa": qa_hashes[0] == qa_hashes[1],
            "exact_srt_bytes": srt_equal, "srt_report_hashes_valid": report_hashes_valid,
            "raw_evidence_sha256": evidence_hashes, "normalized_qa_sha256": qa_hashes,
            "actual_srt_sha256": srt_hashes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("--detector-batch-size", type=int, choices=(1, 2, 4))
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--compare-only", action="store_true")
    parser.add_argument("--exact", action="store_true")
    parser.add_argument("--runtime-host", type=Path)
    args = parser.parse_args()
    compare = compare_exact_runs if args.exact else compare_runs
    if args.serial and args.detector_batch_size not in (None, 1):
        parser.error("--serial cannot be combined with a detector batch larger than one")
    root = args.output.resolve()
    if args.compare_only:
        if args.compare is None:
            parser.error("--compare-only requires --compare")
        comparison = compare(root, args.compare.resolve())
        (root / "strict-comparison.json").write_text(json.dumps(comparison, indent=2),
                                                  encoding="utf-8")
        print(json.dumps(comparison, indent=2), flush=True)
        return 0 if comparison["parity"] else 1
    if root.exists():
        parser.error("Use a fresh isolated output directory")
    root.mkdir(parents=True)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    frozen = {k: v for k, v in config.items() if k not in {
        "output_path", "staging_output_path", "engine_host_path",
    }}
    fingerprint = sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()
    target = root / "benchmark_ZH_FINAL.srt"
    config.update(output_path=str(target), staging_output_path=str(target),
                  engine_host_path=str(Path(__file__).with_name("ocr-trace-host.py")))
    os.environ["DUCAI_ARTIFACT_STAGING_ROOT"] = str(root / "staging")
    os.environ["DUCAI_OCR_PROFILE_PATH"] = str(root / "host-timing.ndjson")
    os.environ.pop("DUCAI_OCR_RAW_PROFILE_PATH", None)
    if args.serial:
        os.environ["DUCAI_OCR_TRACE_SERIAL_PADDLE"] = "1"
    else:
        os.environ.pop("DUCAI_OCR_TRACE_SERIAL_PADDLE", None)
    if args.detector_batch_size is not None:
        os.environ["DUCAI_OCR_TRACE_DETECTOR_BATCH_SIZE"] = str(args.detector_batch_size)
    else:
        os.environ.pop("DUCAI_OCR_TRACE_DETECTOR_BATCH_SIZE", None)
    if args.runtime_host:
        os.environ["DUCAI_OCR_TRACE_RUNTIME_PATH"] = str(args.runtime_host.resolve())
    else:
        os.environ.pop("DUCAI_OCR_TRACE_RUNTIME_PATH", None)
    (root / "probe-config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    worker = OcrWorker("ocr-speed-benchmark")
    events = Counter()
    stage_last = {}
    began = time.perf_counter()

    def record(kind, payload, **_kwargs):
        if kind == "progress":
            phase = payload.get("phase", "")
            events[phase] += 1
            stage_last[phase] = payload
            if phase != "adaptive_ocr" and (
                events[phase] % 10 == 0 or payload.get("current") == payload.get("total")
            ):
                print(json.dumps({"seconds": round(time.perf_counter() - began, 3),
                                  "progress": payload}), flush=True)

    worker.send = record
    worker._configure({"job": {"kind": "ocr", "provider_id": "paddle-fast-quality",
        "provider_config": config, "input_fingerprint": fingerprint,
        "settings_fingerprint": fingerprint}})
    try:
        code = worker._execute()
    finally:
        if worker.engine is not None:
            worker.engine.close()
    seconds = time.perf_counter() - began
    qa_path = target.with_name(target.stem + "_QA.json")
    if not target.is_file():
        target = worker.job_staging / "ocr-review.srt.partial"
        qa_path = worker.job_staging / "ocr-review_QA.json.partial"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            (root / "host-timing.ndjson").open(encoding="utf-8")]
    report = {"seconds": seconds, "exit_code": code, "serial": args.serial,
        "detector_batch_size_override": args.detector_batch_size,
        "configuration_fingerprint": fingerprint,
        "duration_us": config.get("scan_plan", {}).get("duration_us", config["duration_us"]),
        "srt_path": str(target), "qa_path": str(qa_path),
        "raw_evidence_path": str(worker.evidence_path),
        "srt_sha256": sha256(target.read_bytes()).hexdigest(),
        "qa_status": qa["status"], "cue_count": qa["cue_count"], "blockers": qa["blockers"],
        "host_timing": {k: sum(r["timing"][k] for r in rows)
                        for k in ("decode", "inference", "total")},
        "host_requests": len(rows), "event_counts": dict(events), "stages": stage_last,
        "execution_workers": sorted({row.get("worker_count", 1) for row in rows}),
        "execution_fallback_reasons": sorted({row.get("execution_fallback_reason", "")
                                               for row in rows} - {""}),
        "frame_sizes": sorted({tuple(r["frame_size"]) for r in rows})}
    (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.compare:
        report["comparison"] = compare(root, args.compare.resolve())
        (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 1 if args.compare and not report["comparison"]["parity"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
