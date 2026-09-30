---
title: Optimize TTS progress and bounded warm engine
date: 2026-09-25
summary: "Coalesce telemetry, avoid large config reads, and validate safe warm-engine reuse"
---

# Optimize TTS progress and bounded warm engine

Coalesce telemetry, avoid large config reads, and validate safe warm-engine reuse

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

## Evidence and decisions

The apparent slow first voice batch included stale telemetry: 538 cache updates
caused expensive full job reads. A production-shaped 8,587-cue benchmark measured
~275 ms per full read versus ~0.4 ms for compact ownership. Producer cadence and
receiver coalescing preserve the newest progress while durable lifecycle events
remain ordered. Batch size, voice settings and cache identities were not changed.

Implemented a manager-owned exclusive warm engine with 120-second idle expiry,
per-session voice reset, authenticated local JSON and bounded process ownership.
Real EXE smoke exposed a defect that unit tests alone missed: successful workers
aborted at Python finalization while a daemon held the buffered stdin lock. Fixed
the command reader rather than accepting unhealthy exit as safe to reuse.

Full evidence and remaining release checks are tracked in
[verification](../260925-tts-progress-warm-host/verification.md). No production
DB writes, user-process termination, commits or remote publication were performed.
AgentWiki publish skipped.

## Final result

Rebuilt a104 passed 756 tests (one optional Piper skip), lint/compile/QML gates,
and all 2,869 manifest hashes. Real isolated EXE jobs: first 16.032 s, warm second
2.250 s, both valid WAV and QA PASS. Earlier 18/16-second smoke was rejected
because it did not actually retain the engine. User app/project untouched.
