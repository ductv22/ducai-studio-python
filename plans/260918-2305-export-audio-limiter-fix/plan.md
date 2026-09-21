---
title: Export Audio Limiter Fix
status: completed
priority: P1
effort: medium
branch: none
tags: [export, audio, ffmpeg, portable]
created: 2026-09-18
completed: 2026-09-18
owner: codex
---

# Export Audio Limiter Fix

## Overview

Repair distorted near-full-scale audio in mixed-track exports, regenerate the affected video
without overwriting the user's original, and publish a portable Windows build for another
machine.

## Outcome

Exports that mix original audio with the Vietnamese voice retain the selected track volumes,
remain synchronized, and keep a safe peak ceiling instead of being auto-raised to 0 dBFS.

## Constraints

- Preserve the user's A1 volume of 18% and D1 volume of 100%.
- Preserve video, burned subtitles, duration, and current public project schema.
- Do not overwrite the existing final MP4.
- Do not include projects, media, credentials, caches, cookies, or clone profiles in the
  portable package.
- The source reconstruction has no Git metadata, so verification must rely on tests, hashes,
  manifests, and empirical media probes rather than commit history.

## Non-goals

- Do not change VieNeu synthesis, voice speed, subtitle timing, or the user's mix choices.
- Do not redesign the export pipeline or alter unrelated UI behavior.

## Phases

- [x] Reproduce and measure the affected final MP4 and source WAV.
- [x] Trace the mixed-audio filter graph and identify the root cause.
- [x] Add a regression test that fails for the unsafe limiter contract.
- [x] Implement the minimal limiter correction and bump the alpha version.
- [x] Run focused export tests, then lint, compile, and the full test suite.
- [x] Regenerate and probe a repaired MP4 from the user's sources.
- [x] Build and validate the normal and portable Windows releases.
- [x] Review the changed contract and record final evidence.

## Dependencies

- Local FFmpeg/ffprobe 8.1.1.
- Existing Python 3.12 project environment and PySide6/Nuitka toolchain.
- Existing `DUCAI_STUDIO_DATA` runtimes and model caches for portable packaging.

## Acceptance Criteria

- The pre-fix regression test fails against `alimiter=limit=0.95` with implicit auto-level.
- The generated filter explicitly disables limiter auto-level, compensates look-ahead latency,
  and applies a -1.5 dBFS ceiling.
- The repaired video retains one H.264 video stream and one 48 kHz AAC audio stream, matches
  the source duration within the existing export tolerance, and measures below the ceiling.
- Focused and full project quality gates pass.
- A versioned portable package is present with a valid manifest and no forbidden private data.

## Final Evidence

- Full suite: 690 passed, 1 environment-marked skip.
- Export regression suite: 10 passed; portable runtime suite: 4 passed.
- Repaired MP4 peak: -3.5 dBFS after AAC encode; original peak: -0.0 dBFS with
  13,648 samples in the 0 dB histogram bucket.
- Original and repaired H.264 video streams share SHA-256
  `e87116a5828075db2b395ba8d39a8d3fd05eff08c6a8c1e3bc3c54ef716110e3`.
- Portable manifest: version 2.0.0a97, 59,374 files, 14,815,225,383 bytes,
  zero forbidden private roots and zero VieNeu clone profiles.
- Portable archive: 5,416,389,215 bytes; 7-Zip integrity test passed; SHA-256
  `33be15da0c204adb7128cda41429923fcc17c2d738eea6c9aedf5982fa3b2bae`.
