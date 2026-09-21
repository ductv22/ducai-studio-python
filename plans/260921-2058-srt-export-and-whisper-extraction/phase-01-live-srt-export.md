---
phase: 1
title: "Live SRT export contract"
status: pending
priority: P1
effort: "0.5d"
dependencies: []
---

# Phase 1: Live SRT export contract

## Overview

Add a short Job Manager action that serializes the latest persisted source or
translated cues to a user-selected SRT path.

## Requirements

- Functional: export `source_cues` or `translated_cues`; preserve current index,
  time range, line breaks and text; reject an empty language track.
- Non-functional: UTF-8 BOM, `\n` line endings, atomic temporary-file replace,
  `.srt` validation, no project revision or cache mutation.

## Architecture

QML opens `FileDialog.SaveFile` and sends language/path through the ViewModel.
The server dispatches to `JobManagerService.export_subtitle_srt`, reloads the
latest snapshot, renders with the shared `render_srt`, writes atomically, and
returns path/cue count/language. The ViewModel shows a Vietnamese toast/status.

## Related Code Files

- Modify: `src/ducai_studio/job_manager/service.py`
- Modify: `src/ducai_studio/job_manager/server.py`
- Modify: `src/ducai_studio/ui/viewmodels/application_view_model.py`
- Modify: `src/ducai_studio/ui/qml/screens/SubtitleEditorScreen.qml`
- Modify: `tests/integration/test_job_manager_server.py`
- Modify: `tests/unit/test_ui_view_models.py`

## Implementation Steps

1. Add service validation and atomic exporter using the canonical SRT renderer.
2. Add the versioned IPC action and compact response payload.
3. Add source/translated save dialogs and buttons in the matching workflow;
   expose both in Biên tập so manual fixes can be exported immediately.
4. Add success/error state handling and prevent duplicate clicks while writing.
5. Test Unicode, line breaks, exact timestamps, manual-edit freshness, invalid
   extension, empty translated track and existing-file replacement.

## Success Criteria

- [ ] Editing a cue then exporting writes the edited value, not an old job file.
- [ ] Source and Vietnamese exports cannot be swapped.
- [ ] Export does not change revision, track, cache or job state.
- [ ] UI reports the saved path and cue count in Vietnamese.

## Risk Assessment

Main risk is stale UI data. The server must reload by project ID instead of
accepting cue text from QML. Failure signal: exported hash/text differs from the
repository snapshot; response is to reject and reload rather than copy cache.
