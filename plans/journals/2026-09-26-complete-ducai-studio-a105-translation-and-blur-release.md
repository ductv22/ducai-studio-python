---
title: Complete DUCAI STUDIO a105 translation and blur release
date: 2026-09-26
summary: "Completed and verified a105 translation style, dynamic subtitle blur and smooth effect interaction."
---

# Complete DUCAI STUDIO a105 translation and blur release

## What happened

Release a105 added project-scoped translation style, one-pass targeted style repair, OCR-region subtitle blur, and optimistic effect patches. The final review found two UI-state edge cases: rapid translation-preference saves could race, and custom rules were still rendered as a single-line field. Both were corrected before the final test run.

## Decisions

- Keep CJK as the existing blocker; unresolved style findings remain warnings.
- Keep SRT timestamps authoritative and use OCR boxes only for blur geometry.
- Preserve legacy project behavior: old projects retain fixed blur until explicitly switched.
- Avoid SQLite migration; all a105 snapshot fields remain optional.
- Do not alter user production projects, caches, or the user's running a104 processes during verification.

## Evidence

- Full pytest: 779 passed, 1 skipped.
- Focused UI tests: 192 passed.
- Ruff, compileall, QML lint and final diff check passed.
- Windows release 2.0.0a105 built and manifest hashes verified.
- Packaged UI and media-import smoke tests completed successfully with isolated data roots.

## Next steps

No required a105 work remains. Commit or push only when explicitly requested.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
