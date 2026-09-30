---
title: Complete a114 subtitle typography and Timeline interaction
date: 2026-09-30
summary: "Shipped per-cue S1/Sn typography, tighter scene backgrounds, hover Preview controls, Timeline seek and reviewed performance fixes."
---

# Complete a114 subtitle typography and Timeline interaction

## What happened

The a114 work unified S1 and scene-text typography, fixed font-size controls that were neutralized by geometry fitting, tightened scene-label backgrounds, made Preview controls hover-only, added precise Timeline click seek, raised hard-sub concealment to 90%, and moved Vietnamese auto-wrap to 68 characters.

Review found two non-obvious regressions before release: subtitle hit areas could steal pointer input from logo/blur editing, and scene-role lookup scanned all cues for every Timeline clip. The first was gated off in Effects mode; the second now uses a cue-ID row index.

## Decisions

Display lane and semantic role remain separate. A scene cue may sit on S1 when dialogue is absent but never enters dialogue SRT/TTS/D1. The panel remains a placement handle rather than a hidden font-size clamp. Hard-sub cover conceals source text but cannot reconstruct burned-in pixels.

## Verification

Full pytest passed with one environment-gated skip. Ruff, compileall, QML lint, lock check and diff check passed. Clean a114 build completed; all 2,876 manifest hashes verified. The packaged EXE exited 0 in an isolated offscreen smoke and rendered version 2.0.0a114.

## Artifact

E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a114-win64

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
