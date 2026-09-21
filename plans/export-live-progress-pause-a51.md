# Export live progress and pause a51

## Outcome

Publish a Windows release in which a long export visibly enters the running
state, receives fresh progress without blocking the UI, and can be paused and
resumed without losing completed chunk cache.

## Constraints and non-goals

- Do not cancel, restart or modify the user's active a49 export.
- Keep FFmpeg chunk rendering, checksums, probes and final atomic publish intact.
- Keep the Job Manager as the only SQLite writer.
- Progress is confirmed FFmpeg telemetry; do not fabricate processed duration.
- Do not open a51 alongside a49 while the active export still owns the database.

## Root cause evidence

- The active job is `running` and its owned FFmpeg process advances through
  later chunks while a49 still displays `Đang bắt đầu xuất`.
- In a49 each progress event and each `list_jobs` poll deserializes the complete
  multi-megabyte job configuration. UI state therefore arrives behind render
  telemetry, so QML never sees `running` and keeps Pause disabled.
- The source already has durable Pause/Resume controls and suspends only its
  owned FFmpeg process. The broken boundary is telemetry ingestion/UI polling,
  not the encoder control implementation.
- The completed 30,673-second export spends about 24–26 seconds per ten-minute
  render chunk, but the old finalization spends 306.6 seconds remuxing the same
  10.16 GB output with `+faststart`. A measured local-MP4 remux without that
  second full-file relocation takes 125.3 seconds with identical byte size.
- Final concat previously used blocking `subprocess.run`, and the final SHA pass
  had no phase telemetry or control polling. The UI therefore showed render at
  100% while finalization remained both opaque and temporarily unpausable.

## Implementation

1. Persist progress through the compact event path and return compact job
   activity summaries without cue arrays or full provider configuration.
2. Keep at most one `list_jobs` request in flight per UI runtime.
3. Bound export progress production to one event per second while retaining
   forced chunk/final updates.
4. Cover UI poll backpressure and worker Pause/Resume with regression tests.
5. Persist the small TTS purpose discriminator outside large job configs and
   index latest activity by `(job_id, kind, sequence)`.
6. Reuse an aligned source audio stream, skip same-size Lanczos scaling and
   decode static logos once.
7. Run final concat through the same owned, pauseable FFmpeg progress loop as
   chunk rendering; expose separate concat, probe, checksum and publish phases.
8. Keep the guarded final SHA but hash it in larger interruptible blocks with
   real byte progress. Publish a local-playback MP4 without `+faststart`, whose
   only benefit is progressive HTTP playback and whose measured second full-file
   relocation is the dominant avoidable cost on this 10.16 GB output.

## Acceptance criteria

- A second timer tick cannot enqueue another `list_jobs` request while the prior
  request is pending.
- Export emits no more than one non-forced progress event per second.
- Pause checkpoints completed chunks, suspends the owned FFmpeg PID and only
  resumes it after a correlated Resume command.
- Blocking probe/copy/hash/finalize phases never turn a Pause retry into Stop.
- Final concat and SHA acknowledge Pause without deleting approved chunk cache;
  the UI shows their real phase progress instead of remaining at render 100%.
- Export activity shows the current chunk, processed duration, elapsed time,
  ETA and expected completion time from real telemetry.
- Equivalent output keeps subtitle, blur, logo, voice, CQ and encoder preset;
  speed improvements do not silently trade away quality.
- Focused tests, full pytest, Ruff, compileall, QML lint, release build and
  manifest verification pass.
