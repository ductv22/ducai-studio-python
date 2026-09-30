---
title: "Phase 3: OCR regions and dynamic subtitle blur"
status: completed
---

# Phase 3: OCR regions and dynamic subtitle blur

## Overview

Persist source subtitle geometry and use it for timestamp-bound automatic blur.

## Requirements

- [x] Normalize OCR crop boxes into full-frame coordinates and persist them per source cue.
- [x] Drive automatic blur from source-SRT timing and OCR geometry with a labelled estimated fallback.
- [x] Keep manual blur independent and make preview/export share the same conform time map.

## Implementation Steps

1. Extend OCR QA and project snapshot contracts.
2. Add preview and grouped FFmpeg export rendering.

## Todo

- [x] Verify box round-trip, overlapping cues and fallback geometry.
- [x] Verify a 100-cue fixture groups blur sources by radius.

## Success Criteria

Preview and export agree while SRT remains the timing authority.
