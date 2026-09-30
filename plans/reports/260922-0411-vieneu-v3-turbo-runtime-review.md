# VieNeu v3 Turbo runtime review

---
date: 2026-09-22T04:11:00+07:00
scope: DUCAI runtime 3.3.0 versus upstream VieNeu-TTS 3.8.1
status: complete
---

## Summary

DUCAI already uses the VieNeu **v3 Turbo** model and CUDA/BF16 path, but pins the
SDK/runtime to `vieneu==3.3.0`. Upstream `main` and the newest SDK tag are
`v3.8.1`. Versions after 3.3.0 add a fused CUDA Graph frame loop, streaming and
continuous batching, lower CPU load on the clone-reference path, and several
audio/chunking fixes.

The upstream speed improvement is real enough to justify an isolated migration
benchmark. It is not safe to upgrade the shared environment in place: DUCAI's
host deliberately rejects any runtime other than 3.3.0, and its local optimizer
patches private 3.3.0 APIs.

## Method

- Inspected the installed runtime metadata, package versions, model cache and
  DUCAI engine-host implementation.
- Inspected official GitHub README, current `pyproject.toml`, tags and the
  `v3.3.0...v3.8.1` comparison on 2026-09-22.
- Compared upstream claims with DUCAI's existing v3.3 process-local CUDA batch
  optimization. No package, model or running job was changed.

## Findings

### Installed DUCAI runtime

- Package: `vieneu==3.3.0`.
- Mode/model: `v3turbo`, `pnnbao-ump/VieNeu-TTS-v3-Turbo`, 48 kHz.
- GPU stack: `torch==2.8.0+cu128`, `torchaudio==2.8.0+cu128`,
  `transformers==4.57.6`.
- Provider batches up to 64 cues with OOM backoff and keeps clone profiles as a
  192D speaker embedding plus reference codes.
- DUCAI has its own process-local optimization: repetition history remains on
  CUDA and the MOSS codec decodes bounded batches. It reduces synchronization
  and CPU/GPU copies, but it does not implement the new upstream fused CUDA
  Graph frame path.

### Upstream state

- VieNeu v3 Turbo remains the newest open-source model family; v4 is proprietary.
- Current SDK tag and `pyproject.toml`: `3.8.1`.
- The official RTX 3060 benchmark reports batched GPU RTF `0.011-0.02`
  (roughly 50-91x faster than real time) and one short sentence at RTF `0.10`.
- The upstream comparison is 76 commits ahead of v3.3.0. Relevant changes:
  - fused CUDA Graph frame execution, described by upstream as about 6x faster
    for that hot loop;
  - continuous GPU batching and streaming;
  - reduced CPU load during voice-clone reference processing;
  - reference-padding, chunk-join and babble-guard fixes.

### Interpretation

The new SDK should improve cloned-voice **synthesis throughput**, especially for
many cues on RTX 3060. Reference enrollment is a one-time operation per profile;
the largest user-visible gain is expected while generating the many output
cues, not while initially reading the 3-8 second sample.

The claimed 6x does not mean an entire DUCAI voice job becomes 6x faster. Cache
lookup, audio QA, per-cue file writes, FFmpeg fitting and D1 assembly are outside
that upstream hot-loop benchmark. DUCAI also already backports part of the batch
optimization, so only an A/B run on identical cues/profile can establish the
net gain over the current application.

## Compatibility and security

- Do not run `pip install -U vieneu` inside `vieneu_3_3_runtime`: the current
  host checks for exactly 3.3.0 and would fail to start.
- The local optimization depends on private 3.3.0 classes and must be removed or
  rewritten for 3.8.1 rather than silently applied.
- Existing clone profiles and cache fingerprints need explicit compatibility
  tests before reuse; regenerating every profile or cue without need would waste
  time and invalidate cache.
- Clone profiles are biometric-like voice assets and must remain excluded from
  portable distributions unless the speaker authorized redistribution.

## Recommendation

Build the upgrade as a separate managed runtime/provider revision, keep 3.3.0
as rollback, and benchmark both engines using the same clean reference, cue set,
batch sizes and GPU. Accept the migration only if audio QA, speaker similarity,
cue completeness, cancellation, OOM recovery and cache behavior all pass.

## References

- [VieNeu-TTS repository and benchmarks](https://github.com/pnnbao97/VieNeu-TTS)
- [Current SDK metadata](https://github.com/pnnbao97/VieNeu-TTS/blob/main/pyproject.toml)
- [v3.3.0 to v3.8.1 comparison](https://github.com/pnnbao97/VieNeu-TTS/compare/v3.3.0...v3.8.1)

## Unresolved questions

- Net end-to-end speedup of 3.8.1 over DUCAI's patched 3.3.0 on the same real
  subtitle workload.
- Whether old 3.3 clone-profile NPZ files are bit-contract compatible with the
  3.8.1 reference representation without re-enrollment.
