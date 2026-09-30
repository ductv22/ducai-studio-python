# a114 subtitle typography, Preview and Timeline completion

Status: completed

## Delivered

- S1 and scene/manual cues share per-cue size, font, colour, orientation,
  background and frame-position editing.
- Scene cues may use display lane S1 without entering dialogue SRT/TTS; overlap
  with S1 dialogue is rejected and legacy decode repairs invalid collisions.
- Font size 1–100 is monotonic and independent from placement-box resizing.
- Scene background hugs painted text with independent X/Y padding.
- Bundled Playwrite VN, Dancing Script and Lobster fonts include OFL notices and
  verified Vietnamese glyph coverage.
- Preview transport is hover-only; plain Timeline clicks seek at the pointer.
- Vietnamese automatic wrapping uses 68 characters; source-subtitle cover is 90%.
- Timeline cue role lookup is indexed by cue ID; subtitle interaction is disabled
  during effect editing so it cannot steal pointer input from logo/blur handles.

## Verification

- Focused domain, repository, service, ViewModel, QML and export tests: passed.
- Full `uv run pytest -q`: passed; one environment-gated skip.
- Ruff, compileall, QML lint, lock check and diff check: passed.
- Clean build and 2,876 manifest hashes: verified.
- Packaged EXE smoke with isolated data root: exit code 0.

## Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a114-win64`

## Open findings

None for the accepted a114 scope. Hard-sub concealment remains cover-and-replace;
it cannot reconstruct source pixels hidden by burned-in text.
