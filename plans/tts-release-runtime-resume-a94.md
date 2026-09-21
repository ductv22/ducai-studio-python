# TTS release runtime resume a94

## Status

Completed and packaged for `2.0.0a94`.

## Outcome

Resume an interrupted or failed TTS job created by an older packaged DUCAI
release with the current packaged TTS engine host while retaining the exact job,
fingerprint, cue cache and latest checkpoint.

## Constraints and non-goals

- Do not mutate production SQLite or active user processes during delivery.
- Do not replace an explicit user-owned custom engine host.
- Do not change VieNeu/Piper synthesis behavior, retry policy or database schema.
- Preserve the existing OCR runtime-refresh contract.

## Acceptance criteria

- A missing packaged a92 TTS host resumes through the a94 host with the same job
  and checkpoint cursor.
- A valid custom TTS host remains selected.
- The worker validates the current host SHA-256 before loading the provider.
- Runtime failures retain a safe internal code and show a Vietnamese reason and
  recovery action.
- Focused and full tests, Ruff, compileall, QML lint, release build and manifest
  verification pass.

## Implementation

- [x] Detect only release-packaged OCR/TTS engine-host paths in Job Manager.
- [x] Supply the current packaged TTS host and SHA-256 through a server-owned
  worker override without rewriting job configuration or resume fingerprint.
- [x] Ignore inherited stale overrides for user-owned custom TTS hosts.
- [x] Validate the selected runtime host before provider initialization.
- [x] Preserve a safe internal error code while carrying a Vietnamese reason
  and recovery action to Multi/UI.
- [x] Update architecture and user workflow documentation.
- [x] Pass focused/full quality gates and package the a94 Windows release.

## Verification

- Red/green reproduction: a93 lacked the TTS override; a94 passes six targeted
  runtime-host/error tests.
- Focused Job Manager/Multi/TTS/runtime suite: 137 passed.
- Full suite: 678 collected, 677 passed and one environment-gated skip.
- Ruff, compileall, QML lint and `uv lock --check`: passed.
- Windows release: `DUCAI-STUDIO-2.0.0a94-win64`.
- Manifest: 2,841 tracked files, zero missing and zero SHA-256 mismatches.
