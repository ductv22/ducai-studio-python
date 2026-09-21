# UI feedback, logo safety and faithful preview

## Status

`2.0.0a47` is packaged with safe active-history deletion, source-compatible
cache selection, single-active-translation enforcement and blocker-only resume.
The release manifest verifies all 2,839 packaged files. The preserved 13,385-cue
translation remains the preferred resume source and was not modified by build
or automated verification. Interactive smoke is intentionally deferred because
the user requested no desktop control during this delivery.

## Outcome

Every shared action button acknowledges a click immediately, long jobs display
their real lifecycle text, and pause/cancel actions use unambiguous warning
colors. A short top-centre toast confirms accepted actions. Adding or editing a
logo must never be dropped behind another effect save, project mutations must
not blank live checkpoint translations, and the Effects preview must use the
same output canvas, subtitle margins, opacity and overlay geometry as export.

## Constraints

- Do not delete, recreate or restart the preserved translation job while
  implementing and testing this release.
- Keep geometry saves coalesced for smooth pointer dragging, but retain strict
  ordering across semantic effect commands such as selecting a logo, enabling
  an overlay, adding blur or deleting blur.
- Toasts report that an action was accepted, not that a long job finished.
- Keep subtitles above logo/blur in preview and FFmpeg export.
- Build a new release directory instead of overwriting the running `a44` build.

## Acceptance criteria

- OCR, translation, voice and export buttons show queued/running/pausing/
  paused/resuming/cancelling/succeeded/failed text as applicable.
- Pause uses an amber warning treatment; cancel uses a dark red background and
  yellow text with accessible contrast.
- Every enabled `DsButton` emits one top-centre feedback toast for roughly four
  seconds, with a short slide and fade animation.
- `set_logo_asset` survives an in-flight geometry save and the logo becomes the
  selected editable overlay after choosing a file.
- Applying same-project effect/timeline/subtitle snapshots preserves live cache
  rows and requests a ledger preview rehydrate.
- Effects preview uses the selected export resolution/aspect ratio and renders
  subtitle fill opacity separately from its outline.
- A single-cue Gemini QA row with an omitted id is recovered to that only
  possible cue; an ambiguous multi-cue omission remains a format error.
- Elapsed time continues locally between backend polls, translation cards show
  the current batch, and resume ETA is based only on progress made in the new
  worker session.
- Focused pytest, QML lint, broader tests, packaging and live EXE smoke pass.
- Active OCR/translation history can be cancelled and automatically deleted
  after its worker exits; deleting one row does not touch another cache.
- The cache selector lists only exact source fingerprints, prioritizes the most
  complete ledger, and prevents a newer partial job from hiding the selected
  complete translation.
- A project cannot create a second active translation job, including during the
  UI request-to-first-poll race.
- Resume reopens only QA-review chunks, and a targeted repair containing CJK is
  retried before any checkpoint or live delta can persist it.

## Validation and rollback

Focused view-model/resource/translation tests, QML lint and Ruff pass; the full
suite collected 410 tests with 409 passed and one environment-dependent skip.
The a47 package was built without opening it and its 2,839 manifest hashes
match. Interactive smoke remains a later explicit step. Rollback is continued
use of a46; the database schema and durable job ledger are unchanged.
