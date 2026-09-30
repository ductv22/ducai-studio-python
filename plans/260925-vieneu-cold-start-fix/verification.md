# VieNeu cold-start fix verification

Date: 2026-09-25. Scope: a103 health-probe repair, not a model upgrade.

## Diagnosis

The a102 TTS job failed before the first batch with `TtsRuntimeUnavailable`.
Replaying its exact provider health call took 11.047 seconds and returned unavailable.
Running the interpreter independently with a sufficient deadline imported torch in
23.328 seconds and confirmed VieNeu 3.8.1, torch 2.8.0+cu128 and available CUDA.
The provider's 10-second subprocess timeout and caching of failure results were
therefore a reproducible false-negative path. No runtime reinstall was needed.

## Change and review

- Bounded probe deadline: 90 seconds; timeout has a specific Vietnamese message.
- Cached helper propagates subprocess and decode exceptions. Public wrapper maps
  them to safe diagnostic codes without persisting stderr, credentials or paths.
- Successful results retain file-stamp invalidation and bounded cache size.
- Runtime, provider IDs, fingerprints, SQLite schema, profiles and voice QA unchanged.
- GUI catalog does not invoke the expensive probe on the main thread.
- Initial stale-packaging suspicion was disproven: PySide finalizes by copying the
  generated distribution before release assembly. Fresh EXE hashes matched.

## Fresh verification

| Check | Result |
|---|---|
| Pre-fix regression cases | 5 failed |
| Post-fix probe/catalog | 14 passed |
| Focused TTS/runtime | 92 passed, 1 opt-in Piper test skipped |
| Full suite | 742 passed, 1 same skip |
| Ruff / compileall / QML lint | Passed |
| Real provider, original job profile | 64/64 cues, valid 48 kHz WAV |
| First batch, load included | 37.953 s for 58.070 s audio |
| Second batch, model warm | 1.750 s for 69.040 s audio |
| Fresh EXE worker replay | 32 cues succeeded in 29.734 s |
| EXE master QA | PASS, coverage 1.0, timing issues/warnings 0 |
| Release manifest | 2,869 file hashes verified |

## Handoff and limits

Build: `outputs/DUCAI-STUDIO-2.0.0a103-win64`. Close a102 and open a103, then
resume the failed TTS job. User projects, source translation, profile and production
WAV cache were not edited. Test outputs live in isolated `_a103_tts_smoke` and
`_a103_frozen_tts_smoke` output folders; test processes exited.

This verifies the original failure and two short real synthesis paths, not completion
of the entire 8,587-cue production job. Warm batch speed is not end-to-end throughput:
first model loading, cache checking, audio processing, assembly and QA add time.
