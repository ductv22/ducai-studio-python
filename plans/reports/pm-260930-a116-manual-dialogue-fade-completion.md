# a116 manual dialogue, role-based TTS and scene fade completion

Status: completed

## Delivered

- TTS/SRT now follow durable `cue.role`; dialogue remains spoken on S2+ and an
  explicit scene-text move to S1 promotes the paired source/translation cue.
- Shared Timeline/Subtitle Editor `T` dialog creates dialogue or scene text with
  complete per-cue typography. Dialogue inherits the nearest spoken cue.
- Scene text supports optional fade-in/out, 50–2,000 ms per end, default 300 ms.
  Preview and ASS export clamp short cues to half duration and do not fade the
  Chinese-source concealment layer.
- Manual dialogue and manual scene text survive OCR/translation reruns; preserved
  manual rows never re-enter the translation provider.
- Legacy project decoding defaults fade off and requires no SQLite migration.

## Review finding resolved

- Initial OCR merge preserved only `manual_scene_text`. Review proved this could
  delete OCR-repair dialogue added with `T`. The merge now preserves both manual
  roles, resolves lane collisions without demoting dialogue and has regression
  coverage in OCR import and translation worker tests.

## Verification

- Pytest: 916 collected; 915 passed, 1 environment-gated skip.
- Ruff, compileall, full QML lint and `git diff --check`: passed.
- Clean Nuitka release build: passed.
- Manifest: version `2.0.0a116`; 2,888/2,888 hashes verified.
- Packaged EXE: isolated offscreen navigation smoke passed, screenshot/state DB
  created, stderr empty, process and Job Manager exited cleanly.

## Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a116-win64`

## Open findings

None for the accepted a116 scope.
