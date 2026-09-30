---
phase: 1
title: "Phase 1: Backward-compatible contracts"
status: completed
priority: P1
effort: "2h"
dependencies: []
---

# Phase 1: Backward-compatible contracts

## Overview

Add an optional concealment mode to project and render contracts while preserving old project appearance.

## Requirements

- [x] Add `concealment_mode` with `blur` and `cover` values to source-subtitle blur overlays.
- [x] Default missing values to `blur`; seed new automatic overlays as `cover`.
- [x] Carry the field through repository decode, effect patches and export snapshots.

## Implementation Steps

1. Extend domain validation and serialization boundaries.
2. Add decoder and effect-command compatibility tests.

## Todo

- [x] Confirm legacy snapshots round-trip without visual behavior changes.
- [x] Confirm unsupported modes are rejected with a clear error.

## Success Criteria

All contracts support Che kín without migration or implicit changes to saved projects.
