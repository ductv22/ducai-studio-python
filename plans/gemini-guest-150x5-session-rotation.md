# Gemini Guest 150 cue / 5 request session rotation

## Status

Implementation and packaged verification complete for `2.0.0a24`; live resume
is waiting for the user-approved window to close `2.0.0a23` and open `a24`.

## Outcome

Continue the interrupted Chinese-to-Vietnamese translation from its immutable
ledger without retranslating completed cue IDs, use visible anonymous Gemini
Guest only, send at most 150 source cues in a translation request, open a fresh
Gemini chat for every provider request, and replace the complete guest browser
after five requests.

## Constraints

- Preserve the existing job ledger and every non-empty cached translation.
- Keep one browser, one page and one request active at a time.
- Count Film Bible, translation, QA and repair requests toward the five-request
  browser limit because every one is a real Gemini chat submission.
- Disable Codex/Luna fallback for this mode so no OpenAI API charge can occur.
- Run headed Guest mode for the requested live observation.
- Preserve exact source cue IDs, timestamps, Film Bible, QA, repair and guarded
  final SRT publication.
- Use the Job Manager for all durable state changes; do not edit SQLite or cache
  artifacts out of band.

## Non-goals

- Do not change OCR, TTS, effects, audio mixing or export behavior in this slice.
- Do not run parallel Gemini tabs or parallel translation jobs.
- Do not remove independent QA merely to reduce request count.
- Do not claim that browser rotation can eliminate Google server-side limits.

## Implementation

1. Add bounded Gemini Web session rotation with a fresh chat before each submit,
   transport telemetry, and exact failure reasons.
2. Raise the supported semantic translation chunk limit to 150 cues and set the
   visible Guest profile to 150 cues with a non-binding source-character cap.
3. Disable Codex fallback explicitly for this profile and verify that no Codex
   provider/process is created.
4. Add a safe retry-time Web transport refresh that preserves the checkpoint
   fingerprint while updating only allow-listed resumable transport/chunk
   options.
5. Re-plan only the unfinished ledger suffix. Preserve the contiguous QA-pass
   prefix and every cached translation in the suffix; translate only missing
   cue IDs and re-run QA for unfinished/review chunks.
6. Build `2.0.0a24`, resume the existing job in headed Guest mode and observe at
   least one five-request rotation with timings and failure classification.

## Acceptance criteria

- A unit test proves request 1–5 use one session, request 5 closes it and request
  6 creates a new session.
- A unit test proves transient failure closes the current session and preserves
  the exact unfinished request for retry.
- A chunking test proves translation requests can carry up to 150 cue IDs without
  the old 24-cue or 1,800-character defaults splitting them first.
- A resume test proves the old ledger keeps all cached translations, preserves
  the contiguous QA-pass prefix and re-plans only the unfinished suffix.
- A worker test proves `codex_fallback_enabled=false` creates no Codex provider.
- The existing job resumes from its current immutable ledger; cue 1 is not sent
  for translation again, and the visible browser is the only remote transport.
- Focused tests, the complete test suite, Ruff, QML validation and Windows
  packaging pass before the release is used for the live run.

## Risks and rollback

- Gemini Guest may impose server-side rate, challenge or anonymous-use limits;
  browser rotation is an experiment, not a guarantee. Telemetry must retain the
  exact classified failure so the result can be evaluated honestly.
- Large prompts may still exceed a page/model limit. Semantic splitting and the
  character cap remain active, and failed multi-cue requests retain the existing
  bounded split recovery.
- If resume migration validation fails, stop with the prior immutable ledger
  untouched. Rollback is to reopen `2.0.0a23` and resume with the old settings.
