---
title: Complete a112 multi-lane subtitle Timeline
date: 2026-09-29
summary: "Added deterministic S2…Sn scene-text lanes, stable multi-cue editing and a direct Timeline text tool."
---

# Complete a112 multi-lane subtitle Timeline

## What happened

Scene labels that appeared at the same time competed for one S2 lane, cue
formatting was not fully independent, and editor delegate refreshes could move
focus while a text edit was being committed. Manual labels also needed a direct
Timeline workflow tied to the current playhead.

## Decision

Reserve S1 for dialogue and allocate overlapping scene labels with deterministic
interval partitioning across S2…Sn. Keep track identity separate from frame
geometry. Address all selection and edit ownership by stable cue ID, and make
pointer gestures local previews that produce one atomic backend command only on
release. Manual `T` cues use the exact playhead and requested duration, failing
clearly rather than being truncated at the media boundary.

## Verification

Focused regression suites and full pytest passed with one expected skip, along
with Ruff, compileall, QML lint and diff checks. Independent review confirmed
the final vertical lane, group collision and end-of-media fixes. A clean a112
package passed production EXE import. Its manifest has 2,872 entries with no
missing, mismatched or extra files. Manifest SHA-256:
`C09C53AF3895547065D8E298227FDAAC2DA512FDDD8C9FF954190966E23EE036`.

## Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a112-win64`

## Boundary

The Windows computer-use channel exposed no controllable apps during final
verification, so no native click-through is claimed. Packaged E2E and offscreen
GUI smoke were used instead.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
