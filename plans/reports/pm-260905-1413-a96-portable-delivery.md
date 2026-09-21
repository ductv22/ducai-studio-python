## Release complete: DUCAI STUDIO 2.0.0a96 portable

### Outcome

| Requirement | Result |
|---|---|
| Translation/OCR UI hides Web popup, login/guest, Paddle/Quality/Rapid, notes and technical detail | Complete |
| Portable FFmpeg/ffprobe | Functional smoke passed |
| Portable PaddleOCR GPU and offline model cache | Functional inference smoke passed |
| Portable Piper voice | Functional WAV synthesis passed |
| Portable VieNeu 3.3 | Functional WAV synthesis passed with an external clone profile |
| No personal project/cache/credential/clone profile | Verified |
| Relocatable Python runtimes | Marker-state package; machine-bound launchers removed |
| Integrity manifest | 59,363 files; 14,496,244,807 bytes; 56,520 critical SHA-256 entries |

### Verification

- Focused tests: `8 passed`.
- Full suite: `690 passed, 1 skipped` (intentional opt-in real-Piper test).
- Ruff, compileall and QML lint: passed.
- Packaged UI smoke: Translation and Transcription screens passed before the pristine rebuild.
- Final package was rebuilt after smoke; its three `pyvenv.cfg` files retain `__DUCAI_PORTABLE_PYTHON_HOME__`.
- No builder user-profile path, workspace path or API-key pattern found in distributable text/content.
- RAR integrity test: passed.

### Artifact

- Folder: `DUCAI-STUDIO-2.0.0a96-PORTABLE-win64`
- Folder size: 14,496,244,807 bytes (13.501 GiB)
- Archive: `DUCAI-STUDIO-2.0.0a96-PORTABLE-win64.rar`
- Archive size: 4,261,879,502 bytes (3.969 GiB; 70.6% smaller)
- Archive SHA-256: `84030b6a5717d3ce2322f2e4220aaa534984549442fb4e2291ea91e7a9a97929`

### User-facing notes

- Extract the full archive into a writable folder before launch.
- Do not copy or run `DucaiStudio2.exe` by itself.
- VieNeu clone profiles, browser sessions and API credentials are intentionally not included.
- NVIDIA GPU/driver compatibility still depends on the recipient machine.

### Unresolved questions

- None blocking delivery.
