---
phase: 2
title: "Minimal signed build artifacts"
status: pending
priority: P1
effort: "1d"
dependencies: [1]
---

# Phase 2: Minimal signed build artifacts

## Overview

Extend current packaging to emit an app-only update ZIP and deterministic signed manifest, without publishing private source or heavy portable data.

## Requirements

- Preserve existing standard and portable full-release outputs. Update assets are separate; never upload portable archive or DUCAI_STUDIO_DATA as an update.
- ZIP includes only app-owned frozen runtime, QML/assets/fonts, interpreter-matched compiled engine-host bytecode, runtime notices and updater helper; exclude `.py` source, wheel/source archive, projects, profiles, secrets, models, caches and user-specific state.
- Manifest records exact ZIP size/hash and included file hashes/paths. Identical input ZIP bytes yield identical manifest bytes; do not claim byte-identical Nuitka builds without evidence.

## Architecture

Stage a clean allowlisted payload from deployment/DucaiStudio2.dist plus release-owned extras, then hash final ZIP bytes. Reuse build-release.ps1's validated GUI tree, not build-portable-release.ps1's runtime/data copy logic. Sign canonical manifest bytes with a private key supplied outside repository/artifacts. A no-sign dry run can validate packaging but cannot publish an installable release.

## Related code files

- Modify scripts/build-release.ps1: app-only artifact invocation and gates.
- Create scripts/build-update-assets.ps1 (or small Python companion if deterministic ZIP requires it): allowlist, ZIP, hashes, manifest, signing, exclusions.
- Modify scripts/build-portable-release.ps1 only as needed to assert supported layout/data separation.
- Modify tests/unit/test_release_build_contract.py; create tests/unit/test_update_assets.py.
- Modify src/ducai_studio/__init__.py, pyproject.toml, uv.lock together for 2.0.0a118.

## Implementation steps

1. Define one ownership list shared by packager and installer, with DucaiStudio2.exe required and protected-root rejection. Bundle helper in trusted current app rather than fetching code during installation. Compile OCR host with managed Python 3.11 and Whisper/TTS hosts with managed Python 3.12; source runs keep their `.py` fallback.
2. Generate ZIP from clean stage; hash exact archive; write canonical manifest and detached signature. Fail publish mode if signing material missing.
3. Scan staged paths and archive contents for `.py`/wheel/source, DUCAI_STUDIO_DATA, .env, keys, projects/media/cache/models, private Git metadata and machine-bound paths.
4. Build candidate from dirty worktree without cleaning unrelated edits; compare metadata with package. Do not create public repo/release yet.

## Success criteria

- [ ] Artifact tests prove minimal set, deterministic manifest for identical input, and size/hash/signature verification with shipped public key.
- [ ] Full standard/portable packaging remains compatible; external data stays outside update assets.

## Risk assessment

- ReuseExistingDeployment can pair stale EXE with new source version. Signal: frozen binary reports version different from manifest; response: fail and require fresh deployment for publish.
- Accidental source/privacy inclusion exposes private material. Signal: allowlist/scan fails; response: stop before upload, inspect and rebuild.
