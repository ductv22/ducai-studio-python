---
title: Complete a106 subtitle concealment and OCR watermark filtering
date: 2026-09-28
summary: "Built and verified a106 with stronger hard-sub cover, conservative Bilibili watermark masks, and a clean packaged smoke test."
---

# Complete a106 subtitle concealment and OCR watermark filtering

## What happened

The a106 release strengthened automatic hard-sub concealment and added conservative OCR-driven Bilibili watermark filtering. The final packaged smoke test opened the real executable, passed runtime diagnostics, imported a one-second video into an isolated project and exited without leaving a DucaiStudio2 process.

## Decisions

- Keep legacy project appearance unchanged; only new automatic overlays default to cover mode.
- Require watermark evidence in at least two frames before creating an automatic mask.
- Keep watermark boxes separate from subtitle evidence so channel text never becomes an SRT cue.
- Preserve normal dialogue by combining fuzzy Bilibili anchoring, spatial proximity and repeated-overlay evidence instead of broad keyword deletion.
- Treat hard-sub removal honestly as concealment, not background reconstruction.

## Verification

Focused and broad pytest suites, Ruff, compileall, QML lint, diff checks, independent review, manifest verification and the packaged executable smoke all passed. Release hashes and detailed counts are recorded in plans/reports/pm-260928-2133-a106-completion.md.

## Next steps

No required a106 follow-up. Test cover appearance on representative long-form source footage before choosing whether to tune default veil opacity in a later release.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
