## Plan Complete: a47 translation history and cache safety

### Summary

| Metric | Result |
|---|---|
| Release | `2.0.0a47` |
| Automated tests | 410 collected; 409 passed; 1 skipped; 0 failed |
| Static gates | Ruff, compileall, QML lint passed |
| Package integrity | 2,839/2,839 manifest hashes match |
| DB schema | Unchanged |
| Desktop control | Not used; EXE not opened |

### Achievements

- Active history deletion: cancel worker, preserve checkpoint while stopping,
  then delete the exact job automatically.
- Per-project clear history handles terminal and active OCR/translation jobs.
- Cache selector accepts only exact source fingerprints and prioritizes cue
  completion, then blocker count and recency.
- Selected complete cache cannot be hidden by a newer partial job or a late
  translation delta.
- Single active translation enforced on create, start, retry and resume.
- Resume reopens only `QA_REVIEW` chunks; verified `QA_PASS` chunks remain cached.
- Targeted repair containing CJK retries before checkpoint/live delta persistence.
- Corrupt historical ledger is skipped without hiding other valid caches.
- Explicit preview reads only the selected ledger instead of rescanning all
  histories a second time.

### Preserved production evidence

- Preferred job `5f1f18bb-60fb-4d28-b355-17055f7e4fe1`: failed with
  `translation_review_required`, checkpoint `13,385` cue, immutable artifact hash
  present.
- Accidental jobs `b2b88a75-ee8a-40fc-a0c2-138e78e0e06c` and
  `de2eddb7-3669-4b71-bb98-ca166f583c67`: interrupted, `434` cue each.
- Read-only verification only; no job, checkpoint or project row changed.

### Known limitation

- Interactive EXE smoke is intentionally deferred until the user permits desktop
  control. Rollback remains a46; no schema or ledger migration is required.

### Documentation updates

- `docs/user-guide.md`: active deletion, destructive clear-all warning, compatible
  cache selection and one-active-job rule.
- `plans/ui-feedback-logo-preview-live-translation-a45.md`: a47 release evidence.
