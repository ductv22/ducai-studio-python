---
title: Complete a117 responsive subtitle controls
date: 2026-09-30
summary: "Shipped stable dense subtitle controls and opaque black ink scene-text defaults."
---

# Complete a117 responsive subtitle controls

## What happened

The subtitle typography controls were reorganized around the editing task rather
than around individual widgets. Numeric fields now retain enough width for the
minus button, editable value and plus button, and dense actions explain their
purpose through Vietnamese tooltips.

New scene text uses one shared domain default: Comforter Brush with white text
on a fully opaque black `Mực loang` background. OCR, the manual `T` flow and the
server fallback all consume the same constants, while stored project values are
left untouched.

## Review finding

The first full test run caught a real 720p regression: the persistent toolbar
could squeeze the track list to 16 px. The layout now reserves enough height for
the toolbar and an interactive track row, restoring D1 double-click navigation.

## Verification

Full pytest passed 918 tests with one environment-gated skip. Ruff, compileall,
QML lint and diff validation passed. A clean a117 build completed, all 2,888
manifest hashes verified, and the packaged EXE passed its Timeline smoke.

## Artifact

E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a117-win64

> Historical work record. Prefer current product documentation and source
> contracts for durable behavior.
