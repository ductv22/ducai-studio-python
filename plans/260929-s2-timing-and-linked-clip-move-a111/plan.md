---
title: "Subtitle canvas editing and OCR region control a111"
description: "Unify S1/S2/S3 editing across Preview and Timeline, and split OCR dialogue from scene text by user-controlled regions."
status: completed
priority: P1
effort: "medium"
branch: "ducai-studio-python-a98"
tags: [timeline, scene-text, subtitles, preview, linked-clips, release]
created: 2026-09-29
---

# Subtitle canvas editing and OCR region control a111

## Outcome Contract

- A user can drag an S2 clip body left or right to show the translated scene text earlier or later without changing its duration.
- A user can drag either edge of an S2 clip to shorten or extend its display duration.
- Timeline and video Preview react while the pointer moves; the project is persisted exactly once when the pointer is released.
- S2 source and translated cues, the S2 timeline clip, Preview and Export keep one authoritative timing after save, reload, undo and redo.
- A translated S2 cue can be edited explicitly in the subtitle editor with immediate Preview feedback and the existing undo/redo path.
- After cutting or trimming linked V1/A1 media, a user can drag either remaining clip to the Timeline start; both linked clips move together and snap exactly to `00:00`.
- A subtitle cue selected from Preview or any subtitle track can be edited, formatted, retimed or deleted through one authoritative cue identity; deleting it removes its translated text and generated cover/background together.
- Users can add missing scene text directly on the paused video frame, choose a 1-100 relative font size, horizontal/vertical flow and a background color, with the same result in Preview and Export.
- OCR dialogue is classified into S1 only when it intersects the configured dialogue scan region. Text outside that region is classified as S2 scene text, and configured exclusion regions are never OCR input.
- The OCR region editor uses the current frame of the project video so its normalized geometry cannot drift from the real media.
- Very short one-character dialogue can be recovered by a bounded local sampling burst without increasing the scan rate across the entire video.

## Constraints and Non-goals

- Store and submit integer microseconds; S2 duration must remain at least 100 ms and stay inside the current project duration.
- S2 edits must not change S1 dialogue timing, D1 voice timing, video timing or audio speed.
- Formatting and deletion apply only to the selected subtitle cue; they must not alter hard-sub pixels already burned into the source video.
- Scene-text background removal means removing the generated cover/background owned by that cue, not reconstructing pixels under a hard-sub source.
- Media movement changes only timeline placement. It must not trim source media, change duration/speed or unlink synchronized video/audio.
- Preserve locked-track protection and existing D1 edge-resize behavior.
- Do not add SQLite migration or invalidate old project/cache files.
- OCR region settings are optional normalized project fields. Projects without them keep the existing default dialogue band and full-frame scene scan.
- Exclusion regions take precedence over both the dialogue region and scene scan. Manual scene-text cues are preserved when OCR or translation is rerun.
- Font size 1-100 is a relative editor scale resolved against video size and detected text geometry; it is not a fixed desktop-pixel size.
- Preserve all unrelated dirty-worktree changes from a100-a110.

## Implementation

- Add an undoable S2-specific subtitle timing command that updates paired source/translation cue ranges and the matching S2 timeline clip in one revision.
- Extend the compact subtitle patch with only the changed S2 clip, avoiding a full project snapshot.
- Add an in-memory S2 timing preview/rollback path so Preview follows pointer motion without IPC or disk writes.
- Add Timeline body-drag and left/right handles for S2, with local geometry, a timing label and one backend command on release.
- Add a selected-S2 Vietnamese text field that targets the translated cue identity and never overwrites source Chinese.
- Add an undoable Timeline `move` command for video/original-audio clips; apply the same delta to their link group and preserve source ranges.
- Add local linked-clip move preview, a `00:00` snap zone/guide and a single Timeline command on release.
- Make subtitle deletion remove the paired source/translation cue and its S1/S2/S3 Timeline clip in one undoable revision, with compact clip removals sent to the UI.
- Add Preview/Timeline double-click navigation to the matching row in Biên tập phụ đề, plus context-menu deletion from both surfaces.
- Replace the old S2 percentage slider with a compact Word-like subtitle toolbar backed by a cue formatting command: translated text, size 1-100, orientation and background color.
- Add a manual scene-text command and Preview placement gesture that creates a real cue/track clip at the playhead and therefore participates in save, undo/redo, Preview and Export.
- Add optional dialogue and exclusion OCR regions to the project snapshot, OCR fingerprint and worker request. Provide a frame-backed rectangle editor in the extraction workspace.
- Filter OCR observations by region before consensus so dialogue and scene text can never merge, then use bounded 100-150 ms rescue samples around brief one-character observations.
- Update user/architecture documentation and release metadata to `2.0.0a111`.

## Acceptance Criteria

- [x] Dragging an S2 body preserves duration and changes start/end by the same delta.
- [x] Dragging either S2 edge changes only that edge and cannot create a duration below 100 ms.
- [x] S2 movement is clamped to `0..project duration` and Preview responds before release.
- [x] Pointer movement sends no IPC/save; releasing a changed cue sends exactly one command/revision.
- [x] Backend failure or Escape restores the prior S2 timing and visible Preview.
- [x] Source and translated S2 cue ranges and the S2 track clip remain synchronized after reload, undo and redo.
- [x] Editing selected S2 Vietnamese text updates Preview immediately, persists to translated cues and never changes source text.
- [x] Dragging a linked V1/A1 clip moves both by the same delta, preserves duration/source range and snaps to `00:00`.
- [x] Locked clips cannot move; S1/D1 and unselected tracks remain unchanged.
- [x] Existing D1 resize, clip selection, double-click and context-menu behavior remain functional.
- [x] Deleting S2 from Preview or Timeline removes its source/translation pair, clip, translated text and generated cover/background immediately; undo restores all of them.
- [x] Double-clicking S1/S2/S3 in Preview or Timeline opens the exact cue row in Biên tập phụ đề and seeks to its start.
- [x] The formatting toolbar updates selected subtitle text, size 1-100, horizontal/vertical direction and background color consistently in Preview, Timeline metadata, reload and Export; the old 50-200% control is gone.
- [x] A manual text cue can be placed on the current video frame, retimed, edited and deleted without being overwritten by later OCR/translation publication.
- [x] The OCR-region editor uses a real paused frame, stores normalized geometry, supports reset, multiple exclusion regions and makes exclusion precedence visible.
- [x] Dialogue-region evidence produces S1 only; outside evidence produces S2 only; no cross-role consensus or timestamp jumping occurs.
- [x] A 0.5-second single-character cue such as `爹` or `娘` is recovered when at least two bounded rescue samples agree, without raising global OCR sampling cost.
- [x] Focused tests, full pytest, Ruff, compileall, QML lint, build and packaged smoke pass.

## Risks and Rollback

- QML pointer handlers can compete with click/double-click and resize handles. Start body drag only after a small movement threshold and keep edge handles above the body area.
- Large projects must not reset models on every pointer event. Use local preview state and a compact one-clip subtitle patch.
- Legacy S2 clips may lack cue metadata. Match by cue ID/index first, then by the unique prior range, and persist metadata after the first timing edit.
- Region selection can be visually correct but geometrically wrong when letterboxing is present. Convert pointer coordinates through the actual painted-video rectangle and store only normalized full-frame coordinates.
- One-character rescue is vulnerable to watermark noise. Require spatial overlap plus two-frame agreement and continue applying the existing watermark/exclusion filters.
- Rollback is limited to the two new command paths, QML gesture state and optional compact-patch fields; old snapshots remain readable.

<!-- slug: s2-timing-and-linked-clip-move-a111 -->
