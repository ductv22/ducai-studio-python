---
title: "Repair subtitle editing and S2…Sn synchronization a113"
description: "Preserve cue text on focus, make plain-click selection exclusive, migrate legacy overlapping scene text into visible lanes and keep manual text responsive."
status: complete
priority: P1
effort: "medium"
branch: "ducai-studio-python-a98"
tags: [subtitles, timeline, qml, migration, bugfix, release]
created: 2026-09-29
---

# Repair subtitle editing and S2…Sn synchronization a113

## Outcome Contract

- Clicking or double-clicking a source or Vietnamese cue never clears either
  text field. Copy, select, edit and explicit cue deletion remain available.
- A plain click always leaves exactly one cue selected. Ctrl+click alone
  toggles additive selection; changing rows never inherits a stale modifier.
- Existing projects whose overlapping OCR scene cues still share S2 are
  normalized deterministically into S2, S3, S4… on decode. Preview, editor,
  Timeline and Export all consume the same assigned lane.
- Manual Timeline text can be positioned at any valid playhead region. A short
  subtitle save does not discard the request; it is queued and dispatched as
  soon as the current atomic save completes.
- A newly created or selected S3+ cue is scrolled into view and the Timeline
  exposes an explicit vertical scrollbar.

## Constraints and Non-goals

- Preserve source and translated cue IDs, timestamps, geometry, typography and
  cache identity during compatibility normalization.
- Preserve explicit manual-text lanes and existing non-overlapping S3+ edits;
  compatibility reflow applies only to legacy automatic scene cues with missing
  lane metadata or overlapping S2 placement.
- Keep one subtitle command in flight. Queue only manual text additions rather
  than introducing concurrent project revisions.
- Manual text still requires an opened video and a valid interval inside media;
  “any time” does not create text beyond the media end.
- No SQLite migration and no mutation of user project data during diagnosis.

## Root-cause evidence

- Offscreen Qt reproduction: the conditional editor `Binding` changes
  `TextArea.text` from populated content to `""` when `activeFocus` becomes
  true, after which focus loss submits the empty value.
- The active project head contains four automatic scene cues at
  `20.700–22.540s`, all with empty `subtitle_track_id`, and all four clips on
  S2. Preview therefore draws four regions while Timeline delegates overlap.
- The same project already contains a successfully-created manual cue on S3;
  it is outside the visible track viewport, which has neither a vertical
  scrollbar nor selection-follow scrolling.

## Implementation

- Replace focus-disabled text bindings with explicit synchronization that never
  restores the empty default while an editor gains focus. Commit using captured
  cue ID and language only when the user actually changed the captured value.
- Pass an explicit additive boolean from QML and use one selection helper so
  plain-click semantics are deterministic in editor, Timeline and Preview.
- Add a compatibility lane normalizer in the subtitle domain. Reserve explicit
  manual lanes first, allocate missing/legacy automatic scene cues to the
  lowest non-overlapping S2…Sn lane, mirror the lane to translated cues and
  rebuild subtitle tracks without changing IDs or ranges.
- Invoke normalization while decoding project snapshots so UI and export agree,
  while new OCR publication continues using the same allocator.
- Add a bounded FIFO for manual text requests received during a subtitle save;
  drain it after success or failure releases the busy flag.
- Expose cue-to-track index lookup, add a vertical Timeline scrollbar and keep
  the selected cue track visible after selection/model patches.
- Bump release metadata and user-visible documentation to `2.0.0a113`.

## Acceptance Criteria

- [x] Focusing populated source and translated editors preserves both values;
  focus loss without edits sends no update command.
- [x] Editing cue A then clicking cue B cannot write blank text or A's text into B.
- [x] Plain click produces one selected cue; Ctrl+click is the only additive path.
- [x] Four overlapping legacy S2 scene cues decode as S2/S3/S4/S5 with matching
  tracks and clips; non-overlapping cues reuse the lowest safe lane.
- [x] A manual S3+ cue retains its explicit lane during legacy normalization.
- [x] Preview, Timeline, editor and export resolve identical cue text/lane data.
- [x] `T` remains available while a subtitle save is in flight, queues the
  manual addition once and creates it after the current save completes.
- [x] Selecting or creating S3+ scrolls that lane into view; users can also use
  the visible vertical scrollbar.
- [x] Focused regressions, full pytest, Ruff, compileall, QML lint, clean build
  and packaged application smoke pass.

## Risks and Rollback

- Compatibility normalization must not override intentional manual lane edits.
  Preserve manual cues and already-valid explicit multi-lane layouts before
  allocating legacy automatic cues.
- Selection-follow scrolling must not fight the user during a drag. Scroll only
  when selection changes or a new cue patch arrives, never on pointer movement.
- Queued additions must be project-scoped and cleared if the current project
  changes before dispatch.

<!-- slug: subtitle-editor-lane-sync-a113 -->
