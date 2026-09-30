# Third-party notices

DUCAI STUDIO 2 currently bundles the following visual assets.

## Be Vietnam Pro

- Upstream: `https://github.com/bettergui/BeVietnamPro`
- Files: `src/ducai_studio/ui/assets/fonts/`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/OFL.txt`

## Playwrite Việt Nam

- Upstream: `https://github.com/TypeTogether/Playwrite`
- Google Fonts family: `ofl/playwritevn`
- File: `src/ducai_studio/ui/assets/fonts/PlaywriteVN-Variable.ttf`
- Source SHA-256: `DA510F64CE3E9844F169F7C742A9F1370ADE81BCF9AD17C7409137BA3F3D604C`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/PLAYWRITE-VN-OFL.txt`

## Dancing Script

- Upstream: `https://github.com/googlefonts/DancingScript`
- Google Fonts family: `ofl/dancingscript`
- File: `src/ducai_studio/ui/assets/fonts/DancingScript-Variable.ttf`
- Source SHA-256: `21808625578FE8D8CD10CB684BE546DCA077B27CD03A53A2F1EC11DC743C924C`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/DANCING-SCRIPT-OFL.txt`

## Lobster

- Upstream: `https://github.com/impallari/The-Lobster-Font`
- Google Fonts family: `ofl/lobster`
- File: `src/ducai_studio/ui/assets/fonts/Lobster-Regular.ttf`
- Source SHA-256: `D6568E697FD50CEDC0BE04D8AAE4127FE95ADD607E7BFF954CA88604BE80C205`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/LOBSTER-OFL.txt`

## Agbalumo

- Upstream: `https://github.com/SorkinType/Agbalumo`
- Google Fonts family: `ofl/agbalumo`
- File: `src/ducai_studio/ui/assets/fonts/Agbalumo-Regular.ttf`
- Source SHA-256: `D9E35C6DEBF1319267140838F3C30C3C51D9F47C7050B5482C14C5ACCF7F4D94`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/AGBALUMO-OFL.txt`

## Comforter Brush

- Upstream: `https://github.com/googlefonts/comforter-brush`
- Google Fonts family: `ofl/comforterbrush`
- File: `src/ducai_studio/ui/assets/fonts/ComforterBrush-Regular.ttf`
- Source SHA-256: `7BD357DC8BF83D71F27FD5155ECE5C8C100A2042D4530E4487635473C5836F95`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/COMFORTER-BRUSH-OFL.txt`

## Water Brush

- Upstream: `https://github.com/googlefonts/water-brush`
- Google Fonts family: `ofl/waterbrush`
- File: `src/ducai_studio/ui/assets/fonts/WaterBrush-Regular.ttf`
- Source SHA-256: `2CBDCCBCA5D4BE005FE4EDD8895B91618A7F96639EAEEB4987AD83E81AFC71A7`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/WATER-BRUSH-OFL.txt`

## Splash

- Upstream: `https://github.com/googlefonts/splash`
- Google Fonts family: `ofl/splash`
- File: `src/ducai_studio/ui/assets/fonts/Splash-Regular.ttf`
- Source SHA-256: `42C2DDC11917333BCD24A3EB0BC7DF5205278226E2F6521459384479F1B6675C`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/SPLASH-OFL.txt`

## Hurricane

- Upstream: `https://github.com/googlefonts/hurricane`
- Google Fonts family: `ofl/hurricane`
- File: `src/ducai_studio/ui/assets/fonts/Hurricane-Regular.ttf`
- Source SHA-256: `5EA86B562CA5C2B1893C233367CDE1AEE0D677BB6D30AF9C3F699C270880DA13`
- License: SIL Open Font License 1.1
- Full license: `src/ducai_studio/ui/assets/fonts/HURRICANE-OFL.txt`

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
- Preview files `vi_VN-vais1000-medium.wav` and `vi_VN-vivos-x_low.wav` were
  generated locally from the corresponding official Piper models. They retain
  the model/dataset licenses shown in the catalog: CC BY 4.0 and CC BY-NC-SA 4.0.

## PaddleOCR / PaddlePaddle GPU

- Upstream: `https://github.com/PaddlePaddle/PaddleOCR`
- Portable package: isolated PaddleOCR/PaddlePaddle GPU runtime and four
  PP-OCRv6 detection/recognition model directories.
- The runtime, CUDA dependencies and model files retain their upstream licenses.

## faster-whisper / CTranslate2

- Upstream: `https://github.com/SYSTRAN/faster-whisper` and
  `https://github.com/OpenNMT/CTranslate2`
- Optional external runtime: `faster-whisper` 1.2.1 and CTranslate2 4.8.1.
- Optional model: `Systran/faster-whisper-large-v3`, installed only by the
  explicit Whisper setup script or an opt-in portable build.
- The engine, converted model and optional NVIDIA CUDA runtime packages retain
  their upstream licenses and notices; none are stored in this source repository
  or bundled in the standard GUI release.

## VieNeu-TTS runtime

- Upstream: `https://github.com/pnnbao97/VieNeu-TTS`
- Pinned source tag: `v3.8.1` (`592ba27c8f932b80768f6cee405badaef32bdb17`)
- Runtime version: 3.8.1 with PyTorch/Torchaudio 2.8.0 CUDA 12.8
- Runtime and model dependencies retain their upstream licenses.
- The bundled `vieneu-*.wav` catalog previews were generated from the 25 preset
  voices shipped in VieNeu 3.8.1. The preset metadata declares CC BY-NC 4.0 and
  non-commercial use; DUCAI preserves that restriction in every voice card.
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
