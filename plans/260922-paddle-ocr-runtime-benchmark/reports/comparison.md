# Paddle OCR runtime comparison

Benchmark completed 2026-09-22 on the target RTX 3060. The product runtime is
PaddlePaddle GPU 3.2.2, PaddleOCR 3.7.0, PaddleX 3.7.2 and PP-OCRv6.

## Method and limits

- One representative 600-second project video; 8 timestamps and 2 subtitle bands,
  producing 16 crops per measured pass.
- Two warmup passes, then the median of 5-7 measured passes. Raw p95 values remain
  in JSON but are not used for the decision because the sample count is small.
- Throughput covers the complete engine-host call: video seek/decode, crop, OCR and
  GPU synchronization. It is not isolated neural-network inference throughput.
- `engine_init_seconds` in raw reports measures media open only. Model creation is
  lazy and occurs during the first warmup, so this field is not a full cold start.
- Candidate correctness means its final measured pass matched the baseline of the
  same mode for normalized OCR text and segment count. Fast and Quality have
  different expected hashes.
- This is a local microbenchmark for DUCAI's subtitle workload, not a universal
  Paddle performance claim.

## Fast mode

| Runtime | CUDA / cuDNN | Open media (s) | Median (s/16 crops) | Crops/s | Versus 3.2.2 baseline |
|---|---|---:|---:|---:|---:|
| Paddle 3.2.2 | 12.6 / 9.5.1.17 | 1.628 | 1.016 | 15.745 | baseline |
| Paddle 3.2.2 | 12.6 / 9.9.0.52 | 1.807 | 1.045 | 15.306 | 2.8% slower |
| Paddle 3.3.1 | 12.6 / 9.5.1.17 | 2.062 | 1.091 | 14.661 | 6.9% slower |
| Paddle 3.3.1 | 12.6 / 9.9.0.52 | 1.913 | 1.073 | 14.917 | 5.3% slower |
| Paddle 3.4.0 | 12.9 / 9.9.0.52 | 2.348 | 1.162 | 13.768 | 12.6% slower |

## Quality mode

The first pair used the original 3.2.2/3.3.1 CUDA 12.6 environments. The repeat
group measured cuDNN 9.9 and Paddle 3.4 candidates against a fresh 3.2.2 baseline.

| Run group | Runtime | CUDA / cuDNN | Open media (s) | Crops/s | Result within group |
|---|---|---|---:|---:|---:|
| Original | Paddle 3.2.2 | 12.6 / 9.5.1.17 | 1.846 | 14.498 | baseline |
| Original | Paddle 3.3.1 | 12.6 / 9.5.1.17 | 1.890 | 14.017 | 3.3% slower |
| Repeat | Paddle 3.2.2 | 12.6 / 9.5.1.17 | 2.100 | 13.716 | baseline |
| Repeat | Paddle 3.3.1 | 12.6 / 9.9.0.52 | 2.456 | 11.578 | 15.6% slower |
| Repeat | Paddle 3.4.0 | 12.9 / 9.9.0.52 | 2.082 | 12.080 | 11.9% slower |

## Findings

- PaddleOCR 3.7.0 and PaddleX 3.7.2 are already the current compatible line, and
  the product already uses PP-OCRv6 tiny/small for Fast and medium for Quality.
- PaddlePaddle 3.4.0 is the current framework release and the last release with
  Windows support. Its official CPython 3.11 Windows GPU wheel is available through
  the CUDA 12.9 index, not the CUDA 12.6 index used by the product.
- Paddle 3.4.0 passed GPU health checks and produced matching mode-specific output,
  but reduced this host-call throughput by 12.6% in Fast and 11.9% in Quality.
- cuDNN 9.9 removed the compiled/runtime mismatch warning but did not improve this
  workload. A version-only upgrade would add packaging change and make OCR slower.

## Decision

Retain PaddlePaddle GPU 3.2.2. Do not build a new release for Paddle: the OCR package
and model family are already current, while every tested runtime replacement was
slower on the target machine. Revisit with a broader corpus when a newer PaddleOCR
or PP-OCR model is released, or when a new runtime shows a local performance gain.

## Official references

- https://github.com/PaddlePaddle/PaddleOCR
- https://github.com/PaddlePaddle/Paddle/releases
- https://www.paddleocr.ai/main/en/
- https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html

## Unresolved questions

- A larger multi-video corpus could estimate variance across codecs and resolutions;
  it is unnecessary for rejecting candidates that already regress this representative
  workload, but should be used before making a universal performance claim.
