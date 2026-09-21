---
title: DUCAI STUDIO a93 Multi worker and checkpoint stability
date: 2026-08-31
summary: "Stopped false TTS startup cancellation, interruption retry loops, checkpoint growth and misleading Multi states."
---

# DUCAI STUDIO a93 Multi worker and checkpoint stability

## What happened

Multi TTS could remain at startup with no correlated ACK, while the manager's generic retry policy eventually stopped a still-live worker. An interrupted child was then interpreted as retryable work, which manufactured repeated attempt rows. Technical checkpoint snapshots also accumulated and the Multi cards collapsed startup, cache restore, pause-pending and interruption into misleading labels.

The final verification uncovered a separate Windows WAL regression: sqlite3.Connection used as a context manager did not close the replay-probe handle before a long artifact copy. Hard-killing the fixture at copy_started could make the next PRAGMA synchronous = NORMAL fail with sqlite3.OperationalError: disk I/O error.

## Decision

Start control now retries indefinitely while the owned worker remains alive and records one slow-start diagnostic after 30 seconds. Unexpected interruption becomes a durable batch boundary requiring explicit resume of the same child/checkpoint. Replacement GC is restricted to unreferenced technical artifacts. The checkpoint replay probe is explicitly closed before copying. Disk preflight errors have a dedicated Vietnamese action instead of generic API/model advice.

## Evidence

Focused suite: 257 passed. Full suite: 672 collected, 671 passed and one environment-gated skip. Ruff, compileall and QML lint passed. Release DUCAI-STUDIO-2.0.0a93-win64 tracks 2,841 files with zero missing or SHA-256 mismatches.

## Next steps

No interactive GUI smoke was run because the user requested no computer control. The packaged source/QML and manifest were verified without launching the application.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
