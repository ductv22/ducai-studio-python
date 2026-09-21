# D1 voice track cue splitting

Status: completed in `2.0.0a66`; publish/timing interaction hardened in `2.0.0a67`

## Outcome

Represent the published D1 voice as one timeline clip per subtitle cue, using
the exact source/output cue timestamps already measured by the TTS ledger or
voice-conform plan. Keep the single mastered WAV as the media source so
preview/export remain lossless and cannot double-play a master plus cue files.

## Constraints

- Source SRT timestamps remain canonical integer microseconds.
- Do not rerun VieNeu/Piper, rewrite or delete WAV caches, or change DB schema.
- Do not crop, overlap, or silently omit any approved voice.
- Legacy imports without durable per-cue evidence keep the existing one-clip
  fallback.
- Existing conform, reset, timeline editing, preview, and export contracts must
  continue to work with multiple D1 clips.

## Acceptance criteria

- A production TTS import with a valid ledger creates one D1 clip per cue,
  sorted by cue index, with source and timeline ranges equal to the measured
  master-WAV interval for that cue.
- A conformed D1 import creates one clip per `VoiceConformCueDecision` using
  its output `voice_start_us`/`voice_end_us` range.
- All D1 clips reference the same published master/conformed media asset and
  no extra master clip remains.
- Base-job resolution, conform apply/reset, preview descriptor, and export
  work when D1 has many clips.
- Missing/corrupt ledger timing produces a clear error or legacy fallback; it
  never invents timestamps.
- Focused and full tests pass, then the Win64 release is rebuilt.

## Completion evidence

- D1 is rendered as cue clips and preserves one shared master WAV.
- Legacy a64/a65 D1 metadata is recovered only when the cue number and stable
  cue-ID tail identify exactly one source cue; ambiguous clips remain legacy.
- Cue #6 in the production project `Hãy xem chiếc xe mới!` restored its text
  and `7.400–9.800s` SRT slot, and double-click opened the matching editor row.
- Full suite: `523 passed, 1 skipped`; Ruff, compileall and QML lint passed.
- Windows release `2.0.0a66` was built and smoke-tested through the GUI.

## a67 hardening

- D1 publication no longer depends on the job row already being marked
  `succeeded`; the same no-loss serialization policy now drives ledger rows,
  master WAV ranges and imported clips during the worker-success race window.
- Timing presentation is derived from the SRT slot with a strict 50 ms
  tolerance: overflow red, underflow/position mismatch yellow, matched green.
- Resize previews locally in QML, maps the pointer through `clipLayer`, shows
  snap/distance guides and sends one backend command only on release.
