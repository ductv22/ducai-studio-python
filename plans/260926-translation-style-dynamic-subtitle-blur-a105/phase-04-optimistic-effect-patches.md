---
title: "Phase 4: Optimistic effect patches"
status: completed
---

# Phase 4: Optimistic effect patches

## Overview

Make logo and manual-blur manipulation immediate without full project refreshes.

## Requirements

- [x] Update overlay geometry locally while dragging or resizing.
- [x] Send one compact patch after release without refreshing the full project model.
- [x] Ignore stale acknowledgements and rollback to the latest confirmed geometry on failure.

## Implementation Steps

1. Add patch responses and stable overlay models.
2. Add optimistic QML interaction and response generation guards.

## Todo

- [x] Verify no IPC call occurs during pointer movement.
- [x] Verify rapid operations do not snap backwards or stall the UI.

## Success Criteria

Each settled interaction updates one row through one compact command.
