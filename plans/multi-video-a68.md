# Multi video sequential workflow

Status: in progress

## Outcome

Add a durable **Multi video** workspace that imports two or more videos as
independent projects and runs the existing OCR, translation, and voice jobs
sequentially for the selected projects. Each card must expose truthful job
state and progress, keep its own cache/history, and allow one-project-at-a-time
detailed Timeline editing.

## Constraints

- One imported video continues to use the existing single-project workflow.
- A Multi batch runs exactly one child job at a time in import order.
- A batch snapshots OCR, translation, and TTS configuration without storing
  credential secrets.
- Reuse valid stage cache/checkpoints; do not change OCR, translation, TTS, or
  cache internals.
- Each stage receives one initial attempt plus at most five immediate retries;
  a deferred failed item receives one final attempt at the failed stage.
- Pauses first request a safe checkpoint, then use a bounded owned-worker
  interruption fallback when a provider call cannot acknowledge quickly;
  interrupted batches never resume automatically after restart.
- Removing a card only removes Multi membership, never its project, media,
  jobs, or cache.
- The Multi screen uses one shared media decoder and does not include Timeline,
  logo, blur, or inspector editing.
- Existing user-owned a67 processes and application data must not be touched by
  build or smoke verification.

## Non-goals

- Multi video does not export finished videos automatically.
- Multi video does not run selected videos concurrently.
- Multi video does not redesign the underlying OCR, translation, or TTS
  providers.
- Detailed clip editing remains in the existing Timeline screen.

## Acceptance criteria

- Multi-file import creates or reuses one project per media file, appends them
  in stable order, and opens a responsive 3/2/1-column Multi grid.
- Selection, select-all, count, locked membership while running, card preview,
  subtitle/audio choice, remove-membership, and single-project detailed editing
  behave as specified.
- OCR, translation, and TTS run sequentially with stage-fingerprint reuse,
  checkpoint resume, exact retry budgets, one deferred final pass, safe pause,
  and explicit interrupted recovery.
- Persistent SQLite schema version 4 stores batch, item, and attempt state;
  migration keeps existing projects and cache intact.
- Card status, color, and progress are driven by child-job events and UI updates
  are limited to approximately 5–10 Hz.
- Manual jobs warn about shared resources and cannot duplicate the active stage
  for the same project.
- Focused/full tests, Ruff, compileall, QML lint, schema upgrade checks, Win64
  build, release manifest verification, and isolated packaged-EXE smoke checks
  pass for `2.0.0a68`.

## Execution status

- [x] Domain contracts, schema v4, repository, coordinator, IPC, ViewModel,
  responsive QML screen, navigation, version bump, and initial focused tests.
- [ ] Close remaining coverage gaps for import branching, pause boundaries,
  event throttling, fingerprint invalidation, 50-card behavior, and migration.
- [ ] Run full quality gates and resolve deterministic regressions.
- [ ] Complete code review and performance/contract audit.
- [x] Update user and architecture documentation.
- [ ] Build and smoke-test `DUCAI-STUDIO-2.0.0a68-win64` with isolated app data.

## Follow-up reliability releases

- [x] Release a89 makes Multi controls optimistic, bounds pause latency with a
  checkpoint-preserving worker interruption, keeps pause/resume on the current
  stage, refreshes manual CJK counts immediately, and retries failed cards from
  their durable stage instead of resetting them to OCR.

- [x] Release a71 classifies deterministic `ocr_review_required` results as
  non-retryable during the immediate pass, preserves the review SRT/checkpoint,
  defers the video once, and keeps transient failures on the five-retry policy.
