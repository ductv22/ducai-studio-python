---
phase: 3
title: "UI, settings and Multi integration"
status: pending
priority: P1
effort: "1d"
dependencies: [2]
---

# Phase 3: UI, settings and Multi integration

## Overview

Expose the extraction method without making the current Trích xuất tab or
Multi pipeline ambiguous.

## Requirements

- Functional: clear `OCR khung hình` / `Whisper âm thanh` selector; method-aware
  primary button, progress and errors; persistent local choice; Multi snapshot
  carries the chosen method when explicitly selected.
- Non-functional: default stays OCR for old settings/batches; no model loading
  in GUI; controls react immediately and avoid polling.

## Architecture

The ViewModel owns a QSettings-backed extraction method and resolves its
external runtime path. QML hides FPS for Whisper and shows model/device/runtime
health instead. New Multi snapshots include the method/runtime/model fields;
legacy snapshots without them resolve to `frame_ocr`.

## Related Code Files

- Modify: `src/ducai_studio/ui/viewmodels/application_view_model.py`
- Modify: `src/ducai_studio/ui/qml/screens/SubtitleEditorScreen.qml`
- Modify: `src/ducai_studio/ui/qml/controls/JobActivityPanel.qml`
- Modify: `src/ducai_studio/job_manager/multi_video_coordinator.py`
- Modify: `tests/unit/test_ui_view_models.py`
- Modify: `tests/integration/test_multi_video_coordinator.py`
- Modify/add: QML contract tests used by the repository

## Implementation Steps

1. Add settings properties/slots and validated method-specific create request.
2. Add the two-option selector, hide irrelevant FPS, show preflight and label
   Whisper progress phases (`tách audio`, `nạp model`, `nhận dạng`, `hoàn tất`).
3. Preserve the one-button start/pause/resume/cancel state machine for both modes.
4. Snapshot the explicit method into Multi; keep missing/default config on OCR.
5. Test state transitions, disabled reasons, restart persistence, old settings,
   and that only one extraction worker runs for a project.

## Success Criteria

- [ ] Users can distinguish which evidence source will run before clicking.
- [ ] Switching method cannot resume a cache from the other method.
- [ ] Old projects and old Multi batches continue with OCR.
- [ ] Progress shown is worker-derived rather than a UI timer.

## Risk Assessment

Changing the globally selected method can unintentionally affect a queued Multi
batch. Mitigation: each batch captures an immutable snapshot at Start; later UI
changes affect only new manual jobs/batches.
