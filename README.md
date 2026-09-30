# DUCAI STUDIO 2

DUCAI STUDIO 2 is a ground-up Windows desktop rebuild of the existing DUCAI
workflow. It keeps proven OCR, translation, voice and FFmpeg behavior behind
replaceable providers while moving project state, jobs and the timeline into
testable contracts.

## Current status

The rebuild is under active development. `DucAITool 0.1.99` remains the
production oracle and is not modified by this project.

The current alpha includes crash-atomic schema migrations, the versioned
project/job schema, a single-writer Job Manager with pre-owned worker process
trees, restart-safe checkpoint retry and the responsive QML application shell.
Video, audio and SRT import now create real timeline tracks; clip/track editing,
viewport virtualization and synchronized Qt Multimedia preview are operational.
Paddle Fast OCR, targeted Paddle Quality rescue and Rapid compatibility fallback
now run in a separate managed runtime with pause/resume checkpoints, sampled-frame
coverage QA, guarded final SRT import and a virtualized subtitle editor. Adaptive
bursts confirm one- and two-character dialogue without a second full-video decode
pass. A multi-character line seen once by Fast and once by Quality is also retained
when the independent readings agree in text, time and geometry. Sparse full-frame
samples separate character/technique/artifact/location
cards onto simultaneous S2…Sn layers, filter persistent platform marks, translate accepted scene text and
replace only its Chinese glyph area while retaining the original panel. Horizontal,
vertical and calligraphic Vietnamese labels start from the detected source-glyph size
and support per-cue text, true 1–100 font scaling, horizontal/vertical flow,
tight background padding, letter spacing, bold/italic, independent text/background
opacity and bundled Vietnamese-capable calligraphy fonts. Scene text can opt into
four original vector ink backgrounds (`Mực loang`, `Mực đại mặc`, `Mực tụ`,
`Nét cọ`) plus eight bundled Vietnamese-capable calligraphy faces. Preview and
libass export share the same deterministic, aspect-safe silhouettes. Preview and every
subtitle Timeline track share stable cue-ID selection, double-click editing,
independent/bulk formatting and undoable cue deletion. Timeline click, Ctrl+click,
marquee and Ctrl+A can select cue groups; drag moves their time/lane while direct
Preview handles move or resize their frame geometry. Moving a scene label to S1
explicitly promotes it to spoken dialogue; moving dialogue to S2+ keeps it spoken,
because TTS follows cue role instead of display lane. The shared `T` tool creates
either dialogue (with voice) or scene text (visual-only) at the playhead and exposes
the complete per-cue typography before insertion. Dialogue inherits the nearest
spoken cue's placement and style, while new scene text is placed on a fresh S layer
with Comforter Brush, white text and a fully opaque black `Mực loang` background by
default. Dense subtitle controls use width-safe numeric steppers and contextual
tooltips so their decrement, value and increment regions remain distinct at 720p.
Scene text can also fade in and out with one per-cue duration shared by Preview and
libass export. Legacy simultaneous scene labels are repaired
into visible S2…Sn lanes when a project opens, without changing their cue IDs,
timing or manual lanes. A frame-backed OCR region editor
separates dialogue S1 from scene-text S2…Sn, masks user-defined no-OCR regions and
preserves manually added scene text across later OCR/translation runs; scene text
never enters the dialogue SRT or TTS/D1
request. The same
screen can explicitly use local Whisper large-v3 audio transcription with streamed
progress, checkpoint resume and CUDA-to-CPU fallback. Source and Vietnamese subtitle
tracks can be exported from the latest persisted manual edits. Gemini Web, Gemini CLI/OAuth,
direct Gemini API and OpenAI-compatible DeepSeek, Hy3/TokenHub, Qwen, GLM and
custom endpoint translation are operational behind one cue-integrity pipeline.
Piper 1.6 and VieNeu-TTS v3 Turbo 3.8.1 now run through isolated persistent
engine hosts with a real voice catalog, preview, two-tier per-cue speed cache/QA,
targeted retry, onset cleanup, 1.10x-3.00x pitch-preserving read speed, EBU R128
mastering and automatic D1 timeline import. A cache-only post-dubbing conform
stage can stretch video, speed voice or combine both through one monotonic
preview/export time map; Timeline can visualize and apply timestamped per-cue
voice-speed decisions directly on D1. Logo/blur, mixer,
resumable FFmpeg export, guarded final MP4, one-way
0.1.99 migration and runtime diagnostics are now implemented for release QA.
The Multi video workspace catalogs every existing video project and also routes
one or more imports from its dedicated video picker into per-media projects,
reusing an existing owner for duplicate media, and processes selected projects
one at a time through OCR, translation and TTS with durable pause/resume,
fingerprinted cache reuse and a deferred failure pass; export remains an
explicit per-project action. Packaged builds from a118 also expose a manual,
Ed25519-verified GitHub updater with real-byte download progress, a Job
Manager-wide install reservation, out-of-process health confirmation and
automatic rollback while keeping `DUCAI_STUDIO_DATA` outside the update payload.

