---
phase: 1
title: "Baseline benchmark"
status: completed
priority: P1
effort: "1h"
dependencies: []
---

# Phase 1: Baseline benchmark

## Overview

Capture a reproducible VieNeu 3.3.0 baseline before changing code, packages or
runtime files.

## Requirements

- Functional: use the installed clone profile, identical Vietnamese cues and a
  fixed warm-up/measured sequence.
- Non-functional: do not mutate project/cache data or benchmark while another
  DUCAI/VieNeu process owns the GPU.

## Architecture

Invoke the existing versioned engine host with its production runtime and
record load time, synthesis wall time, output duration, RTF, batch size, WAV
validity and engine receipt.

## Related Code Files

- Read: `src/ducai_studio/engine_hosts/tts_engine_host.py`
- Create: `plans/260922-0419-vieneu-381-runtime-migration/reports/baseline.json`

## Implementation Steps

1. Verify no running DUCAI/VieNeu process owns the runtime or GPU.
2. Select one existing authorized clone profile without exposing its content.
3. Run a warm-up plus short measured batch and retain output outside project data.

## Success Criteria

- [x] 3.3.0 health/load receipt captured.
- [x] Measured WAV files are finite, 48 kHz and non-empty.
- [x] Wall time, generated duration and RTF are recorded.

## Risk Assessment

CUDA/model warm-up can distort the first measurement. Keep warm-up and measured
batches separate and record both.
