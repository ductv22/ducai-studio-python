---
title: Migrate DUCAI to VieNeu-TTS 3.8.1
date: 2026-09-21
summary: "Upgraded clone TTS, verified 21.60x measured speedup, built a101, and safely removed the 3.3 runtime."
---

# Migrate DUCAI to VieNeu-TTS 3.8.1

## What happened

DUCAI's pinned VieNeu 3.3.0 runtime was replaced with VieNeu-TTS 3.8.1. The original integration depended on a private monkey patch, serialized effective inference to batch one, and trusted runtime metadata too readily. The migration retained the authorized clone profile and all model/voice caches while separating 3.3 and 3.8 fingerprints.

## Decision

Use provider `vieneu-3.8`, the managed `vieneu_3_8_runtime`, upstream fused CUDA Graph inference and verified multi-cue batching. Health probes must execute the managed interpreter and verify package version, profile structure, CUDA 12.8 and the GPU. Legacy 3.3 jobs are rejected before durable mutation; no silent cross-version cache reuse is allowed.

## Evidence

- Full suite: 727 passed, 1 skipped, 0 failed; Ruff, compileall, QML lint and release build passed.
- Eight-cue wall time: 35.805 s to 1.657 s; RTF: 1.076 to 0.048.
- ASR similarity: 0.982574 average, 0.940299 minimum on 8/8 cues.
- Release a101 manifest: zero missing/mismatched hashes; packaged and post-cleanup WAV smoke passed.
- Removed only `DUCAI_STUDIO_DATA/vieneu_3_3_runtime`, freeing 8.54 GiB. Profiles, model cache and voice cue cache remain.

## Next steps

None for this migration. Use the a101 release and re-run the normal project workflow when a real-video acceptance sample is desired.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
