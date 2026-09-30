# VieNeu 3.3.0 → 3.8.1 benchmark

Measured on the same Windows workstation, RTX 3060 12 GB, clone profile and
eight Vietnamese cues. Both runs used one warm-up cue before the measured set.

| Metric | 3.3.0 | 3.8.1 | Change |
|---|---:|---:|---:|
| Model load | 65.668 s | 12.759 s | 5.15× faster |
| Warm-up cue | 5.363 s | 1.953 s | 2.75× faster |
| Eight-cue measured wall time | 35.805 s | 1.657 s | 21.60× faster |
| Generated audio | 33.280 s | 34.400 s | +3.37% duration |
| Real-time factor | 1.076 | 0.048 | 22.33× better |
| Valid output | 8/8 | 8/8 | unchanged |

Every output in both runs is non-empty mono PCM16 at 48 kHz. VieNeu 3.8.1
reported the new explicit 192D/reference-code binding and reused the exact same
profile fingerprint. The measured duration delta is small and expected from
non-deterministic sampling; no result was empty or implausibly short.

A separate local faster-whisper large-v3 CUDA pass transcribed all eight 3.8.1
outputs. Normalized source/transcript similarity averaged 0.983 and the lowest
cue scored 0.940. Five cues were exact after normalization; the other three had
minor ASR substitutions. This materially supports complete, intelligible speech
without obvious truncation or repeated tails, while remaining an automated ASR
check rather than a human MOS or formal speaker-similarity evaluation. Evidence:
[`asr-validation.json`](./asr-validation.json).

On this 12 GB GPU the corrected 3.8.1 host completed the measured set at an
effective engine batch size of eight. A one-cue warm-up no longer pins later
requests to batch one; the host retains a lower safe batch size only after a
proven CUDA OOM. The upstream fused CUDA Graph path and real multi-cue batching
therefore both contribute to this result. Ordered output and per-cue QA remain
unchanged.

Sources: [`baseline.json`](./baseline.json) and [`after.json`](./after.json).
