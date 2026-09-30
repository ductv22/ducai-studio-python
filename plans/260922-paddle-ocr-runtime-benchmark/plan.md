---
title: "Paddle OCR Windows runtime benchmark"
description: "Benchmark the newest compatible PaddlePaddle GPU runtime before updating the packaged PP-OCRv6 engine."
status: completed
priority: P1
effort: "0.5d"
branch: ducai-studio-python-a98
tags: [ocr, paddle, runtime, performance, release]
created: 2026-09-22
---

# Paddle OCR Windows runtime benchmark

## Outcome contract

- Outcome: use the fastest stable official Windows/CUDA Paddle runtime available for
  the existing PaddleOCR 3.7.0 and PP-OCRv6 subtitle pipeline.
- Constraints: benchmark on the current RTX 3060 with identical media, models and
  settings; build the candidate beside the working runtime; preserve OCR cache,
  projects and unrelated dirty worktree changes; keep a reproducible installer and
  truthful diagnostics.
- Non-goals: changing OCR sampling/QA semantics, switching model families, forcing a
  source build of an unavailable wheel, or claiming upstream A100/OpenVINO figures as
  local performance.
- Acceptance: the candidate passes Paddle/CUDA health, returns equivalent OCR
  evidence, improves measured warm inference without a material cold-start or memory
  regression, passes focused/runtime tests, and is safe to package. If it does not,
  retain 3.2.2 and record the measured reason.

## Implementation

- [x] Verify installed PaddleOCR, PaddleX, PaddlePaddle GPU and PP-OCRv6 model tiers.
- [x] Verify official Windows CUDA 12.6 wheel availability.
- [x] Measure the PaddlePaddle GPU 3.2.2 baseline on representative project media.
- [x] Build and validate an isolated PaddlePaddle GPU 3.3.1 candidate runtime.
- [x] Build and validate an isolated PaddlePaddle GPU 3.4.0 CUDA 12.9 candidate
      runtime, because the current release has no CUDA 12.6 Windows wheel.
- [x] Compare end-to-end host-call throughput, normalized result evidence, media-open
      time and GPU memory. Lazy model initialization is included in warmup, not the
      media-open measurement.
- [x] Promote only a verified winner. No candidate won, so preserve the current
      runtime and do not add misleading setup or release changes.
- [x] Run focused OCR/runtime/portable tests, lint/compile the benchmark, validate
      reports and document the result.

## Verification

- PaddleOCR 3.7.0 and PaddleX 3.7.2 are already current; PP-OCRv6 is already used.
- PaddlePaddle 3.3.1 and 3.4.0 matched the baseline of the same mode for normalized
  text and segment count from the final measured pass, but both were slower in Fast
  and Quality. Matching cuDNN 9.9 removed the runtime warning without a speed gain.
- PaddlePaddle 3.4.0 was tested with its official CPython 3.11 Windows CUDA 12.9
  wheel; it was 12.6% slower in Fast and 11.9% slower in Quality than the repeated
  3.2.2 baseline on this representative workload.
- Focused test result: 29 passed. Benchmark Ruff, py_compile and eleven JSON report
  validation checks passed.
- Candidate runtimes and temporary caches were removed after measurement. The
  installed runtime remains Paddle 3.2.2/PaddleOCR 3.7.0/PaddleX 3.7.2 on GPU.
- No user-visible behavior or runtime changed, so no release or evergreen docs update
  is required.

## Safety and rollback

- The working runtime is never modified before the candidate passes the benchmark.
- Promotion keeps the previous runtime until health and application smoke tests pass.
- Rollback restores the previous runtime directory; model cache and project data are
  not migrated or deleted.

<!-- slug: paddle-ocr-runtime-benchmark -->
