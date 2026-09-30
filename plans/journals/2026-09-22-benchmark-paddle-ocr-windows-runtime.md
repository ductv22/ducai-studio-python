---
title: Benchmark Paddle OCR Windows runtime
date: 2026-09-22
summary: "PaddleOCR is current; compatible newer Paddle runtimes were slower on RTX 3060, so 3.2.2 remains packaged."
---

# Benchmark Paddle OCR Windows runtime

## What happened

Checked the packaged OCR stack against current official releases and isolated
runtime candidates. The product already used PaddleOCR 3.7.0, PaddleX 3.7.2 and
PP-OCRv6. Tested PaddlePaddle 3.3.1 on CUDA 12.6 and PaddlePaddle 3.4.0 using its
official CPython 3.11 Windows CUDA 12.9 wheel.

## Evidence

On the same RTX 3060 representative video/model workload, current Paddle 3.2.2
reached 15.745 Fast crops/s. Paddle 3.3.1 reached 14.661 with cuDNN 9.5 and 14.917
with cuDNN 9.9; Paddle 3.4.0 reached 13.768. In the repeated Quality group,
3.2.2 reached 13.716 crops/s, 3.3.1/cuDNN 9.9 reached 11.578 and 3.4.0 reached
12.080. Candidates matched the same-mode baseline for normalized text and segment
count from the final measured pass. Focused OCR/runtime/portable tests passed 29/29.

These figures measure the full engine-host call including seek, decode, crop, OCR
and GPU synchronization on one video. They are not inference-only or universal
Paddle benchmarks. The raw media-open timer also excludes lazy model creation in
the first warmup.

## Decision

Keep the packaged Paddle 3.2.2 runtime. Do not release a version-number-only upgrade that measurably slows subtitle OCR. Candidate runtimes and temporary caches were removed after the benchmark.

## Next steps

Revisit only when an official newer Windows GPU wheel or PaddleOCR/PP-OCR model release shows a measurable local gain. See plans/260922-paddle-ocr-runtime-benchmark/reports/comparison.md.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
