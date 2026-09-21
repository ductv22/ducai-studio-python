---
title: a47 translation history and cache safety
date: 2026-08-26
summary: "Safe active-history deletion, compatible cache selection, single translation guard, blocker-only resume, and verified a47 package."
---

# a47 translation history and cache safety

## What happened

Translation history deletion rejected active jobs, cache preview favored the newest partial job, UI polling could reopen the Translate button before create returned, and resume repeated verified suffix chunks. Targeted CJK repair could checkpoint a still-contaminated response before the next QA pass caught it.

## Decision

Delete active history through cancel-and-wait, select caches only by exact source fingerprint, rank by completed cue count then blockers, preserve the selected preview against unrelated late deltas, and enforce one active translation on create/start/retry/resume. Resume only QA_REVIEW chunks. Reject CJK repair before cache or live delta.

## Evidence

Release 2.0.0a47 built without opening the EXE. Ruff, compileall and QML lint passed. Pytest collected 410 tests: 409 passed, one skipped. All 2,839 manifest hashes match. Read-only DB check preserved the 13,385-cue cache and both 434-cue interrupted jobs.

## Next steps

Run interactive smoke only after the user permits desktop control. Use per-row X to remove accidental jobs while keeping the 13,385-cue cache; avoid Clear all unless every cache should be deleted.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
