## Plan Complete: Subtitle editor and lane synchronization a113

### Outcome

- Focusing, copying or double-clicking a cue no longer clears source or
  translated text; edits remain pinned to the captured cue identity.
- Plain click selects exactly one cue and Ctrl+click is the explicit additive
  path across editor, Preview and Timeline.
- Legacy simultaneous scene text is normalized deterministically into visible
  S2…Sn lanes without moving an explicit manual lane.
- Manual text requested during a subtitle save is queued once and created when
  the atomic revision completes.
- S3+ tracks are vertically scrollable and selected cues are brought into view.

### Verification

- Full pytest passed: 879 tests collected, one environment-dependent skip.
- Ruff, compileall, all QML lint targets, `git diff --check` and
  `uv lock --check` passed.
- A packaged offscreen import smoke created and persisted a project revision,
  then shut down cleanly.
- A read-only decode of the real project produced 211 S1 clips plus visible
  S2…S6 scene lanes while preserving the manual S3 cue.

### Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a113-win64`

### Compatibility boundary

No SQLite migration was added and the real project database was never written
during verification.
