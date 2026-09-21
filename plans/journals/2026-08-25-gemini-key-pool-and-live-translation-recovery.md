---
title: Gemini key pool and live translation recovery
date: 2026-08-25
summary: "Fixed structured API output, bounded named-key failover, live cue replay, and Gemini Web recovery in 2.0.0a12."
---

# Gemini key pool and live translation recovery

## What happened

Gemini API job failed with `Gemini response translations must be an array` because every request used a generic object response schema and `gemini-3.5-flash-lite` legally returned `{}`. Review also exposed incomplete invalid-key classification, project-switch delta loss, stale terminal status, raw Playwright exceptions bypassing recovery, and secret-shaped credential names.

## Decision

Use explicit purpose-specific JSON schemas. Keep API keys only in Windows Credential Manager and persist ordered reference names. Fail over sequentially only after verified quota or authentication/key-invalid failures. Checkpoint before emitting live deltas, scope replay by project/job/cursor, and treat terminal project data as authoritative. Recreate Gemini Web sessions after both classified transient errors and raw browser target failures.

## Verification

Full pytest reached 100%; Ruff, compileall and QML lint exited 0. Clean `2.0.0a12` packaging exited 0. All 2,829 manifest hashes matched, the PE subsystem is GUI, and packaged-app smoke testing rendered the multi-key API controls before closing cleanly.

## Next steps

Run one real imported-project translation or resume an existing cache to consume provider quota and observe live Vietnamese cue updates under production network conditions.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
