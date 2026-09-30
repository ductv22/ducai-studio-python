---
title: "Phase 6: Quality gates release and smoke test"
status: completed
---

# Phase 6: Quality gates release and smoke test

## Overview

Run the complete quality, review, build and packaged-runtime verification gates.

## Requirements

- [x] Pass focused and full pytest suites, Ruff, compileall and QML lint.
- [x] Complete code and UI review without unresolved blockers.
- [x] Build and verify the 2.0.0a105 Windows manifest.

## Implementation Steps

1. Run repository quality gates and review the final diff.
2. Smoke-test the packaged UI and packaged media import with isolated data roots.

## Todo

- [x] Confirm full pytest reports 779 passed and 1 skipped.
- [x] Confirm packaged smoke tests exit successfully without mutating user projects or stopping user processes.

## Success Criteria

The verified Windows release is present and all requested acceptance checks pass.
