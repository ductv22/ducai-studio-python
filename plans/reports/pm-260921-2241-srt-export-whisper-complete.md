# Plan Complete: SRT export and Whisper extraction

## Summary

| Metric | Result |
|---|---|
| Plan | `260921-2058-srt-export-and-whisper-extraction` |
| Progress | 18/18 tasks (100%) |
| Release | `DUCAI-STUDIO-2.0.0a99-win64` |
| Full tests | 718 passed, 1 skipped |
| Static gates | Ruff, compileall and full QML lint passed |
| Smoke test | Frozen EXE opened the Trích xuất workspace and exited cleanly |
| Source | Pushed to `origin/ducai-studio-python-a98` |

## Delivered

- Export source and Vietnamese SRT from the latest persisted cue state using
  UTF-8 BOM and atomic replacement.
- Explicit frame OCR / Whisper audio extraction modes with separate
  fingerprints, checkpoints and guarded publication.
- Pinned external faster-whisper 1.2.1 / CTranslate2 4.8.1 runtime contract,
  large-v3 Chinese decoding, CUDA float16 and CPU int8 fallback.
- Worker-derived progress, responsive pause/cancel, five-second resume rewind
  to a completed segment boundary, and Multi snapshot compatibility.
- Standard a99 package includes the small Whisper host only; runtime, model,
  project data, cache and credentials remain external.

## Verification and limitations

- Standard release size: 367.42 MiB; no Whisper runtime/model payload found.
- Secret and machine-specific path scans found no production credentials or
  local absolute paths in committed changes.
- The machine did not have the optional Whisper runtime/model installed, so the
  real multi-gigabyte model was not downloaded or live-transcribed. Protocol,
  worker, pause/resume, fallback and import paths were exercised with the
  deterministic fake host and integration suite.
- An existing a98 user session was left untouched. The a99 smoke test used
  isolated app/data roots to avoid its single-instance lock and project state.

## Remaining user action

- Run `scripts/setup-whisper-runtime.ps1` only on machines that should use
  Whisper; frame OCR and SRT export work without it.
