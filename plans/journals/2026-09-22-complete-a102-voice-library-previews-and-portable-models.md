---
title: Complete a102 voice library previews and portable models
date: 2026-09-22
summary: "Shipped immediate voice auditions, verified installs and a privacy-clean portable runtime."
---

# Complete a102 voice library previews and portable models

## What happened
Implemented a102 voice-library support: 25 VieNeu v3 Turbo presets, two verified Piper voices, 27 truthful 5-8 second previews, background Piper installation and portable packaging. The first portable smoke exposed that the UI treated a full CUDA health probe as the installation flag, so freshly relocated runtimes appeared as "Chưa cài" and the Qt thread could stall.

## Decision
Installation state now comes from immutable runtime assets and pinned metadata; synthesis still performs the full provider/CUDA health check. Personal clone audio is never represented by another speaker's preview. The portable package includes VieNeu, Piper, PaddleOCR and Whisper but excludes user projects, caches, credentials and clone profiles.

## Verification
Focused tests, full pytest, Ruff, compileall, QML lint and both release builds passed. Qt Multimedia loaded installed and uninstalled voice previews. Portable first-launch smoke displayed VieNeu cards as installed, and catalog construction measured 23.1 ms. Archive SHA-256: 742e4c15f8881bf08a2cb3b44785fe37b76c820baf516214adfb830c373a27b9.

## Next steps
No code work remains for a102. The user can distribute the portable archive; recipients must supply their own credentials and personal clone sample.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
