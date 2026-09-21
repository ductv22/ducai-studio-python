---
title: Fix DUCAI export audio distortion
date: 2026-09-18
summary: "Made FFmpeg cue alignment sample-accurate, disabled limiter auto-level, repaired the video, and published portable a97."
---

# Fix DUCAI export audio distortion

## What happened
A completed 27-minute export peaked at effectively 0 dBFS and contained 13,648 samples in the 0 dB histogram bucket. The export graph rounded cue delays to milliseconds and FFmpeg's `alimiter` used its default auto-level behavior, which raised the mix back toward full scale.

## Decision
Resample every export track to 48 kHz, express cue delay in exact samples, mix with normalization disabled, and apply a -1.5 dBFS limiter with `level=0` and latency compensation. Preserve A1=18%, D1=100%, the H.264 video bitstream, subtitles, duration, and project schema.

## Results
The repaired MP4 peaks at -3.5 dBFS after AAC encoding and its video stream hash matches the original. Full tests passed (690 passed, 1 environment skip); focused export and portable-runtime tests passed. DUCAI STUDIO 2.0.0a97 normal and portable packages were built. The portable archive passed 7-Zip integrity verification and has SHA-256 `33be15da0c204adb7128cda41429923fcc17c2d738eea6c9aedf5982fa3b2bae`.

## Packaging note
Windows bsdtar 3.8.8 crashed on bundled VieNeu filenames containing Vietnamese characters. The release script now prefers 7-Zip on Windows and retains tar.xz as the fallback.

## Next steps
Smoke-launch the portable package on the receiving Windows machine. If two languages remain audible, lower or mute A1; that is the selected 18% original-language mix, not clipping.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
