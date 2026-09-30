---
phase: 4
title: "Release and old runtime cleanup"
status: completed
priority: P1
effort: "2-3h"
dependencies: [3]
---

# Phase 4: Release and old runtime cleanup

## Overview

Build and smoke-test the next Windows release, then remove the verified obsolete
3.3.0 runtime without touching profiles, model cache, cue cache or projects.

## Requirements

- Functional: the packaged application discovers and uses 3.8.1.
- Non-functional: deletion is limited to the resolved
  `DUCAI_STUDIO_DATA/vieneu_3_3_runtime` directory after all gates pass.

## Architecture

Build through the existing release script and manifest verification. Launch the
new executable for a short provider health/preview smoke test. Only then verify
the absolute legacy path and delete it with native PowerShell.

## Related Code Files

- Modify: version metadata, `README.md`, owning runtime/user guide docs
- Use: `scripts/build-release.ps1`
- Delete at runtime: `DUCAI_STUDIO_DATA/vieneu_3_3_runtime`

## Implementation Steps

1. Update user-visible documentation and release version.
2. Build and verify the Windows manifest.
3. Smoke-test the packaged engine host and application discovery.
4. Verify the resolved legacy path is inside `DUCAI_STUDIO_DATA`, then delete it.

## Success Criteria

- [x] New release manifest verifies.
- [x] Packaged health/clone smoke test passes with 3.8.1.
- [x] 3.3.0 runtime directory is absent; profiles/model/cache/project data remain.

## Risk Assessment

Deleting early removes rollback. Cleanup is last and is skipped automatically
if any correctness, performance or build gate fails.
