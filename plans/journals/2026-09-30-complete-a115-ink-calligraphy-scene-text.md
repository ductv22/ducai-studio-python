---
title: Complete a115 ink backgrounds and Vietnamese brush fonts
date: 2026-09-30
summary: "Shipped licensed Vietnamese brush fonts, aspect-safe ink backgrounds and matching Preview/Export rendering."
---

# Complete a115 ink backgrounds and Vietnamese brush fonts

## What happened

The a115 work turned the reference calligraphy into a safe product contract:
scene text can choose four original vector ink silhouettes and four additional
brush fonts with complete Vietnamese coverage. Preview uses QML Canvas while
export uses deterministic ASS paths derived from the same shape data, so no
raster decoder or FFmpeg input is added per cue.

The screenshot font names alone did not establish redistribution rights. Rather
than mislabel substitute fonts or ship unlicensed files, the release bundles
Comforter Brush, Water Brush, Splash and Hurricane under SIL OFL and displays
their real family names.

## Review findings

A 1280×720 smoke exposed clipping in the pre-existing background-control row.
Labels and button captions were compacted while preserving full accessible
names, leaving all controls visible without increasing the toolbar height.

## Verification

Focused pytest passed 301 tests. Full pytest passed 909 tests with one
environment-gated skip. Ruff, compileall, QML lint and diff check passed. Clean
a115 packaging completed, all 2,887 manifest hashes verified, and the packaged
EXE passed an isolated startup/process-cleanup smoke.

## Artifact

E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a115-win64

> Historical work record. Prefer current product documentation and source
> contracts for durable behavior.
