# Third-party notices

DUCAI STUDIO 2 currently bundles the following visual assets.

## Be Vietnam Pro

- Upstream: `https://github.com/bettergui/BeVietnamPro`
- Files: `src/ducai_studio/ui/assets/fonts/`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/OFL.txt`

## Lucide icons

- Upstream: `https://github.com/lucide-icons/lucide`
- Pinned source commit: `23f9abc4ed0146cffededd3d7f94c1018bfdf693`
- Files: `src/ducai_studio/ui/assets/icons/`
- License: ISC
- Full license: `src/ducai_studio/ui/assets/icons/LICENSE`

The SVG stroke color is adapted to the DUCAI dark theme; icon geometry is
otherwise unchanged.

## Piper runtime

- Upstream: `https://github.com/OHF-Voice/piper1-gpl`
- Package: `piper-tts` 1.6.0 in an isolated user-managed runtime
- License: GPL-3.0-or-later
- Voice-model rights are separate from the engine license and are displayed per
  catalog entry. The tool does not claim that community or real-person-labelled
  weights are commercially safe.

## PaddleOCR / PaddlePaddle GPU

- Upstream: `https://github.com/PaddlePaddle/PaddleOCR`
- Portable package: isolated PaddleOCR/PaddlePaddle GPU runtime and four
  PP-OCRv6 detection/recognition model directories.
- The runtime, CUDA dependencies and model files retain their upstream licenses.

## VieNeu-TTS runtime

- Upstream: `https://github.com/pnnbao97/VieNeu-TTS`
- Pinned source commit: `f49d70246f18dece32666d79057e98ec94fd699e`
- Runtime version: 3.3.0
- Runtime and model dependencies retain their upstream licenses.
- User reference audio and derived clone profiles remain local.

## FFmpeg

- Upstream: `https://ffmpeg.org/`
- Windows distribution: Gyan FFmpeg full build.
- The portable package includes `ffmpeg.exe`, `ffprobe.exe` and the distributor's
  license/readme files under `bin/`.

## Noto Sans CJK SC

- Upstream: `https://github.com/notofonts/noto-cjk`
- File: `src/ducai_studio/ui/assets/fonts/NotoSansCJKsc-Regular.otf`
- Source SHA-256: `2C76254F6FC379FDDFCE0A7E84FB5385BB135D3E399294F6EEB6680D0365B74B`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/NOTO-CJK-LICENSE`
