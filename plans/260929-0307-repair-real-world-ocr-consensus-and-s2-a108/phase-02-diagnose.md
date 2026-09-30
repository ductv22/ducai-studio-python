---
phase: 2
title: "Diagnose"
status: complete
dependencies: [1]
---

# Phase 2: Diagnose

- [x] Reproduce missing S1 from the saved OCR evidence.
- [x] Reproduce missing S2 and false Bilibili S2 from the saved OCR evidence.
- [x] Trace each symptom to its source contract.

## Confirmed causes

- Fast and Quality evidence are grouped separately; one correct frame in each pass cannot satisfy the two-frame consensus in either pass.
- Scene evidence is filtered at confidence 0.82 before fuzzy temporal clustering, discarding corroborating variants.
- A single extremely strong scene card is always discarded, while an overlapping Bilibili companion glyph can survive as an independent segment.

