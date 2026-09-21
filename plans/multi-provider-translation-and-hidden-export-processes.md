# Multi-provider translation and hidden export processes

## Outcome

Add real OpenAI-compatible translation providers for DeepSeek, Tencent TokenHub
Hy3, Alibaba Qwen, Zhipu GLM, and a user-configurable endpoint, while keeping
the existing cue-integrity pipeline, named Credential Manager pools,
checkpoint/resume behavior, and live translation deltas. Ensure every
FFmpeg/ffprobe invocation used by export and capability/QA probes is hidden on
Windows without losing bounded diagnostics.

## Constraints

- API keys remain only in Windows Credential Manager; jobs, settings, logs and
  checkpoints store references, never secret values.
- One provider transport and one key at a time; rotate only on verified auth or
  quota failures and never discard a translation ledger.
- Existing Gemini provider IDs and behavior remain compatible.
- Preset endpoints are canonical; only the custom OpenAI-compatible endpoint is
  user-editable and must pass safe URL validation.
- Do not interrupt or modify the currently running export job.

## Non-goals

- No parallel translation requests, web-session redesign, or model quality
  claims beyond provider configuration and response validation.
- No database schema migration; provider metadata is persisted in the existing
  job configuration JSON.

## Acceptance criteria

1. A shared OpenAI-compatible provider sends Bearer-authenticated
   `/chat/completions` requests, extracts standard JSON content, classifies
   auth/quota/transient/format failures without exposing keys, and rotates a
   finite named pool safely.
2. Job Manager and worker accept the new provider IDs, persist canonical URL,
   model, JSON/thinking settings and credential references, and retry from the
   existing checkpoint.
3. QML exposes the new providers, editable model, custom URL, provider-scoped
   named key list, and working start/resume actions.
4. Existing translation and UI tests remain green, with focused coverage for
   OpenAI-compatible success, malformed output, auth/quota rotation and URL
   validation.
5. All export capability probes, chunk/final FFmpeg runs and ffprobe QA runs
   use `CREATE_NO_WINDOW` on Windows; export diagnostics and exit-code handling
   remain intact.
6. Focused tests, full pytest, Ruff, compileall and QML lint pass; docs describe
   the new provider setup and hidden-process behavior.

## Status

Completed 2026-08-26. Full verification passed after the final endpoint and
quota-classification hardening; release artifact is `DUCAI-STUDIO-2.0.0a44-win64`.
