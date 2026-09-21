# Voice timing auto-repair and workflow controls a54

Status: completed — 2026-08-27

Release: `outputs/DUCAI-STUDIO-2.0.0a54-win64`

## Outcome

Publish a Windows release that turns real TTS timing overflow into a cue-scoped,
recoverable workflow: list and highlight every Vietnamese cue whose measured
voice is too long, automatically ask the compatible translation provider to
condense only those cues while preserving meaning and Film Bible names, reuse
all unchanged WAV cache, regenerate changed cues, and retry production TTS.
The same release adds voice-job history deletion and folds OCR pause/resume into
one prominent stateful action button.

## Constraints

- The Job Manager remains the only SQLite writer; workers communicate through
  versioned messages and immutable checkpoints.
- Use measured per-cue WAV/fit evidence, not character-count guesses, as the
  authoritative `CUE_TOO_LONG` source.
- Preserve cue IDs, timestamps, source text, Film Bible names and complete
  Vietnamese meaning. Never silently drop or merge cues.
- Keep approved WAVs addressable in the global fingerprint cache. A timing
  repair may invalidate only cues whose Vietnamese text changed.
- Cap adaptive time-stretch at 1.5x. Limit automated repair/retry cycles and
  stop with an actionable manual-review state instead of looping forever.
- Package a new release directory; do not overwrite a53 or launch it beside an
  already-running older GUI.

## Non-goals

- Do not extend or slow the source video to conceal synchronization failure.
- Do not automatically accept an overflowing voice track. Keeping the full voice
  is an explicit user fallback after timing QA reports a failure.
- Do not retranslate cues that have no measured timing blocker.
- Do not change OCR extraction engines, translation model selection, VieNeu
  synthesis quality, loudness targets or export contracts.
- Deleting job history does not delete source media, project revisions or the
  global reusable voice-cue cache.

## Current evidence

- Project `5625cdf6-127f-4b7a-bece-4ab4ce617d85` has 268 approved cue WAVs but
  no D1 track because production TTS ends 8.870541 s after the video and reaches
  8.849416 s maximum accumulated delay.
- Existing per-cue ledger `fit.timeline_overflow_us` identifies 107 cues that
  remain too long after the 1.5x cap; the worst individual slot would otherwise
  require about 7.89x.
- The compatible translation source is a successful Gemini Web Guest job with
  an immutable ledger and Film Bible. Translation repair already supports
  cue-scoped issues and preserves QA-pass chunks.
- `translationQa` and its editor navigator already expose cue-scoped blockers;
  the subtitle list currently lacks timing roles/highlights.
- `JobActivityPanel` supports individual deletion, but clear-all is hard-coded
  to OCR/translation and Voice Studio does not enable the feature.
- OCR currently presents three separate start/pause/resume buttons even though
  the view model has a single durable job state.

## Implementation plan

1. Extend TTS review QA with a stable `timing_issues` array containing cue ID,
   cue index, slot/allowed/audio durations, measured overflow, required speed,
   current Vietnamese text and an explicit Vietnamese error message.
2. Add Job Manager read/create actions for voice timing review and targeted
   translation repair. Build the repair job from the latest compatible
   translation ledger/provider configuration, current project translations,
   inherited Film Bible and only the measured timing issues.
3. Initialize the translation worker with QA-pass cached chunks plus pending
   timing-repair chunks. Add duration-aware repair prompts and a local guard
   that rejects unchanged/longer answers. Publish/import a complete guarded SRT.
4. In the application view model, expose voice timing QA, apply issue roles to
   the subtitle model, navigate to Translation on review, start automatic
   repair when enabled, then start production TTS after a successful repair.
   Bound the cycle count and preserve an explicit manual fallback.
5. In QML, add an auto-condense setting and repair action, a complete clickable
   `Cue dài` list, and yellow pulsing rows/badges in Translation and Subtitle
   Editor. Show detailed provider/repair/retry errors instead of a generic fail.
6. Generalize history clearing by job kind, enable per-item and clear-all
   deletion in both Voice Studio activity panels, and retain global WAV cache.
7. Add one view-model `toggleOcr` action and replace separate pause/resume
   controls with a single prominent stateful OCR button plus the existing red
   Cancel button.
8. Add focused pipeline, worker, service, view-model and QML resource tests;
   run full pytest, Ruff, compileall and QML lint; update user-facing docs,
   bump version and build/verify a new Win64 release.
9. When timing QA is in review, expose an explicit `Thêm nguyên voice vào
   Timeline` fallback. Reuse the approved WAV cache, preserve every spoken
   sample, allow the D1 track to extend beyond the source-video duration, and
   leave later voice/video speed adjustment to the user.

## Acceptance criteria

- A TTS review exposes exactly the measured long cues with stable IDs and
  enough timing evidence to explain the failure.
- Clicking a long-cue row selects it, centers the virtualized list and seeks the
  video; the same cue visibly pulses yellow in Translation/Editor.
- Auto repair sends only long cues, preserves all IDs/timestamps/names, imports
  the complete SRT, reuses unchanged WAV cache and regenerates changed cues.
- After bounded retries, TTS either creates/imports a QA-PASS D1 track or leaves
  a truthful manual-review list with no background retry loop.
- Voice Studio can delete one or all TTS history rows for the open project while
  reusable global cue cache remains intact.
- The OCR primary button alone starts, pauses and resumes according to durable
  job state, returns to `Chạy OCR` after terminal completion, and has visually
  distinct ready/pause/resume states.
- A timing-review screen offers `Thêm nguyên voice vào Timeline`; invoking it
  creates a cache-compatible override job, imports a complete uncropped D1
  track even when it ends after the video, and never activates implicitly.
- Focused and full validation gates pass, and the release manifest verifies.

## Risks and rollback

- A provider can make a cue shorter but change meaning. Existing integrated QA,
  Film Bible checks and a maximum repair cycle bound remain mandatory.
- Importing a repaired SRT changes project revision and cue fingerprints; only
  text-changed cues should miss global WAV cache. Tests compare unchanged cue
  fingerprints and cache restoration counts.
- Automatic cross-job handoff can duplicate work after restart. Persist parent
  job/cycle identifiers in provider configuration and reject an already-active
  or already-created equivalent repair.
- Rollback is selecting a53; shared projects and unchanged global cache remain
  readable because no database schema migration is required.

## Delivery evidence

- Ruff and Python compile gates: PASS.
- Full QML lint: PASS.
- Pytest: 443 passed, 1 skipped.
- Release manifest: schema `ducai-release-manifest-v1`, version `2.0.0a54`,
  2,840 files verified with zero missing or hash mismatches.
- Frozen smoke start: GUI PID 35528 and its sole Job Manager child PID 6400
  running from the a54 release directory.
