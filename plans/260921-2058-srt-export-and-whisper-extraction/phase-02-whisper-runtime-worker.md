---
phase: 2
title: "Whisper runtime and extraction worker"
status: completed
priority: P1
effort: "2d"
dependencies: []
---

# Phase 2: Whisper runtime and extraction worker

## Overview

Port the deterministic local ASR behavior from 0.1.99 into an external
Whisper engine while retaining the current durable extraction job contract.

## Requirements

- Functional: `large-v3`, Chinese default, beam 5, temperature 0,
  `condition_on_previous_text=false`, word timestamps, conservative VAD, audio
  extraction to 16 kHz mono, CUDA float16 then CPU int8 fallback.
- Non-functional: external runtime/model, real progress, checkpointed segments,
  responsive pause/cancel, stable provider/model/runtime fingerprints.

## Architecture

`create_ocr_job` accepts `extraction_method` (`frame_ocr` default or
`whisper_audio`) and produces method-specific provider config/fingerprints.
The existing OCR worker dispatches the Whisper method to a dedicated streaming
engine host launched by `whisper_runtime`. Segment rows are persisted as NDJSON
under managed staging. Control messages checkpoint the last safe segment;
resume rewinds a bounded overlap, deduplicates by normalized text/time, and
continues. Final rows convert seconds to integer microseconds once, validate
monotonic non-empty cues, render SRT and publish through a method-aware QA receipt.

## Related Code Files

- Create: `src/ducai_studio/engine_hosts/whisper_engine_host.py`
- Create: `scripts/setup-whisper-runtime.ps1`
- Modify: `src/ducai_studio/workers/ocr_worker.py`
- Modify: `src/ducai_studio/job_manager/service.py`
- Modify: `src/ducai_studio/job_manager/server.py`
- Modify: `src/ducai_studio/infrastructure/runtime_paths.py` or owning runtime locator
- Modify: `scripts/build-release.ps1`
- Modify: `scripts/build-portable-release.ps1`
- Create/modify: focused tests under `tests/ocr/`, `tests/workers/`, and
  `tests/integration/`

## Implementation Steps

1. Extract only the proven ASR parameters/audio-selection behavior from the
   legacy worker; do not copy Electron endpoints or hard-sub rescue.
2. Add a pinned `whisper_runtime` setup and metadata receipt; share the external
   Hugging Face cache while requiring an explicit large-v3 model preflight.
3. Add method-aware job creation, runtime/model fingerprints and output/QA names.
4. Implement streamed segment protocol, progress, checkpoint, overlap resume,
   pause/cancel termination and CUDA→CPU fallback.
5. Normalize segments into canonical `SubtitleCue` timing and publish S1 using
   the current overwrite/promotion transaction.
6. Extend diagnostics and Vietnamese error mapping for missing runtime/model,
   unsupported GPU, corrupt checkpoint and invalid ASR output.

## Success Criteria

- [x] A fixture produces deterministic monotonic Chinese cues and SRT.
- [x] CUDA failure retries once on CPU int8 without corrupting the job.
- [x] Pause acknowledges at a safe segment boundary; resume does not duplicate
      or omit boundary speech.
- [x] Cancel leaves checkpoint/cache only and never publishes partial S1.
- [x] OCR and Whisper jobs for one media have different resume fingerprints.

## Risk Assessment

The critical risk is pretending full-file `faster-whisper` is pausable. The
host must stream segment events and be safely terminable; a host that returns
only one final JSON response fails this phase. If the runtime cannot support
bounded pause latency without quality loss, replan the host protocol rather
than weakening the UI promise.
