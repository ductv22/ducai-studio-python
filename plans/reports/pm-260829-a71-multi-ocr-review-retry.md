# A71 Multi OCR review retry fix

## Outcome

- Release: `DUCAI-STUDIO-2.0.0a71-win64`
- Root cause: Multi classified every failed child job by retry count alone, so
  deterministic `ocr_review_required` QA results consumed all immediate retries.
- Repair: OCR review is atomically deferred on the first observed result; review
  SRT/checkpoints stay intact and transient worker/provider failures retain the
  original retry budget.
- UI: Multi cards report `OCR đã xong`, the QA item count, and the deferred/final
  state in warning color instead of presenting another active OCR attempt.

## Evidence

- Reproduced before repair: `ocr_review_required` produced
  `state=retrying, retry_count=1`.
- Production-state copy: the paused a69 item at `retrying/5` transitioned to
  `deferred/6` without touching the live database.
- Focused Multi/Job Manager/UI tests passed.
- Full gate: 603 passed, 1 skipped; Ruff, compileall and QML lint passed.
- Release manifest hashes passed with zero mismatches.
- Isolated packaged EXE smoke exited 0, created its state database and left no
  child process.

## Plan sync

The repair maps to the a68 retry/cache acceptance criteria and is recorded as an
a71 follow-up. The older a68 plan remains `in progress` because its historical
unchecked a68-specific audit/build items were not reclassified by this fix.

## Operational handoff

Keep a69 paused, close it cleanly, start a71 against the existing app data, then
press **Tiếp tục Multi video**. The existing OCR checkpoint is reused; no new
full scan is required by this policy change.
