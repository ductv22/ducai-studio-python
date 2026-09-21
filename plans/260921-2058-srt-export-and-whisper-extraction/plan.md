---
title: "SRT export and Whisper extraction"
description: "Xuất SRT từ cue hiện hành và port Whisper cục bộ từ DUCAI 0.1.99 vào kiến trúc Python/PySide6."
status: in-progress
priority: P1
effort: "3-5d"
tags: [subtitles, whisper, asr, export, windows]
created: 2026-09-21
---

# SRT export and Whisper extraction

## Overview

Khôi phục luồng nhận dạng lời nói Whisper của bản cũ dưới dạng một nguồn trích
xuất độc lập với OCR khung hình, đồng thời thêm xuất SRT nguồn và SRT Việt từ
trạng thái cue mới nhất của project. Giải pháp dùng lại hợp đồng job/cache/publish
hiện tại, giữ model/runtime lớn ở `DUCAI_STUDIO_DATA`, không nhúng vào Git hoặc
tiến trình GUI.

## Outcome contract

- **Outcome:** Người dùng chọn `OCR khung hình` hoặc `Whisper âm thanh`, chạy qua
  cùng màn Trích xuất, nhận S1 có timestamp microsecond; sau OCR/Whisper hoặc sau
  dịch có thể lưu SRT hiện hành ra vị trí tùy chọn.
- **Constraints:** Job Manager là SQLite writer duy nhất; tác vụ nặng chạy ngoài
  GUI; QML chỉ gửi action; cache/fingerprint tách theo engine/model; không lưu key,
  media hay model vào Git; project cũ và Multi mặc định vẫn chạy OCR.
- **Non-goals:** Không bê nguyên Electron/React; không tự trộn OCR cứu hard-sub vào
  kết quả Whisper; không thêm diarization/cloud ASR; không tự thay đổi timestamp
  khi xuất; không giấu việc tải model lớn.
- **Observable acceptance:** Whisper có tiến độ thật, pause/cancel/resume từ
  checkpoint và CUDA→CPU fallback; SRT xuất lại sau chỉnh tay phản ánh đúng cue
  mới nhất; test, lint, compile, QML lint và build release đều đạt.

## Source manifest

- Source behavior: read-only sibling `../DUCAI_STUDIO_NEXT`, đặc biệt
  `ducai_engine_worker.py` và `apps/web/src/dubbing/components/recognition-view.tsx`.
- Packaged oracle: DUCAI STUDIO `0.1.99` (read-only local artifact), bundled
  `faster-whisper 1.2.1`/`ctranslate2 4.8.1` and `large-v3` behavior.
- Local target: this Python/PySide6 repository, branch
  `ducai-studio-python-a98`, baseline commit `dc00b29`.

## Source anatomy

1. The old UI exposes ASR separately from frame OCR.
2. `run_asr` probes media, extracts dialogue to 16 kHz mono, loads
   `faster-whisper large-v3`, prefers CUDA float16, and falls back to CPU int8.
3. Decoding uses Chinese language hints, word timestamps, conservative VAD,
   fixed-temperature beam search, progress derived from segment end time, then
   emits an SRT/result JSON.
4. The old worker mixes optional quality guards and hard-sub OCR rescue. The
   current product already owns a guarded PaddleOCR pipeline, so only the ASR
   behavior is ported; the two evidence sources remain explicit.

## Dependency matrix

| Source component | Current equivalent | State | Adaptation |
|---|---|---|---|
| ASR/OCR mode switch | `SubtitleEditorScreen.qml` | EXISTS | Add extraction method selector and method-specific controls |
| `run_asr` job | `JobKind.OCR` + `ocr_worker.py` | CONFLICT | Keep durable `ocr` stage, dispatch by `extraction_method` |
| `faster-whisper` in frozen app | External Paddle/VieNeu runtimes | EXISTS | New optional `whisper_runtime`; never add to GUI dependencies |
| Segment/result JSON | `SubtitleCue` + SRT publish contract | EXISTS | Convert segment seconds once to integer microseconds |
| Old SRT download | `render_srt` + project snapshots | EXISTS | Export current source/translated cue tuple atomically |
| Old hard-sub rescue | Guarded PaddleOCR | EXISTS | Do not merge automatically; user chooses the source |
| Progress/cancel | versioned WorkerMessage/checkpoints | EXISTS | Stream segment progress and persist resumable NDJSON ledger |

