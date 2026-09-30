## Plan Complete: Fit S2 to source glyphs and add per-cue scale a110

### Outcome

- S2 Preview and Export now derive automatic size from the detected source-glyph
  box at output resolution, apply the persisted per-cue 50–200% scale and retain
  the panel/frame cap without the former 240 px ceiling.
- The selected S2 cue exposes decrement, slider, increment and reset controls;
  pointer and keyboard changes persist, Preview updates optimistically and failed
  saves roll back.
- Source/translation cue scale stays paired and undoable; legacy snapshots default
  the optional field to `1.0` without a SQLite migration.
- Horizontal line breaks now agree between Preview and Export. Export also emits
  a hard ASS panel clip, so font-engine variance cannot escape the detected panel.

### Implementation

- Added validated `scene_font_scale` cue metadata, repository decoding and
  propagation into translated cues.
- Added the paired subtitle command, compact Job Manager patch and optimistic UI
  model path.
- Made export sizing use the requested output resolution and retained horizontal
  multiline layout in both rendering paths.
- Calibrated Playwrite fit from raster probes: Qt Preview width/line-height ratios
  `1.96/2.55`; libass Export ratios `0.90/0.95`.
- Added QML `Text.Fit`/clipping and a hard ASS panel clip, and removed the fixed
  240 px cap so 4K sizing remains proportional.
- Updated user/architecture guidance and release metadata to `2.0.0a110`.

### Verification

- 12 focused scale/persistence/UI/export regressions passed after the review fixes.
- Full pytest: 853 collected, 852 passed, 1 expected skip.
- Ruff, compileall, all 28 QML files and `git diff --check` passed.
- Clean a110 build completed; the packaged wheel contains the a110 contract,
  final sizing logic and controls. Packaged Preview/Editor QML SHA-256 values
  match source exactly.
- Packaged offscreen GUI smoke completed two navigation cycles, rendered the
  Settings screen with version `v2.0.0a110`, and exited 0 after 7.7 seconds;
  GUI and Job Manager shut down with no remaining `DucaiStudio2` process.
- Release manifest: 2,871 entries, zero missing files, zero hash mismatches and
  zero unmanifested files (excluding `manifest.json`).

### Artifact

- Path: `E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a110-win64`
- Directory size: 413,009,596 bytes (393.88 MiB); 2,872 files including the
  manifest.
- Manifest SHA-256 (release digest):
  `6bc552ac58e3c03d61512952c1a584ed20c5e0e4bde1a075fd653019136756c3`
- `DucaiStudio2.exe` SHA-256:
  `33d2ea4aa93ad435e902b962db201b0f6fe587029981c025d2e129bb7b73650b`
- Wheel SHA-256:
  `8d6f2bce7636402092ca185bd664103c968e2be333d536fbe4a9a8642c3814ed`

### Compatibility

- Existing projects without `scene_font_scale` load at 100%.
- No schema migration was added; OCR, translation, S1, TTS/D1 filtering and
  scene-text placement contracts remain covered by the full regression suite.

### Residual risks

- Qt and FFmpeg/libass remain different font engines; the raster-calibrated
  coefficients target comparable bounds, not pixel-identical glyph shapes.
- Long Vietnamese labels can shrink substantially to remain bounded; the manual
  scale intentionally cannot override the safety cap.
- Fit quality still depends on OCR glyph/panel geometry being accurate.
