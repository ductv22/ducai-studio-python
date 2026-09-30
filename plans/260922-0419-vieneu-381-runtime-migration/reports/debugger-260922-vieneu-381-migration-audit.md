# VieNeu 3.8.1 migration audit

> Final status (2026-09-22): the defects recorded in the original audit below
> have been resolved and reverified. This file now preserves the original
> findings as historical context; the authoritative final evidence is the
> resolved-status section immediately below and the linked benchmark reports.

## Resolved final status

- Legacy VieNeu 3.3 retry/start is rejected before any durable job-state
  mutation. Regression coverage includes the failed/interrupted retry path.
- Runtime health now executes the managed interpreter and verifies the actual
  VieNeu 3.8.1 package, profile structure, CUDA availability/version and GPU.
  The live probe reports CUDA 12.8 on an RTX 3060 with a valid retained profile;
  corrupt runtime/profile fixtures are rejected.
- The corrected host completed the eight-cue benchmark at effective batch size
  eight. Measured synthesis time improved from 35.805 seconds to 1.657 seconds
  (21.60x wall-time improvement; RTF improved 22.33x).
- All eight outputs are non-empty mono PCM16 WAV at 48 kHz. A local
  faster-whisper large-v3 CUDA validation scored 0.982574 average normalized
  text similarity and 0.940299 minimum, supporting intelligible complete speech
  without obvious truncation or repeated tails.
- Source and runtime metadata use version 2.0.0a101 / VieNeu 3.8.1. Final
  packaged build, packaged smoke and removal of the legacy runtime remain the
  release-finalization gates and are intentionally performed after this audit.

Evidence: [comparison.md](./comparison.md), [after.json](./after.json), and
[asr-validation.json](./asr-validation.json).

## Superseded original audit

The sections below describe the pre-fix state and are not current product
behavior.

## Executive summary

- **Confirmed regression:** retrying a failed/interrupted VieNeu 3.3 job changes
  its durable state to `queued`; rejection happens only on the subsequent start.
- **Confirmed fail-open diagnostic:** provider health reports `READY` from file
  existence and self-declared JSON metadata, even when the runtime executable and
  profile are invalid bytes.
- **Verified:** the real retained profile is accepted by 3.8.1 unchanged; job and
  cue-cache fingerprints separate 3.3 from 3.8; batch OOM backoff is covered and
  the real benchmark fell back safely to batch one.
- **Release blocker:** source is `2.0.0a101`, but there is no a101 release/portable
  manifest or packaged smoke result. The 3.3 runtime correctly remains present
  while that gate is incomplete.

## Confirmed findings

### P1: legacy retry mutates state before incompatibility rejection

`JobManagerServer._retry_job` has no VieNeu 3.3 guard. It transitions a failed
legacy job to `queued`. `_start_job` then rejects it, leaving the durable record
queued and the UI in a non-terminal resume path.

Minimal reproduction output:

```text
before=failed
retry_result=queued
after_retry=queued
start_error=ValueError: Tác vụ VieNeu 3.3 cũ không thể tiếp tục bằng engine 3.8.1...
final=queued
```

The existing regression test covers direct `_start_job` rejection only, so it
does not exercise the real failed/interrupted resume sequence used by the UI.

### P1: runtime discovery can report false readiness

`VieNeuTtsProvider.health()` requires only:

- `runtime_python.is_file()`;
- host script existence;
- `ducai_runtime.json` declaring version `3.8.1`;
- `.npz` and `.json` profile files existing.

A temporary runtime containing a 20-byte non-executable `python.exe`, a 10-byte
invalid NPZ and metadata `{"version":"3.8.1"}` returned:

```text
state=ready
summary=VieNeu 3 Turbo 3.8.1 sẵn sàng với CUDA và profile clone cục bộ
```

This violates the diagnostic contract: discovery claims CUDA/profile readiness
without probing the installed package, CUDA backend, or profile structure. The
real installed runtime is healthy; this finding concerns corrupted, partial or
manually copied runtime discovery.

## Verified contracts

### Profile compatibility

The retained profile and sidecar have the same composite fingerprint in the
3.3 and 3.8 benchmark receipts:
`dfd76fa02bc93af097263c056f6e06e65bc5bc124cca6df0dae69ac36873cef3`.
The live 3.8 host reports a 192D speaker embedding, stored/effective code shape
`[100, 16]`, no pad frame removed, and binding
`explicit_v381_speaker_embedding_192d_ref_codes`. The NPZ was not rewritten.

### Cache/provider separation

`cue_request_fingerprint` includes provider ID and provider version. The same
cue/profile/settings produced different hashes:

```text
3.3: c865ec3318704b4ca093b850765da6d8d7cca18e2ed79d657d074f281ab8d11b
3.8: d1e40c3c315e17b003c832f46a778f86832bd573cca2cb05e934d24b5c836faf
```

Completed-job reuse also compares provider ID, provider version, model, input
fingerprint and settings fingerprint.

### Batch/OOM behavior

The host halves the requested batch only for an out-of-memory `RuntimeError`,
empties the CUDA cache best-effort, and retains the safe batch size for later
requests. The focused fake-engine test proves `32 -> 16 -> 16`. The real 3.8
benchmark requested eight and reported effective engine batch one after OOM,
then produced eight ordered WAVs. All eight are distinct, non-empty, mono PCM16
at 48 kHz.

### Runtime and release discovery

The actual managed runtime reports VieNeu `3.8.1`, PyTorch/Torchaudio
`2.8.0+cu128`, CUDA `12.8`, and RTX 3060. Portable-runtime repair, diagnostics
component naming, release scripts and manifest contract tests point to
`vieneu_3_8_runtime`.

No `DUCAI-STUDIO-2.0.0a101-win64` or a101 portable manifest exists under the
shared outputs. Existing release directories stop at a100, while source version
is a101. Therefore packaged runtime discovery and packaged clone smoke are not
yet evidenced.

## Verification evidence

```text
.venv/Scripts/python.exe -m pytest \
  tests/tts/test_tts_engine_host.py \
  tests/integration/test_tts_job_service.py \
  tests/integration/test_tts_worker.py \
  tests/integration/test_job_manager_server.py \
  tests/unit/test_portable_runtime.py \
  tests/unit/test_release_build_contract.py -q

99 passed
```

The A/B artifacts contain 8/8 valid WAVs on both sides. The 3.8 set has eight
unique file hashes. This proves container validity and cue coverage, not spoken
content accuracy; the current evidence does not objectively rule out phoneme
errors, within-file repetition or truncated final words.

## Required follow-up

1. Reject VieNeu 3.3 in `_retry_job` before any state transition and test the
   complete failed/interrupted UI resume sequence.
2. Make VieNeu health probe the runtime interpreter/package and CUDA, and inspect
   the selected profile; keep the probe bounded so Diagnostics stays responsive.
3. Build a101, verify its manifest, run packaged VieNeu health/profile/clone
   smoke, then and only then remove the resolved legacy 3.3 runtime.
4. Add an intelligibility/repetition QA step or explicitly narrow the benchmark
   conclusion to format, coverage, duration and speed.
