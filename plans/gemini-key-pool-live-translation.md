# Gemini key pool and live translation

## Status

Completed in `2.0.0a12` on 2026-08-25. The Windows release is under
`outputs/DUCAI-STUDIO-2.0.0a12-win64`.

## Outcome

DUCAI STUDIO translates with Gemini Web or API using purpose-specific JSON
contracts, accepts an ordered named pool of API keys, fails over safely when a
key is quota-exhausted or no longer authenticates, and shows newly checkpointed
Vietnamese cue text in the bilingual editor while the job continues in the
background.

## Constraints

- API secrets remain only in Windows Credential Manager. SQLite, project files,
  job configuration, cache, events and logs may contain reference names but no
  raw keys.
- Keep exact cue IDs, source timestamps, semantic chunking, Film Bible, QA,
  repair and final PASS/REVIEW gates.
- Preserve user-entered key order. A successful key remains active until the API
  verifies quota exhaustion or authentication failure.
- Do not rotate on an unclassified 429 or transient network/server error.
- Live editor updates must not persist a full project revision per chunk or make
  tab navigation stop the worker.

## Non-goals

- No parallel multi-key chunk dispatch; this release implements ordered
  failover, not concurrent translation.
- No raw-key display after credentials have been stored.
- No partial SRT publication or export eligibility before the existing final QA
  gate passes.

## Acceptance criteria

- Film Bible, translation/repair and QA requests carry explicit required JSON
  schemas accepted by Gemini API; `gemini-3.5-flash-lite` cannot legally return
  an empty object for a translation request.
- Web and CLI keep the same strict parsers and prompts, with no relaxation of ID
  or cue coverage validation.
- The API screen accepts one credential per line as `name=key` or a raw key with
  an automatically generated stable name, stores all entries through the Job
  Manager and lists saved names in entered order.
- A start click can save a non-empty pasted key list and then start the API job
  without a second click.
- Verified quota and authentication failure move to the next named key; an
  unverified 429 does not. Exhausting the finite pool terminates once with a
  bounded secret-free error.
- Every successfully parsed subchunk is checkpointed, then emitted as a
  `translation_delta`. The UI polls only unseen delta events and updates the
  matching `translatedText` roles without reloading or saving the full project.
- Pause/resume, cancel, app restart event replay and final SRT auto-import remain
  compatible.
- Focused tests, full pytest, Ruff, QML lint and Windows packaging pass.

## Validation

1. [x] Reproduce the prior `{}` response with a generic object schema and assert all
   production request types now expose explicit properties and required fields.
2. [x] Test ordered key storage/parsing, quota failover, authentication failover,
   unclassified 429 behavior and finite-pool termination.
3. [x] Test worker delta ordering relative to checkpoint and UI row patching/event
   cursor behavior.
4. [x] Run the full project suite, Ruff, QML lint and package a new GUI-subsystem
   release under `outputs`.

Additional review guards cover the official HTTP 400 `API_KEY_INVALID` shape,
secret-shaped reference names, project-switch delta replay, terminal cache status,
late terminal deltas and raw Playwright target-closed failures.

## Risk and rollback

The main risks are leaking keys through batch request context, duplicate delta
replay and stale deltas being applied after the user switches projects. Batch
contexts contain reference names only; event application is scoped by job and
project with a monotonic sequence cursor. Rollback removes batch input and delta
polling while retaining existing single-key Credential Manager entries and
translation checkpoints.
