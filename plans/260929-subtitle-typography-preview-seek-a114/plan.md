---
title: "Unify subtitle typography, lane placement and Timeline navigation a114"
description: "Make S1…Sn text formatting reliable, tighten scene-label backgrounds, add precise Timeline seek and remove intrusive Preview controls."
status: completed
priority: P1
effort: "large"
branch: "ducai-studio-python-a98"
tags: [subtitles, timeline, preview, typography, export, release]
created: 2026-09-29
---

# Unify subtitle typography, lane placement and Timeline navigation a114

## Outcome Contract

- Dialogue cues on S1 and scene/manual cues on S1…Sn expose the same per-cue
  typography controls: size, orientation, text colour, background colour and
  position. Font size 1–100 produces a visible monotonic change in Preview and
  Export rather than being silently neutralized by automatic fitting.
- A scene/manual cue may move to S1 while retaining its scene role. It never
  enters dialogue SRT/TTS, and a time overlap with an S1 dialogue cue is rejected
  with a clear Vietnamese error.
- Scene-label backgrounds wrap the rendered glyphs with adjustable horizontal
  and vertical padding instead of filling the whole geometry box. Geometry and
  typography remain independently editable so text can move higher without
  shrinking the font.
- Preview transport controls appear only while the pointer is over Preview and
  hide immediately when it leaves, including while paused.
- Clicking any non-interactive time position in Timeline seeks the video to that
  exact time. Drag, resize, marquee and track-header operations keep their current
  meaning and must not trigger accidental seeks.
- Automatic Vietnamese line wrapping starts only above 68 characters. Existing
  explicit line breaks and manual edits remain under user control.
- Automatic subtitle concealment is 90% opaque in Preview and Export.
- Additional bundled calligraphy fonts with redistribution licenses are
  selectable per cue and render consistently in Preview and Export.

## Constraints and Non-goals

- No SQLite migration. New cue fields are optional and decode to safe defaults.
- Do not change scene cues into dialogue cues when they are displayed on S1.
- Automatic scene-lane allocation still starts at S2; S1 placement is an
  explicit manual action only.
- Do not write to the user's real project database during tests or smoke checks.
- Do not issue IPC, save revisions or refresh the project model while a pointer
  is moving; persist one atomic command at gesture end.
- Do not claim that a 90% cover restores pixels hidden by burned-in subtitles;
  it conceals them before Vietnamese text is drawn.

## Root-cause evidence

- `TimelinePreview.sceneFontPixels()` caps the requested cue size by a
  geometry-derived fit and scene labels also use `Text.Fit`, so larger values can
  render identically.
- Scene background rectangles fill the entire draggable geometry instead of
  using the text's painted bounds.
- `maybeHideControls()` forces controls visible whenever playback is not in the
  Playing state, making pause permanently obscure the video.
- Scene timing commands and Timeline QML clamp lanes to S2 or later and identify
  scene clips from track ID instead of cue role.
- Translation wrapping defaults to 46 characters and editor auto-break defaults
  to 42, below the requested reading threshold.
- Automatic subtitle concealment is hard-coded to 42% in both Preview and the
  FFmpeg graph.

## Implementation

- Extend optional cue typography with background padding and a licensed font
  family/style identifier; preserve the fields through OCR/translation,
  repository serialization, patches, ViewModels, Preview and Export.
- Remove the legacy font-size cap as an authoritative output. Use geometry to
  choose an automatic base only; apply the explicit 1–100 scale monotonically
  and bound only to a safe render maximum. Let clipping/geometry remain visible
  feedback instead of silently shrinking the requested font.
- Render scene backgrounds from painted glyph bounds plus cue padding. Keep the
  transparent geometry handle as the independent move/resize hit target.
- Generalize formatting to S1 dialogue cues and identify scene behavior from the
  cue role. Permit explicit scene moves to lane 1 after collision validation.
- Seek from Timeline pointer coordinates only for completed plain clicks outside
  interactive drag/resize/marquee controls.
- Make transport visibility hover-owned, unify the 68-character wrap constant,
  and raise automatic cover alpha to 0.90 in Preview and FFmpeg.
- Bundle a small set of Vietnamese-capable OFL calligraphy fonts and document
  their licenses.
- Bump release metadata and user documentation to `2.0.0a114`.

## Acceptance Criteria

- [x] Selecting an S1 dialogue cue and changing size/colour/background/orientation
  updates Preview immediately and persists into Export.
- [x] Font sizes 12, 36 and 72 produce strictly increasing rendered size for the
  same scene cue, including a manually-created cue.
- [x] Text can move to the frame edge without the font being reduced by the move;
  invalid geometry is clamped, not resized.
- [x] Scene background wraps text with adjustable X/Y padding and no longer fills
  the entire editable geometry by default.
- [x] Scene cues can move to S1 without changing role; S1 dialogue overlap fails
  clearly and encode/decode preserves a valid explicit S1 scene placement.
- [x] Two or more selectable Vietnamese-capable calligraphy fonts render in both
  Preview and Export and include license notices.
- [x] Preview transport hides immediately on pointer exit while playing or paused.
- [x] Plain Timeline clicks seek accurately; clip drag, edge resize and marquee
  selection do not seek accidentally.
- [x] Automatic Vietnamese text at 68 characters remains one line; longer text
  wraps without dropping or concatenating whitespace.
- [x] Subtitle concealment uses 90% opacity consistently in Preview and Export.
- [x] Focused tests, full pytest, Ruff, compileall, QML lint, clean build,
  manifest verification and packaged smoke pass.

## Risks and Rollback

- Font metrics differ by platform. Bundle exact font files and make Preview and
  FFmpeg resolve the same packaged path/family.
- S1 contains mixed semantic roles after explicit scene placement. Every caller
  must use cue role for OCR/translation/TTS semantics and track ID only for draw
  order.
- Timeline click handlers overlap with clip gestures. Accept seek only after a
  non-dragging click and keep child handlers authoritative.

<!-- slug: subtitle-typography-preview-seek-a114 -->

## Completion evidence

- Full `pytest -q`: passed with one environment-gated skip.
- Ruff, compileall, `uv lock --check`, `git diff --check` and full QML lint: passed.
- Review fixed Preview pointer contention in Effects and indexed cue-role lookup
  to avoid linear scans per Timeline clip.
- Clean Windows build: `DUCAI-STUDIO-2.0.0a114-win64`.
- Manifest: version `2.0.0a114`, 2,876 file hashes verified.
- Packaged offscreen smoke: exit code 0; isolated data root, no real project DB.
