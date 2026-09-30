---
title: TTS responsiveness and bounded warm engine
status: completed
priority: P1
effort: medium
tags: [tts, performance]
created: 2026-09-25
---

# TTS responsiveness and bounded warm engine — a104

Status: completed. See [verification](verification.md) for tests, review limitations,
benchmarks and actual EXE cold/warm smoke results.

## Contract

Outcome: truthful, timely TTS startup/progress and reuse of a loaded VieNeu engine
between sequential jobs. Preserve voice quality, batch size 32, timestamps,
cache fingerprints, checkpoint ordering and job controls. Do not stop the user's
current a103 job, mutate production SQLite, change models or retain VRAM forever.

Evidence: production job 46ed842e had 8,587 cues and 537 cache progress events;
worker elapsed 23.75 s at end of cache scan, while manager persisted that event
195 s after its initial progress. Each event synchronously triggers persistence,
Multi membership/status and a full jobs.get (large cue configuration). Progress
is emitted per 16 cache cues with no time gate. Provider host is closed at job
end, so subsequent workers load their own model again.

Correction to prior diagnosis: persistence timestamps are receipt timestamps,
not inference timestamps. Exact SQLite vs Multi overhead is not yet separately
profiled; the confirmed fault is the entire unbounded telemetry consumer path.

## Delivery

1. Bound producer cache progress by elapsed time; preserve first/last and phase
   boundaries. Coalesce receiver progress before expensive work; never coalesce
   checkpoint, ACK, failures or terminal results. Avoid full job config reads
   on telemetry and duplicate Multi status publication.
2. Report actual runtime/model/binding/inference stages; no fabricated percent.
3. Manager-owned authenticated local engine reuse, exclusive job leases,
   identity validation and voice rebinding; 120 s idle expiry; idle eviction for
   OCR; discard on unhealthy termination; shutdown ownership. Standalone fallback
   for simultaneous jobs or custom hosts, without changing inference behavior.
4. Regression/focused/full tests, Ruff, compileall, QML lint, independent review,
   build a104 and isolated smoke/benchmark. No production workload restart.

## Acceptance

- Large cache scan emits O(time) rather than O(cues) progress; final count exact.
- Burst progress delivers newest state without delaying lifecycle ordering.
- Normal TTS cache/resume and pause/cancel tests unchanged.
- One warm engine across two jobs, correct voice binding, bounded idle and
  cancellation cleanup; invalid credentials/identity rejected.
- Real bounded synthesis produces valid WAV; compare cold/warm when production
  GPU is free (otherwise explicitly report resource blocker).
- New release manifest verified; old release and production data retained.

Rollback: use a103; changes require no DB migration or cache migration.
