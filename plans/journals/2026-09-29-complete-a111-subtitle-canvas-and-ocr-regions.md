---
title: Complete a111 subtitle canvas editing and OCR regions
date: 2026-09-29
summary: "Unified S1/S2/S3 editing, added manual scene text and separated dialogue OCR from scene-text OCR with user-defined regions."
---

# Complete a111 subtitle canvas editing and OCR regions

## What happened

Scene text could be translated and retimed, but deletion/editing was split
between surfaces, the S2 scale control was too narrow, and OCR still had to infer
dialogue versus scene labels from overlapping evidence. The requested workflow
also needed manual labels, editable backgrounds and protection against logos or
other unwanted OCR areas.

## Decision

Use the subtitle cue as the authoritative identity across Preview, Timeline and
the editor. Store typography and display-track metadata on optional cue fields,
and implement manual scene labels as real S2 cues instead of overlay-only state.
Store one normalized dialogue region plus multiple normalized exclusions on the
project. Mask exclusions at inference time and exclude the dialogue region from
the scene pass so S1/S2 evidence never competes in one consensus set.

Short single-glyph recovery remains bounded: sample six offsets around accepted
visual transitions, reduce the transition cap to keep the previous inference
budget, and require two strong aligned readings. This improves `爹`/`娘` without
globally increasing OCR FPS or accepting single-frame noise.

## Verification

Final source passed 866 tests with one optional skip, plus Ruff, compileall, QML
lint and diff checks. The clean a111 package passed Job Manager IPC smoke and an
offscreen 1180×720 GUI navigation smoke. Visual review caught and fixed a one-row
toolbar overflow before release; the final two-row toolbar exposes Add text,
Delete cue, size 1–100, direction and background controls simultaneously.

The release manifest has 2,872 entries with no missing, mismatched or extra
files. Manifest SHA-256:
`4E3310A5FE3D2E0C7CDA5ECE234C0249220D2F951FE93A45DE126437E3BD3605`.

## Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a111-win64`

## Boundary

Deleting a cue removes only generated text/background state; source hard-sub
pixels remain in the video. Outside-region text is not accepted blindly and must
still pass scene-text QA. Very brief glyph recovery still depends on two usable
OCR observations.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
