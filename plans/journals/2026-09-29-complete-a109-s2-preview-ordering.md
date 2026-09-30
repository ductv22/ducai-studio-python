---
title: Complete a109 S2 preview ordering repair
date: 2026-09-29
summary: "Restored translated S2 at the playhead and removed scene-scan duplicates of S1."
---

# Complete a109 S2 preview ordering repair

## What happened

OCR and translation had produced valid S2 data, but the subtitle view model
kept the serialized order: every S1 cue followed by every S2 cue. Its playhead
lookup uses binary search and therefore silently returned no S2 because the
start-time index was not globally sorted.

The scene scanner also sees the ordinary hard-subtitle band, causing most S2
review rows in the supplied project to be duplicates of S1 rather than real
names, places, techniques or artifacts.

## Decision

Sort presentation rows by timeline time before building binary-search indexes.
For new OCR runs, remove scene evidence only when it agrees with a finalized S1
cue in text, time and image geometry. Do not filter merely by matching words,
because a real name card can repeat a spoken name elsewhere in the frame.

## Result

The existing project displays accepted translated S2 directly in Timeline
Preview without export or a rerun. Real evidence replay retains 13 scene cues
and removes 61 duplicated dialogue readings. Full quality gates, clean a109
build, manifest verification and packaged smoke passed.

> Historical work record — not durable authority. Prefer the user guide and
> source/tests for current behavior.
