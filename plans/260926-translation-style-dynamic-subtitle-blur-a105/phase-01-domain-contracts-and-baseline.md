---
title: "Phase 1: Domain contracts and baseline"
status: completed
---

# Phase 1: Domain contracts and baseline

## Overview

Extend the domain model without breaking existing projects, jobs or exports.

## Requirements

- [x] Record the existing translation, OCR-region, effect-overlay and export contracts.
- [x] Extend project snapshots with optional, backward-compatible a105 fields.
- [x] Preserve legacy defaults and avoid a SQLite migration.

## Implementation Steps

1. Add optional fields and safe decoder defaults.
2. Add baseline and compatibility tests.

## Todo

- [x] Verify old project/job/export snapshots remain readable.
- [x] Verify no production project or cache is mutated during testing.

## Success Criteria

All new fields are optional and legacy snapshots retain their prior behavior.
