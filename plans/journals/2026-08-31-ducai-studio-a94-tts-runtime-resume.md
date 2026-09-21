---
title: DUCAI STUDIO a94 TTS runtime resume
date: 2026-08-31
summary: Rebased stale packaged TTS hosts across releases without losing job checkpoints or custom host ownership.
---

# DUCAI STUDIO a94 TTS runtime resume

## What happened

A production Multi TTS job kept the absolute `engine_host_path` from release a92. After a92 was removed, a93 reused the correct job and checkpoint but VieNeu failed at provider health with `TtsRuntimeUnavailable`; the worker then collapsed the useful reason to the exception class name.

## Decision

Treat only release-packaged OCR/TTS hosts as executable-owned runtime paths. Job Manager supplies the current release host and SHA-256 through a server-owned worker override while leaving the durable job specification, resume fingerprint and checkpoint unchanged. Explicit custom hosts remain authoritative, even if stale internal environment values exist.

## Verification

The pre-fix regression failed at both the server and worker boundaries. The a94 implementation passes six targeted runtime-host/error tests, 137 focused tests, and the full suite with 677 passed plus one environment-gated skip. Ruff, compileall, QML lint and lock checks passed. The Windows manifest tracks 2,841 files with no missing files or hash mismatches.

## Next steps

Open a94 manually when convenient and resume the failed Multi item. It should reuse the existing checkpoint rather than regenerate approved cue WAVs. No GUI or production SQLite mutation was performed during this delivery.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
