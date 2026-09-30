---
title: Complete a113 subtitle editor and lane synchronization
date: 2026-09-29
summary: "Kept subtitle text stable during focus changes and repaired visible S2…Sn lane synchronization."
---

# Complete a113 subtitle editor and lane synchronization

## Decision

Own editor commits by stable cue ID and make additive selection explicit instead
of deriving it from stale modifier state. Normalize only legacy automatic scene
text with missing or conflicting lane metadata; preserve explicit manual lanes.
Queue a manual `T` request while one subtitle revision is in flight rather than
dropping it or issuing concurrent revisions.

## Result

Editor focus no longer empties cue text, plain click no longer accumulates
selection, simultaneous legacy scene labels occupy visible S2…Sn lanes and the
Timeline can scroll/follow those tracks.

## Verification

Full pytest, Ruff, compileall, QML lint, lock/diff checks, clean packaging and a
packaged import smoke all passed. The real project was decoded read-only and its
automatic and manual scene lanes were preserved as intended.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
