---
phase: 3
title: "Verification and compatibility"
status: completed
priority: P1
effort: "2-4h"
dependencies: [2]
---

# Phase 3: Verification and compatibility

## Overview

Prove correctness and compare 3.8.1 against the captured 3.3.0 baseline using
the same profile, texts and machine.

## Requirements

- Functional: enrollment, preview, batch synthesis, retry/cache and D1 job path
  remain valid.
- Non-functional: report actual measurements without extrapolating the upstream
  hot-loop claim to the whole DUCAI workflow.

## Architecture

Use focused unit/contract/integration tests, then the production engine host for
an A/B clone benchmark. Validate PCM properties and duration as well as speed.

## Related Code Files

- Modify: `tests/tts/test_tts_engine_host.py`
- Modify: runtime/provider/release contract tests
- Create: `plans/260922-0419-vieneu-381-runtime-migration/reports/after.json`
- Create: `plans/260922-0419-vieneu-381-runtime-migration/reports/comparison.md`

## Implementation Steps

1. Run focused provider/host/build tests.
2. Run identical warm-up and measured batch on 3.8.1.
3. Run full pytest, Ruff, compileall and QML lint.
4. Review all changed contracts and callers for regressions.

## Success Criteria

- [x] Same cue count and valid 48 kHz WAV output.
- [x] 3.8.1 benchmark is recorded and compared honestly with 3.3.0.
- [x] Focused and broad quality gates pass.
- [x] Cache/profile compatibility behavior is covered by tests.

## Risk Assessment

A faster but truncated/repetitive output is a failure. Audio validity, cue
coverage and duration plausibility gate the performance conclusion.
