---
title: "a118 — GitHub self-updater with verified rollback"
description: "In-app signed binary-only GitHub updates for normal and portable Windows installs."
status: completed
priority: P1
effort: high
branch: ducai-studio-python-a98
tags: [feature, frontend, infra, security]
blockedBy: []
blocks: []
created: 2026-09-30
---

# a118 — GitHub self-updater with verified rollback

## Outcome

Users can check, download with real byte progress, and explicitly install a newer DUCAI STUDIO GUI build from public ductv22/ducai-studio-releases. Installation waits for all OCR, translation, TTS, export, Multi, and manual jobs to finish. An out-of-process helper swaps only application files, restarts the app, confirms health, and rolls back on failure. The source repository remains private; no PAT is embedded.

## Constraints and non-goals

- Preserve DUCAI_STUDIO_DATA (including portable sibling data), projects, media, models, caches, credentials, QSettings, and external engine/FFmpeg runtimes. No auto-resume or interruption of active work.
- HTTPS only. A pinned Ed25519 public key authenticates a detached signature over deterministic manifest bytes. GitHub API asset size/digest are cross-checks, not substitutes for signature verification.
- No forced or silent installs, delta patching, heavy model/cache update, or embedded GitHub token.
- Preserve the dirty worktree and unrelated changes. Release publication is last, after a secret-safe build and validation.

## Decisions

| Concern | Decision |
|---|---|
| Channel | Query published releases. stable accepts final PEP 440 versions only; testing accepts final and prerelease. Select highest eligible Version, not lexical string. Do not use GitHub /latest, which excludes prereleases. |
| Trust | Publish an app-only ZIP, versioned update-manifest.json and detached signature. Manifest binds product, version, channel, platform, ZIP name/size/SHA-256, and the exact managed-file paths/hashes. Private signing key stays outside source and assets. Public assets contain no `.py` source; the three external-runtime hosts are compiled to interpreter-matched `.pyc`. |
| Install | Verify before shutdown; helper runs outside install directory, receives the verified manifest digest as a command argument, rechecks that digest plus every staged file hash, waits for the old process, backs up/replaces only paths in the new signed manifest, starts the new EXE, waits for a PID/root/version/nonce-bound ready signal, and restores backup on failure. It never derives deletion authority from the unsigned a117 manifest. |
| Layout | One app-only ZIP serves normal and portable roots. Never replace portable DUCAI_STUDIO_DATA, bin, or its runtime/model manifest. |

## Phases

| Phase | Objective | Status | Dependency |
|---|---|---|---|
| 1. [Release contract](./phase-01-start.md) | Trust, ownership, version/channel selection | Pending | — |
| 2. [Build artifacts](./phase-02-trusted-release-contract.md) | Minimal ZIP and reproducible signed metadata | Pending | 1 |
| 3. [Install and rollback](./phase-03-updater-install-and-rollback.md) | Staging, external helper, health handshake | Pending | 1–2 |
| 4. [UI and publication](./phase-04-ui-jobs-and-release-validation.md) | Immediate state/progress, global job gate, test/docs/release | Pending | 1–3 |

## Acceptance criteria

- [x] Deterministic fixture upgrades an a117-shaped directory to a118 in normal and portable layouts; portable data and arbitrary user files remain byte-identical.
- [x] Altered ZIP, manifest, signature, wrong size, unsafe archive path, wrong platform, and downgrade are rejected before managed-file mutation.
- [x] Global nonterminal job/Multi activity and pending manual starts block installation; update remains ready and is offered when work finishes, without auto-installing.
- [x] Failed launch/health timeout restores old managed files and starts the old app; successful health confirmation removes backup safely.
- [x] Settings supports manual check, channel choice, available/none/error states, real download bytes/total, cancel/retry and explicit Install.
- [x] Identical ZIP bytes and release metadata yield identical manifest bytes/signature input; public assets contain no source, secrets, models, caches or user data.
- [x] Focused tests, full pytest, Ruff, compile, wheel/release build, QML lint and packaged smoke gates pass before publication.
- [x] Public update assets contain no Python source files; packaged engine hosts execute as Python 3.11/3.12 bytecode through the existing managed runtimes.

## Release boundary and hidden constraint

The current a117 EXE has no updater entry point. An a117→a118 fixture proves replacement compatibility, but an already distributed a117 EXE cannot initiate an in-app update itself; its first upgrade requires manual installation or a separately authorized bootstrap. a118 is the first self-updating build. GitHub's [release API](https://docs.github.com/en/rest/releases/releases) exposes prerelease flags; [asset records](https://docs.github.com/en/rest/releases/assets) include size and SHA-256 digest. Create the public binary-only repository only after the final package passes the secret scan.