## Challenge decisions

| # | Decision | Source answer | Local answer | Risk if wrong | Recommendation |
|---|---|---|---|---|---|
| 1 | Whisper replaces OCR? | ASR could include OCR rescue | OCR and ASR have different evidence | Hidden mixing makes wrong text hard to diagnose | Keep two explicit extraction methods |
| 2 | Bundle model in GUI? | 0.1.99 froze a very large runtime | Models live in external data | Release grows by several GB and Git becomes unusable | External pinned runtime/model with visible preflight |
| 3 | New database job kind? | Separate legacy endpoint | Multi and history already treat extraction as `ocr` | Migration and retry regression | Keep `JobKind.OCR`; fingerprint provider/method separately |
| 4 | Full-file synchronous decode? | Old worker had no durable pause | Current jobs promise fast control/checkpoint | Pause can hang for minutes and resume restarts | Stream segments to the existing worker and checkpoint safely |
| 5 | Export cached file or live cues? | Old UI exported in-memory recognition | Current edits live in project snapshots | Export can silently ignore manual edits | Render from latest persisted snapshot only |
| 6 | Preserve timestamps exactly? | Float seconds from Whisper | Domain requires integer microseconds | Repeated rounding creates drift/overlap | Convert once, normalize monotonically, never retime during export |
| 7 | Enable Whisper in Multi immediately? | No equivalent durable batch | Multi snapshots the OCR config | Changing defaults reruns many projects | Support snapshotted method, keep default OCR and old batches unchanged |
| 8 | Include old quality guards wholesale? | Several optional, partially missing modules | Current validation is independently tested | Unverifiable behavior and duplicate OCR policy | Port only deterministic ASR cleanup with focused tests |

**Risk score:** Medium (3 critical assumptions: external runtime availability,
pause/resume semantics, timestamp normalization). Each is covered by preflight,
checkpoint regression tests, and a no-data-mutation rollback.

## Goals

| # | Goal | Priority |
|---|------|----------|
| 1 | Export source and Vietnamese SRT from the current project state | P1 |
| 2 | Add local Whisper extraction with old large-v3 decoding behavior | P1 |
| 3 | Preserve pause/resume, cache, Multi defaults and release portability | P1 |

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | [Live SRT export contract](./phase-01-live-srt-export.md) | Completed |
| 2 | [Whisper runtime and extraction worker](./phase-02-whisper-runtime-worker.md) | Completed |
| 3 | [UI, settings and Multi integration](./phase-03-ui-and-multi.md) | Completed |
| 4 | [Verification, docs and release](./phase-04-verification-release.md) | In progress |

## Success Criteria

- [x] Source SRT and Vietnamese SRT export use the latest persisted cue text,
      order and timestamp after manual edits.
- [x] Exports are UTF-8 BOM, valid SRT, atomic, and report Vietnamese
      success/failure without mutating the project.
- [x] Whisper defaults to Chinese `large-v3`, uses CUDA float16 when available,
      falls back to CPU int8, and stores no model in the repository.
- [x] Whisper progress is real; pause/cancel are acknowledged promptly; resume
      reuses the compatible checkpoint without duplicating or dropping cues.
- [x] OCR and Whisper caches cannot be confused; rerun overwrite semantics remain intact.
- [x] Existing projects/batches without the new option keep OCR behavior.
- [x] Focused and full quality gates pass and a new release is built/smoke-tested.

## Rollback

Remove the new UI selector/actions, Whisper branch/host and runtime setup while
leaving existing OCR/translation code paths untouched. Because the design adds
no schema migration and uses provider-specific fingerprints, old projects,
jobs and cache remain readable. SRT export is stateless and can be removed
without project migration.

<!-- slug: srt-export-and-whisper-extraction -->
