# Multi video project catalog

Status: complete (released as a69; selection-latency follow-up in a70)

## Outcome

Make **Multi video** a project-backed catalog: every existing project that owns
at least one video appears automatically, regardless of which folder contains
the source file. Cards show truthful batch/manual-job/project-artifact state,
and the tab has its own video-only importer accepting one or many files.

## Constraints

- Keep the global multi-file media importer and its one-file single-project
  behavior unchanged.
- A file imported from inside Multi always creates/reuses a one-video project,
  adds it to Multi, and selects it, even when only one file was chosen.
- Auto-discovered projects are visible but unselected and never auto-start.
- Existing checkbox choices and card order remain stable across refresh/restart.
- **Xóa khỏi Multi** remains persistent and never deletes project, media, job,
  artifact, checkpoint, or cache. Auto-discovery must not immediately re-add it.
- Multi orchestration, retry, pause/resume, cache reuse, and one-child-at-a-time
  behavior from a68 remain unchanged.
- Keep one shared decoder, viewport-lazy thumbnails, partial status updates, and
  the current dark dense QML visual language.

## Non-goals

- Do not scan arbitrary folders or drives for loose media.
- Do not split a multi-video project into multiple cards; one eligible project
  remains one card and uses its primary video, matching the a68 project contract.
- Do not auto-select, auto-run, or auto-export discovered projects.
- Do not redesign the Projects screen or the global media import flow.

## Acceptance criteria

- Opening Multi or starting the app synchronizes every non-hidden project with a
  video into the durable card list; projects without video are excluded.
- Removed cards stay hidden after refresh/restart, can be restored, and retain all
  project/cache data.
- The header includes **Nhập video**. Its video-only picker accepts one or many
  paths; Unicode/spaces are normalized in Python and duplicate media reuses the
  correct one-video project.
- Status precedence is: pending removal; active/current Multi child; active manual
  job; recent terminal failure/interruption; project artifacts (`D1 sẵn sàng`,
  `Đã dịch`, `Đã trích xuất SRT`, `Chưa xử lý`).
- Progress is derived from real worker progress and project artifacts. Batch
  progress/status still overrides catalog state while a selected item is running,
  retrying, paused, deferred, failed, or completed.
- New catalog cards are unselected; Multi-imported/restored cards are selected.
  Selection count only includes visible eligible cards.
- Card status updates do not reset GridView delegates or reload thumbnails; one
  decoder remains active and 50-card scrolling stays responsive.
- Individual and **Chọn tất cả** checkbox changes appear in the card model in the
  same UI turn; durable persistence uses a lightweight status path and failed
  writes roll back with a Vietnamese explanation.
- Checkbox/menu/import controls have descriptive accessibility names and status is
  conveyed by text as well as color.
- Schema migration preserves all a68 project, job, attempt, cache, and membership
  data. Focused/full tests, Ruff, compileall, QML lint, migration tests, Win64
  build, manifest verification, and isolated packaged-EXE smoke checks pass.

## Execution status

- [x] Inspect a68 contracts, screenshot, project/job repositories, QML, tests, and
  documentation; complete UX contract review.
- [x] Add durable project-catalog discovery, exclusion/restore, and truthful status.
- [x] Add in-tab one/many video import and responsive/accessibility UI states.
- [x] Update version, documentation, migration coverage, and focused tests.
- [x] Run full quality gates, review, build, and isolated EXE smoke test.
- [x] Remove delayed checkbox propagation in a70 with optimistic model updates,
  lightweight persistence responses, stale-response protection and rollback tests.
