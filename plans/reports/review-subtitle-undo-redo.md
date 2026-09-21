# Subtitle undo/redo review

Scope: subtitle command history, immutable project revisions, compact subtitle patches, and UI busy-state/error handling.

## Verification

Focused tests passed: `tests/subtitles/test_subtitle_commands.py`, subtitle integration tests in `tests/integration/test_job_manager_server.py`, and subtitle/optimistic UI tests in `tests/unit/test_ui_view_models.py` (15 selected tests).

## Findings

### P2 — Invalid compact patch can leave subtitle UI permanently busy

`ApplicationViewModel._action_succeeded()` applies a compact patch before clearing `_subtitle_busy` (`src/ducai_studio/ui/viewmodels/application_view_model.py:6136-6141` vs. `6190-6192`). `ProjectStateModel.apply_subtitle_patch()` raises for a wrong project or stale/malformed revision (`src/ducai_studio/ui/viewmodels/project_job_models.py:2156-2162`). The exception path does not call `_action_failed()` or clear the busy flag. A direct reproduction with a wrong patch id raised `ValueError` and left `subtitleBusy` true. A project switch while a command is in flight can produce this stale-result path.

### P1/P2 — Cached editor can reuse a failed immutable-conflict snapshot and clobber newer content

`JobManagerService.subtitle_command()` caches the `SubtitleEditor` before mutating it and only compares editor/repository revisions when selecting it (`src/ducai_studio/job_manager/service.py:3266-3270`). The save can reject a same-revision different-content snapshot because the revision ledger is immutable (`src/ducai_studio/infrastructure/project_repository.py:279-287`). On that failure the mutated editor remains cached. If another writer published the conflicting snapshot at that same revision, the next subtitle command sees matching revision numbers, reuses the stale editor, and saves a new revision based on stale full-project content, potentially overwriting the competing change. Invalidate/reload the editor after save failure or compare snapshot content/digest, not revision alone.

## Confirmed behavior

- `SubtitleEditor.undo()` and `.redo()` restore full snapshots while assigning `current.revision + 1`, preserving strictly increasing revisions for actual history transitions (`src/ducai_studio/application/subtitle_commands.py:350-371`).
- `ProjectRepository.save()` keeps `projects.head_revision` monotonic with `MAX` and rejects changed content at an existing revision.
- Compact responses contain both cue arrays and omit tracks; the client applies only cue state and keeps the rest of the current project intact.
- The normal asynchronous failure path rolls back optimistic text changes and clears subtitle busy state (`ApplicationViewModel._action_failed()`).

Note: the client guard intentionally permits an equal revision (`revision < self._revision`) so no-op commands can return an idempotent patch. Therefore strict increase is guaranteed for edits/undo/redo, while duplicate/no-op responses are non-decreasing.

Status: DONE_WITH_CONCERNS
