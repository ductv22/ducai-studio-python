---
title: "Repair real-world OCR consensus and S2 a108"
description: "Recover clear dialogue split across Fast/Quality evidence and publish real scene-title cards without admitting Bilibili fragments."
status: completed
priority: P1
effort: "medium"
branch: "ducai-studio-python-a98"
tags: [bugfix, ocr, subtitles, scene-text, release]
created: 2026-09-29
---

# Repair real-world OCR consensus and S2 a108

## Outcome Contract

- On the 997.4-second `Video Project 8` fixture, clear dialogue at 00:58 and 03:51 is retained as S1 when Fast and Quality each provide one agreeing high-confidence observation.
- `乱星海` around 00:08 and `海王兽` around 01:58 become translated S2 scene cards visible in Timeline Preview after translation.
- Bilibili fragments such as `独` and `独播` never become S2.

## Constraints and Non-goals

- Preserve Python 3.12/PySide6 contracts, SQLite schema, old projects and OCR cache decoding.
- Do not lower the global one-character dialogue gate or admit a single weak OCR observation.
- Do not route scene text into TTS, D1 or user-facing dialogue SRT.
- Do not change Paddle model/runtime in this repair.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | [Scout](./phase-01-start.md) | Complete |
| 2 | [Diagnose](./phase-02-diagnose.md) | Complete |
| 3 | [Implement](./phase-03-implement.md) | Complete |
| 4 | [Verify and prevent](./phase-04-verify.md) | Complete |
| 5 | [Review](./phase-05-review.md) | Complete |
| 6 | [Finalize release](./phase-06-finalize.md) | Complete |

## Success Criteria

- [x] Exact evidence replay creates S1 for `好吧` and `在这呢`.
- [x] Exact evidence replay creates accepted S2 for `乱星海` and `海王兽`.
- [x] Exact evidence replay creates no S2 from Bilibili companion glyphs.
- [x] Existing one-glyph noise, ambiguous center dialogue and watermark regression tests remain green.
- [x] Focused/full tests, Ruff, compileall, QML lint, clean a108 build and packaged smoke pass.

<!-- slug: repair-real-world-ocr-consensus-and-s2-a108 -->
