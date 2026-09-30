---
title: "Strong subtitle concealment and watermark OCR filtering a106"
description: "Make automatic source-subtitle concealment stronger and remove Bilibili-style channel watermarks from OCR without filtering real dialogue."
status: completed
priority: P1
effort: "medium"
tags: [ocr, effects, export, subtitles, release]
created: 2026-09-28
---

# Strong subtitle concealment and watermark OCR filtering a106

## Outcome contract

- New projects use stronger automatic source-subtitle blur and a default
  **Che kín** layer; users can toggle Che kín independently from blur.
- Preview and export render the same concealment geometry and strength.
- OCR removes a recognized Bilibili logo together with adjacent channel-name
  and numeric watermark fragments on the same visual row.
- Repeated channel markers such as `4K超清漫剧|3473`, `呼啦呼啦2313` and
  `小李看漫剧` are removed when they are proven overlays, without treating
  ordinary dialogue containing digits or the word `漫剧` as a watermark.

## Constraints and non-goals

- Hard subtitles are baked pixels; this release conceals them in rendered
  output and does not claim to reconstruct the original background.
- Existing projects preserve their saved blur strength and remain visually
  unchanged until the user enables Che kín or resets effect defaults.
- No SQLite migration and no destructive modification of source video.
- SRT timing and OCR boxes remain the authority for when and where concealment
  appears.

## Goals

| # | Goal | Priority |
|---|------|----------|
| 1 | Preserve backward-compatible concealment contracts | P1 |
| 2 | Render stronger blur and Che kín consistently | P1 |
| 3 | Filter anchored and proven Bilibili-style watermarks | P1 |
| 4 | Verify and build `2.0.0a106` | P1 |

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | [Phase 1: Backward-compatible contracts](./phase-01-start.md) | Completed |
| 2 | [Phase 2: Strong automatic subtitle concealment](./phase-02-strong-automatic-subtitle-concealment.md) | Completed |
| 3 | [Phase 3: Anchored watermark filtering](./phase-03-anchored-watermark-filtering.md) | Completed |
| 4 | [Phase 4: Verification, release and documentation](./phase-04-verification-release-and-documentation.md) | Completed |

## Success Criteria

- [x] A newly created auto blur uses radius 46, wider safe padding and Che kín enabled.
- [x] Che kín can be toggled without disabling blur; preview and FFmpeg export agree.
- [x] Old snapshots missing the new field decode to their prior appearance.
- [x] A recognized Bilibili segment removes only spatially adjacent channel fragments.
- [x] The three supplied watermark forms are removed while real Chinese dialogue survives.
- [x] Focused/full tests, Ruff, compileall, QML lint, packaged smoke and manifest verification pass.

<!-- slug: strong-subtitle-concealment-and-watermark-ocr-filtering-a106 -->
