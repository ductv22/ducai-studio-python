---
title: Ship SRT export and Whisper extraction
date: 2026-09-21
summary: Released a99 with live SRT export and optional checkpointed Whisper audio extraction.
---

# Ship SRT export and Whisper extraction

## What happened

Implemented live source/Vietnamese SRT export and ported local Whisper audio extraction into the existing durable OCR job boundary. The work also added external runtime/model setup, method-aware cache fingerprints, streaming checkpoints, CUDA-to-CPU fallback, Multi snapshots, UI controls, packaging contracts and Vietnamese diagnostics.

## Verification

The release build reran all gates: 718 tests passed, one intentional skip, Ruff/compileall/full QML lint passed, and the frozen a99 EXE opened the Trích xuất workspace then exited cleanly under isolated app/data roots. The standard 367.42 MiB package includes the Whisper host but no runtime or large-v3 model.

## Lessons

A green suite did not initially prove the workflow placement: the first review found SRT export buttons only in Biên tập, so matching actions were added to Trích xuất and Dịch thuật. The first EXE smoke returned code 3 because an a98 instance owned the shared single-instance lock; isolating DUCAI_APP_DATA_ROOT proved a99 itself was healthy without stopping the user's active process.

## Next steps

Install the optional external Whisper runtime/model only when real ASR is needed, then run a live-media transcription smoke. Frame OCR remains the compatible default for old projects and Multi snapshots.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
