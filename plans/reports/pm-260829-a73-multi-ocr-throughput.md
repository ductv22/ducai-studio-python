# A73 Multi OCR throughput repair

## Outcome

- Release: `DUCAI-STUDIO-2.0.0a73-win64`
- Multi now snapshots the same OCR FPS selected for a single-video run and
  persists that setting in QSettings.
- Bundled Paddle/Rapid hosts expose a batched-band path; Paddle processes upper
  and lower subtitle crops in one `predict` call while preserving band labels,
  evidence IDs and filtering.

## Root-cause evidence

The live a71 child job `c1af75ab-2a46-4767-aa33-48015366901b` uses a 0.8-second
configured interval, which produces 59,587 fast samples for a 6h37m video. It
measured about 18 samples/second and an ETA of 39 minutes at 17,040/59,587,
matching the single-video rate. No duplicate Multi OCR child was present.

The engine host submitted the same decoded frames to Paddle once for the upper
subtitle band and again for the lower band. Live sampling showed the OCR Python
process CPU-bound with GPU utilization around 36%, so eliminating the duplicate
model-call boundary is the cause-aligned throughput change. The exact speedup
depends on detector batching and must be measured on a new a73 child; it is not
claimed from the a71 process.

`MultiVideoCoordinator` also hard-coded 800,000 microseconds, so a user who
selected another FPS in the single-video OCR screen silently received a
different sampling policy in Multi.

## Verification

- Focused OCR, worker, UI model and QML resource tests passed.
- Full pytest: exit 0, one expected skip.
- Ruff, compileall and QML lint passed.
- Regression tests cover one batched call for both subtitle bands and persisted
  OCR FPS shared by single and Multi workflows.
- Built release manifest `DUCAI-STUDIO-2.0.0a73-win64`; all 2,841 file hashes
  matched the final package.
- Packaged EXE smoke opened the Multi video screen, captured a valid screenshot,
  exited 0, and left no a73 child process behind.

## Operational note

The user's currently running job is a71 and keeps its original worker/runtime
by design. The a73 batching change applies when the next OCR child is started or
resumed under the new release; the current checkpoint and cache are untouched.
