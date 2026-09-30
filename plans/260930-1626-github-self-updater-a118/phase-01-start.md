---
phase: 1
title: "Release contract and trust boundary"
status: pending
priority: P1
effort: "1d"
dependencies: []
---

# Phase 1: Release contract and trust boundary

## Overview

Specify and test signed release metadata, version/channel rules, and exact application file ownership before network or file mutation.

## Requirements

- An alpha such as 2.0.0a118 appears on testing; stable never selects a prerelease. Reject drafts, unpublished releases, wrong tags/platform, same/older versions, and duplicate/conflicting assets.
- Pin one Ed25519 public key in shipped code. Private key remains outside source, public repo, CI logs, and package. Key rotation requires an old-key-signed transition or manual new build.
- Canonical UTF-8 JSON manifest uses sorted keys/fixed separators and no timestamps/local paths; detached signature covers exact bytes. Bind ZIP SHA-256/size and per-file hashes/path list.
- Forbid DUCAI_STUDIO_DATA, bin, projects, caches/models and external runtime roots. Reject traversal, absolute paths, alternate data streams, Windows reserved names, trailing dots/spaces, links/reparse points, duplicate case-folded names and archive expansion abuse. ZIP entries must equal the signed file list exactly.

## Architecture

Pure metadata validation lives below Qt. Network client anonymously queries the public GitHub releases API with bounded pagination and selects highest eligible signed version. GitHub tag or API asset digest alone is not trusted. Require HTTPS and expected hosts on every redirect, with timeouts and bounded metadata size. Asset names are DUCAI-STUDIO-<version>-win64-update.zip, update-manifest.json, update-manifest.sig.

## Related code files

- Create src/ducai_studio/infrastructure/update_manifest.py: schema, canonical bytes/signature, versions/channels, safe paths.
- Create src/ducai_studio/infrastructure/github_updates.py: public API discovery and HTTPS/redirect policy.
- Modify pyproject.toml and uv.lock: explicit packaging and Ed25519 verification dependency if not direct already.
- Create tests/unit/test_update_manifest.py and tests/unit/test_github_updates.py.

## Implementation steps

1. Write fixtures for manifest/signature, a117→a118 comparison, stable/testing, published/prerelease filters, malformed metadata and wrong repository/asset.
2. Implement narrow typed parsing and exact product/platform/asset checks; validate all managed paths before extraction.
3. Implement public release discovery without PAT. Treat 403 rate limit and offline as retryable check errors, not no-update.
4. Record the signing public-key fingerprint and rotation rule; never commit signing private key.

## Success criteria

- [ ] Selection/signature tests pass; tampering and channel mismatches fail closed.
- [ ] Runtime needs no source/private repository access or credential.

## Risk assessment

- Key loss/compromise blocks trustworthy releases. Signal: signing fixture fails or key disclosure; response: halt publication and use reviewed rotation/manual bootstrap, never disable verification.
- GitHub API may change. Signal: schema/HTTP integration failure; response: show retryable error and preserve app; adapt against [official release docs](https://docs.github.com/en/rest/releases/releases).
