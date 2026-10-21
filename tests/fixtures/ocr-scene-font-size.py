"""Replay real OCR QA/SRT imports in an isolated DB; never re-run recognition."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ducai_studio.infrastructure.project_repository import (
    decode_project_snapshot,
    encode_project_snapshot,
)
from ducai_studio.job_manager.service import (
    JobManagerService,
    _apply_ocr_cue_regions,
    _translation_cue_payload,
)
from ducai_studio.ui.viewmodels.project_job_models import SubtitleCueListModel
from ducai_studio.workers.ocr_worker import OcrWorker
from ducai_studio.workers.translation_worker import _cue_from_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    baseline, output = args.baseline.resolve(), args.output.resolve()
    assert not output.exists(), "Use a new isolated output directory"
    output.mkdir(parents=True)
    original = decode_project_snapshot(json.loads(
        (baseline / "input-project.json").read_text(encoding="utf-8")))
    clone = replace(original, id=str(uuid4()), revision=1)
    source = baseline / "retained_ZH_FINAL.srt"
    qa = json.loads((baseline / "retained_ZH_FINAL_QA.json").read_text(encoding="utf-8"))
    service = JobManagerService(database_path=output / "state.db",
                                artifact_root=output / "artifacts")
    imported = service.media_imports.import_srt(clone, str(source), language="zh")
    updated = _apply_ocr_cue_regions(imported, qa)
    scenes = tuple(cue for cue in updated.source_cues if cue.is_scene_text)
    dialogue = tuple(cue for cue in updated.source_cues if cue.is_dialogue)
    assert scenes and all(cue.text_font_size == 40 for cue in scenes)
    assert dialogue and all(cue.text_font_size == 12 for cue in dialogue)
    assert len(updated.source_cues) == len(imported.source_cues)
    assert all(a.text == b.text and a.range == b.range
               for a, b in zip(imported.source_cues, updated.source_cues, strict=True))
    service.projects.save(project_path=output / "scene-font-size.ducai", snapshot=updated)
    assert service.projects.load(clone.id) == updated
    model = SubtitleCueListModel()
    model.replace(json.loads(encode_project_snapshot(updated)[0])["source_cues"], [])
    assert all(model.find(cue.id)["text_font_size"] == 40 for cue in scenes)
    assert all(_cue_from_config(_translation_cue_payload(cue)).text_font_size == 40
               for cue in scenes)
    os.environ["DUCAI_ARTIFACT_STAGING_ROOT"] = str(output / "staging")
    worker = OcrWorker("scene-font-size-replay")
    worker.config = {"duration_us": original.media[0].duration_us}
    generated_qa = worker._qa_payload(
        status=qa["status"], output_path=source, cue_count=len(updated.source_cues),
        cues=updated.source_cues, blockers=qa["blockers"], warnings=qa.get("warnings", []),
        coverage={
            "minimum_coverage": qa["coverage_threshold"],
            **{key: qa[key] for key in (
                "coverage_ratio", "subtitle_positive_timestamps", "covered_timestamps",
                "continuity_gap_limit_us", "maximum_positive_gap_us",
                "timeline_continuity_passes", "unmatched_samples",
            )},
        },
    )
    restored = _apply_ocr_cue_regions(imported, generated_qa)
    assert tuple(cue.text_font_size for cue in restored.source_cues) == tuple(
        cue.text_font_size for cue in updated.source_cues)
    report = {
        "status": "PASS", "baseline": str(baseline), "total_cues": len(updated.source_cues),
        "scene_cues": len(scenes), "scene_font_sizes": sorted({c.text_font_size for c in scenes}),
        "dialogue_cues": len(dialogue), "dialogue_font_sizes": [12],
        "text_and_timestamps_unchanged": True, "save_reopen": "PASS",
        "translation_input_preserves_size": True, "worker_qa_round_trip": "PASS",
        "ui_scene_sizes": [40],
        "recognition_rerun": False, "production_state_modified": False,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
