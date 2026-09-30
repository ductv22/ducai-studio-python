---
title: Fix Gemini API transport and stale cache resume
date: 2026-09-21
summary: "Fixed nested Gemini usage serialization, provider-scoped key pools, and provider/model-safe translation resume in a100."
---

# Fix Gemini API transport and stale cache resume

## What happened
Gemini API returned HTTP 200, but translation failed while serializing nested usage metadata with `FrozenJsonMapping is not JSON serializable`. Separately, Gemini retry pools absorbed unrelated Windows Credential Manager references, and terminal translation controls could resume a stale Gemini Web job after the user selected Gemini API.

## Decision
Recursively thaw provider usage before worker protocol serialization. Keep credential pools provider-scoped. Bind terminal cache resume to the exact provider/model pair; when a new API key is pasted for that same job, store it and retry the existing checkpoint.

## Outcome
Added red-green regressions, updated user guidance, passed 196 focused tests and the full 721-test suite (1 skipped), and built release 2.0.0a100. No database migration or project/cache deletion was needed.

## Next steps
Revoke the temporary Gemini key exposed in chat. Smoke-test the packaged EXE after the currently running a99 instance is closed, to avoid two app versions sharing the same data root.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
