---
title: "VieNeu 3.8.1 runtime migration"
description: "Replace pinned VieNeu 3.3.0 with supported 3.8.1, benchmark clone throughput and remove only the obsolete runtime after release validation."
status: completed
priority: P1
effort: "1d"
tags: [tts, vieneu, cuda, performance, release]
created: 2026-09-22
---

# VieNeu 3.8.1 runtime migration

## Overview

Upgrade DUCAI's quality-first cloned-voice provider from the pinned VieNeu 3.3.0
SDK to 3.8.1, adopt upstream fused CUDA behavior, retain application QA/cache
guarantees and remove the old runtime only after a same-machine A/B benchmark
and packaged smoke test succeed.

## Outcome contract

- Outcome: installed and packaged VieNeu 3.8.1 produces complete, valid cloned
  voice faster than or no worse than the current engine on the same workload.
- Constraints: preserve projects, clone assets, model/cache data and unrelated
  in-flight Gemini changes; never silently reuse unverified 3.3 artifacts as
  3.8 output.
- Non-goals: VieNeu v4, v3 Nano, TTS workflow redesign or provider-side speech
  speed changes.
- Acceptance: real A/B evidence, focused/full gates, release manifest, packaged
  smoke test, and safe deletion of only the obsolete 3.3 runtime.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | [Baseline benchmark](./phase-01-start.md) | Completed |
| 2 | [Runtime and provider migration](./phase-02-runtime-and-provider-migration.md) | Completed |
| 3 | [Verification and compatibility](./phase-03-verification-and-compatibility.md) | Completed |
| 4 | [Release and old runtime cleanup](./phase-04-release-and-old-runtime-cleanup.md) | Completed |

## Dependencies

The baseline must be captured before runtime mutation. Cleanup depends on every
code, audio, benchmark and release gate succeeding.

## Success Criteria

- [x] VieNeu 3.8.1 is the only installed DUCAI VieNeu runtime.
- [x] Existing authorized clone workflow produces complete 48 kHz WAV output.
- [x] Before/after measurements use identical profile, cues and hardware.
- [x] Cache/resume never crosses incompatible engine fingerprints silently.
- [x] Focused tests, full pytest, Ruff, compileall, QML lint and release build pass.
- [x] Old 3.3 runtime is removed without deleting profiles, model cache or projects.

<!-- slug: vieneu-381-runtime-migration -->
