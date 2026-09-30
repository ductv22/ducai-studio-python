---
title: Repair VieNeu cold-start health checks in a103
date: 2026-09-25
summary: Fix false runtime-unavailable timeout and verify real cloned audio in the release EXE
---

# Repair VieNeu cold-start health checks in a103

## What happened

VieNeu health killed torch cold import after 10 seconds and cached the failure. Real import took 23.328 seconds on the target Windows machine despite a valid 3.8.1/CUDA 12.8 runtime.

## Decision

a103 gives the probe a bounded 90-second deadline, retains successful file-stamped caching, does not cache subprocess/JSON failures, and distinguishes timeout in Vietnamese. No model, profile, schema, provider fingerprint or production cache change.

## Verification

Five regression cases failed before and passed after. Full suite: 742 passed, 1 optional Piper skip. Lint, compile and QML lint passed. Original clone profile produced 64 valid WAVs; warm batch generated 69.04 seconds audio in 1.75 seconds. Fresh EXE completed 32 cues, master WAV and QA PASS. All 2,869 manifest hashes matched. Initial suspicion of stale packaging was disproven by PySide finalize copying and EXE hash verification.

## Next steps

User closes a102, opens a103, and resumes the production job. Full 8,587-cue job was not automatically resumed. Test processes exited; original app and project were preserved.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
