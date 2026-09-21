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
coverage QA, guarded final SRT import and a virtualized subtitle editor. Gemini Web, Gemini CLI/OAuth,
direct Gemini API and OpenAI-compatible DeepSeek, Hy3/TokenHub, Qwen, GLM and
custom endpoint translation are operational behind one cue-integrity pipeline.
Piper 1.6 and VieNeu 3.3 Turbo now run through isolated persistent
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
explicit per-project action.

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

The voice catalog reuses installed ONNX/JSON pairs from
`DUCAI_STUDIO_DATA/piper_voices`. Community and real-person-labelled models keep
their explicit rights warning and are never downloaded silently. VieNeu requires
runtime 3.3.0 and fails closed unless a clone profile proves both a 192D speaker
embedding and non-empty reference codes. A new local reference can be enrolled
from Voice Studio; reference audio is never uploaded. Piper remains the fast
default, while VieNeu is presented as the slower quality/clone option.

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
