---
title: "Phase 3: Anchored watermark filtering"
phase: 3
status: completed
priority: P1
effort: "4h"
dependencies: [1]
---

# Phase 3: Anchored watermark filtering

## Overview

Remove Bilibili-style watermark clusters before they become subtitle cues, with repeated-overlay fallback for OCR variants.

## Requirements

- [x] Treat fuzzy `bilibili` recognition as a spatial anchor.
- [x] Remove nearby same-row fragments only when they have a channel/watermark signature.
- [x] Learn persistent pure-CJK channel markers such as `小李看漫剧` without weakening ordinary dialogue protection.
- [x] Preserve unrelated subtitles even when they share a frame with a watermark.

## Implementation Steps

1. Retain segment geometry until anchored watermark-cluster filtering completes.
2. Add conservative channel-fragment signatures and distance/row checks.
3. Extend finalization fallback for repeated known channel markers.

## Todo

- [x] Test all three supplied watermark forms and fuzzy Bilibili OCR variants.
- [x] Test legitimate dialogue with digits, `看漫剧` and distant text survives.

## Success Criteria

Known watermark clusters disappear from final SRT without broad keyword-only filtering.
