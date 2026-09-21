# a67 Timeline D1 release report

Status: complete

## Delivered

| Area | Result |
|---|---|
| D1 publish race | Timing policy no longer depends on the delayed job-state transition. |
| Timing colors | Overflow over 50 ms is red; underflow/position mismatch is yellow; matched is green. |
| Resize | Local drag preview, one command on release, SRT snap guides, 0.50x–3.00x limits. |
| Model refresh | Vertical track scroll is restored so D1 does not jump out of view after resize/undo. |
| Errors | Structural failures are reported in Vietnamese with cue and timestamp context. |
| Release | `2.0.0a67` Win64 package built and verified. |

## Evidence

- Real job `31b8df91-81fd-462d-8df1-836832abfc30` resumed from cache and
  published all 359 D1 clips without another VieNeu synthesis pass.
- Real-project Inspector showed the expected Vietnamese explanations for a
  matched cue, a 52 ms underflow, and a 108 ms overflow.
- Each tested edge release added one revision. Two undo operations restored the
  tested cue ranges to their original values; no cache or media was deleted.
- Final packaged-EXE smoke test resized cue 1 from matched/green to
  underflow/yellow, kept D1 in view across model refresh, and restored the cue
  to matched/green at 1.00x with one undo. No white screen or visible stall was
  observed.
- The project remains complete after the reversible smoke test: S1=359,
  D1=359; 22 overflow, 243 underflow, 23 position mismatch, and 71 matched.
- Full suite: 535 passed, 1 skipped. Ruff, compileall, QML lint, build, manifest,
  and packaged-QML verification passed.
- Release EXE SHA256:
  `4bc62147009d2a04c562fea1662e60265c40032f65f6dbf8632207714a98789a`.
- Source and packaged `TimelineScreen.qml` SHA256 both equal
  `dec4a0a79bdb3458aa029fbf865cf3da8d937be1cd77a9f466a7fa1501f2d194`.

## Unresolved questions

- None for the a67 acceptance criteria.
