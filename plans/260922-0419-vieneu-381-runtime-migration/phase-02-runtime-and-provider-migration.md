---
phase: 2
title: "Runtime and provider migration"
status: completed
priority: P1
effort: "3-5h"
dependencies: [1]
---

# Phase 2: Runtime and provider migration

## Overview

Move DUCAI from the private 3.3.0 host contract to supported VieNeu 3.8.1 APIs
and a separately installed managed runtime.

## Requirements

- Functional: clone enrollment, batch inference, cancellation, OOM backoff,
  per-cue output and 48 kHz audio remain operational.
- Non-functional: runtime install is explicit and reproducible; no model or
  profile is embedded in Git or ordinary GUI packages.

## Architecture

Rename the provider/runtime contract to 3.8, remove the 3.3 private monkey
patch, call upstream batch APIs directly, and keep application-level QA/cache
semantics intact. Existing provider IDs/jobs receive explicit compatibility
handling rather than being silently resumed under a new engine.

## Related Code Files

- Modify: `src/ducai_studio/engine_hosts/tts_engine_host.py`
- Modify: `src/ducai_studio/providers/tts/vieneu.py`
- Modify: `src/ducai_studio/job_manager/service.py`
- Modify: `src/ducai_studio/workers/tts_worker.py`
- Modify: UI/view-model provider constants and release/runtime scripts
- Delete: `src/ducai_studio/engine_hosts/vieneu_v330_optimization.py`
- Create/modify: VieNeu managed-runtime setup script

## Implementation Steps

1. Inspect upstream 3.8.1 profile and inference contracts against 3.3.0.
2. Add a pinned, idempotent 3.8.1 runtime installer and metadata validation.
3. Update host/provider/config contracts and compatibility handling.
4. Update build/portable runtime discovery and user-visible naming.

## Success Criteria

- [x] Managed runtime reports exactly 3.8.1 and CUDA backend on RTX 3060.
- [x] Existing authorized clone profile is reused or deterministically re-enrolled.
- [x] No 3.3 private optimization is imported by the new host.
- [x] Old jobs/cache never masquerade as freshly verified 3.8 output.

## Risk Assessment

Private NPZ representation may have changed. If 3.8 rejects a 3.3 profile,
re-enroll from retained reference audio when present; otherwise retain the old
profile file and report the required user action instead of corrupting it.
