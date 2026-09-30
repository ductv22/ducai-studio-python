# Gemini transport and resume fix

Status: completed

## Outcome

- Gemini API responses with nested usage metadata complete without crashing the worker.
- Translation retries use only the credential references assigned to that provider.
- Changing translation provider or model starts a matching job instead of resuming a stale failed job.

## Constraints and non-goals

- Preserve project data, checkpoints, credential secrets and SQLite schema.
- Keep the existing Gemini endpoint, model selection and retry semantics.
- Do not redesign translation QA, prompts or the Multi video coordinator.

## Acceptance criteria

- The pre-fix `FrozenJsonMapping is not JSON serializable` reproduction serializes successfully.
- A Gemini retry does not append unrelated vault references.
- A failed Web job followed by an API selection creates an API job; an unchanged selection still resumes cache.
- Focused tests, full pytest, Ruff, compileall, QML lint and the Windows release build pass.

## Verification

- Regression baseline failed in five expected places before the production fix.
- Focused suite: 196 passed.
- Full suite: 721 passed, 1 skipped.
- Ruff, compileall and QML lint passed.
- Windows release built with manifest version `2.0.0a100`.
