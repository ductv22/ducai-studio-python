# Export cue timeline fix report

Date: 2026-09-19

Status: complete

Version: 2.0.0a98

## Diagnosis

The a97 peak limiter worked, but the export graph padded every delayed cue before
mixing. FFmpeg consequently emitted each finite cue at the start of its chunk.
Hundreds of D1 cues overlapped during the first few seconds, while the rest of
the chunk contained only A1. The former QA check still passed because it checked
streams, playability, and duration rather than narration placement.

The failure was reproduced with a real FFmpeg render: a late cue after a gap
decoded as digital silence before the fix.

## Change

- Removed per-cue `apad` and full-chunk `atrim` filters.
- Kept each cue finite and delayed at its intended sample position.
- Mixed all finite branches first, then padded and trimmed the completed mix once.
- Kept the a97 limiter setting (`level=0`, latency compensation, -1.5 dB target).
- Added a real-media regression test that checks a fourth cue after a timeline gap.

## Repaired user video

- File: `Nên chọn loại nào_ iPhone 18 Pro&Duo FINAL_AUDIO_FIXED_a98.mp4`
- Size: 341,238,572 bytes
- SHA-256: `e5deb07dfa4db27683b6ea6fe3dc1350794ce77dddd5bf8497db781688c7a722`
- Duration: 1,631.296 seconds
- Audio: AAC stereo, 48 kHz, full-file mean -21.0 dBFS, peak -3.5 dBFS
- Full decode: passed with no FFmpeg errors
- Voice spot checks: passed at 100, 700, 1,201, 1,400, and 1,629.5 seconds
- Encoded video SHA-256: `e87116a5828075db2b395ba8d39a8d3fd05eff08c6a8c1e3bc3c54ef716110e3`,
  identical to the user's a97 export

## Verification

- Focused export tests: 21 passed
- Full suite: 691 passed, 1 environment skip
- Ruff: passed
- Compile and QML lint gates: passed through the release build
- Portable manifest: 59,374 files, 14,815,224,988 bytes, no clone profiles
- Portable archive integrity: `7z t` passed

## Deliverables

- Normal: `DUCAI-STUDIO-2.0.0a98-win64`
- Portable: `DUCAI-STUDIO-2.0.0a98-PORTABLE-win64`
- Archive: `DUCAI-STUDIO-2.0.0a98-PORTABLE-win64.7z`
- Archive SHA-256: `d387a50ab13cd65d26f288f2c89357f076c18f833db8d735d1379cce46d4b1ff`

The already-running desktop window remains a97 and was deliberately not stopped.
Future exports must be made after opening the a98 executable.
