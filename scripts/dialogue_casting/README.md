# Dialogue casting recognition lab

This experimental local recognizer is connected to DUCAI's dialogue-casting
job, project state, voice assignment and TTS path. It is not production-ready:
the independent Chinese-animation accuracy/coverage gate remains
`NOT_EVALUATED`, and a single video smoke test cannot establish it.

## Install explicitly

From the repository, with Python 3.12 and `uv` available:

```powershell
./scripts/setup-dialogue-casting-runtime.ps1 -DataRoot '<local DUCAI_STUDIO_DATA>'
```

The dedicated `dialogue_casting_runtime/.venv` uses CPU-only execution. The GUI
environment receives no recognition ML dependencies. `requirements.in` pins direct versions;
`requirements.txt` additionally pins every transitive version and distribution
hash. Installation uses `uv pip sync --require-hashes --only-binary :all:`.
`model-lock.json` pins model sizes, SHA-256, preprocessing, label order and source.
The setup command is the only operation that downloads models. `-Offline` also
supports installation from populated uv/model caches. Changed or incomplete
files cause an actionable error; they are never converted to an unknown label.

## Prepare independent listening labels

Call `scripts/dialogue-casting.py prepare --help` for options. The JSON source
manifest has a `films` array. Each film requires `id`, `title`, `media_path`,
`timebase: "original_audio"`, `language: "zh"`, `genre: "animation"` and either
`srt_path` (dialogue SRT in original source time) or `cues`. JSON cue records use
`id`, `start_us`, `end_us` and optional `role`; only dialogue participates.
Relative paths resolve beside the manifest. Never pass timestamps from an
edited export as if they were original-source timestamps.

```powershell
.venv/Scripts/python.exe scripts/dialogue-casting.py prepare `
  --manifest '<source-manifest.json>' --output '<new listening-pack directory>'
```

Default: 15 calibration + 70 held-out cues per film. Seeded selection preserves
short/hard cues; it does not inspect predictions. Selected listening intervals
including 0.5-second context do not overlap. Source SHA-256 and stable sampled
cue identity bind every WAV to its original film/times. Existing pack directories
are never overwritten. A failed extraction leaves a partial directory for diagnosis;
use a fresh output directory after correcting its cause.

Open `NGHE-VA-GAN-NHAN.html` locally in Edge/Chrome. The page runs without a server
or network; it hides model predictions, subtitles and calibration/test splits.
Listen, label, export JSON. Import accepts matching clip IDs/checksums when a
later pack adds another film. User labels are never generated automatically.
See `annotation-guide.md` for Vietnamese listening instructions.

## Offline prediction and evaluation

Use the isolated runtime's Python for `predict` and `verify`; the pure data and
evaluation commands also work in the regular project environment:

```powershell
& '<runtime>/.venv/Scripts/python.exe' scripts/dialogue-casting.py verify --model-root '<runtime>/models'
& '<runtime>/.venv/Scripts/python.exe' scripts/dialogue-casting.py predict `
  --corpus '<pack>/corpus.json' --model-root '<runtime>/models' --output '<predictions.json>'
.venv/Scripts/python.exe scripts/dialogue-casting.py calibrate `
  --corpus '<pack>/corpus.json' --predictions '<predictions.json>' `
  --labels '<independent-labels.json>' --output '<frozen-calibration.json>'
.venv/Scripts/python.exe scripts/dialogue-casting.py evaluate `
  --corpus '<pack>/corpus.json' --predictions '<predictions.json>' `
  --labels '<independent-labels.json>' --frozen '<frozen-calibration.json>' --output '<report.json>'
```

Analysis uses 60-second blocks, five-second context and at most three clear
one-to-five-second classifier references per speaker. Only two CPU threads are
used by default. Block speaker identities are local to that context; there is no
global cross-block speaker merging yet. Overlapping/competing speakers, weak
speech, insufficient reference, contradictory evidence or low confidence yield
explicit fallback reasons. The standalone prototype rejects cues over 20 seconds
with an explicit error; DUCAI's integrated job marks such cues as fallback and
does not omit them or silently split them.

Completed acoustic windows are cached atomically, bound to source bytes/times,
models, dependencies, code and acoustic settings. Interrupted work can reuse only
matching complete windows. Changing the assignment confidence does not invalidate
acoustic evidence. `--limit` is for smoke checks; partial results cannot pass.

