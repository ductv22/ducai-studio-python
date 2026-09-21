# Rerun and overwrite pipeline outputs

## Outcome

Allow a user to intentionally run OCR, translation, or production TTS again
even when the project already has that result. A successful rerun becomes the
single active S1, Vietnamese subtitle set, or D1 result. The previous active
result remains untouched if the replacement job is cancelled or fails.

## Constraints

- Keep source video, cue cache, checkpoints, job records, and project revision
  history.
- Publish worker candidates atomically; workers must not write directly over
  the active project output before the manager accepts the result.
- A new source SRT invalidates the old translation and D1. A new translation
  invalidates the old D1. No duplicate S1 or D1 track may be created.
- Multi retains its normal valid-cache reuse policy. Explicit manual reruns
  always create a new job and replace the active result on success.
- No SQLite migration or credential-contract change.

## Non-goals

- Do not add automatic batch export.
- Do not delete or compact historical jobs/caches.
- Do not make Multi recompute every valid stage by default.

## Implementation

- [x] Give new OCR, translation, and production TTS jobs a job-scoped candidate
      output while preserving the stable active output path in the job spec.
- [x] Validate the candidate and QA before atomically promoting it to the active
      output; rollback the active file if project publication fails.
- [x] Replace S1/translated cues/D1 rather than accumulating tracks and
      invalidate downstream data when an upstream result changes.
- [x] Make rerun/overwrite intent explicit in the OCR, translation, and Voice
      Studio controls.
- [x] Add regression tests for successful replacement, failure safety, no
      duplicate tracks, downstream invalidation, and preserved job history.
- [x] Run focused and full quality gates; package the next Windows release.

## Acceptance criteria

- Running OCR, translation, or TTS again creates a new job even when an earlier
  matching job succeeded.
- Only a fully accepted result replaces the active file/project data.
- OCR rerun leaves one S1 and clears stale translated cues/D1; translation rerun
  replaces Vietnamese cues and clears stale D1; TTS rerun leaves one D1 pointing
  to the new master.
- Cancelled/failed replacement jobs leave the previous active output byte-for-
  byte unchanged.
- Existing caches and terminal job history remain available.

## Risks and rollback

- File promotion and project revision publication cross filesystem/SQLite
  boundaries. Promotion therefore keeps temporary backups until project save
  succeeds and restores them on any exception.
- Legacy jobs without candidate paths retain the existing direct-output import
  contract for resume compatibility.
