# Plan Complete: Export Audio Limiter Fix

## Summary

| Metric | Result |
|---|---|
| Status | completed |
| Version | 2.0.0a97 |
| Full tests | 690 passed, 1 environment skip |
| Focused tests | export 10/10; portable runtime 4/4 |
| Repaired peak | -3.5 dBFS |
| Portable package | 59,374 files; 14,815,225,383 bytes |
| Archive | 5,416,389,215 bytes; integrity OK |

## Work Completed

- Replaced millisecond-rounded cue delay with 48 kHz sample-accurate delay.
- Disabled FFmpeg limiter auto-level, retained look-ahead compensation, and set
  the mixed-audio ceiling to -1.5 dBFS.
- Preserved A1 at 18%, D1 at 100%, video bitstream, subtitles, and project schema.
- Regenerated the affected MP4 without overwriting the original.
- Built normal and portable a97 releases; validated manifests, runtimes, privacy,
  archive integrity, and relocation behavior.
- Added a Windows 7-Zip archive path because bsdtar 3.8.8 crashes on bundled
  VieNeu sample filenames containing Vietnamese characters.

## Review

- Spec compliance: PASS.
- Code quality: PASS; no critical, important, or minor findings.
- Public contract: unchanged except intended package version and documented mix ceiling.
- Cross-machine live launch: not run while the user's a96 instance is active; portable
  discovery/relocation tests and bundled runtime smoke checks passed instead.

## Artifacts

- Fixed MP4: `Nên chọn loại nào_ iPhone 18 Pro&Duo - BV1cSec6tEux_VI_FINAL_AUDIO_FIXED_a97.mp4`
- Normal release: `DUCAI-STUDIO-2.0.0a97-win64`
- Portable release: `DUCAI-STUDIO-2.0.0a97-PORTABLE-win64`
- Archive SHA-256: `33be15da0c204adb7128cda41429923fcc17c2d738eea6c9aedf5982fa3b2bae`

## Known Limitation

- Transfer and first-run behavior on a physically separate Windows machine still requires
  an end-user smoke launch; package structure and relocation logic are fully verified locally.
