---
title: Translation style, dynamic subtitle blur and smooth effects
status: completed
created: 2026-09-26
---

# DUCAI STUDIO 2.0.0a105

## Outcome contract

- Persist a per-project translation style (`auto`, `xianxia`, `wuxia`,
  `palace`, `modern`, `neutral`) and up to 4,000 characters of custom rules;
  apply the same contract to every translation transport and freeze it per
  project when a Multi batch starts.
- Detect and repair only style-violating cues once. Remaining style issues are
  warnings and never block TTS or Multi; the existing CJK blocker remains.
- Track source OCR regions in full-frame normalized coordinates and use source
  SRT timing plus those regions for automatic hard-sub blur, with a labelled
  estimated fallback and independent manual blur.
- Make logo/manual-blur manipulation local and immediate while dragging, then
  persist one compact patch per settled operation without resetting the project
  or accepting stale acknowledgements.

## Constraints and non-goals

- No SQLite migration, no loss of old project/job/export compatibility and no
  mutation of user production projects during verification.
- SRT timing remains authoritative; OCR boxes only define image geometry.
- Do not mechanically replace animal words or insults with Sino-Vietnamese
  forms. Do not claim perfect speaker identity without diarization.
- Preserve all uncommitted a100-a104 work in the shared worktree.

## Phases

1. [x] Record baseline tests and extend backward-compatible domain contracts.
2. [x] Implement translation style prompts, targeted QA/repair, fingerprints,
       project UI and Multi snapshot freezing.
3. [x] Preserve/map OCR boxes and implement automatic subtitle blur in preview
       and export with estimated fallback.
4. [x] Replace effect snapshot refresh with compact patches and stable optimistic
       overlay interaction.
5. [x] Update user/architecture docs and version to `2.0.0a105`.
6. [x] Run focused/full tests, Ruff, compileall, QML lint, fixture export,
       release build, manifest verification and isolated executable smoke test.

## Acceptance

- Every translation provider and Multi receives the selected style and custom
  rules; style repair runs at most once and never weakens CJK blocking.
- `tỷ tỷ Hồng Ly`, inappropriate modern pronouns and contextual
  `A Cẩu/A Miêu/Lão Cẩu` are found and repaired without rewriting ordinary
  animal/insult text.
- OCR crop boxes round-trip through project storage and drive timestamp-bound
  blur; missing boxes produce a safe `estimated` region.
- Preview/export agree and a 100-cue fixture shares blur sources by radius rather
  than building one full-frame blur branch per cue.
- Drag/resize performs no backend call until release, one settled command updates
  one row, and stale responses cannot move the overlay backwards.
- Fresh a105 release passes the repository quality gates and packaged smoke test.

## Rollback

Use a104. All a105 fields are optional and default to legacy-safe behavior, so
no data migration or cache deletion is required.
