---
title: Complete a108 Paddle OCR and S2 repair
date: 2026-09-28
summary: "Recovered supplied short dialogue and scene cards, closed watermark/restart/performance gaps, and shipped verified a108."
---

# Complete a108 Paddle OCR and S2 repair

## What happened

The 997.4-second Paddle OCR fixture still lost clear short dialogue because Fast and Quality each supplied only one correct observation, while scene-card evidence was filtered before variants could corroborate it. A first repair exposed additional review findings: two-frame `独播` could survive without a Bilibili anchor, transition events were not checkpointed, event bounding was quadratic, and adaptive OCR replayed the video after Fast.

## Decision

Keep Whisper out of a108. Bridge only high-confidence multi-character Fast/Quality evidence; keep one-glyph safety strict. Cluster S2 variants before the strong-reading gate, accept exceptional single cards only outside corners, and reject only geometry-bound known platform badges. Persist transition history and process burst/S2 samples together inside each Fast window with stable 30-second slots.

## Result

Focused combined regressions 16 passed; OCR suite 92 passed; downstream blast radius 145 passed; full non-Whisper suite 818 passed with one expected skip. Full pytest, Ruff, compileall, QML lint and diff checks passed. Clean a108 package contains 2,871 verified manifest files; packaged Job Manager runtime/import smoke passed and shut down cleanly.

## Next steps

Use Paddle OCR for the next real project and re-run OCR rather than trusting the older cached SRT. Inspect S1 at 00:58/03:51 and S2 at 00:08/01:58 before translation/export. Whisper remains available for compatibility but is not part of this release's validated workflow.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
