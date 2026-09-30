---
title: Complete a107 adaptive short subtitles and scene text
date: 2026-09-28
summary: Delivered bounded short-cue recovery and translated in-scene text replacement in the verified a107 Windows release.
---

# Complete a107 adaptive short subtitles and scene text

## What happened

Implemented release a107 to recover one- and two-character Chinese dialogue and detect translated scene labels outside the subtitle band. Added role-aware S1/S2 handling, bounded visual probes, sparse full-frame OCR, conservative watermark recurrence, editable review cues, panel-aware concealment, vertical/horizontal layout and the bundled Playwrite VN font.

## Decision

High-confidence scene text renders automatically; uncertain candidates stay reviewable. Scene text is translated but excluded from TTS, D1 and user-facing dialogue SRT. Burned-in glyphs are concealed without claiming to reconstruct hidden source pixels.

## Verification

Full pytest, Ruff, compileall, QML lint and git diff checks passed. Independent re-review returned no findings. The portable release passed 2,871 manifest hashes, eight focused release regressions and packaged Job Manager smoke. A no-op OCR control-plane benchmark on a 30-second motion fixture measured 15 bounded batch calls versus four in a106; this is scheduling evidence, not Paddle latency.

## Next steps

User testing should focus on real character/title cards with varied brush panels and on long high-motion videos. Tune thresholds only from representative false-positive/false-negative evidence; do not weaken watermark or one-glyph consensus globally.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
