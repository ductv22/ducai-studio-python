# Translation QA navigator a48

## Outcome

Expose the remaining translation QA blockers inside the Translation workspace,
refresh the list from durable checkpoints while translation/repair is running,
show **Đạt** only after every cue has passed QA, and let the editor jump directly
to a failing cue for manual correction. The same release makes long VieNeu
cache restoration observable with a current operation, phase counters and a
dedicated ETA while reusing audio.

## Constraints and non-goals

- Preserve the selected translation cache, cue IDs, timestamps and existing job.
- Do not treat a manual text edit as QA PASS; the issue remains until QA is run
  again and clears it.
- Do not add another remote QA pass or change translation provider behavior.
- Package a new release directory; do not overwrite the a47 release.

## Acceptance criteria

- Translation preview returns a real QA state and cue-scoped blocker details.
- Old review blockers remain discoverable after a failed resume checkpoint.
- Worker checkpoints publish QA snapshots in real time without exposing prompts
  or provider responses.
- The QA inspector shows Đạt, Cần sửa or Chưa QA xong truthfully.
- Clicking a blocker selects the exact cue, centers it in the virtualized list and
  seeks preview/playhead to its start time.
- TTS cache restore progress exposes a phase detail and ETA without changing
  cache contents or rerunning already approved audio.
- Focused service, worker, view-model and QML checks pass before a48 packaging.

## Validation

1. Translation pipeline and worker tests for retained/repaired blockers.
2. Job Manager preview/cache tests, including historical blocker recovery.
3. View-model tests for QA snapshots and cue navigation.
4. QML lint, Ruff, full pytest and release manifest verification.

## Implementation evidence

- QA issue state is published from translation checkpoints and restored from
  immutable historical ledgers when an older resume checkpoint dropped details.
- The Translation workspace exposes the global QA summary and clickable blocker
  rows; manual edits do not clear QA state.
- TTS cache restore progress now includes `phase_detail`, per-phase counters and
  an ETA in the existing Job Activity panel.
- Focused tests, Ruff and QML lint passed; full suite: `410 passed, 1 skipped`.
- Release: `outputs/DUCAI-STUDIO-2.0.0a48-win64`; manifest SHA matches the EXE.
