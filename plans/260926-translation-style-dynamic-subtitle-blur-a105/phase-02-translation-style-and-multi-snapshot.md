---
title: "Phase 2: Translation style and Multi snapshot"
status: completed
---

# Phase 2: Translation style and Multi snapshot

## Overview

Add per-project translation style and use it consistently across translation workflows.

## Requirements

- [x] Persist presets and custom rules capped at 4,000 characters.
- [x] Apply the same style contract to prompts, Film Bible, QA, targeted repair and all providers.
- [x] Freeze per-project style settings in Multi batches and include them in translation fingerprints.

## Implementation Steps

1. Add project contracts, prompt builders and UI controls.
2. Detect style violations and repair only affected cues once.

## Todo

- [x] Cover ancient-setting pronouns, title order and contextual A Cẩu/A Miêu/Lão Cẩu rules.
- [x] Keep CJK as the existing blocker while downgrading remaining style issues to warnings.

## Success Criteria

All translation transports and Multi use the same frozen style contract without blocking TTS on style warnings.
