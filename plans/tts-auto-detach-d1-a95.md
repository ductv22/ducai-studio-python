# TTS auto-detach active D1 before a manual rerun

## Status

Completed for `2.0.0a95`.

## Outcome

When the user starts a new production voice job while D1 already exists, remove
the active D1 projection automatically, release every UI player that may hold
the old WAV, and only then create the replacement TTS job.

Every successful production publish must also retain the cue-scoped timing
ledger and import D1 as one editable Timeline clip per source subtitle cue,
including a rerun that restores every WAV from cache and calls no TTS provider.

## Constraints and non-goals

- Keep the old WAV file, cue cache, checkpoints and job history.
- Do not call VieNeu again for cue WAVs whose cache fingerprint is still valid.
- Do not change preview TTS, resume semantics, synthesis settings or SQLite.
- Do not delete video, subtitles, original audio or export artifacts.
- A deliberately started replacement removes the old D1 immediately. If the
  replacement fails, the old file remains on disk but is not silently restored
  to Timeline.

## Acceptance criteria

- **Tạo lại · ghi đè D1** first releases Voice Studio, Timeline and Multi voice
  playback, then removes D1 and its project media references.
- The project revision is incremented before the new TTS job is created, so the
  replacement job can publish against the correct revision.
- The old WAV, cue cache, checkpoints and job history remain untouched.
- Projects without D1 and preview jobs keep the existing direct start flow.
- A cache-only rerun commits its complete final voice ledger before reporting
  success; importing that result creates exactly one D1 clip per source cue
  instead of silently falling back to the legacy one-bar representation.
- A preparation failure does not create a TTS job and leaves the UI usable with
  a Vietnamese explanation.
- Focused/full tests, Ruff, compileall, QML lint, release build and manifest
  verification pass.

## Implementation

- [x] Add an idempotent media/service operation that removes only active D1.
- [x] Add the guarded IPC action and compact project transport response.
- [x] Chain manual production TTS creation after D1 removal in the view model.
- [x] Release all QML voice players while the replacement is prepared.
- [x] Persist final cache-only TTS evidence so D1 remains cue-scoped.
- [x] Add regression coverage and update user/architecture documentation.
- [x] Pass quality gates and package the a95 Windows release.

## Validation

- Focused TTS/service/UI regression suite: pass.
- Full pytest: `683 passed, 1 skipped`.
- Ruff, compileall and the complete QML lint gate: pass.
- Clean Windows package build and manifest SHA-256 verification: pass.
- Packaged EXE offscreen startup against an isolated application/data root:
  exit code 0, state database created, no owned process left running.

## Risks and rollback

- Removing D1 changes the previous failed-rerun safety contract. The UI and
  documentation must state that the old file/cache remain, but the Timeline
  projection is removed as soon as the user starts a new replacement.
- If preparation fails, no new job is created. Re-enabling playback must not
  leave MediaPlayer or TTS busy state stuck.
