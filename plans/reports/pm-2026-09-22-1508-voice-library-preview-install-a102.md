# Voice library preview/install a102 — completion report

| Item | Result |
|---|---|
| Plan | `plans/260922-voice-library-preview-install-a102/plan.md` |
| Status | Completed, 7/7 checklist items |
| Catalog | 25 VieNeu presets, 2 verified Piper voices, separate personal clone entry |
| Preview | 27 distinct PCM WAV files, 5.008–8.000 seconds |
| Install | Background Piper download, staged validation, progress/error state |
| UI startup | Catalog built in 23.1 ms; no CUDA probe on Qt thread |
| Automated gates | Focused tests, full pytest, Ruff, compileall, QML lint: exit 0 |
| Release | `DUCAI-STUDIO-2.0.0a102-win64` |
| Portable | VieNeu 3.8.1, Piper, PaddleOCR, Whisper large-v3, FFmpeg |
| Portable archive | 8,823,301,985 bytes; SHA-256 `742e4c15f8881bf08a2cb3b44785fe37b76c820baf516214adfb830c373a27b9` |
| Privacy | No project, media, voice cache, credential, browser profile or clone profile |

## Acceptance evidence

- Portable first-launch screenshot shows VieNeu preset cards as **Đã cài/Sẵn sàng**.
- Qt Multimedia loaded both an installed VieNeu sample and an uninstalled VIVOS
  sample without decoder errors.
- VIVOS remains uninstalled but exposes both **Nghe thử** and **Cài**.
- Real preset/Piper synthesis coverage and downloader coverage remain green in the
  TTS integration/focused suites.

## Unresolved questions

- None for this plan. Personal clone preview intentionally requires the user's own
  reference file; DUCAI does not substitute another speaker under that entry.
