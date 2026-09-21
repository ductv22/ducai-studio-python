# Multi worker and checkpoint stability

## Status

Completed and packaged for `2.0.0a93`.

## Outcome

Keep a slow OCR/TTS worker alive until it acknowledges startup, stop an
interrupted Multi child from relaunching without a user action, and prevent
successive technical checkpoints from retaining every superseded artifact.

## Constraints and non-goals

- Preserve schema version 5, a92 projects, jobs, fingerprints and checkpoints.
- Resume the exact child job and cache; do not call the provider again for
  already valid cached cues.
- Do not delete project media, SRT, WAV, review/final artifacts or user cache.
- This release does not redesign OCR, translation, TTS or the Multi retry
  budget.

## Implementation

- [x] Retry the correlated `start` after the launcher hand-off, then wait on an
  unbounded live-worker deadline; never synthesize a `stop` from a delayed ACK.
- [x] Persist an unexpected child interruption once, close duplicate running
  attempt rows and require explicit **Tiếp tục Multi video** before relaunch.
- [x] Reuse the exact interrupted child and its `latest` stage checkpoint.
- [x] Delete only an unreferenced superseded `technical` checkpoint artifact
  after the replacement checkpoint transaction succeeds.
- [x] Reject OCR/TTS/checkpoint work early when the artifact volume lacks a
  conservative free-space reserve and return Vietnamese recovery guidance.
- [x] Show starting, cache restore, pause-pending and interrupted states from
  the durable batch/child lifecycle instead of attempt-history guesses.
- [x] Pass focused and full quality gates and package the a93 Windows release.

## Acceptance criteria

- A worker taking more than six seconds to import cannot be stopped by startup
  control retry.
- An interrupted child creates no automatic retry loop and no duplicate live
  attempt for the same project/stage/pass/job.
- Explicit resume starts the exact job once from its current checkpoint.
- Replacing `latest` removes only its former technical artifact; a separate
  validated review artifact remains.
- Multi cards use per-stage progress and distinguish starting, restoring,
  pause pending, paused and interrupted states.
- Focused tests, full pytest, Ruff, compileall, QML lint, build and manifest
  verification pass for `DUCAI-STUDIO-2.0.0a93-win64`.

## Verification

- Focused Multi/Job Manager/repository/UI suite: 257 passed.
- Full suite: 672 collected, 671 passed and one environment-gated test skipped.
- Ruff, compileall and QML lint: passed.
- Windows release: `DUCAI-STUDIO-2.0.0a93-win64`.
- Manifest: 2,841 tracked files, zero missing and zero SHA-256 mismatches.
- Packaged wheel contains the reviewed checkpoint, coordinator, server,
  service, view-model and QML sources byte-for-byte.
