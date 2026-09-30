---
phase: 3
title: "Verified install, health confirmation and rollback"
status: pending
priority: P1
effort: "2d"
dependencies: [1, 2]
---

# Phase 3: Verified install, health confirmation and rollback

## Overview

Download and stage verified bytes; an external helper replaces only application-owned files after old process exit, confirms restart health and rolls back on failure.

## Requirements

- Streaming download reports received bytes against signed expected size; supports cancel/retry and removes incomplete stage. Verify HTTPS redirects, exact size, SHA-256, signature and per-file hashes before Install.
- Helper runs outside running EXE/install tree. It never touches DUCAI_STUDIO_DATA, QSettings, projects/media/cache/models, external bin or arbitrary user files.
- Startup success requires nonce-bound signal after QML root loads and Job Manager/backend is ready, not process spawn alone. On launch failure/timeout, terminate only helper-launched process, restore old managed set and restart old build.

## Architecture

Keep private staging/backup/journal under app-data update directory with owner-only ACL. Copy a small trusted PowerShell helper from current installation to staging; launch system PowerShell so it does not depend on frozen GUI DLLs. Pass prevalidated absolute paths, nonce and the already verified manifest SHA-256 as command arguments. Helper rechecks the manifest digest, every staged file hash, old PID exit, root containment and reparse-point-free managed path list immediately before writing. Backup current targets named by the new signed manifest and replace them with a transaction journal; do not delete any stale a117 path based on its unsigned manifest. A future removal must be named and old-hash-bound by the new signed manifest. Launch the new EXE and accept health only from the helper-launched PID with matching install root, version and nonce after backend readiness. On failure, stop that process tree, restore old files and launch old EXE. Use per-install lock and bounded timeouts. Plan for journal recovery after helper interruption; power-loss recovery should be tested if claimed.

## Related code files

- Create src/ducai_studio/infrastructure/update_download.py: streaming verification/staging, cancellation, progress callback.
- Create src/ducai_studio/infrastructure/update_install.py: install-root/portable detection, ownership and helper handoff.
- Create scripts/updater-helper.ps1: wait, backup, swap, health wait, rollback, cleanup.
- Modify src/ducai_studio/__main__.py and src/ducai_studio/ui/app.py: nonce-bound health handoff after GUI/backend readiness; preserve frozen worker -m dispatch and single-instance lock.
- Create tests/unit/test_update_download.py and tests/integration/test_update_install.py.

## Implementation steps

1. Write a117-shaped app-root fixtures with user sentinel and portable sibling data. Add tampered ZIP, traversal/link/duplicate entry, short body and unsafe redirect cases.
2. Download to .part; flush/hash/size verify; safely extract into private stage. Never execute downloaded content before trust validation.
3. Validate install-root containment and external handoff after no-active-job gate. Handle locked files, permissions, disk pressure, anti-virus delay and concurrent attempt without data mutation.
4. Implement helper journal/backup, old-process wait, managed-file replacement, bound startup health signal and rollback. Preserve old installed manifest on rollback. Cleanup only owned stage/backup. Do not claim old-build rollback across a data-schema migration unless that release explicitly proves backward compatibility; a118 performs no schema migration.
5. Exercise success and failed startup/timeout with real subprocess lifecycle where feasible; compare protected-file hashes before/after.

## Success criteria

- [ ] a117→a118 fixture installs and health-confirms in normal/portable layouts; protected data remains byte-identical.
- [ ] Tampered/unsafe input never enters install; failed startup restores and launches old build.

## Risk assessment

- Windows file locks/helper policy vary. Signal: packaged smoke cannot replace live EXE; response: retain old files, show actionable error and revise helper before publication.
- A new job could race shutdown. Signal: manager reports activity after idle check; response: abort handoff; use an authoritative reservation in Phase 4, not UI-only polling.
