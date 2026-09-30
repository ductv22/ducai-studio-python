---
title: "Phase 3: Scene-text detection and classification"
phase: 3
status: pending
priority: P1
effort: "8h"
dependencies: [1, 2]
---

# Phase 3: Scene-text detection and classification

## Overview

Detect full-frame character names, techniques, artifacts and location/title cards while keeping dialogue and persistent platform overlays separate.

## Requirements

- [x] Run a sparse full-frame detector pass at scene changes and bounded periodic anchors, followed by local burst confirmation.
- [x] Group horizontal lines left-to-right and vertical upright glyphs top-to-bottom.
- [x] Classify `dialogue`, `scene_text`, `platform_watermark` or `review` using geometry, persistence, corner position, text signature and temporal spread.
- [x] Preserve a106 fuzzy Bilibili rules; persistent corner text is never accepted as a character/title card.
- [x] Estimate the source glyph box, retained panel box, surrounding median color, orientation and style hint.

## Architecture

Keep scene evidence in a distinct checkpoint collection so subtitle coverage QA still measures only dialogue. Full-frame OCR is gated by scene changes, not applied at every sample. A classifier combines deterministic signals; low-confidence text becomes review evidence rather than an automatic render cue.

## Related Code Files

- Modify: `src/ducai_studio/engine_hosts/ocr_engine_host.py`
- Modify: `src/ducai_studio/workers/ocr_worker.py`
- Modify: `src/ducai_studio/application/ocr_pipeline.py`
- Modify: `src/ducai_studio/application/subtitle_finalizer.py`
- Modify: `src/ducai_studio/job_manager/service.py`
- Modify: OCR, repository and Job Manager tests under `tests/`

## Implementation Steps

1. Add scene-change/full-frame request mode without altering current two-band fast path.
2. Retain individual OCR segment boxes for vertical grouping and panel estimation.
3. Add deterministic watermark-versus-scene classification and recurrence evidence.
4. Publish accepted scene cues alongside dialogue cues with stable role metadata.
5. Add the supplied `海王兽`/`bilibili 独播` image as a regression fixture or an equivalent synthetic fixture with the same geometry.

## Todo

- [x] `海王兽` is grouped top-to-bottom as one scene cue.
- [x] `bilibili 独播` is excluded and may contribute to watermark concealment.
- [x] A transient ordinary dialogue line away from the bottom is not silently treated as a watermark.
- [x] Stylized low-confidence text remains visible in the review list.

## Success Criteria

- [x] Full-frame detection catches both horizontal and vertical scene labels.
- [x] Dialogue coverage QA and S1 counts remain independent from scene-text evidence.
- [x] Sparse scanning has a measured, bounded cost relative to a106 OCR.

## Risk Assessment

No deterministic classifier can maximize recall and precision on arbitrary decorative text. If review false positives exceed the fixture budget, tighten automatic acceptance and retain candidates for manual approval instead of deleting or rendering them.
