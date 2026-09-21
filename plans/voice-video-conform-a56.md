# Voice/video conform after Vietnamese dubbing a56

Status: completed — 2026-08-28

## Outcome

Add a post-synthesis conform stage with three cue-scoped modes: stretch video,
speed voice, or combine both. Preview, subtitles, overlays, original audio and
FFmpeg export must share one monotonic source/output time map.

Also make long-running translation recovery explicit and lossless: every
compatible immutable ledger must appear in the cache selector, selecting one
must expose a single stateful **Continue translation from cache** control, and
pause/cancel/failure must resume that exact job instead of creating a new
translation ledger.

## Constraints

- Preserve every spoken cue; never crop, overlap or silently omit voice.
- Reuse synthesized cue WAVs. Mode and speed changes are post-processing only.
- Audio speed is 1.00x-2.00x (default 1.25x); video speed is at least 0.50x.
- Source timestamps remain canonical. Conform changes are persisted separately
  and applied atomically only after QA PASS.
- Job Manager remains the only project/SQLite writer.
- Never delete or invalidate translation checkpoints merely because a provider
  response, integrated QA pass, pause, cancellation or retry command fails.
- Only translation caches whose project and source fingerprint match the
  current source cues are selectable.

## Implementation

1. Add immutable conform settings, cue decisions, blocks and source/output time
   mapping helpers with explicit unresolved-cue evidence.
2. Split synthesis-quality cache identity from timing-render variants and add a
   cache-only conform TTS job that assembles the output D1 WAV.
3. Persist the conform plan with the project, expose mapped duration/cues to the
   Timeline preview, and support apply/reset without cumulative transforms.
4. Extend export snapshot/FFmpeg compilation for piecewise video `setpts`,
   original-audio `atempo`, mapped overlays/subtitles and conform-aware chunks.
5. Add Voice Studio mode controls, 1.00x-2.00x slider, estimates, stateful apply,
   reset and navigation to `CUE_SYNC_LIMIT_EXCEEDED` cues.
6. Run focused and full automated validation, update architecture/user guide,
   build a new isolated Win64 release and verify its manifest/smoke startup.
7. Treat cancelled translation ledgers as resumable, list every compatible
   ledger in the selector, bind selection immediately to the exact job ID and
   use one OCR-like primary button for start/pause/resume/retry-from-cache.
8. Defer malformed Gemini targeted-repair responses while preserving their QA
   blocker and continuing later chunks; surface the remaining issue for review
   instead of terminating and hiding hours of valid cached work.
9. Reject late worker success after pause/cancel before any project publish;
   serialize conform-cache writers and align every variant to absolute sample
   boundaries before overlap QA.

## Acceptance

- `S=2s/A=4s`: video mode yields video 0.5x; audio mode at 2x keeps 2s;
  hybrid at 1.25x yields video 0.625x and 3.2s output.
- A 5x cue is unresolved under the 2x/0.5x quality limits.
- Changing mode/slider does not invoke VieNeu when the synthesis cache exists.
- Preview and exported MP4 agree on duration and cue/overlay positions.
- No partial project revision or D1 replacement is published after failure or
  cancellation.
- Selecting a compatible cancelled/failed/interrupted/review cache changes the
  primary action to **Tiếp tục dịch từ cache** and retry targets the same job ID.
- Pausing or cancelling and restarting does not resend already checkpointed
  cues, and one stale/corrupt/incompatible cache cannot hide other compatible
  caches from the selector.

## Completion evidence

- Full suite: `479 passed, 1 skipped`.
- Focused cache/UI/conform suite: `115 passed`.
- Ruff, Python compileall and QML lint (`27` files): PASS.
- Independent code review: PASS; no evidenced P0/P1/P2 findings remain.
- Win64 release `2.0.0a56` built with manifest SHA-256 verification: `0` missing
  or mismatched files.
- Packaged QML and wheel both contain the stateful **Tiếp tục dịch từ cache**
  action and the immediate cache-record binding implementation.
