---
phase: 4
title: "Verification, docs and release"
status: completed
priority: P1
effort: "1d"
dependencies: [1, 2, 3]
---

# Phase 4: Verification, docs and release

## Overview

Prove the port does not regress OCR, translation, Multi, subtitle editing or
portable runtime behavior, then publish the next release and source commit.

## Requirements

- Functional: smoke-test OCR and Whisper on real media, export both SRT tracks,
  edit and export again, pause/resume/cancel, Multi default OCR and explicit Whisper.
- Non-functional: full tests/lint/compile/QML lint pass; no secret, model, cache,
  machine path or runtime binary enters Git.

## Architecture

Tests mock the external engine protocol for deterministic CI and add one local
runtime smoke test gated by runtime availability. Release scripts validate
metadata/hashes and document standard versus portable runtime contents.

## Related Code Files

- Modify: smallest owning surfaces in `docs/` and `README.md`
- Modify: version source and release manifest owner
- Modify: `THIRD_PARTY_NOTICES.md`
- Modify: release tests/scripts only as required by phase 2

## Implementation Steps

1. Run focused SRT, OCR/Whisper worker, service/server, ViewModel/QML and Multi tests.
2. Run full pytest, Ruff, compileall and QML lint; review shared callers and
   public contract compatibility.
3. Update user guide, architecture/runtime ownership and third-party notices.
4. Build the next Windows release; optionally package Whisper runtime/model only
   in the portable artifact when explicitly requested and present.
5. Smoke-test as a user, verify exported SRT hashes/content and model/runtime
   preflight, then secret/large-file scan, commit and push to the private remote.

## Success Criteria

- [x] All repository quality gates pass without exclusions.
- [x] Standard GUI remains small and starts when Whisper is not installed.
- [x] Missing runtime/model gives an actionable Vietnamese message.
- [x] Portable manifest accurately states whether Whisper runtime/model is included.
- [x] Private remote contains source/tests/docs only, no user project or cache.

## Risk Assessment

The model can add several GB. Standard release must never include it silently;
portable packaging is explicit and manifest-verified. If smoke testing reveals
unacceptable pause latency or timestamp drift, do not ship Whisper enabled by
default; keep OCR intact and replan the engine protocol.
