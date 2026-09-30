---
title: Complete a110 S2 font fitting and manual scale
date: 2026-09-29
summary: "Added glyph-aware S2 sizing, per-cue scale, preview/export parity and a verified Windows release."
---

# Complete a110 S2 font fitting and manual scale

## What happened

S2 scene text was readable but could become oversized because Preview and Export initially sized Vietnamese text from a broad panel rather than the detected source glyph. A first implementation added per-cue scale, then independent review exposed output-resolution, hard-bound, keyboard-slider, multiline, 4K and Qt/libass metric mismatches.

## Decision

Persist an optional per-cue `scene_font_scale` from 0.50 to 2.00, defaulting legacy projects to 1.00. Preview updates optimistically and saves once per committed change with undo/redo. Both render paths start from OCR glyph and panel geometry, but use engine-specific Playwrite metrics because Qt Quick and libass rasterize the bundled variable font differently. Export also adds a hard ASS panel clip and calculates size at the configured output resolution.

## Verification

Final source collected 853 tests; 852 passed and one optional runtime test skipped. Ruff, compileall, 28-file QML lint and diff checks passed. Actual Qt/libass raster probes for horizontal and vertical Playwrite text stayed inside panels with comparable visible bounds. Clean release `2.0.0a110` contains 2,871 manifest entries with no missing, mismatched or extra files. Offscreen packaged smoke completed two navigation cycles, rendered Settings with version a110, exited 0 and left no process.

## Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a110-win64`

Manifest SHA-256: `6BC552AC58E3C03D61512952C1A584ED20C5E0E4BDE1A075FD653019136756C3`

Executable SHA-256: `33D2EA4AA93AD435E902B962DB201B0F6FE587029981C025D2E129BB7B73650B`

## Next steps

Validate a few real S2 cues visually in the user's project, especially unusually long Vietnamese labels. Manual scale cannot override the panel safety boundary by design.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
