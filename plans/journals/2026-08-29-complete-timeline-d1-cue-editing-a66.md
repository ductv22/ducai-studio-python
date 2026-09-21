---
title: Complete Timeline D1 cue editing a66
date: 2026-08-29
summary: "Finished a66, recovered legacy D1 metadata safely, and verified GUI responsiveness on a real project."
---

# Complete Timeline D1 cue editing a66

## What happened

Release a66 completed the accepted Timeline D1 plan. The final live defect was legacy a64/a65 D1 clips that had stable `voice-<index>-<cue-tail>` identifiers but no cue text or SRT slot, plus short resize handles that could cover the clip double-click target.

## Decision

Recover legacy metadata only when cue number and stable ID tail resolve to exactly one cue; never guess by list order. Limit each resize handle to at most one third of a short clip so its body remains clickable. Keep ambiguous legacy audio unlinked with a Vietnamese recovery message.

## Evidence

The production project `Hãy xem chiếc xe mới!` restored cue #6 to `7.400–9.800s` and text `Nhưng hôm nay với Xiaomi Pengcheng,`. Double-click opened that exact editor row. Full pytest passed with 523 passed and 1 skipped; Ruff, compileall and QML lint passed. GUI smoke measured 0.20–0.33 s common interactions, 1.05 s full fit rebuild, correct 5-second keyboard seek, `Responding=True`, and a settled combined working set near 655 MB.

## Next steps

Use `DUCAI-STUDIO-2.0.0a66-win64`. A long soak test on a multi-hour project remains optional evidence beyond this bounded release smoke.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
