---
title: DUCAI STUDIO a67 Timeline D1 hardening
date: 2026-08-28
summary: "Fixed cache-safe D1 publication, truthful timing colors, and smooth one-shot Timeline edge resizing."
---

# DUCAI STUDIO a67 Timeline D1 hardening

## What happened
A completed 359-cue TTS job failed during D1 publication because sequential voice ranges were reconstructed under a different timing policy while the persisted job state still read running. Timeline resizing also needed truthful timing colors and local drag feedback.

## Decision
Derive the timing policy from the job configuration and QA evidence, use one shared policy for master WAV and D1 ranges, and derive timing colors from the SRT slot with a 50 ms tolerance. Keep drag preview local and persist once on release.

## Verification
The real failed job resumed from cache without another VieNeu pass and published 359 D1 clips. Final full tests reported 535 passed and 1 skipped; Ruff, compileall, QML lint, build, manifest and packaged-QML checks passed. The release EXE SHA256 is `4BC62147009D2A04C562FEA1662E60265C40032F65F6DBF8632207714A98789A`; source and packaged `TimelineScreen.qml` both have SHA256 `DEC4A0A79BDB3458AA029FBF865CF3DA8D937BE1CD77A9F466A7FA1501F2D194`. Packaged-EXE smoke testing confirmed Vietnamese red/yellow/green explanations, kept D1 visible through the model refresh after resize, restored the cue with one undo, and showed no white-screen stall.

## Next steps
No remaining work is required for the a67 acceptance criteria.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
