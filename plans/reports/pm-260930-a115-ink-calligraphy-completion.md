# a115 ink background and Vietnamese brush-font completion

Status: completed

## Delivered

- Added four licensed Vietnamese-capable brush faces: Comforter Brush, Water
  Brush, Splash and Hurricane. Preview, export and packaged runtime resolve the
  same family names.
- Added four aspect-safe vector scene-text backgrounds: Mực loang, Mực đại mặc,
  Mực tụ and Nét cọ. Horizontal and vertical cues share deterministic QML and
  ASS geometry without raster inputs per cue.
- Preserved legacy `solid` appearance and added per-cue background selection,
  size, opacity and colour controls.
- Kept the typography toolbar at a stable three-row height. Its compact labels
  remain fully visible in a 1280×720 window with an 856 px Timeline pane.

## Licensing decision

The VNI-ThuPhap, HL Thu Phap, UVN-HanNom Calligraphy and SVN-Chu Nom Modern
screenshots do not supply redistributable font files or license evidence. The
release therefore uses four visually related SIL OFL families and displays
their real names. Font files, licenses, source metadata and SHA-256 values are
recorded in `THIRD_PARTY_NOTICES.md`.

## Verification

- Focused pytest: 301 passed.
- Full pytest: 909 passed, 1 environment-gated skip.
- Ruff, compileall, full QML lint and `git diff --check`: passed.
- QRawFont verified Vietnamese glyph coverage for all eight calligraphy faces.
- QML Canvas smoke covered four backgrounds in horizontal and vertical form.
- FFmpeg/libass raster smoke verified the new fonts and ink layers.
- Clean release build passed. All 2,887 manifest hashes verified.
- Packaged EXE remained running through an isolated eight-second startup smoke,
  started one Job Manager child and left no test processes behind.

## Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a115-win64`

## Open findings

None for the accepted a115 scope. The exact four commercial or license-unknown
faces shown in the screenshots remain intentionally unbundled until the owner
provides the font files and redistribution rights.
