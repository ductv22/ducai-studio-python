---
title: DUCAI STUDIO a54 voice timing fallback
date: 2026-08-27
summary: Added bounded long-cue repair and an explicit uncropped voice-to-Timeline fallback.
---

# DUCAI STUDIO a54 voice timing fallback

## What happened
TTS QA had approved per-cue WAV cache but could not publish D1 when the final voice ended after the source video. The existing repair path also risked marking timing chunks as QA_REVIEW, a terminal state skipped by the translation worker.

## Decision
Keep automatic repair cue-scoped and bounded. Add an explicit fallback that reuses approved WAV cache, extends only the voice timeline, preserves every spoken sample, and never changes video duration automatically. Ordinary audio QA, cue coverage and overlap guards remain mandatory.

## Evidence
Added worker, service, view-model and QML contract tests. Full gate: 443 passed, 1 skipped; Ruff, compileall and QML lint PASS. Release 2.0.0a54 manifest verified 2,840 files with zero mismatches and frozen GUI/Job Manager smoke-started from the a54 directory.

## Next steps
Use `Thêm nguyên voice vào Timeline` only when manual voice/video speed adjustment is preferred over further text condensation.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
