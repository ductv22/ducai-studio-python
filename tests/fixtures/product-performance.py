"""Isolated exact-output benchmarks; no user data, engines or network access."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
from pathlib import Path
from time import perf_counter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--count", type=int, default=12_000)
    args = parser.parse_args()
    if args.source_root:
        sys.path.insert(0, str(args.source_root.resolve()))

    from PySide6.QtCore import QCoreApplication

    from ducai_studio.application.subtitle_commands import SubtitleEditor
    from ducai_studio.domain.project import SubtitleCue
    from ducai_studio.domain.timebase import TimeRange
    from ducai_studio.domain.voice_conform import (
        VoiceConformPlan,
        VoiceConformSegment,
        VoiceConformSettings,
    )
    from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel

    app = QCoreApplication.instance() or QCoreApplication([])
    count = args.count
    second = 1_000_000
    segments = []
    cursor = 0
    for index in range(count):
        duration = second if index % 2 else 1_500_000
        segments.append(VoiceConformSegment(index * second, (index + 1) * second,
                                          cursor, cursor + duration, second / duration))
        cursor += duration
    plan = VoiceConformPlan(1, VoiceConformSettings(), count * second, cursor,
                            tuple(segments), (), (), "isolated-performance")
    results = {"count": count, "benchmarks": {}}

    def measure(name, action, repeats=3):
        timings, identities = [], []
        for _ in range(repeats):
            gc.collect()
            start = perf_counter()
            value = action()
            timings.append((perf_counter() - start) * 1000)
            identities.append(hashlib.sha256(json.dumps(value, ensure_ascii=False,
                                                       sort_keys=True).encode()).hexdigest())
        assert len(set(identities)) == 1
        results["benchmarks"][name] = {"ms": timings, "sha256": identities[0]}

    positions = [(index * 7919 % count) * second + 345_678 for index in range(1200)]
    measure("conform_time_queries", lambda: [
        (plan.source_to_output_us(position),
         plan.output_to_source_us(plan.source_to_output_us(position))) for position in positions
    ])
    cues = tuple(SubtitleCue(f"cue-{index}", index + 1,
                             TimeRange(index * second, index * second + 900_000),
                             f"Dialogue {index}", "vi") for index in range(count))
    measure("normalize_unchanged_cues", lambda: [
        (cue.id, cue.index, cue.text) for cue in SubtitleEditor._normalize_cues(cues)
    ])
    raw_cues = [{"id": cue.id, "index": cue.index, "text": cue.text,
                 "range": {"start_us": cue.range.start_us, "end_us": cue.range.end_us}}
                for cue in cues]
    snapshot = {"id": "isolated-performance", "revision": 1, "name": "Performance",
                "voice_conform": {"plan": plan.as_json()},
                "source_cues": raw_cues, "translated_cues": raw_cues,
                "media": [{"id": "media", "path": "not-opened.mp4", "kind": "video"}],
                "tracks": [{"id": "V1", "kind": "video", "clips": [
                    {"id": "video", "track_id": "V1", "media_id": "media",
                     "timeline_range": {"start_us": 0, "end_us": count * second},
                     "source_range": {"start_us": 0, "end_us": count * second}}]},
                    {"id": "S1", "kind": "subtitle", "clips": [
                        {"id": cue["id"], "track_id": "S1", "cue_id": cue["id"],
                         "timeline_range": cue["range"]} for cue in raw_cues]}]}
    model = ProjectStateModel()

    def load():
        model.replace(snapshot, take_ownership=True)
        return [(cue["cue_id"], cue["start_us"], cue["end_us"])
                for cue in model.subtitle_cues._items]

    measure("load_conformed_project", load, repeats=1)
    measure("conformed_preview_descriptors", lambda: [
        model.previewDescriptor(plan.source_to_output_us(position)) for position in positions
    ])
    title_model = ProjectStateModel()
    title_model.replace({"id": "title-performance", "revision": 1,
                         "title_cues": [{**cue, "role": "title_text",
                                         "subtitle_track_id": "T1"} for cue in raw_cues]})
    measure("title_preview_queries", lambda: [
        [cue["cue_id"] for cue in title_model.titleTextsAt(position)]
        for position in positions[:300]
    ])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results))
    app.processEvents()


if __name__ == "__main__":
    main()
