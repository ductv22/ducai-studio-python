"""Replay final dialogue against immutable real evidence, without inference/DB writes."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from itertools import count
from pathlib import Path

from ducai_studio.application.media_commands import parse_srt
from ducai_studio.application.subtitle_finalizer import finalize_ocr_candidates
from ducai_studio.domain.subtitles import SubtitleCandidate
from ducai_studio.workers.ocr_worker import OcrWorker


def main():
    baseline, output = map(lambda p: Path(p).resolve(), sys.argv[1:3])
    output.mkdir(parents=True, exist_ok=True)
    os.environ["DUCAI_ARTIFACT_STAGING_ROOT"] = str(output / "staging")
    worker = OcrWorker("replay")
    worker.config = json.loads((baseline / "probe-config.json").read_text(encoding="utf-8"))
    ledger = baseline / "staging/scene-quality-verification"
    worker.evidence_path = ledger / "ocr-evidence-working.ndjson"
    qa = json.loads((ledger / "ocr-review_QA.json.partial").read_text(encoding="utf-8"))
    ids = count()
    cues = parse_srt((ledger / "ocr-review.srt.partial").read_text(encoding="utf-8"),
                     language="zh", id_factory=lambda: str(next(ids)))
    regions = {row["index"]: row for row in qa["cue_regions"]}
    candidates = tuple(SubtitleCandidate(
        c.id, c.range, c.text, regions[c.index]["confidence"],
        tuple(regions[c.index]["evidence_ids"]), "ocr",
    ) for c in cues if regions[c.index]["role"] == "dialogue")
    evidence = worker._load_evidence()
    result = finalize_ocr_candidates(candidates,
        video_duration_us=worker.config["scan_plan"]["duration_us"], evidence=evidence)
    # The map injected prefixes into both candidates. Replace those two with
    # the repeatedly observed clean row, preserving every other tuple.
    before = {(c.text, c.range) for c in candidates}
    after = {(c.text, c.range) for c in result.cues}
    removed = before - after
    from ducai_studio.domain.timebase import TimeRange
    assert removed == {
        ("五指中心区域有一圈环形山脉", TimeRange(776_900_000, 780_100_000)),
        ("1福五指湖区域有一圈环形山脉", TimeRange(777_700_000, 779_900_000)),
    }, removed
    assert after - before == {
        ("中心区域有一圈环形山脉", TimeRange(776_900_000, 780_100_000)),
    }, after - before
    assert len(before & after) == len(candidates) - 2
    restored = next(c for c in result.cues if c.range.start_us == 776_900_000)
    assert restored.visual_box[1] > .89 and restored.visual_box[3] < .08
    assert any(w == "same_frame_mixed_rows_collapsed:1" for w in result.warnings)
    assert not any(b.startswith("cue_overlap:") for b in result.blockers), result.blockers
    # Source/segment boundaries and frame identity are required for this decision.
    separate = tuple(replace(row, source_scope="other")
                     if row.timestamp_us >= 777_700_000 and row.confidence < .90
                     else row for row in evidence)
    unproven = finalize_ocr_candidates(candidates,
        video_duration_us=worker.config["scan_plan"]["duration_us"], evidence=separate)
    assert len(unproven.cues) == len(candidates)
    report = {"status": "PASS", "before": len(candidates), "after": len(result.cues),
              "removed": [{"text": text, "range": [r.start_us, r.end_us]}
                          for text, r in removed],
              "restored": restored.text,
              "unchanged_dialogue_tuples": len(before & after), "warnings": result.warnings,
              "scope_boundary_preserved": True, "new_inference": False}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
