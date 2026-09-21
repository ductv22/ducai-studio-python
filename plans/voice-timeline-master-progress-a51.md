# Voice timeline, mastering and progress a51

## Outcome

Publish a Windows release that keeps the current VieNeu cue audio, cache,
timeline timing and two-pass EBU R128 output contract while reducing the wall
time after synthesis and keeping the displayed TTS phase synchronized with the
worker.

## Constraints and non-goals

- Do not regenerate approved cue WAVs or change cue fingerprints.
- Do not replace dynamic `loudnorm`, weaken true-peak/LUFS targets, skip QA or
  publish a partial voice track.
- Keep the Job Manager as the only SQLite writer and retain durable terminal,
  checkpoint and control events.
- Progress is confirmed telemetry only. Do not interpolate cue completion.
- Package a new release directory; do not overwrite an older release.

## Current evidence

- Production job `a37d237f-ceb5-4e47-8613-4211bbae92ae` completed all
  13,385 cues with zero blockers and a PASS QA report.
- Timeline assembly took 159.194 seconds. The first loudness pass took 672.931
  seconds for 30,673.024 seconds of PCM16 audio.
- The final WAV and QA report were complete on disk at 02:08, but the job did
  not become `succeeded` until 02:17 because progress messages were queued
  behind synchronous SQLite/UI work.
- Master progress was emitted every 250 ms. Each event opened multiple SQLite
  connections and parsed the job's multi-megabyte `config_json` twice.
- `list_jobs` reparsed and sent full provider configs (including every TTS cue)
  on every UI poll, then opened two more connections per listed job for latest
  progress/result events.
- A ten-minute representative benchmark measured `loudnorm` at 8.933 seconds
  and `ebur128` at 1.466 seconds. Substituting the measurement changed dynamic
  mastering output, so that shortcut is rejected.

## Implementation plan

1. Add a bounded PCM16 loudness-measure session and stream the same bytes to
   the timeline WAV and FFmpeg concurrently. Reuse the exact measured values
   for the unchanged second `loudnorm` pass.
2. Persist progress through a compact repository path that does not deserialize
   full job configuration or perform redundant existence reads.
3. Return compact job activity summaries with only the small provider fields
   the UI actually consumes, plus latest progress/result in one read connection.
4. Emit FFmpeg progress at one-second cadence, retain explicit final progress,
   and show the combined assembly/measure phase with accurate detail.
5. Display one decimal place for time-based progress and keep elapsed/ETA
   heartbeat live between confirmed events.

## Acceptance criteria

- Streaming measurement sees byte-for-byte the PCM16 frames written to the
  assembled WAV and reports the same loudness measurement as the existing
  file-based first pass within FFmpeg's printed precision.
- Final mastering still reports dynamic two-pass EBU R128, 48 kHz mono PCM16,
  target loudness and true-peak compliance.
- Progress ingestion keeps up with a one-event-per-second producer and terminal
  state is not delayed behind a telemetry backlog.
- `list_jobs` no longer transports cue arrays, API settings or other full
  provider configuration, while preview/production purpose remains available.
- Focused tests, full pytest, Ruff, compileall, QML lint, release build and
  manifest verification pass.

## Risks and rollback

- A broken PCM pipe could fail both assembly and measurement. The temporary WAV
  and FFmpeg process must therefore be aborted together and never replace the
  final project track.
- Compact job summaries could omit a UI dependency. Tests cover every field
  consumed by the current view model, including TTS purpose and terminal result.
- Rollback is selecting a50/a49; shared cache, profile and project data are not
  modified by packaging.
