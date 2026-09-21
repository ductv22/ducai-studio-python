# Export cue timeline fix

Status: complete

## Outcome

Exported videos keep every D1 voice cue at its intended timeline position across
all chunks, without the initial pile-up, missing narration, or peak distortion
seen in the a97 export.

## Constraints

- Preserve A1 at 18% and D1 at 100% for the affected project.
- Keep the existing video stream and burned subtitles unchanged when repairing
  the user's completed video.
- Do not overwrite the user's existing exports.
- Produce a portable Windows package that can run on another machine.

## Non-goals

- No redesign of the mixer UI or track-volume controls.
- No changes to TTS generation or the generated voice master.

## Root cause

Each cue branch applied `apad` before `amix`. With delayed, trimmed ranges from a
shared voice master, FFmpeg discarded the branch's leading timeline gap and
physically emitted the cue at the start of the chunk. Hundreds of cues therefore
overlapped in the first few seconds; the remainder contained only A1.

## Delivery

1. Reproduce the late-cue failure with real FFmpeg.
2. Keep cue branches finite, mix them at their delayed timestamps, and pad only
   the completed mix.
3. Add a real-media regression test with a late cue after a timeline gap.
4. Repair and verify the user's completed MP4 without re-encoding video.
5. Run focused and full quality gates, then build normal and portable a98
   releases and verify the archive.

## Acceptance criteria

- The regression test fails on a97 behavior and passes after the fix.
- Voice is measurable near the beginning, middle, final chunk, and last cue.
- Repaired output decodes without error, peaks safely below 0 dBFS, and has the
  same encoded video-stream hash as the user's a97 export.
- DUCAI STUDIO reports version 2.0.0a98.
- Normal and portable a98 packages are present; the portable archive passes an
  integrity test and has a recorded SHA-256.

## Risk and rollback

The change affects export audio graph construction. Rollback is limited to the
FFmpeg graph change and version bump; existing project data and media are not
modified.

## Result

All acceptance criteria passed. See
[`../reports/pm-260919-0725-export-cue-timeline-fix.md`](../reports/pm-260919-0725-export-cue-timeline-fix.md).
