# Gemini reliability, project safety and preview/timeline polish

## Status

Hardening update in progress for `2.0.0a34`; `2.0.0a33` is the smoke-test
baseline and earlier builds are retired.
baseline running the preserved Gemini job.

Focused and full pytest, Ruff, QML lint, release packaging and an EXE
navigation smoke all passed. Opening the packaged app then resumed the existing
Gemini job from its durable cache; it advanced beyond the former `qa-36` false
1095 stop (currently observed at chunk 38) without creating a second job.

## Outcome

Resume the existing Gemini Guest translation without losing cached cues or
closing a page before its complete JSON response is harvested, while making
project deletion safe and making the Timeline preview larger, resizable and
smooth to operate. New translation jobs perform self-QA in the translation
request, use deterministic local gates and repair only failing cues. Logo and
blur content must track the pointer live without collapsing on press.

## Constraints

- Preserve the existing translation job, immutable ledger and 2,401 cached
  Vietnamese cues; retry only the unfinished request.
- Keep Gemini Guest sequential: at most 150 cues / 8,000 source characters per
  request, a fresh chat per request and a new browser after five real requests.
- Never treat a cue ID such as `1095` or `1905` as a Gemini server error.
- Never delete source media or imported SRT files when deleting a project.
- Refuse deletion while that project owns an active job.
- Do not test deletion against a real user project.
- Preview keyboard shortcuts must not steal arrows from text inputs.
- Timeline zoom must remain playhead-centred and retain “Vừa khung”.
- Selecting displayed source or Vietnamese cue text in Timeline or Translation
  seeks preview to that cue without breaking intentional text editing.
- Logo and blur geometry is stored in normalized video coordinates so direct
  manipulation remains correct across window and export resolutions.

## Acceptance criteria

- Gemini harvests a complete new JSON response before handling uncertain page
  warnings; verified error surfaces still recover, raw browser failures recreate
  the session, transport failures retry the exact chunk, and only complete valid
  ordered JSON subsets can be salvaged.
- Translation progress presents translated cues and QA cues separately.
- Projects screen has a danger-confirmed delete action; X, Escape and Cancel are
  no-ops, active jobs are rejected, internal jobs/cache are cleaned, and source
  files remain untouched.
- Opening a project navigates to Timeline only after the load succeeds.
- Preview has persisted master-volume −/+ controls and Left/Right seek by five
  seconds with duration clamping and input-focus protection.
- Timeline has a persisted draggable vertical split, a taller video preview by
  default, and a smooth slider-based playhead-centred zoom control.
- Clicking a cue's displayed source/Vietnamese text in Timeline or Translation
  selects it and seeks the preview to its start timestamp.
- Effects preview supports mouse drag/resize for logo and blur rectangles, plus
  adding and selecting multiple independent blur regions.
- Pressing an overlay captures its bound geometry before enabling local pointer
  state; logo/blur content follows the frame live and the resize hit area is at
  least 44 px.
- Integrated QA sends no second Gemini QA request per chunk. Final local checks
  still block CJK, unsafe Latin, overlong output and Film Bible name mismatches;
  repair requests contain only failing cue IDs.
- Focused tests, full pytest, Ruff, QML validation, Windows packaging and live
  smoke pass before `2.0.0a34` replaces the smoke-test `2.0.0a33`.

## Implementation

1. Correct Gemini error classification, response identity/harvesting, recovery,
   strict salvage and job-wide retry/circuit-breaker behavior.
2. Add safe project deletion through repository, Job Manager IPC, view model and
   confirmation UI; route successful project opening to Timeline.
3. Add preview volume/seek controls and persistent UI settings.
4. Replace Timeline's fixed preview height and zoom buttons with a persisted
   resizable split and debounced zoom slider; wire cue-text selection to preview
   seeking on both Timeline and Translation screens.
5. Add direct-manipulation overlays for logo and multiple blur regions, backed
   by real persisted effect commands.
6. Replace per-chunk remote QA with integrated self-QA, deterministic gates and
   targeted repair; raise API/CLI batch defaults to 80 cues / 6,000 characters.
7. Add regression tests, run quality gates, review, package and smoke `a34`.

## Risks and rollback

- Gemini Guest remains subject to Google rate limits and challenges; diagnostics
  must distinguish those from DOM/browser failures without logging prompt text.
- Internal artifact cleanup is restricted to verified job-owned directories
  below the DUCAI artifact root. Rollback is to keep using `2.0.0a24`; user data
  and the existing translation checkpoint remain compatible.
