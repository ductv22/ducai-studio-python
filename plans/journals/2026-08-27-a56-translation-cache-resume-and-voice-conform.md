---
title: a56 translation cache resume and voice conform
date: 2026-08-27
summary: "Shipped a56 with immediate selected-ledger resume, cache-only voice conform, and verified Win64 packaging."
---

# a56 translation cache resume and voice conform

## What happened
Translation cache discovery and preview are asynchronous. The selected cache ID was updated before the delayed preview supplied job/provider/state, so a terminal cache could remain bound to stale busy UI state and expose no valid resume action.

## Decision
Treat the selected immutable ledger record as the immediate source of truth. Bind its exact job ID and lifecycle state as soon as the list response selects it; use preview only for cue/QA display. Resume retries the same translation job and preserves checkpoints. Voice conform remains a cache-only post-process with absolute sample-frame cache identity.

## Evidence
Full pytest: 479 passed, 1 skipped. Focused cache/UI/conform tests: 115 passed. Ruff, compileall and QML lint passed. Release 2.0.0a56 manifest verified with zero missing or mismatched files; packaged QML and wheel contain the resume action and binding helper.

## Next steps
User may smoke-test a56 when convenient. No live database, project cache or running GUI was touched during release verification.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
