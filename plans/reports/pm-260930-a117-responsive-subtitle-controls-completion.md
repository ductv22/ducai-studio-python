# a117 responsive subtitle controls completion

Status: completed

## Delivered

- `DsSpinBox` reserves 104 px and separates decrease, editable value and
  increase controls. Numeric controls no longer overlap at the supported
  minimum window size or 125% Qt scale.
- Timeline subtitle formatting is split into stable groups. The inspector tabs,
  add-cue dialog and dense toolbar actions have concise Vietnamese labels and
  explanatory tooltips.
- New scene text, whether created by OCR or the Timeline `T` tool, defaults to
  Comforter Brush, white text, an opaque black `Mực loang` background and 100%
  background opacity. Existing cues retain their stored formatting.
- The vertical Timeline split reserves a usable track viewport while the
  formatting toolbar remains visible.

## Review finding resolved

- Full pytest exposed that the persistent formatting toolbar left only 16 px
  for tracks at 1180×720. A D1 cue was painted outside the interactive viewport,
  so double-click sought the blank timeline instead of opening its subtitle.
  The split constraints now keep at least one complete track row visible. The
  original double-click integration smoke passes again.

## Verification

- Pytest: 918 passed, 1 environment-gated skip.
- Ruff, compileall, full QML lint and `git diff --check`: passed.
- Source UI smoke: Timeline at 1180×720 and 125% Qt scale passed.
- Clean Nuitka release build: passed.
- Manifest: version `2.0.0a117`; 2,888/2,888 hashes verified.
- Packaged EXE: isolated Timeline screenshot smoke passed and exited without an
  orphan process.

## Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a117-win64`

Executable SHA-256:
`71de6148135480fcda565c6955954ea900731ac9e54a072437bbf7828995e0a3`

## Open findings

None for the accepted a117 scope.