Calibration reads calibration labels only and freezes confidence by maximum
coverage among thresholds reaching >=95% calibration accuracy. Test evaluation
requires >=200 independently labelled held-out cues from >=3 distinct, confirmed
Chinese animated films, >=95% auto accuracy **and** >=70% coverage. Mixed/unclear
truth remains in the denominator; automatically assigning it is an error.
All-fallback, missing labels/predictions, source duplication, stale calibration
or model failures never pass. Exit codes: 0 successful command (or evaluation
PASS), 1 invalid input/runtime/inference error, 2 evaluation FAIL/NOT_EVALUATED.

## Provenance and licenses

- [Sherpa official model/example documentation](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)
  provides the Pyannote and Chinese ERes2Net ONNX conversions used here.
- [Pyannote segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
  declares MIT; the conversion archive includes its MIT license, verified and installed.
- [ERes2Net model producer card, revision v1.0.1](https://modelscope.cn/models/iic/speech_eres2net_base_sv_zh-cn_3dspeaker_16k)
  explicitly declares Apache License 2.0 for the model. That card is downloaded
  with its own pinned checksum; this claim does not rely solely on the code license.
- [alefiury original classifier](https://huggingface.co/alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech)
  declares Apache-2.0. [Xenova's conversion](https://huggingface.co/Xenova/wav2vec2-large-xlsr-53-gender-recognition-librispeech)
  identifies that upstream and is pinned to commit `6bea1eddcfca9842add425123f4955d5b4f153f7`.
  The conversion does not contain a separate license file; retain the original
  model attribution when packaging. Its LibriSpeech training score is not evidence
  of Chinese animation accuracy. Label 0 is female; label 1 is male. Inputs are
  16 kHz mono, zero-mean/unit-variance float32 without padding for a single sample.
- Runtime licenses are retained in installed wheels: Sherpa Apache-2.0,
  ONNX Runtime MIT, NumPy BSD with its bundled third-party notices. Retain all
  installed dependency notices if a distributable recognition pack is produced.

GitHub release assets did not expose publisher digests. Their checked-in SHA-256
values are observed from the official downloads, not independently signed upstream
attestations. The gender ONNX hash also matches the Hugging Face LFS SHA-256.
The EXE contains compiled host code and the model/dependency lock files;
recognition weights and isolated Python runtimes stay in the external data root.
The runtime setup script explicitly downloads those verified files.

## Experimental Whisper and web demo

`scripts/dialogue-casting-demo.py` is the separately authorized practical demo.
It does not relax the original product gate or replace the EXE's ASR model.
Installed large-v3 and a local conversion of official
[BELLE Chinese large-v3](https://huggingface.co/BELLE-2/Belle-whisper-large-v3-zh)
run on the same original WAVs with matching production decoding settings.
The official revision/hash and conversion provenance live in
`asr_experiment.py` and the generated provenance JSONs. Model-card benchmark
claims do not establish quality on the user's animation audio.

Commands, in order (run each with `--help`):

1. `download-asr`: explicit network installation of pinned official safetensors.
2. `convert-asr`: local, offline conversion to CTranslate2 float16. Use an
   existing Python environment with torch/transformers and pass the installed
   Whisper site's directory through `--ct2-site`; no dependency installation or
   modifications to either runtime. The converted model uses a separate folder.
3. `prepare`: copy only the 30 calibration clips and extract one complete cached
   recognition window per source. Verify original hashes. Never predict/score
   the 140 reserved held-out cues. Output must be a new directory.
4. `transcribe`: use the installed Whisper Python, explicit local model path,
   `--model-id large-v3` or `--model-id belle-zh` and the same device for both.
   Audio is bounded to 150 seconds. Preserve measured segment and word timestamps.
5. `build`: require complete paired results with identical settings/audio. Split
   ASR sentences using measured speaker changes, pauses and an eight-second
   target; retain all recognized words. Missing word alignment retains the raw
   sentence. Generate `MO-DEMO.html`, embedded data and real per-cue audio.

Open the complete folder's HTML locally in Edge/Chrome; no server is needed.
The page compares machine labels and original human labels, provides source
playback, filters, single/bulk corrections, Automatic reset, one-step undo,
browser drafts and JSON export/import. `demo-assisted-review` exports are kept
separate from independent `human-listening` labels and require the exact demo,
audio and machine identities. The demo plays original audio; Vietnamese TTS,
new-file upload/inference and desktop integration are outside this prototype.

Software checks:

```powershell
.venv/Scripts/python.exe -m pytest tests/dialogue_casting
node --test tests/dialogue_casting/test-demo-review.cjs
```

Two continuous excerpts allow practical listening for repeated errors. Sparse
calibration cues do not measure consecutive-error runs. The continuous excerpts
may overlap prior samples; they are exploratory, not held-out evaluation. Do
not compare ASR text accuracy without independently corrected Chinese text,
and do not compare casting accuracy across models from cue counts alone when
their sentence boundaries differ.

### SRT-bounded demo

The follow-up experiment uses original dialogue SRT intervals. `srt_cues` keeps
text opt-in; independent listening packs still omit it. Only complete cues inside
the verified excerpts participate; partial edge cues are reported separately.
Both Whisper models hear exactly each full cue without the previous 0.5-second
context. SRT text is shown for comparison, never used as a transcription prompt
or gender evidence. Speech outside SRT intervals is excluded.

Additional CLI commands (use `--help` for required paths):

- `prepare-srt`: validate previous demo/review, source manifest/timebase and
  corpus timestamps; preserve returned review bytes and generate exact PCM clips.
- `cast-srt`: run in the isolated casting Python. Reuse verified diarization,
  prioritizing clear 1–5-second classifier samples inside each SRT cue. Short
  cues may explicitly use the existing speaker reference; low confidence and
  conflicting references remain fallback. Multiple/overlapping speakers stay
  fallback. No confidence thresholds are relaxed.
- `transcribe`: run twice with the existing isolated Whisper Python and the two
  local models, then `build-srt` to generate the offline page.

The new page defaults to SRT cues, shows original SRT plus both transcripts and
the old group-reference result at the same timestamps. Saved reviews are carried
only to identical old cues. New boundaries do not inherit gender overrides;
conflicting prior feedback at intersecting audio flags a new cue for review and
keeps the raw acoustic decision visible. The JSON schema remains assisted review,
and imports remain bound to the exact demo/experiment/audio/machine identities.

The supplied excerpts overlap three formerly reserved cue intervals. They have
not been scored as a benchmark; exposed intervals must not be counted as unseen
independent evaluation data. `reserved_audio_exposure` records their identities.
More automatic assignments or fewer fallback cues do not prove higher accuracy.

### Refined SRT demo with ECAPA

The user authorized another repair and web testing before integration. The
separate ECAPA-TDNN experiment leaves the original wav2vec2 lock/runtime and
production gate unchanged. A paired float/int8 diagnostic on exposed audio did
not resolve the known errors. The new classifier is the pinned MIT
[JaesungHuh model](https://huggingface.co/JaesungHuh/voice-gender-classifier);
`ecapa-lock.json` owns code/model hashes, label order, license and runtime pins.
Publisher benchmark scores do not establish Chinese-animation quality.

Additional commands in `dialogue-casting-demo.py` (use `--help`):

- `download-ecapa --output <directory>` explicitly installs only verified model,
  config, model card, reviewed upstream source and MIT license into an experiment
  directory. Inference never downloads. No app dependency/model replacement.
- `prepare-refined --previous-root <SRT-demo> --review <exact-review.json>
  --output <new-directory>` validates the existing demo/ASR/audio/review before
  preserving bytes. It requires a new folder and records the previous identities.
- `cast-refined --root <new-directory> --model-root <ECAPA-directory>` runs with
  the existing separate PyTorch audio Python matching the lock's runtime pins,
  explicitly on CPU with two intra-op threads. It does not run in the GUI Python
  or the small ONNX-only casting Python. No runtime packages are modified.
- `build-refined --root <new-directory>` replays decisions from saved real
  probabilities and verifies experiment/code/model/source identity before writing
  the offline page. The existing Whisper transcripts remain bound to their
  preserved original experiment/audio; they are not relabelled as new ASR runs.

Cached diarizer turns retain their original source/time identity. Every speaker
reference is rescored by ECAPA; clear local 1–5-second references remain preferred.
The original overlap, multiple-speaker, speech/reference length, contradiction and
bounded-memory gates are retained. ECAPA's experimental confidence 0.95 is chosen
after exposed calibration/diagnostics, not a frozen independent product threshold.
Errors remain errors. Weak local evidence is never replaced by strong outside
references. SRT text and human corrections do not enter model inference.

Human review state migrates only across identical stable ID, original-source
time, target time, film and clip SHA. `review_origins` retains the old demo,
experiment and machine binding; old raw files are unchanged. Exported reviews
use the new exact demo/experiment/current-machine binding. Old JSON still cannot
be imported as if it belongs to the new machine. The schema remains assisted
review. Automatic reset, undo, saved drafts and old datasets remain available.
Two new filters show changed machine labels and unresolved unreviewed cues.

The delivered page plays a fixed sample pack and has no Vietnamese TTS or
new-video processing. Source/contract/syntax checks are automated; rendering,
playback and browser draft persistence require real browser confirmation. The
previous local-URL tool refusal explicitly prohibits a server/alternate-browser
workaround. Do not claim that unavailable UI verification has passed.
