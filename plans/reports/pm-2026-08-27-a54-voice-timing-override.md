# DUCAI STUDIO a54 delivery report

| Item | Result |
|---|---|
| Release | `2.0.0a54` |
| Full tests | 443 passed, 1 skipped |
| Ruff / compile / QML lint | PASS |
| Manifest | 2,840 files, 0 mismatch |
| EXE SHA-256 | `fc358ef285dcafbdbee93bb57e430aad58b226581484384f35018802e9e287a5` |
| Running smoke | GUI 35528, Job Manager 6400 |

## Delivered

- Measured long-cue review with clickable yellow `Cue dài` rows.
- Bounded cue-scoped translation repair and unchanged WAV-cache reuse.
- Explicit `Thêm nguyên voice vào Timeline` fallback; complete voice retained
  beyond source-video duration without automatic video stretching.
- Single stateful OCR start/pause/resume button.
- Per-project TTS history deletion while retaining global WAV cache.

## Regression checks

- Override still enforces audio QA, cue coverage, PCM format and overlap guard.
- Failed override releases the UI request lock and can create a new attempt.
- Ordinary activity records do not gain null timing metadata.
- a53 was closed only after the state DB reported no active job.

## Unresolved

- None for the accepted a54 scope.
