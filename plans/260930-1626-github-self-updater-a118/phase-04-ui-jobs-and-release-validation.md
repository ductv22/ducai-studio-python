---
phase: 4
title: "UI, global job gate and publication"
status: pending
priority: P1
effort: "2d"
dependencies: [1, 2, 3]
---

# Phase 4: UI, global job gate and publication

## Overview

Expose the update journey in Settings, block install across all active work, validate packaged behavior and docs, then create the public binary-only release.

## Requirements

- Immediate UI state: checking, none, available, downloading with real bytes/total/percentage, verified, waiting for jobs, installing/restarting, error. Manual check, channel choice, retry/cancel, explicit Install; no fake ETA/progress.
- Gate all projects, not current-project busy flags. Block queued/start/running/pause/resume/cancel jobs, Multi running/pause_requested, and in-flight manual starts. A downloaded update becomes eligible after work ends but never auto-installs.
- Final gate is authoritative and race-safe. Cached list_jobs(limit=100) alone can miss other projects and older active jobs and races a new start.

## Architecture

Add update QObject/view-model with QML properties/signals and background network worker; Qt event loop never blocks. ApplicationViewModel owns it and exposes check/download/install/channel actions. Job Manager IPC atomically reserves the update handoff, checks every nonterminal job and Multi state, and rejects every work-admission path while reserved: all `create_*_job`, `start_job`, `retry_job`, `resume_job`, `multi_start`, `multi_resume`, and coordinator-triggered child starts. Release the reservation on abort/error; retain it through committed shutdown. Existing normal quit shuts workers down, so call it only after manager certifies idle. SettingsScreen displays panel; Main.qml hosts confirmation/restart dialog following existing patterns. Persist channel in QSettings; default testing for alpha build.

## Related code files

- Create src/ducai_studio/ui/viewmodels/update_view_model.py; modify src/ducai_studio/ui/viewmodels/application_view_model.py and src/ducai_studio/ui/app.py.
- Modify src/ducai_studio/job_manager/server.py, src/ducai_studio/job_manager/service.py, and job repository only if needed for atomic global idle/install lease; preserve versioned IPC and existing job semantics.
- Modify src/ducai_studio/ui/qml/screens/SettingsScreen.qml and src/ducai_studio/ui/qml/Main.qml.
- Modify tests/unit/test_ui_view_models.py, tests/integration/test_job_manager_server.py, tests/unit/test_ui_resources.py; add focused updater UI tests.
- Modify docs/user-guide.md, docs/architecture.md and README.md only where release/setup navigation requires it.

## Implementation steps

1. Write UI transition and activity tests: another project's job, paused/queued work, Multi, pending manual start, stale snapshot, 101st job and new start racing Install.
2. Implement manager-owned atomic eligibility/reservation contract; reject new job starts during narrow handoff and release reservation on cancel/error. Avoid database schema change unless source evidence requires it.
3. Implement background check/download and QML controls. Show verified version/channel/size, received bytes, human-readable errors and install-after-work affordance; never silently install.
4. Integrate handoff with requestQuit/quitReady only after final idle certificate. Test normal window close independently from updater close.
5. Update guide/architecture for channels, first-upgrade limitation, data boundary, error/rollback and operator signing/publication. Verify claims against code/tests.
6. Run focused updater tests, then full pytest, Ruff, compileall, wheel/build scripts, QML lint and packaged normal/portable smoke. Secret-scan final assets. Only then create ductv22/ducai-studio-releases as PUBLIC, upload ZIP+manifest+signature as prerelease tagged v2.0.0a118, verify anonymous API/download and asset hashes, and keep source repo private.

## Success criteria

- [ ] Settings manual check/download/install and real progress pass QML/interaction tests.
- [ ] Active jobs block without interruption; Install is offered when idle; no race.
- [ ] All build/test/smoke/security gates pass; public repo has only intended binary release assets, no source/history/PAT.

## Risk assessment

- Existing ApplicationViewModel._replace_jobs is current-project oriented and manager list is limited. Signal: cross-project/101st-job test fails; response: manager-owned global lease/query, not UI polling.
- Signing dependency may not bundle into Nuitka. Signal: frozen app cannot verify fixture; response: fix deployment inclusion and rerun packaged smoke before publication.
