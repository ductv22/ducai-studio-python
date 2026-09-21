# OCR hard-sub coverage gate

## Outcome

DUCAI STUDIO only publishes and auto-imports `_ZH_FINAL.srt` when the finalized
cue timeline covers at least 95% of subtitle-positive sampled frame evidence
from the dense Paddle Fast scan and targeted Paddle Quality gap probes.

## Constraints

- Keep the existing two-band Fast → targeted Quality → Rapid fallback pipeline.
- Do not count cue count as coverage.
- Do not publish a false PASS when coverage evidence is absent or below 95%.
- Keep the current production OCR job running; source changes apply to the next
  packaged release, while its immutable evidence is audited with the same gate.
- Preserve pause/resume fingerprints and existing final/review artifact rules.

## Non-goals

- No speech-to-text fallback for videos whose dialogue has no hard subtitle.
- No destructive cleanup of old projects or caches.
- No weakening of conflict, overlap, or Chinese-text finalization checks.

## Acceptance criteria

- Coverage is measured from distinct subtitle-positive sampled timestamps, not
  cue count or total video duration.
- Repeated evidence in upper/lower bands counts as one sampled frame.
- A frame is covered only when a finalized cue overlaps it and its text agrees.
- Coverage below 0.95 produces `ocr_review_required`, an immutable review SRT,
  and a QA report; it cannot produce `_ZH_FINAL.srt`.
- PASS writes an atomic `_ZH_FINAL_QA.json` and Job Manager verifies that report
  before auto-importing the SRT.
- Focused unit/integration tests, full tests, Ruff, QML lint, and release build
  pass before the new package is used.

## Validation

1. Unit-test duplicate-band accounting, text/timing mismatch, and the 95% edge.
2. Integration-test worker PASS/REVIEW publication and QA payload.
3. Regression-test Job Manager rejecting a final SRT without matching QA.
4. Run the complete project test and packaging gates.

## Production finding

The first full four-hour run exposed two merge defects without invalidating the
95% gate: sparse Quality evidence at a cue's sampled end boundary did not
reinforce that cue, and an adjacent Quality cue could be mistaken for a
conflict merely because the sampling ranges overlapped. Quality readings with
the same character inventory in a different detected row order also require
the higher-quality reading to win. The repair must improve candidate timing and
merge selection; the coverage threshold remains unchanged.

## Risk and rollback

The main risk is false REVIEW from scene text or watermark evidence. Reuse the
same subtitle-band/edge filters as candidate grouping and report unmatched
timestamps for diagnosis. Rollback is limited to removing the coverage gate;
existing project snapshots and evidence artifacts remain compatible.
