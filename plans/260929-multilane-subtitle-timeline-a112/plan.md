---
title: "Multi-lane subtitle Timeline and direct text editing a112"
description: "Allocate simultaneous scene text across S2…Sn, add stable multi-cue editing, direct Preview geometry and a Timeline text tool."
status: completed
priority: P1
effort: "large"
branch: "ducai-studio-python-a98"
tags: [timeline, subtitles, scene-text, multi-select, preview, release]
created: 2026-09-29
---

# Multi-lane subtitle Timeline and direct text editing a112

## Outcome Contract

- S1 remains the spoken-dialogue lane. Simultaneous scene labels are assigned deterministically to the lowest available lane among S2, S3, S4… so overlapping labels are visible at the same time.
- A manual Timeline text tool `T` creates text at the current playhead for the entered duration and always allocates a new subtitle lane below the existing subtitle lanes.
- Subtitle cues can be selected singly, with Ctrl, by marquee, or with Ctrl+A while Timeline has focus. Selection is held by stable cue IDs, never transient row indexes.
- Selected cues can be shifted in time and moved between subtitle lanes. A vertical drop that would overlap an occupied lane uses the next available lane while preserving relative offsets in a selected group.
- Every cue keeps independent visual geometry, font size, orientation, text/background color and timing. Bulk edits apply only to the selected cue IDs.
- Preview supports direct move/resize of selected generated subtitle text. Cues without overrides continue to use the existing effect/default position.
- Double-clicking a cue opens the exact row in Biên tập phụ đề. Editing remains pinned to that cue until its save finishes, even when the model reorders, refreshes or recycles delegates.
- Preview, Timeline, save/reload, undo/redo and Export use the same cue lane, timing and visual formatting.

## Constraints and Non-goals

- Preserve the existing project schema compatibility: new fields remain optional and no SQLite migration is introduced.
- Keep SRT/source microseconds authoritative and keep every cue duration at least 100 ms.
- Pointer movement is local UI preview only. Persist exactly once on release; never save or reset the project model on every mouse event.
- Track/lane identity controls Timeline organization and draw order. `visual_box` controls position in the video frame; changing one must not silently change the other.
- Manual text creates generated subtitle/background data only. Removing it cannot reconstruct hard-sub pixels burned into source media.
- Preserve existing default subtitle/effect positioning for cues with no per-cue geometry override.
- Do not modify video/audio/D1 timing while editing subtitle cues.
- Preserve all unrelated dirty-worktree changes from a100-a111.

## Implementation

- Add reusable subtitle-lane allocation helpers using interval partitioning. Re-project OCR scene cues into the lowest non-overlapping S2…Sn lane while keeping S1 reserved for dialogue.
- Extend subtitle commands with multi-cue format, geometry, timing/lane movement and manual-new-lane creation; each user action creates one undoable revision and one compact patch.
- Make cue/clip lookup work on every subtitle track rather than hard-coded S2.
- Add selected-cue-ID state and optimistic patch/rollback helpers in the view model/model, including Ctrl toggling, select-all and marquee result application.
- Extend Timeline subtitle body drag to preview horizontal time and vertical lane together; add marquee selection and selected styling without affecting whole-track selection.
- Add a compact `T` tool near Timeline controls. Start equals current playhead; duration is entered in seconds; placement/formatting continues in Preview and the shared toolbar.
- Add direct Preview move/resize handles for the selected generated cues, with normalized geometry and one backend commit per gesture.
- Bind the shared subtitle toolbar to all selected cue IDs and expose mixed values without forcing unrelated fields.
- Fix editor commit ownership by capturing cue ID, language, and initial value on focus entry; discard recycled-delegate focus events and navigate by stable cue ID after model patch.
- Update export projection and project decoding only where needed to preserve lane/geometry parity.
- Update release metadata and the smallest owning user/architecture documentation to `2.0.0a112`.

## Acceptance Criteria

- [x] Three scene cues with overlapping ranges produce S2, S3 and S4; later non-overlapping cues reuse the lowest available lane.
- [x] Dialogue cues remain on S1 and never compete with scene-text lane allocation.
- [x] Manual `T` text starts exactly at the playhead, ends after the requested duration and creates a new lowest-free-below-existing S lane.
- [x] Click, Ctrl+click, marquee and Timeline-focused Ctrl+A select the expected cue IDs without selecting track headers.
- [x] Horizontal drag shifts selected cue timing; vertical drag reassigns lane; occupied destinations resolve deterministically without overlap.
- [x] Single and bulk formatting/geometry changes affect only selected cues and survive reload, undo/redo and Export.
- [x] Preview move/resize stays under the pointer, sends no IPC while moving and commits once on release.
- [x] Editing cue A and losing focus after a model refresh can never write its text into cue B.
- [x] Double-click navigation remains on the requested cue after sorting or patching.
- [x] Existing S1/S2 projects load unchanged; legacy cues infer safe lane/default geometry values.
- [x] Focused tests, full pytest, Ruff, compileall, QML lint, clean build and packaged application smoke pass.

## Risks and Rollback

- Timeline body drag, marquee and edge resize gestures can conflict. Marquee begins only on empty lane space, edge handles retain priority and body drag uses a movement threshold.
- Reassigning OCR lanes can reorder tracks but must not change cue identity, cue index or timestamps. Tests assert deterministic output and cache-safe identities.
- Bulk editing can create an invalid group at project boundaries. Clamp by one shared delta so relative cue spacing is preserved.
- Direct Preview geometry must account for letterboxing. Reuse the existing painted-video coordinate conversion and store normalized full-frame values.
- Rollback is limited to new subtitle command paths, selection state and QML gestures; optional fields and legacy snapshots remain readable.

<!-- slug: multilane-subtitle-timeline-a112 -->

## Completion

- Source verification passed: focused regression suites, full pytest with one
  expected skip, Ruff, compileall, all QML lint targets and `git diff --check`.
- Independent review confirmed vertical-only lane persistence, shared group
  collision offsets and exact manual-cue playhead timing.
- Clean Windows package and packaged EXE import smoke passed. The release
  manifest contains 2,872 entries with zero missing, mismatched or extra files.
- Artifact: `E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a112-win64`.
- Native GUI click-through was unavailable because the Windows computer-use
  channel exposed no controllable apps; packaged E2E and offscreen smoke were
  used instead, and this limitation is recorded rather than treated as a pass.
