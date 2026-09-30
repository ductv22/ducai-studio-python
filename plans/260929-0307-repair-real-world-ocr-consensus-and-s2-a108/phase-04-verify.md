---
phase: 4
title: "Verify and prevent"
status: complete
dependencies: [3]
---

# Phase 4: Verify and prevent

- [x] Replay the saved real evidence and compare before/after outputs.
- [x] Run OCR, worker, translation, TTS, SRT and export blast-radius tests.
- [x] Run full pytest, Ruff, compileall, QML lint and `git diff --check`.
- [x] Confirm public data contracts and SQLite schema remain unchanged.

## Evidence

- Real video probes: `好吧` at 58.6–58.8 s, `看哪里`/`在这呢` at
  231.0–232.4 s, `乱星海` at 7.6–9.04 s and `海王兽` at 118.24 s.
- Full pytest passed with one expected opt-in skip; non-Whisper independent run:
  818 passed, 1 skipped, 20 deselected.
- OCR/worker/release suite: 92 passed; downstream blast radius: 145 passed.
- Ruff, compileall, QML lint and `git diff --check` passed.