## Development setup

```powershell
uv sync --python 3.12
uv run pytest
uv run ducai-studio
```

See [the Vietnamese user guide](docs/user-guide.md) for the production workflow,
quality gates, recovery behavior and legacy import rules.

Run QML validation before changing the shell:

```powershell
$qml = @('src/ducai_studio/ui/qml/Main.qml') +
  (Get-ChildItem src/ducai_studio/ui/qml/controls,src/ducai_studio/ui/qml/screens -Filter *.qml)
.venv/Scripts/pyside6-qmllint.exe -I src/ducai_studio/ui/qml $qml
```

Heavy OCR and TTS engines use separate managed runtimes. They are intentionally
not installed into the GUI environment.

Install or repair the isolated Piper runtime with:

```powershell
./scripts/setup-piper-runtime.ps1
```

Whisper is optional and is never downloaded during normal application startup.
Install its pinned external runtime and the large-v3 model explicitly with:

```powershell
./scripts/setup-whisper-runtime.ps1
```

This writes the runtime and model under `DUCAI_STUDIO_DATA`; neither is included
in Git or the standard GUI release.

The voice catalog reuses installed ONNX/JSON pairs from
`DUCAI_STUDIO_DATA/piper_voices`. Every usable catalog voice has a bundled 5-8
second audition clip that plays without a project. Verified Piper entries can be
installed from their own card; the download runs in the background and is checked
before it becomes selectable. Unverified community and real-person-labelled
models are not published as fake or silently downloaded entries. VieNeu requires
runtime 3.8.1 and fails closed unless a clone profile proves both a 192D speaker
embedding and non-empty reference codes. A new local reference can be enrolled
from Voice Studio; reference audio is never uploaded. The 25 presets shipped by
VieNeu v3 Turbo are also selectable directly and carry their non-commercial
license label. Piper remains the fast default, while VieNeu is the quality-first
preset/clone option.

Install or repair the isolated VieNeu CUDA runtime explicitly with:

```powershell
./scripts/setup-vieneu-runtime.ps1
```

The installer pins VieNeu 3.8.1, PyTorch/Torchaudio CUDA 12.8 and validates a
CUDA-capable NVIDIA device. Existing 3.3 clone NPZ files are normalized only in
memory; their files, model cache and prior voice cache are not rewritten.

The Paddle runtime can be selected through the application setting or the
`DUCAI_PADDLE_PYTHON` environment variable. OCR jobs fingerprint that runtime,
the host script, model files and crop settings before permitting checkpoint
resume.

Gemini translation creates a Film Bible, packs semantic chunks, validates exact
cue IDs and timestamps, and performs translation plus self-QA in the same request.
A fast deterministic gate then checks CJK, unsafe Latin, length and Film Bible names;
only failing cues are repaired before each completed chunk is checkpointed. Gemini Web
uses one isolated browser session with bounded
1095/1905/about:blank recovery plus first-response and incomplete-JSON stall
guards. Persistently failing multi-cue chunks split after two worker rounds,
while isolated cues retain the deeper retry budget so recovery stays fast
without silently dropping dialogue. The free Guest profile never falls back to
Codex/Luna or another paid model: after bounded clean-session retries it stops at
the latest immutable checkpoint. Selecting that compatible cancelled, failed or
interrupted ledger exposes one stateful **continue from cache** action which
retries the same job ID. Cache resume keeps completed translations and targets
only remaining deterministic blockers; real blockers still require a fresh PASS.
Gemini CLI runs in official headless JSON mode.
Gemini API keys are stored only in Windows Credential Manager; the UI accepts
an ordered named key pool and jobs persist reference names only. The active key
rotates after verified `429 RESOURCE_EXHAUSTED` or authentication failure, while
unclassified throttling remains a retryable error instead of burning through
the pool. Newly checkpointed cue translations stream into the bilingual editor
without publishing a partial final SRT. Reopening a project hydrates that preview
from the immutable ledger before new deltas arrive, avoiding event-replay races.
Keys from the same Google Cloud project may still share quota.

## Source layout

- `src/ducai_studio/domain/`: immutable project, timeline, cue and job rules.
- `src/ducai_studio/application/`: commands and job orchestration.
- `src/ducai_studio/infrastructure/`: SQLite, artifacts and process adapters.
- `src/ducai_studio/providers/`: OCR, translation, voice, preview and export ports.
- `src/ducai_studio/job_manager/`: durable single-writer job process.
- `src/ducai_studio/ui/`: PySide6 view models and QML resources.
- `tests/`: unit, contract, integration, golden and end-to-end coverage.

Bundled fonts and icons retain their upstream licenses in
`src/ducai_studio/ui/assets/` and are summarized in `THIRD_PARTY_NOTICES.md`.
