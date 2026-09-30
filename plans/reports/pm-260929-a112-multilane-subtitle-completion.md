## Plan Complete: Multi-lane subtitle Timeline a112

### Outcome

- Simultaneous scene text is allocated deterministically across S2, S3, S4…;
  non-overlapping cues reuse the lowest available scene-text lane and S1 stays
  reserved for dialogue.
- The Timeline `T` tool creates a real subtitle cue exactly at the playhead for
  the requested duration and allocates a new subtitle lane below existing ones.
- Click, Ctrl+click, marquee and Timeline-focused Ctrl+A select stable cue IDs.
  Selected cues can be shifted in time or moved between lanes while group lane
  spacing is preserved during collision resolution.
- Every cue has independent timing, frame geometry, font size, orientation,
  text colour and background colour. The same selection can be formatted in
  bulk without touching unselected cues.
- Preview move/resize is optimistic and pointer-synchronous; persistence occurs
  once when the gesture ends. Timeline/editor navigation remains pinned to the
  exact cue ID, preventing model refreshes from saving into another row.

### Implementation

- Added interval-partitioned subtitle lane allocation and generalized lookup
  across all S tracks instead of hard-coding S2.
- Added atomic subtitle commands and compact patches for group formatting,
  geometry, timing/lane movement and manual text creation.
- Added stable multi-selection state, marquee selection, group timing/lane drag
  and direct Preview geometry editing with rollback on rejected commits.
- Added a shared subtitle text toolbar and preserved all per-cue fields through
  project serialization, preview, undo/redo and FFmpeg export.
- Fixed editor focus ownership by capturing cue ID and edit context before a
  delegate can be recycled or the model can reorder.

### Verification

- Focused subtitle, Timeline, UI/ViewModel and export regression suites passed.
- Full pytest passed with one expected skip.
- Ruff, compileall, all QML lint targets and `git diff --check` passed.
- Independent final review found no remaining issue in the corrected paths.
- Clean Windows build completed and the final packaged EXE imported a real MP4
  through the production E2E harness successfully.
- Release manifest: 2,872 entries, zero missing files, zero hash mismatches and
  zero unmanifested files (excluding `manifest.json`).

### Artifact

- Path: `E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a112-win64`
- Directory size: 413,777,345 bytes (394.61 MiB); 2,873 files including the
  manifest.
- Manifest SHA-256:
  `c09c53af3895547065d8e298227fdaac2da512fddd8c9ff954190966e23ee036`
- `DucaiStudio2.exe` SHA-256:
  `15b252401696d401062d7b288971a64ad5c26c3edcc5998407fe1c1bc7816888`
- Wheel SHA-256:
  `ec3517fe442112f1b5a93837e2520005b5d4744d4a285a14010e0c530bc2fdaa`

### Compatibility and boundaries

- Existing projects remain readable because the new subtitle fields are
  optional and no SQLite migration was added.
- Moving a cue between S tracks changes Timeline organization/draw order;
  moving it inside the video frame remains a separate visual-box operation.
- Manual text that extends beyond the media end is rejected explicitly instead
  of being silently shifted or shortened.
- Native GUI click-through could not be executed because the Windows
  computer-use channel exposed no controllable apps. Packaged E2E plus existing
  offscreen GUI smoke cover startup/import/render contracts, but are not falsely
  reported as a human-style click test.
