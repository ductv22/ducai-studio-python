---
title: Paddle OCR Windows runtime benchmark report
date: 2026-09-22
status: completed
---

# Paddle OCR Windows runtime benchmark report

## Summary

| Metric | Result |
|---|---:|
| Plan tasks | 8/8 |
| Focused tests | 29 passed |
| Benchmark reports | 11 valid JSON files |
| Mode-specific normalized output regressions | 0 |
| Candidate/temp storage cleaned | approximately 10.1 GiB |
| Runtime promoted | No |

## Decision

- Keep PaddlePaddle GPU 3.2.2, PaddleOCR 3.7.0 and PaddleX 3.7.2.
- Do not build a new release: PaddleOCR and PP-OCRv6 are already current, and each
  tested replacement runtime was slower on the target RTX 3060 workload.
- Do not promote Paddle 3.4.0 merely because it is newer. Its CUDA 12.9 Windows
  runtime passed health and correctness checks but made Fast 12.6% slower and
  Quality 11.9% slower than the repeated 3.2.2 baseline.

## Evidence

- Fast baseline 3.2.2/CUDA 12.6/cuDNN 9.5: 15.745 crops/s.
- Fast 3.3.1/cuDNN 9.5: 14.661 crops/s; 6.9% slower.
- Fast 3.3.1/cuDNN 9.9: 14.917 crops/s; 5.3% slower.
- Fast 3.2.2/cuDNN 9.9: 15.306 crops/s; 2.8% slower.
- Fast 3.4.0/CUDA 12.9/cuDNN 9.9: 13.768 crops/s; 12.6% slower.
- Repeat Quality baseline: 13.716 crops/s; 3.3.1/cuDNN 9.9 reached 11.578
  and 3.4.0 reached 12.080 crops/s.
- Candidate outputs matched the baseline of the same mode for normalized OCR text
  and segment count from the final measured pass.

This is a one-video microbenchmark of the complete host call, including seek,
decode, crop, OCR and synchronization. It does not prove universal Paddle inference
performance. Raw p95 values are not used for the decision because each report has
only 5-7 measured passes.

Full method and table:
`plans/260922-paddle-ocr-runtime-benchmark/reports/comparison.md`.

## Documentation impact

No evergreen product documentation changed because product behavior, runtime,
setup and public contracts remain unchanged.

## Unresolved questions

- Re-run against a broader codec/resolution corpus before making any universal
  performance claim. This does not affect the present no-upgrade decision.
