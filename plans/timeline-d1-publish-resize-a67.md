# Timeline D1 publish and resize hardening

Status: complete

## Outcome

Publish a cache-complete D1 reliably during the worker-success/job-state race,
show truthful red/yellow/green timing states, and make edge resizing follow the
pointer without backend churn.

## Constraints

- Source SRT microseconds remain authoritative.
- Only duration overflow above 50 ms is an error/red state.
- Do not rerun VieNeu when a valid base WAV cache exists.
- Resize changes speed of the complete cue source and stays within 0.50x–3.00x.
- No SQLite migration; a66 projects and cache remain readable.

## Acceptance criteria

- A running job whose worker has completed can serialize overlapping measured
  cue ranges and publish D1 without a false overlap.
- Underflow and positional mismatch are yellow; matched is green; only overflow
  is red. Auto-fit changes only overflow clips.
- Dragging changes local geometry/color only, uses shared clip-layer pointer
  coordinates, shows snap and distance guides, and sends one command on release.
- Focused/full tests, Ruff, compileall, QML lint, Win64 build and live cache
  resume/smoke checks pass for `2.0.0a67`.

## Release verification

- The real failed job `31b8df91-81fd-462d-8df1-836832abfc30` resumed from its
  359-cue cache, reused the cached WAVs, succeeded, and published 359 D1 clips.
- Timeline smoke testing confirmed Vietnamese Inspector explanations and the
  intended colors for overflow, underflow, and matched cues. Narrow clip labels
  no longer paint over adjacent clips at the 60-second zoom level.
- Each completed edge drag produced exactly one project revision; undo restored
  the original cue ranges. The app remained responsive without a white screen
  or visible main-thread stall during the tested operations.
- Model refresh now preserves the Timeline's vertical track position, so D1
  remains visible after a persisted resize or undo.
- Full verification passed: 535 tests passed, 1 skipped; Ruff, compileall, QML
  lint, the Win64 build, release manifest, and packaged QML checks all passed.
- Final Win64 package:
  `<output-root>\DUCAI-STUDIO-2.0.0a67-win64`.
