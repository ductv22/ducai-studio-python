## Plan Complete: Subtitle canvas editing and OCR region control a111

### Outcome

- Preview and Timeline now use one cue identity for S1/S2/S3 selection,
  double-click navigation, formatting and undoable deletion. Deleting a cue
  removes its translated text, subtitle clip and generated cover/background.
- The former S2-only percentage control was replaced by a responsive two-row
  editor for Vietnamese text, font size 1–100, horizontal/vertical direction
  and background colour. The two-row layout keeps **Add text** and **Delete cue**
  visible at 1180 px.
- Missing scene labels can be placed directly on the paused video frame as real
  S2 cues. Their timing, position, typography and background survive project
  reload, OCR reruns and translation reruns.
- Extraction can use one frame-backed dialogue OCR region and multiple no-OCR
  regions. Dialogue evidence becomes S1, while the separate scene scan excludes
  that region and can publish accepted outside text as S2.
- Brief one-character dialogue receives six bounded samples around a detected
  transition, still requiring two strong spatially stable readings.

### Implementation

- Added persisted optional OCR geometry and per-cue font/track metadata without
  a SQLite migration; legacy projects use safe defaults.
- Added compact subtitle commands/patches for text format, manual scene text,
  OCR regions and paired cue/clip deletion.
- Added local Preview/Timeline interactions and a reusable responsive subtitle
  toolbar. The old S2 50–200% control is no longer exposed.
- Applied exclusion masks in crop coordinates immediately before OCR without
  mutating decoded frames. The scene pass also receives the dialogue region as
  an exclusion, preventing S1/S2 cross-role consensus.
- Preserved manual scene cues during OCR publication and seeded them as complete
  during translation so providers never overwrite user-authored labels.
- Updated Preview and FFmpeg/ASS export to share font size, direction and cue
  background behavior.

### Verification

- Focused subtitle/OCR/UI/export suite: 449 passed.
- Final UI/ViewModel/subtitle suite after the responsive-toolbar correction:
  215 passed.
- Full pytest on the final source: 866 passed, 1 expected skip.
- Ruff, compileall, QML lint and `git diff --check` passed.
- Clean Windows build completed. Packaged Job Manager startup/list/shutdown IPC
  smoke passed and left no a111 process.
- Packaged offscreen GUI navigated through two workspace cycles, rendered the
  Subtitle editor at 1180×720 and exited cleanly. Visual inspection confirmed
  both **Add text** and **Delete cue** are visible in the two-row toolbar.
- Release manifest: 2,872 entries, zero missing files, zero hash mismatches and
  zero unmanifested files (excluding `manifest.json`).

### Artifact

- Path: `E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a111-win64`
- Directory size: 413,512,607 bytes (394.36 MiB); 2,873 files including the
  manifest.
- Manifest SHA-256:
  `4e3310a5fe3d2e0c7cda5ece234c0249220d2f951fe93a45de126437e3bd3605`
- `DucaiStudio2.exe` SHA-256:
  `b4e8c566532fd671535281c33b73b0e031a8b3df04ea755356691f6f40ff0245`
- Wheel SHA-256:
  `a006653a853741937618c99eca7c95bc4f51ccf7cc80cfa403512e9c7990ed72`

### Compatibility and boundaries

- Deleting generated text/background does not reconstruct pixels from Chinese
  hard-subs already burned into the source video.
- Text outside the dialogue region is eligible for S2; it still has to pass the
  existing scene-text confidence, persistence and watermark filters rather than
  being accepted blindly.
- The short-glyph burst improves evidence near detected transitions but cannot
  guarantee recovery when compression, motion or contrast prevents two Paddle
  readings from agreeing.
- Existing projects without the new optional fields remain readable. No schema
  migration was added.
