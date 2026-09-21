---
date: 2026-09-19
summary: "Fixed FFmpeg cue timing collapse, repaired the completed video, and published portable a98."
---

# Fix export cue timeline collapse

The follow-up investigation showed that a97 had solved peak auto-level behavior
but had not preserved delayed cue placement. Applying `apad` independently to
each delayed cue caused FFmpeg to collapse every cue toward the beginning of the
chunk. A real-media regression made the late-cue silence deterministic.

The exporter now mixes finite delayed branches and pads only the final mix. The
regression passes, as do all 691 project tests (with one expected environment
skip). The user's repaired MP4 keeps the exact encoded video stream, decodes
cleanly, peaks at -3.5 dBFS after AAC, and retains voice through the final cue.

DUCAI STUDIO 2.0.0a98 normal and portable packages were built. The 5,416,388,365
byte portable archive passed `7z t` and has SHA-256
`d387a50ab13cd65d26f288f2c89357f076c18f833db8d735d1379cce46d4b1ff`.
