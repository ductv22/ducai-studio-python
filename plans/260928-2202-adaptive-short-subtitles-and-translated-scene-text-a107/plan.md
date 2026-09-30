---
title: "Adaptive short subtitles and translated scene text a107"
description: "Catch one- and two-character dialogue reliably, translate non-dialogue scene labels, and replace their burned-in Chinese text in place without sending them to TTS."
status: completed
priority: P1
effort: "large"
branch: "ducai-studio-python-a98"
tags: [feature, ocr, subtitles, translation, export, frontend]
blockedBy: []
blocks: []
created: 2026-09-28
---

# Adaptive short subtitles and translated scene text a107

## Outcome Contract

- S1 retains short one- and two-character Chinese dialogue that is presently lost by the two-character/two-sample gate.
- A separate scene-text role captures character names, techniques, artifacts and location/title cards anywhere in the frame.
- Scene text is translated but never synthesized by TTS; preview and export conceal only the source glyph region and place Vietnamese inside the retained panel/background.
- Layout follows the detected horizontal/vertical direction, auto-fits the source box and uses an OFL Vietnamese handwriting display font when the source is classified as calligraphic.

## Constraints and Non-goals

- Keep old projects readable without SQLite migration; missing cue metadata defaults to normal dialogue.
- Preserve the existing a106 conservative Bilibili filter. A persistent corner logo must not become scene text.
- Do not modify source media or claim to reconstruct pixels hidden by burned-in glyphs.
- Do not scan every full-resolution frame with PaddleOCR. Use inexpensive visual-change detection plus bounded OCR bursts.
- Automatic typography is best-effort; low-confidence scene text remains reviewable rather than being silently exported.

## Goals

| # | Goal | Priority |
|---|------|----------|
| 1 | Add backward-compatible scene-text cue metadata | P1 |
| 2 | Recover short dialogue with adaptive OCR bursts | P1 |
| 3 | Detect and classify full-frame scene text without admitting watermarks | P1 |
| 4 | Translate, edit, preview and export in-place Vietnamese replacements | P1 |
| 5 | Verify and build `2.0.0a107` | P1 |

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | [Backward-compatible cue and render contracts](./phase-01-start.md) | Pending |
| 2 | [Adaptive short-cue OCR](./phase-02-adaptive-short-cue-ocr.md) | Pending |
| 3 | [Scene-text detection and classification](./phase-03-scene-text-detection-and-classification.md) | Pending |
| 4 | [Translation, editing and visual replacement](./phase-04-translation-editing-and-visual-replacement.md) | Pending |
| 5 | [Preview, export, verification and release](./phase-05-preview-export-verification-and-release.md) | Pending |

## Success Criteria

- [ ] A one-character subtitle lasting 250–600 ms is retained when a bounded burst confirms it, while an isolated noisy glyph is rejected.
- [ ] Two-character dialogue no longer requires two samples at the global 1.25 FPS interval.
- [ ] In the supplied fixture, `海王兽` becomes translated scene text and `bilibili 独播` remains excluded.
- [ ] Scene text preserves its panel, direction and position; Vietnamese auto-fits and calligraphic source uses the bundled display face.
- [ ] Scene text is excluded from D1/TTS and ordinary dialogue SRT export.
- [ ] Preview and FFmpeg export match; focused/full tests, Ruff, compileall, QML lint, visual fixtures and packaged smoke pass.

<!-- slug: adaptive-short-subtitles-and-translated-scene-text-a107 -->
