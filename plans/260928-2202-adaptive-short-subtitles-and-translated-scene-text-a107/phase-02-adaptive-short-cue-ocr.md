---
title: "Phase 2: Adaptive short-cue OCR"
phase: 2
status: pending
priority: P1
effort: "6h"
dependencies: [1]
---

# Phase 2: Adaptive short-cue OCR

## Overview

Recover brief one- and two-character dialogue without multiplying full Paddle inference across the whole video.

## Requirements

- [x] Keep the global sampling control at 0.67–2.5 FPS and add inexpensive subtitle-band visual-change probes between OCR samples.
- [x] Trigger bounded 3–5-frame Paddle bursts around appearance/change events.
- [x] Permit one-character dialogue only inside subtitle bands with high confidence, stable geometry and at least two agreeing burst frames.
- [x] Permit two-character dialogue from burst consensus even when the global sampler sees it once.
- [x] Route uncertain short candidates through the existing Quality rescue path; never accept a single isolated glyph automatically.

## Architecture

Decode sequentially as today. Compute grayscale/downscaled band-difference scores without Paddle inference. Add event timestamps only when the image change crosses a tested threshold, de-duplicate them against regular samples and submit the bounded crop batch through the persistent Paddle host. Generalize consensus thresholds by CJK length and evidence kind.

## Related Code Files

- Modify: `src/ducai_studio/engine_hosts/ocr_engine_host.py`
- Modify: `src/ducai_studio/workers/ocr_worker.py`
- Modify: `src/ducai_studio/application/ocr_pipeline.py`
- Modify: `tests/ocr/test_ocr_pipeline.py`
- Modify: `tests/ocr/test_ocr_engine_host.py`
- Modify: `tests/integration/test_ocr_worker.py`

## Implementation Steps

1. Add deterministic band-change scoring and bounded event timestamp generation.
2. Persist burst evidence and timing origin in OCR checkpoints so pause/resume does not repeat completed work.
3. Replace the global `cjk_count >= 2` rule with role-specific short-cue consensus.
4. Recompute cue start/end from first/last agreeing burst observations.
5. Add fixtures for 1-char, 2-char, transient noise, flash effects and adjacent normal dialogue.

## Todo

- [x] A 250–600 ms one-character cue passes when two burst samples agree.
- [x] A one-frame spark/noise glyph fails.
- [x] Long normal cues preserve a106 text and timing.
- [x] Measure inference-call increase on a representative fixture and keep it bounded.

## Success Criteria

- [x] Short-cue recall improves without lowering the safety threshold globally.
- [x] Pause/resume, Multi and targeted rescue retain deterministic results.
- [x] Baseline videos without short cues do not incur an unbounded OCR slowdown.

## Risk Assessment

If cheap change detection triggers continuously on action scenes, the observable signal is burst timestamps approaching the global frame count. Cap bursts per second/window, coalesce events and fall back to regular sampling rather than overloading Paddle.
