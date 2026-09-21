# A68 Multi video coordinator investigation

## Executive summary

- **Issue:** Three deterministic state-machine gaps violated pause and retry contracts.
- **Impact:** A safe pause could remain stuck in `pause_requested`; a stage after a
  retried stage could inherit an exhausted budget; exhaustion after the deferred
  pass could leave an item deferred with no remaining pass.
- **Root cause:** `_advance()` ignored `pause_requested`, while retry metadata was
  item-wide even though its contract is stage-local; terminal retry selection did
  not account for an already-consumed global deferred pass.
- **Status:** Source gaps fixed in the coordinator and covered by red-green tests.
- **Scope:** No worker, UI/QML, repository, user data, or running DUCAI process was
  changed.

## Findings and evidence

1. **Initial plus five retries / final one-shot pass**
   - Existing boundary tests prove failure number six moves the first-pass item to
     `deferred`, and a `final_pass` failure moves directly to `failed`.
   - New coverage proves a successful stage resets `retry_count` and `final_pass`,
     so every later stage receives a fresh initial-plus-five budget.
   - New coverage proves a stage exhausting its six attempts after `retry_pass=1`
     becomes `failed`, not an unreachable second `deferred` pass.

2. **Pause and interruption do not spend retries**
   - Red reproduction: `_advance()` returned for every state except `running`, so
     the worker's later `paused` event could not complete the batch handshake.
   - Fix observes the current child while `pause_requested`, then persists
     `paused` without changing `retry_count`.
   - Isolated diagnostic resumed an `interrupted` job as the same `job_id` with
     `resume_checkpoint_key=latest` and preserved `retry_count=4`.

3. **At most one running child**
   - Isolated diagnostic supplied a running current child plus a second waiting
     item. `_advance()` launched no second job and retained the current project.

4. **Stage-specific fingerprint reuse**
   - `_create_or_reuse_completed_job()` reused only a successful job matching
     kind, provider, provider version, model, input fingerprint, and settings
     fingerprint. A changed settings fingerprint and a different kind both
     created new jobs in the isolated diagnostic.

5. **Membership removal persistence**
   - Repository test reopens SQLite, removes one Multi membership, verifies the
     project row remains, and verifies the other membership remains ordered.

6. **Migration v3 to v4**
   - Isolated literal-v3 database migrated to versions `[1, 2, 3, 4]`, added all
     three Multi tables, and preserved the existing project, succeeded job, and
     checkpoint resume fingerprint.

## Verification

- Red phase: all three tests in `tests/unit/test_multi_video_debug.py` failed on
  the original coordinator for the expected assertions.
- Green focused gate: `66 passed in 14.05s` across Multi unit tests, repository
  integration tests, and Job Manager server integration tests.
- `uv run ruff check src tests`: passed.
- `uv run python -m compileall -q src tests`: passed.
- Full `uv run pytest -q`: one unrelated failure at
  `tests/unit/test_ui_resources.py:21`. The test expects the old single-file
  expression `importPath(selectedFile.toString())`, while the concurrently edited
  `Main.qml` now collects `selectedFiles` and dispatches `importPaths(values)` for
  multi-import. The coordinator change did not touch either file.

## Remaining concern

- The UI resource assertion and current multi-file import implementation need
  reconciliation by the UI/test owner before the full-suite gate can be called
  clean. No coordinator finding remains open from this investigation.
