---
title: DUCAI STUDIO a71 Multi OCR review retry
date: 2026-08-29
summary: "Stopped deterministic OCR review results from consuming Multi's immediate retry budget and shipped a verified a71 release."
---

# DUCAI STUDIO a71 Multi OCR review retry

## What happened

A 7h42m video completed its Paddle Fast/Quality OCR pass in about 80 minutes, but Multi video kept retrying the same `ocr_review_required` QA result. The live batch reached five failed attempts and paused during attempt six even though coverage was 96.85% and the review SRT/checkpoint had already been stored.

## Root cause

`MultiVideoCoordinator` sent every failed child to `MultiVideoRepository.record_stage_failure`, whose decision used only the durable failure count. It had no input distinguishing transient engine/provider failures from deterministic OCR review results.

## Decision and changes

Added an explicit retryability input owned by the coordinator. Only `(ocr, ocr_review_required)` bypasses immediate retries: it is atomically deferred on the first pass and fails after the single final pass. Other failures keep the existing one-initial-plus-five-retry contract. Multi cards now say OCR completed, show the blocker count and use warning color; the review SRT/cache remain unchanged.

## Verification

The regression was reproduced before the change. Focused tests and a copied live a69 database verified both a fresh review result and the existing retry-count-5 case. Final gates passed with 603 tests and 1 skipped, plus Ruff, compileall and QML lint. The a71 manifest had zero hash failures and the isolated packaged EXE smoke exited 0 without orphaned children.

## Next step

Close the paused a69 application cleanly, start a71 with the same app data, and press **Tiếp tục Multi video**. The paused job resumes from its checkpoint and is deferred instead of entering another immediate retry loop.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
