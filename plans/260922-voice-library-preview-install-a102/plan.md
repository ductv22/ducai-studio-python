---
title: "Voice library preview and installation"
description: "Ship truthful per-voice previews, expose VieNeu v3 Turbo presets and install verified Piper voices without blocking the UI."
status: completed
priority: P1
effort: "1d"
tags: [tts, voice-library, qml, release]
created: 2026-09-22
---

# Voice library preview and installation

## Outcome contract

- Outcome: every usable voice displayed in Voice Studio can be auditioned immediately;
  installed voices can be selected and verified Piper voices can be downloaded from
  the card without leaving the app.
- Constraints: keep the current dark editor layout, never block the Qt main thread,
  preserve project/cache contracts, verify downloads before publish, and preserve the
  license/source of every bundled preview.
- Non-goals: downloading unverified NGHI-TTS/personality voices, synthesizing a fake
  preview with another speaker, changing voice speed/timing, or migrating SQLite.
- Acceptance: VieNeu v3 Turbo presets synthesize through the existing provider, all
  visible catalog entries have 5-8 second preview audio, preview works without a
  project, install progress is immediate and atomic, and release a102 passes test,
  lint, QML, build and packaged smoke gates.

## Implementation

- [x] Extend the catalog contract with preset, preview and installation metadata.
- [x] Add official VieNeu v3 Turbo preset binding to the engine host/provider.
- [x] Add a background Piper installer with byte/digest/config validation and atomic
      publication.
- [x] Bundle truthful per-voice WAV previews and record their source/license.
- [x] Add independent Play/Stop and Install actions to each compact voice card.
- [x] Update user/release documentation and version to `2.0.0a102`.
- [x] Run focused tests, full pytest, Ruff, compileall, QML lint, release build and
      packaged smoke test.

## Verification

- Full pytest, Ruff, compileall and QML lint completed with exit code 0.
- Standard and portable manifests report `2.0.0a102`; both contain all 27 preview
  WAV files and the standard manifest hashes every preview.
- Qt Multimedia loaded an installed VieNeu preview and the not-yet-installed VIVOS
  preview at 5.120 s and 5.008 s without decoder errors.
- Portable first-launch smoke shows VieNeu presets as installed immediately; catalog
  construction measured 23.1 ms and no longer runs a CUDA health subprocess on the
  Qt thread.
- The portable package contains VieNeu 3.8.1, Piper, PaddleOCR, Whisper large-v3 and
  no project, media, cache, credential, browser profile or clone profile.

## Safety and rollback

- A failed or interrupted download leaves only `.part` files and never marks a model
  installed.
- Voices with unclear redistribution or voice-personality rights are not installable
  and are not represented by misleading audio.
- Rollback removes the new catalog/install/UI code and preview assets; existing Piper
  models, VieNeu runtime, projects and caches remain untouched.

<!-- slug: voice-library-preview-install-a102 -->
