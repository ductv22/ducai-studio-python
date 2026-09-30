---
title: Complete a116 role-based TTS and scene-text fade
date: 2026-09-29
summary: "Shipped manual dialogue roles, scene fade, OCR rerun preservation and verified a116 release."
---

# Complete a116 role-based TTS and scene-text fade

## What happened

Implemented a116 so TTS/SRT follow `cue.role` instead of subtitle lane. The shared `T` dialog now creates spoken dialogue or visual scene text with complete typography; dialogue inherits the nearest spoken cue. Scene text gains optional 50–2,000 ms fade-in/out shared by Preview and ASS export, clamped to half the cue duration while source-text concealment remains opaque.

## Review finding

Cross-module review found OCR reruns preserved only `manual_scene_text`, which could delete dialogue added to repair missed OCR. The merge and translation preservation paths now retain both `manual_dialogue` and `manual_scene_text`, resolve lane collisions without demoting dialogue and avoid provider calls for preserved manual rows.

## Verification

916 tests collected: 915 passed, one environment-gated skip. Ruff, compileall, QML lint and diff checks passed. Clean a116 release built; all 2,888 manifest hashes verified. Packaged EXE completed isolated navigation smoke with empty stderr and clean process shutdown.

## Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a116-win64`

## Next steps

User acceptance test on a real project: add a dialogue cue with `T`, add/fade a scene cue, move cues across S1/S2 and regenerate voice/export.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
