---
title: "Phase 4: Translation editing and visual replacement"
phase: 4
status: pending
priority: P1
effort: "10h"
dependencies: [1, 3]
---

# Phase 4: Translation editing and visual replacement

## Overview

Translate accepted scene text, expose it as an editable S2 lane and render Vietnamese inside the retained source panel without creating voice cues.

## Requirements

- [x] Translate scene text with the same Film Bible/style preset and cue-ID integrity checks as dialogue.
- [x] Exclude scene text from TTS, D1 timing QA and ordinary dialogue SRT export.
- [x] Show scene cues in subtitle editing and Timeline as `S2 · Chữ trong cảnh`, with role/orientation/style badges and manual text correction.
- [x] Conceal only the Chinese glyph box using surrounding panel color plus bounded blur/cover; preserve decorative borders/backgrounds.
- [x] Auto-fit Vietnamese to the detected panel; horizontal text stays horizontal, vertical text stacks Vietnamese words top-to-bottom.
- [x] Use a bundled OFL Vietnamese handwriting face for calligraphy and the existing Be Vietnam Pro family for standard/display text.

## Architecture

Split translated cues at render time. Dialogue continues through the global ASS style. Scene cues produce per-cue ASS events with `\\pos`, `\\an5`, fitted `\\fs`, font override and line breaks. Glyph concealment runs before scene-text rendering and before ordinary Vietnamese subtitles. Bundle and license the selected calligraphy font; pass an explicit `fontsdir` to libass so portable export matches preview.

## Related Code Files

- Modify: translation job/worker and prompt code under `src/ducai_studio/`
- Modify: `src/ducai_studio/application/export_pipeline.py`
- Modify: `src/ducai_studio/providers/export/ffmpeg.py`
- Modify: `src/ducai_studio/ui/viewmodels/project_job_models.py`
- Modify: `src/ducai_studio/ui/qml/controls/TimelinePreview.qml`
- Modify: `src/ducai_studio/ui/qml/screens/SubtitleEditorScreen.qml`
- Add: OFL Vietnamese handwriting font and license under `src/ducai_studio/ui/assets/fonts/`
- Modify: `THIRD_PARTY_NOTICES.md`, build scripts and relevant tests

## Implementation Steps

1. Include scene-role context in translation prompts while preserving the response schema.
2. Filter scene roles at every TTS/D1/SRT entry boundary and add regression tests.
3. Extend view models and editor filters for S1/S2 without creating a second project snapshot.
4. Implement panel-aware mask geometry, fit calculation and vertical word stacking.
5. Bundle/register the selected OFL font and make FFmpeg libass consume the same file.
6. Render preview and export from the same scene-text descriptor.

## Todo

- [x] `海王兽` translates to a corrected Vietnamese value such as `Hải Vương Thú` and remains editable.
- [x] The black brush panel remains while the source white glyphs are unreadable.
- [x] Vietnamese text fits without clipping or crossing the panel boundary.
- [x] No scene cue appears in a VieNeu/Piper synthesis request.

## Success Criteria

- [x] Preview and export agree on box, orientation, font family, line breaks and timing.
- [x] Project reload preserves manual scene-text corrections.
- [x] Calligraphy uses a redistributable, bundled Vietnamese-capable font with license notice.

## Risk Assessment

Background reconstruction is impossible for burned-in glyphs. If the sampled panel ring is not sufficiently uniform, use strong blur/cover and flag the cue for review rather than filling a visibly wrong solid color. Style classification remains heuristic and must be manually overridable.
