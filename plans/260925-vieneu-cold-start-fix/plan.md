---
title: VieNeu cold-start health check repair
status: completed
created: 2026-09-25
---

# VieNeu cold-start health check repair

## Outcome and boundary

Restore TTS on the installed 3.8.1 runtime without changing voice identity,
translations, cache fingerprints or other running applications. Prove actual
speech generation with the failing job's clone profile; keep diagnostics honest.
No model upgrade, GPU takeover, data deletion or unrelated OCR changes.

## Evidence and cause

- App a102 fails before first synthesis with `TtsRuntimeUnavailable`.
- `providers/tts/vieneu.py` health probe kills subprocess after 10 seconds and
  caches the empty result through `lru_cache`.
- Same interpreter imports torch in 23.328 seconds, reports 2.8.0+cu128,
  CUDA available and VieNeu 3.8.1. This is a false unavailable classification.
- Scope: clone/preset provider health, worker preflight and batch startup;
  existing catalog/engine-host/worker tests cover callers.

## Phases

1. [x] Inspect read-only job evidence and reproduce probe failure.
2. [x] Regression tests, bounded cold-start allowance, uncached transient failures,
   Vietnamese diagnostic distinctions.
3. [x] Real clone synthesis, focused/full tests and static checks.
4. [x] Review, release a103, verify packaged behavior, document result.

## Acceptance

Longer-than-10-second initialization succeeds; failed probes are retried on next
call while successful probes are reused; corrupted/non-CUDA runtimes stay blocked;
real WAV is nonempty and decodable; no project/cache mutation in smoke test.

## Verification so far

- Five regression cases fail before the patch and pass afterward.
- Focused suite: 92 passed, 1 opt-in Piper test skipped.
- Full suite: 742 passed, same single skip; Ruff, compileall and QML lint pass.
- Real clone replay, first 64 job cues: 64/64 valid 48 kHz WAVs.
- First 32 cues (load included): 37.953 seconds / 58.070 seconds audio.
- Next 32 cues (warm): 1.750 seconds / 69.040 seconds audio.
- Reviewed cache boundaries: process failures, timeout, JSON errors and non-object
  responses escape cached helper; only parsed probe results are reused. Existing
  runtime/profile/package file stamps remain cache invalidation keys.
- Reviewed UI boundary: voice catalog uses filesystem readiness rather than calling
  the expensive probe from the GUI; health checking remains outside that path.
- Public provider identifiers, job specs, model versions and SQLite schema unchanged.
- Compiled a103 worker: 32/32 cues completed in 29.734 seconds; master 48 kHz,
  4,905,600 frames, QA PASS, coverage 1.0, no timing issues/warnings.
- Release manifest verifies all 2,869 files; compiled EXE hash matches the fresh
  deployment output. Initial suspicion of stale packaging was disproven by the
  PySide finalize copy and hash comparison; packaging script was not changed.
- No production job was resumed automatically; the user's a102 process is untouched.
