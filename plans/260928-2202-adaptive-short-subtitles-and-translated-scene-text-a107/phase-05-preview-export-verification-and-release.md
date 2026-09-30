---
title: "Phase 5: Preview export verification and release"
phase: 5
status: pending
priority: P1
effort: "6h"
dependencies: [2, 3, 4]
---

# Phase 5: Preview export verification and release

## Overview

Verify accuracy, visual fidelity, performance and compatibility, document the workflow and publish a clean Windows a107 build.

## Requirements

- [x] Add fixture-based OCR, classification, translation, TTS-exclusion, QML and FFmpeg tests.
- [x] Compare a106 versus a107 inference calls and elapsed time on the same short-sub/scene-text fixture.
- [x] Verify preview/export visual parity at 1080p and the supplied wide-frame aspect ratio.
- [x] Update user guide, architecture, third-party notices and release metadata.
- [x] Build and smoke `DUCAI-STUDIO-2.0.0a107-win64` without touching user projects/cache.

## Related Code Files

- Modify: focused tests under `tests/ocr/`, `tests/integration/`, `tests/export/`, `tests/unit/`
- Modify: `docs/user-guide.md`, `docs/architecture.md`, `README.md`, `THIRD_PARTY_NOTICES.md`
- Modify: release version/build contract files

## Implementation Steps

1. Run focused tests while implementing each contract.
2. Render deterministic image fixtures and compare geometry/text placement numerically plus visual inspection.
3. Run full pytest, Ruff, compileall, QML lint and `git diff --check`.
4. Build release, verify manifest/hash and run isolated executable smoke.
5. Record measured performance and known limitations in the completion report.

## Todo

- [x] Short dialogue and scene-text acceptance matrix passes.
- [x] Persistent/one-frame watermark and noise rejection matrix passes.
- [x] Preview/export and font portability tests pass.
- [x] Independent regression review reports no blocker.

## Success Criteria

- [x] All plan acceptance criteria are demonstrated by tests or packaged smoke evidence.
- [x] Release a107 is ready for user testing and old a106 projects open unchanged.

## Risk Assessment

If sparse full-frame OCR causes unacceptable slowdown or false positives on long footage, keep dialogue recovery enabled and expose scene-text scanning as a separate opt-in pass until thresholds are tuned from representative fixtures.
