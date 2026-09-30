---
title: "Phase 2: Strong automatic subtitle concealment"
phase: 2
status: completed
priority: P1
effort: "4h"
dependencies: [1]
---

# Phase 2: Strong automatic subtitle concealment

## Overview

Make automatic source-subtitle concealment materially stronger while keeping interaction immediate and export efficient.

## Requirements

- [x] Seed new automatic blur with radius 46, 2% horizontal padding and 1.4% vertical padding.
- [x] Render a restrained dark concealment veil only when Che kín is enabled.
- [x] Add a clear Che kín toggle beside the automatic blur controls.
- [x] Keep generated Vietnamese subtitles above the concealment layer.

## Implementation Steps

1. Update preview rendering and effect UI with optimistic patch behavior.
2. Update FFmpeg rendering without adding one full-frame blur branch per cue.
3. Cover overlapping source cues and conform timing consistently.

## Todo

- [x] Verify preview/export parameters and cue timing match.
- [x] Verify legacy overlays remain blur-only.

## Success Criteria

Original hard-sub text is no longer readable in cover mode and no source file is modified.
