"""Compare uninterrupted OCR candidate aggregation with checkpoint reconstruction."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from ducai_studio.application.subtitle_finalizer import finalize_ocr_candidates
from ducai_studio.workers.ocr_worker import OcrWorker, _sampling_intervals


def main():
    baseline, output = (Path(p).resolve() for p in sys.argv[1:3])
    assert not output.exists()
    output.mkdir(parents=True)
    os.environ["DUCAI_ARTIFACT_STAGING_ROOT"] = str(output / "staging")
    config = json.loads((baseline / "probe-config.json").read_text(encoding="utf-8"))
    ledger = baseline / "staging/scene-quality-verification/ocr-evidence-working.ndjson"
    rows = [json.loads(line) for line in ledger.open(encoding="utf-8")]
    quality = {"paddle-quality", "rapid-quality-fallback"}
    fast_rows = [row for row in rows if row.get("provider_id") not in quality]
    duration = config["scan_plan"]["duration_us"]
    fast_interval, quality_interval = _sampling_intervals(config["sample_interval_us"])
    reports = {}
    for label, initial in (("uninterrupted", fast_rows), ("checkpoint", rows)):
        worker = OcrWorker(label)
        worker._configure({"job": {"kind": "ocr", "provider_config": config}})
        worker._append_evidence(initial)
        candidates, windows = worker._fast_candidates(duration, fast_interval)
        if label == "uninterrupted":
            worker._append_evidence([row for row in rows if row.get("provider_id") in quality])
        final = worker._final_candidates(candidates, windows, duration, quality_interval)
        evidence = worker._load_evidence()
        result = finalize_ocr_candidates(final, video_duration_us=duration, evidence=evidence)
        reports[label] = {
            "fast": [asdict(c) for c in candidates],
            "windows": [asdict(w) for w in windows],
            "final": [asdict(c) for c in final],
            "result": [asdict(c) for c in result.cues],
            "blockers": result.blockers,
            "evidence": [asdict(e) for e in evidence if 60_000_000 <= e.timestamp_us < 68_000_000],
        }
        print(label, len(candidates), len(final), result.blockers, flush=True)
        for phase in ("fast", "final", "result"):
            print(phase, [(c["text"], c["range"]) for c in reports[label][phase]
                          if 60_000_000 <= c["range"]["start_us"] < 68_000_000], flush=True)
    (output / "report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    assert reports["uninterrupted"]["result"] == reports["checkpoint"]["result"]
    assert not reports["checkpoint"]["blockers"]


if __name__ == "__main__":
    main()
