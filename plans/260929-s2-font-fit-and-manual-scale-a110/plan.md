---
title: "Fit S2 to source glyphs and add per-cue scale a110"
description: "Keep translated scene text inside its source region and let users tune each S2 cue without diverging preview and export."
status: completed
priority: P1
effort: "medium"
branch: "ducai-studio-python-a98"
tags: [scene-text, subtitles, preview, export, ui, release]
created: 2026-09-29
---

# Fit S2 to source glyphs and add per-cue scale a110

## Outcome Contract

- S2 starts from the detected Chinese glyph size instead of expanding to fill a large panel.
- Vietnamese S2 never renders outside its panel or video frame in Preview or Export.
- A user can adjust the selected S2 cue from 50% to 200% and reset it to 100%.
- The manual scale is persisted per cue, updates Preview immediately and is undoable.
- Preview and Export use the same sizing inputs and panel-bounded fit policy,
  with metrics calibrated for their respective font engines.

## Constraints and Non-goals

- Preserve existing OCR, translation, S1 subtitle and scene-text placement behavior.
- Do not change SQLite schema or invalidate existing project/cache files.
- Existing projects default missing scale data to 100%.
- Do not claim pixel-identical Chinese/Vietnamese glyph metrics; match visual height conservatively and fit the detected region.
- Preserve all unrelated dirty-worktree changes from a100-a109.

## Implementation

- Add optional `scene_font_scale` to the cue contract and repository decoder.
- Preserve the field when source metadata is copied into translated cues.
- Add an undoable subtitle command that updates paired source/translation S2 cues.
- Expose the scale through the cue model and an optimistic Preview update path.
- Add a selected-S2 size control to the subtitle editor with decrement, slider, increment and reset.
- Persist both pointer and keyboard slider changes through the same undoable command path.
- Replace panel-only font sizing with glyph-aware, output-resolution-aware calculations in QML and export.
- Preserve horizontal line breaks, calibrate Qt/libass Playwrite metrics and remove the legacy 240 px cap.
- Add clipping/fit safeguards to Preview and a hard ASS panel clip to Export.
- Update user documentation and release metadata to `2.0.0a110`.

## Acceptance Criteria

- [x] A small OCR glyph inside a large panel no longer produces oversized Vietnamese text.
- [x] 50%, 100%, 125% and 200% controls behave immediately and persist after reload.
- [x] Increasing scale is still capped by panel/video geometry and cannot overflow the frame.
- [x] Horizontal and vertical S2 cues remain readable and bounded.
- [x] Source and translated cue metadata remain synchronized; undo/redo restores scale.
- [x] Legacy project JSON without the field loads at 100%.
- [x] Preview/export sizing fixtures agree and existing S1/scene-text behavior remains compatible.
- [x] Focused tests, full pytest, Ruff, compileall, QML lint, build and packaged smoke pass.

## Risks and Rollback

- Font engines have different metrics. Use raster-calibrated engine-specific coefficients,
  QML `Text.Fit` and an ASS panel clip as final guards.
- Very long Vietnamese labels may become small; manual scale cannot override the no-overflow safety cap.
- Rollback is limited to the new optional field, command, UI and sizing helper; old snapshots remain readable.

<!-- slug: s2-font-fit-and-manual-scale-a110 -->
