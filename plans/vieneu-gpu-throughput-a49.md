# VieNeu GPU throughput a49

## Outcome

Create a new Windows release that preserves every a48 workflow while improving
VieNeu 3.3 Turbo throughput on CUDA. The optimized host must keep clone binding,
repetition protection, per-cue QA, adaptive timing, retry and cache semantics.

## Constraints and non-goals

- Do not stop, resume or modify the production a47 TTS job currently running.
- Do not edit the shared `DUCAI_STUDIO_DATA/vieneu_3_3_runtime` source tree.
- Preserve the host protocol, job/config schemas, provider/model identity, cue
  fingerprint and existing processed/raw cache compatibility.
- Do not disable repetition penalty, QA, hash verification or OOM backoff.
- Do not change OCR, translation, timeline, effects or export behavior.
- Package a new release directory; do not overwrite a48 or the running a47.

## Current evidence

- Production VieNeu uses CUDA/BF16 with batch 64 on an RTX 3060 but sustains
  only about 19-44% GPU utilization and about 1.19 cue/s.
- The batch acoustic loop performs `B * 16` CUDA-to-host `.item()` calls per
  generated frame to maintain repetition history, plus per-row EOS sync.
- The decoder copies each generated code tensor CUDA -> CPU -> CUDA and decodes
  every row sequentially.
- Disk utilization is below 2%; VRAM usage is about 4.5/12 GB. The bottleneck is
  launch/synchronization overhead, not storage or memory capacity.
- Focused host/release tests pass (7 tests); the full suite passes with 414 tests
  and 1 intentional skip. Ruff, compileall and QML lint pass.
- A tensor-level equivalence check against Python `Counter`/`deque` histories
  passes for bounded and unbounded repetition windows. The optimized hot loop
  contains one batch completion `.item()` per frame and no per-row/channel sync.
- The production a47 job remains active, so an uncontaminated same-GPU audio
  benchmark is intentionally deferred until that engine host releases the GPU.
- The a49 Windows release builds successfully with 2,840 manifest-tracked files;
  every hash verifies, and the packaged host/optimization helper exactly match
  source. The real managed runtime health check reports VieNeu 3.3.0 and the
  selected clone profile validates as a 192D embedding with `[100, 16]` codes.
- Runtime-level synthetic checks pass for `no_grad`, batched decoder item
  order/length and fallback to the original decoder when its private batch API
  rejects the call. A regression test proves an engine-host hash upgrade does
  not change per-cue cache or ledger fingerprints.

## Implementation plan

1. Add a version-gated, release-local VieNeu optimization module loaded by the
   TTS engine host after VieNeu 3.3.0 initializes.
2. Replace Python repetition sets with GPU-resident count/ring tensors while
   preserving the 64-frame sliding-window penalty exactly.
3. Track EOS, frame caps and output lengths as batch tensors, synchronizing only
   once per frame and once when materializing results.
4. Decode generated codes through MOSS `batch_decode` in bounded groups with
   CUDA OOM backoff; avoid CUDA -> CPU -> CUDA round trips.
5. Fall back to the unmodified runtime methods if compatibility checks fail or
   the backend is not CUDA/PyTorch.
6. Package the optimization helper beside `tts_engine_host.py`, bump the release
   version and document the behavior.

## Acceptance criteria

- New release contains the optimization helper and reports whether it was
  enabled in the host load receipt.
- CPU/ONNX and incompatible runtime internals retain the original execution path.
- CUDA path preserves repetition-window, EOS/frame-cap, retry and output-order
  behavior without per-row `.item()` calls in the hot loop.
- Batched decoder returns the same item count/order, valid finite 48 kHz audio
  and passes representative audio QA.
- A representative benchmark using the existing clone profile demonstrates a
  throughput improvement over the original path on the same GPU/text/batch.
- Existing cache entries remain reusable because no fingerprinted setting or
  provider/model/voice identity changes.
- Focused tests, full pytest, Ruff, compileall, QML lint and release manifest
  verification pass.

## Risks and rollback

- MOSS batched decode can use more VRAM. Decode groups therefore start bounded
  and halve on CUDA OOM before falling back to the original decoder.
- Runtime internals are private. Installation is guarded by exact VieNeu 3.3.0
  compatibility checks and retains original methods for fallback.
- Any audio mismatch, truncated output or failed QA blocks the release. Rollback
  is selecting a48; shared model/profile/cache data is not mutated by packaging.
