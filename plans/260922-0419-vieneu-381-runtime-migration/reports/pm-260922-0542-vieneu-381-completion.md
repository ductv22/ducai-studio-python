# Plan complete: VieNeu 3.8.1 runtime migration

## Summary

| Metric | Result |
|---|---:|
| Plan progress | 4/4 phases, 14/14 criteria, 100% |
| Full tests | 727 passed, 1 skipped, 0 failed |
| Release | DUCAI-STUDIO-2.0.0a101-win64 |
| Manifest | 0 missing hashes, 0 mismatches |
| Legacy space removed | 8.54 GiB |

## Delivered

- VieNeu-TTS 3.8.1 managed runtime with Torch/Torchaudio 2.8.0+cu128.
- Upstream fused CUDA Graph path and real multi-cue batching; old 3.3 private
  monkey patch removed.
- Runtime health verifies package 3.8.1, clone profile structure, CUDA 12.8 and
  the active RTX 3060 instead of trusting metadata alone.
- VieNeu 3.3 and 3.8 provider/cache/job identities remain isolated. Legacy job
  retry is rejected before durable state mutation.
- Existing clone profile, model cache and voice cue cache retained.
- Obsolete `vieneu_3_3_runtime` removed only after source, release and packaged
  synthesis gates passed.

## Benchmark and audio QA

| Metric | 3.3.0 | 3.8.1 | Change |
|---|---:|---:|---:|
| Model load | 65.668 s | 12.759 s | 5.15x faster |
| Eight-cue wall time | 35.805 s | 1.657 s | 21.60x faster |
| RTF | 1.076 | 0.048 | 22.33x better |
| Valid 48 kHz WAV | 8/8 | 8/8 | unchanged |

- Faster-whisper large-v3 validation: 0.982574 average normalized similarity,
  0.940299 minimum across 8/8 outputs.
- Packaged and post-cleanup smoke WAVs: mono PCM16, 48 kHz, non-empty.

## Quality gates

- Focused tests and regression probes: passed.
- Full pytest, expanded Ruff, compileall, QML lint and `git diff --check`:
  passed.
- Independent review: no remaining code findings.
- Executable launch: remained alive for ten seconds without crash; only the
  process opened by the smoke test was terminated afterward.

## Documentation impact

User-visible runtime requirements, setup/recovery and architecture changed.
The owning surfaces were updated: `README.md`, `docs/user-guide.md`,
`docs/architecture.md`, `THIRD_PARTY_NOTICES.md`, runtime setup/build scripts
and the release manifest contract.

## Remaining items

None for this plan. No commit was created.
