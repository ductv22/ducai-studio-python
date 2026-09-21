# DUCAI STUDIO 2 development context

## Product boundary

DUCAI STUDIO 2 is a Windows desktop workflow for hard-sub extraction,
Chinese-to-Vietnamese translation, voice generation, timeline editing, preview
and FFmpeg export.

## Architecture rules

- Domain and application packages never import PySide6, OCR/TTS frameworks,
  Playwright, FFmpeg wrappers or SQLite.
- Store timeline values as integer microseconds. Convert seconds only at media
  provider boundaries.
- The Job Manager is the only SQLite writer. UI and workers communicate through
  versioned messages.
- OCR, translation, TTS and export run outside the GUI process.
- QML renders model state and dispatches named actions. It never owns long jobs.
- Preserve `0.1.99` as a read-only behavior oracle until end-to-end parity passes.

## Quality gates

- Run focused pytest tests before the full suite.
- Do not display fake telemetry, progress or ETA.
- Do not store API keys in project files, SQLite payloads or logs.
- Do not silently skip cues, effects or export stages.

