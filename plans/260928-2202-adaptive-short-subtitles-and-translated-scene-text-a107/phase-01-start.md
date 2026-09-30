---
phase: 1
title: "Phase 1: Backward-compatible cue and render contracts"
status: pending
priority: P1
effort: "4h"
dependencies: []
---

# Phase 1: Backward-compatible cue and render contracts

## Overview

Extend cue, repository, IPC and export contracts so dialogue and scene text share translation infrastructure but retain different speech and rendering behavior.

## Requirements

- [x] Add optional cue metadata: `role=dialogue|scene_text`, `orientation=horizontal|vertical`, `style_hint=standard|display|calligraphy`, OCR box, source glyph box, background color and review state.
- [x] Default all absent metadata to dialogue-compatible values; do not bump SQLite schema.
- [x] Add scene-text render descriptors with geometry, font, orientation, fit and concealment data.
- [x] Keep stable cue IDs and source/translated pairing across project reload and revision history.

## Architecture

Use optional fields on `SubtitleCue` instead of a second unrelated persistence tree. Translation keeps one cue-ID contract; TTS, ordinary SRT export and D1 explicitly select `role=dialogue`. Timeline/view-model code derives `S1` and `S2 · Chữ trong cảnh` from role without changing internal track identity.

## Related Code Files

- Modify: `src/ducai_studio/domain/project.py`
- Modify: `src/ducai_studio/domain/effects.py`
- Modify: `src/ducai_studio/infrastructure/project_repository.py`
- Modify: `src/ducai_studio/application/export_pipeline.py`
- Modify: `src/ducai_studio/providers/export/ffmpeg.py`
- Modify: repository, contract and export tests under `tests/`

## Implementation Steps

1. Add enum-like validated string fields with safe defaults and JSON round-trip support.
2. Add `RenderSceneText` and scene-text concealment descriptors; preserve legacy export decoding.
3. Add helpers that partition dialogue and scene text once, reused by translation, TTS, SRT and rendering.
4. Cover old snapshots, malformed geometry and mixed cue roles with tests.

## Todo

- [x] Legacy project fixture decodes with every old cue classified as dialogue.
- [x] Mixed-role snapshot round-trips without changing IDs, ranges or visual boxes.
- [x] TTS/SRT selectors exclude scene text before later phases depend on them.

## Success Criteria

- [x] No SQLite migration or public IPC frame-size regression.
- [x] Old export snapshots decode unchanged.
- [x] New scene-text fields survive close/reopen and undo-safe project revisions.

## Risk Assessment

Load-bearing assumption: all translation paths preserve cue IDs. If any provider renumbers mixed-role cues, stop and add an explicit role-aware translation envelope rather than matching by list position.
