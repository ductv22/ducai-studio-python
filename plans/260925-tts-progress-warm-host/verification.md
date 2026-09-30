# Verification record

## Telemetry

- Regression restored original per-16-cue condition: FAILED with 538 updates
  versus at most 12 allowed. Restored elapsed-time gate: PASS, exact last count.
- Isolated 8,587-cue SQLite benchmark: full job read 274.776 ms/event, compact
  project owner read 0.377 ms/event. Burst of 538 progress snapshots: 18.659 ms,
  one persisted newest snapshot. Earlier run 7.601 ms; timings vary with load.
- Checkpoint, control and terminal events are not coalesced; pending latest
  progress is flushed before them. Burst and timer/per-job isolation tests pass.

## Warm-host review and checks

Reviewed identity, voice isolation, token lifetime, cancellation, idle eviction,
late-session cleanup and process ownership locally. Independent reviewer could
not run due to upstream quota/service failure; do not claim independent approval.

- Exact runtime/package/host identity plus data root, batch capacity and timeout.
- One-use token, loopback only, bounded JSON, launch config manager-owned.
- Voice reset/rebind each session. Concurrent lease uses standalone fallback.
- Cancel kills owned host tree; old session cannot discard newer session host.
- 120 s idle expiry; OCR evicts idle model; close rejects new registration.
- Host launcher assigned to process tree before launch gate, byte-exact gate
  read avoids swallowing first child command. EOF wakes response waiters.
- Tests cover real lightweight host subprocesses, changed voice, credentials,
  idle identity, cancellation and Windows process exit.

## EXE validation finding

Initial compiled smoke produced valid 48 kHz WAV and PASS QA in both runs
(18.265 s / 16.453 s), but did NOT retain model. Follow-up source manager with
compiled workers proved worker exit 3 after success; stderr explicitly reported
`_enter_buffered_busy` on stdin during interpreter shutdown. Retaining the model
despite unhealthy exit was rejected. Fixed TTS reader to use unbuffered OS pipe
reads. New subprocess regression keeps stdin open and requires clean exit 0.

First full gates: 755 passed, one optional Piper runtime test skipped. After
shutdown fix: focused 41 pass; final full suite 756 passed, one optional Piper
test skipped in 74.30 seconds. Ruff, compileall and QML lint passed.

Final rebuilt EXE manager + worker smoke in `_a104_warm_verified`: first two-cue
job 16.032 s, second two-cue job 2.250 s; both 48 kHz WAV, QA PASS, coverage 1.0.
Master frames 583926 / 572688. These are short, different-text jobs, not a claim
about total production-video speed. Manager exited 0. Release a104 manifest
verified all 2,869 hashes with zero mismatches. Initial unverified build retained
under `DUCAI-STUDIO-2.0.0a104-preverification-win64`; deliver only the normal a104
directory. Production project was never written by tests and no user process
was stopped. Smoke manager and descendants were owned and shut down.
