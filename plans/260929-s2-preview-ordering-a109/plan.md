---
title: "Restore chronological S2 preview lookup a109"
description: "Make translated scene text visible at the playhead and remove S1 duplicates from new S2 OCR results."
status: completed
priority: P1
effort: "small"
branch: "ducai-studio-python-a98"
tags: [bugfix, ocr, timeline, scene-text, release]
created: 2026-09-29
---

# Restore chronological S2 preview lookup a109

## Outcome Contract

- Translated S2 appears in Timeline Preview at its own timestamp without requiring export.
- Appending S2 after all S1 cues cannot break playhead lookup.
- Full-frame OCR does not duplicate an existing S1 reading into S2 when text, time and image region agree.
- Existing S1 editing, live translation updates and export remain compatible.

## Constraints and Non-goals

- Preserve SQLite/project schemas and existing project/cache decoding.
- Do not change Paddle models or reintroduce Whisper work.
- Do not alter the currently running a108 application or its project data.
- Keep genuine scene labels that repeat spoken words when they occupy a different image region.

## Acceptance Criteria

- [x] Regression reproduces the non-chronological `S1...S1,S2` source order.
- [x] Cue model is globally ordered by timestamp before binary-search indexes are built.
- [x] The real project returns Loạn Tinh Hải, Hàn Lập, Hải Vương Thú and Tịch Tà Thần Lôi at their playheads.
- [x] Duplicate S1/S2 classifier regression passes.
- [x] Focused/full tests, Ruff, compileall, QML lint and release build pass.
- [x] Clean `DUCAI-STUDIO-2.0.0a109-win64` package is verified.

<!-- slug: s2-preview-ordering-a109 -->
