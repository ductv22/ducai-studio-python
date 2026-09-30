from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.subtitles import NormalizedBox, RescueWindow, SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.job_manager.service import _apply_ocr_cue_regions
from ducai_studio.workers import ocr_worker as ocr_worker_module
from ducai_studio.workers.ocr_worker import EngineHostClient, OcrWorker
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def test_ocr_scene_cues_are_projected_to_a_separate_s2_track() -> None:
    first = TimeRange(0, 800_000)
    second = TimeRange(900_000, 1_700_000)
    media = MediaAsset("subtitle-media", "fixture.srt", MediaKind.SUBTITLE, 1_700_000)
    snapshot = ProjectSnapshot(
        "project",
        "Fixture",
        1,
        media=(media,),
        tracks=(
            TimelineTrack(
                "S1",
                TrackKind.SUBTITLE,
                "Phụ đề nguồn",
                (
                    TimelineClip("clip-1", "S1", first, first, media.id),
                    TimelineClip("clip-2", "S1", second, second, media.id),
                ),
            ),
        ),
        source_cues=(
            SubtitleCue("cue-1", 1, first, "走", "zh"),
            SubtitleCue("cue-2", 2, second, "海王兽", "zh"),
        ),
    )

    updated = _apply_ocr_cue_regions(
        snapshot,
        {
            "cue_regions": [
                {
                    "index": 2,
                    "start_us": second.start_us,
                    "end_us": second.end_us,
                    "box": [0.72, 0.22, 0.08, 0.31],
                    "panel_box": [0.68, 0.18, 0.16, 0.39],
                    "role": "scene_text",
                    "orientation": "vertical",
                    "style_hint": "calligraphy",
                    "review_state": "accepted",
                }
            ]
        },
    )

    assert [(track.id, track.name, len(track.clips)) for track in updated.tracks] == [
        ("S1", "Phụ đề nguồn", 1),
        ("S2", "Chữ trong cảnh", 1),
    ]
    assert updated.tracks[0].clips[0].id == "clip-1"
    assert updated.tracks[1].clips[0].id == "clip-2"
    assert updated.source_cues[1].role == "scene_text"


def test_adaptive_rescue_is_bounded_without_a_second_video_decode_pass() -> None:
    events = list(range(200_000, 60_000_000, 200_000))

    burst, scene = OcrWorker._adaptive_timestamps_from_events(events, 60_000_000)

    assert len(burst) <= 160
    assert len(scene) <= 86
    assert tuple(range(0, 60_000_000, 10_000_000)) == tuple(
        timestamp for timestamp in scene if timestamp % 10_000_000 == 0
    )


def test_adaptive_event_bounding_is_stable_while_the_window_grows() -> None:
    early = list(range(200_000, 10_000_000, 200_000))
    complete = list(range(200_000, 30_000_000, 200_000))

    early_burst, _ = OcrWorker._adaptive_timestamps_from_events(
        early, 30_000_000
    )
    complete_burst, _ = OcrWorker._adaptive_timestamps_from_events(
        complete, 30_000_000
    )

    assert set(early_burst).issubset(complete_burst)
    assert len(complete_burst) <= 80


def test_full_frame_events_extend_scene_scan_without_expanding_dialogue_bursts() -> None:
    dialogue_events = [1_000_000]
    full_frame_events = [1_000_000, 5_000_000]

    burst, scene = OcrWorker._adaptive_timestamps_from_events(
        dialogue_events,
        12_000_000,
        scene_events=full_frame_events,
    )

    assert 5_000_000 not in burst
    assert 5_000_000 in scene
    assert 5_240_000 in scene
    assert {
        900_000,
        1_000_000,
        1_100_000,
        1_200_000,
        1_300_000,
        1_400_000,
    }.issubset(burst)


def test_worker_final_candidates_bridges_one_fast_and_one_quality_observation() -> None:
    evidence = (
        SubtitleEvidence(
            "fast-dialogue",
            "frame-232200000",
            232_200_000,
            "upper",
            "在这呢",
            0.999950,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.46, 0.89, 0.09, 0.06),
        ),
        SubtitleEvidence(
            "quality-dialogue",
            "frame-232400000",
            232_400_000,
            "lower",
            "在这呢",
            0.999866,
            "paddle-quality",
            "PP-OCRv6-medium",
            NormalizedBox(0.461, 0.895, 0.088, 0.058),
        ),
    )
    worker = object.__new__(OcrWorker)
    worker._load_evidence = lambda: evidence

    candidates = worker._final_candidates(
        (),
        (RescueWindow(TimeRange(230_000_000, 235_000_000), ("coverage_gap",)),),
        300_000_000,
        800_000,
    )

    assert len(candidates) == 1
    assert candidates[0].text == "在这呢"
    assert candidates[0].source == "paddle-cross-pass-consensus"


def test_ocr_engine_process_receives_portable_paddlex_cache(
    tmp_path: Path, monkeypatch
) -> None:
    python = tmp_path / "DUCAI_STUDIO_DATA" / "paddle_runtime" / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    cache = tmp_path / "DUCAI_STUDIO_DATA" / "paddle_model_cache"
    cache.mkdir()
    host = tmp_path / "ocr-engine-host.py"
    host.touch()
    captured: dict[str, object] = {}

    class FakeProcess:
        stderr: list[str] = []

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(ocr_worker_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ocr_worker_module, "isolated_python_environment", lambda: {})

    EngineHostClient(python, host).start()

    assert captured["env"] == {"PADDLE_PDX_CACHE_HOME": str(cache.resolve())}


def test_ocr_worker_uses_checkpoint_compatible_runtime_engine_override(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    configured_host = tmp_path / "old-engine-host.py"
    configured_host.write_text("old", encoding="utf-8")
    runtime_host = tmp_path / "fixed-engine-host.py"
    runtime_host.write_text("fixed", encoding="utf-8")
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"fixture")
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    monkeypatch.setenv("DUCAI_OCR_ENGINE_HOST_PATH", str(runtime_host))
    monkeypatch.setenv(
        "DUCAI_OCR_ENGINE_HOST_SHA256", sha256(runtime_host.read_bytes()).hexdigest()
    )
    captured: dict[str, object] = {}

    class FakeEngineHostClient:
        def __init__(self, python_path: Path, host_path: Path) -> None:
            captured["python_path"] = python_path
            captured["host_path"] = host_path

        def request(self, action: str, payload: dict) -> dict:
            captured["action"] = action
            captured["payload"] = payload
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", FakeEngineHostClient)
    worker = OcrWorker("ocr-runtime-host")
    worker.config = {
        "engine_python": sys.executable,
        "engine_host_path": str(configured_host),
        "media_path": str(media),
    }

    worker._open_engine("fast")

    assert captured["host_path"] == runtime_host.resolve()
    assert captured["action"] == "open"


def test_ocr_worker_runs_external_host_and_publishes_only_final_srt(tmp_path: Path) -> None:
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"video fixture")
    output = tmp_path / "fixture_ZH_FINAL.srt"
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_ocr_engine_host.py"
    environment = os.environ.copy()
    environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio.workers.ocr_worker", "ocr-fixture"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    ready = WorkerMessage.from_json_line(process.stdout.readline())
    assert ready.kind == "ready"
    start = WorkerMessage(
        PROTOCOL_VERSION,
        "ocr-fixture",
        "start-request",
        "control",
        {
            "action": "start",
            "command_id": "start-command",
            "job": {
                "kind": "ocr",
                "provider_config": {
                    "media_path": str(media),
                    "duration_us": 2_400_000,
                    "sample_interval_us": 800_000,
                    "window_frame_count": 3,
                    "engine_python": sys.executable,
                    "engine_host_path": str(fake_host),
                    "output_path": str(output),
                },
            },
        },
    )
    process.stdin.write(start.to_json_line())
    process.stdin.flush()
    kinds = []
    while True:
        message = WorkerMessage.from_json_line(process.stdout.readline())
        kinds.append(message.kind)
        if message.kind in {"succeeded", "failed"}:
            terminal = message
            break
    assert terminal.kind == "succeeded", terminal.payload
    assert terminal.payload["status"] == "PASS"
    assert terminal.payload["cue_count"] == 1
    assert process.wait(timeout=10) == 0
    assert output.is_file()
    assert output.read_text(encoding="utf-8-sig").count("-->") == 1
    assert "checkpoint" in kinds
    assert not list(tmp_path.glob("*partial*.srt"))


def test_ocr_worker_pause_resume_keeps_checkpoint_and_finishes(tmp_path: Path) -> None:
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"video fixture")
    output = tmp_path / "fixture_ZH_FINAL.srt"
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_ocr_engine_host.py"
    environment = os.environ.copy()
    environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)
    environment["DUCAI_FAKE_OCR_DELAY_MS"] = "60"
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio.workers.ocr_worker", "ocr-control"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    assert WorkerMessage.from_json_line(process.stdout.readline()).kind == "ready"
    start = WorkerMessage(
        PROTOCOL_VERSION,
        "ocr-control",
        "start-request",
        "control",
        {
            "action": "start",
            "command_id": "start-command",
            "job": {
                "kind": "ocr",
                "provider_config": {
                    "media_path": str(media),
                    "duration_us": 8_000_000,
                    "sample_interval_us": 800_000,
                    "window_frame_count": 2,
                    "engine_python": sys.executable,
                    "engine_host_path": str(fake_host),
                    "output_path": str(output),
                },
            },
        },
    )
    process.stdin.write(start.to_json_line())
    process.stdin.flush()
    while WorkerMessage.from_json_line(process.stdout.readline()).kind != "progress":
        pass
    pause = WorkerMessage(
        PROTOCOL_VERSION,
        "ocr-control",
        "pause-request",
        "control",
        {"action": "pause", "command_id": "pause-command"},
    )
    process.stdin.write(pause.to_json_line())
    process.stdin.flush()
    seen = []
    while "paused" not in seen:
        seen.append(WorkerMessage.from_json_line(process.stdout.readline()).kind)
    assert "checkpoint" in seen
    resume = WorkerMessage(
        PROTOCOL_VERSION,
        "ocr-control",
        "resume-request",
        "control",
        {"action": "resume", "command_id": "resume-command"},
    )
    process.stdin.write(resume.to_json_line())
    process.stdin.flush()
    terminal = None
    while terminal is None:
        message = WorkerMessage.from_json_line(process.stdout.readline())
        if message.kind in {"succeeded", "failed"}:
            terminal = message
    assert terminal.kind == "succeeded", terminal.payload
    assert process.wait(timeout=10) == 0
    assert output.is_file()


def test_targeted_rescan_returns_evidence_without_publishing_partial_final(
    tmp_path: Path,
) -> None:
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"video fixture")
    output = tmp_path / "fixture_ZH_FINAL.srt"
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_ocr_engine_host.py"
    environment = os.environ.copy()
    environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio.workers.ocr_worker", "ocr-rescan"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    assert WorkerMessage.from_json_line(process.stdout.readline()).kind == "ready"
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "ocr-rescan",
            "start-request",
            "control",
            {
                "action": "start",
                "command_id": "start-command",
                "job": {
                    "kind": "ocr",
                    "provider_config": {
                        "media_path": str(media),
                        "duration_us": 5_000_000,
                        "sample_interval_us": 400_000,
                        "window_frame_count": 4,
                        "engine_python": sys.executable,
                        "engine_host_path": str(fake_host),
                        "output_path": str(output),
                        "rescan_cue_id": "cue-one",
                        "rescan_cue_start_us": 1_000_000,
                        "rescan_cue_end_us": 3_000_000,
                        "scan_windows": [
                            {
                                "start_us": 0,
                                "end_us": 4_200_000,
                                "reasons": ["user_targeted_rescan"],
                            }
                        ],
                    },
                },
            },
        ).to_json_line()
    )
    process.stdin.flush()
    terminal = None
    while terminal is None:
        message = WorkerMessage.from_json_line(process.stdout.readline())
        if message.kind in {"succeeded", "failed"}:
            terminal = message

    assert terminal.kind == "succeeded", terminal.payload
    assert terminal.payload["rescan"]["text"] == "我要修炼成仙"
    assert len(terminal.payload["rescan"]["evidence_ids"]) >= 2
    assert process.wait(timeout=10) == 0
    assert not output.exists()


def test_quality_checkpoint_resume_rewinds_overlap_without_dropping_fast_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    artifact = tmp_path / "immutable.ndjson"
    rows = [
        {"id": "fast", "provider_id": "paddle-fast", "timestamp_us": 1_600_000},
        {"id": "quality-0", "provider_id": "paddle-quality", "timestamp_us": 0},
        {
            "id": "quality-800",
            "provider_id": "paddle-quality",
            "timestamp_us": 800_000,
        },
        {
            "id": "quality-1600",
            "provider_id": "paddle-quality",
            "timestamp_us": 1_600_000,
        },
    ]
    artifact.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    worker = OcrWorker("resume-quality")
    worker.config = {"sample_interval_us": 800_000}

    worker._restore_checkpoint(
        {
            "payload": {"stage": "quality", "cursor_us": 1_600_000},
            "artifact": {"uri": str(artifact)},
        }
    )

    retained = [
        json.loads(line)["id"]
        for line in worker.evidence_path.read_text(encoding="utf-8").splitlines()
    ]
    assert worker.cursor_us == 800_000
    assert retained == ["fast", "quality-0"]


def test_fast_checkpoint_resume_restores_transition_history_before_rewind(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    artifact = tmp_path / "immutable.ndjson"
    artifact.write_text("", encoding="utf-8")
    worker = OcrWorker("resume-fast-events")
    worker.config = {"sample_interval_us": 800_000}

    worker._restore_checkpoint(
        {
            "payload": {
                "stage": "fast",
                "cursor_us": 5_000_000,
                "change_events_us": [1_000_000, 3_000_000, 4_000_000],
                "scene_change_events_us": [2_000_000, 3_200_000, 4_400_000],
            },
            "artifact": {"uri": str(artifact)},
        }
    )

    assert worker.cursor_us == 3_400_000
    assert worker._change_events == [1_000_000, 3_000_000]
    assert worker._scene_change_events == [2_000_000, 3_200_000]


def test_watermark_evidence_stays_out_of_cues_and_requires_persistent_edge_samples(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("watermark-regions")
    worker.config = {"duration_us": 8_000_000}
    rows = [
        {
            "id": "dialogue",
            "frame_id": "frame-000000000000",
            "timestamp_us": 0,
            "band": "lower",
            "text": "真正的台词",
            "confidence": 0.99,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.2, 0.7, 0.6, 0.1],
        },
        {
            "id": "watermark-1",
            "frame_id": "frame-000000000000",
            "timestamp_us": 0,
            "band": "lower",
            "text": "小李看漫剧bilibili",
            "confidence": 0.98,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.78, 0.62, 0.2, 0.08],
            "ignored_overlay": True,
        },
        {
            "id": "watermark-2",
            "frame_id": "frame-000000000800",
            "timestamp_us": 800_000,
            "band": "lower",
            "text": "小李看漫剧bilibili",
            "confidence": 0.97,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.79, 0.621, 0.19, 0.079],
            "ignored_overlay": True,
        },
        {
            "id": "watermark-3",
            "frame_id": "frame-000000002000",
            "timestamp_us": 2_000_000,
            "band": "lower",
            "text": "小李看漫剧bilibili",
            "confidence": 0.97,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.785, 0.62, 0.195, 0.08],
            "ignored_overlay": True,
        },
        {
            "id": "single-observation",
            "frame_id": "frame-000000001600",
            "timestamp_us": 1_600_000,
            "band": "upper",
            "text": "bilibili",
            "confidence": 0.96,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.02, 0.2, 0.12, 0.05],
            "ignored_overlay": True,
        },
    ]
    worker.evidence_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    assert [item.id for item in worker._load_evidence()] == ["dialogue"]
    regions = worker._watermark_regions()

    assert len(regions) == 1
    assert regions[0]["start_us"] == 0
    assert regions[0]["end_us"] == 8_000_000
    assert regions[0]["sample_count"] == 3
    assert regions[0]["box"] == pytest.approx([0.772, 0.614, 0.216, 0.092])


def _start_fake_whisper_worker(
    tmp_path: Path,
    job_id: str,
    *,
    environment_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], Path]:
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"video fixture")
    output = tmp_path / "fixture_ZH_FINAL.srt"
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    job_staging = staging / job_id
    job_staging.mkdir()
    (job_staging / "dialogue-16k-mono.wav").write_bytes(b"W" * 8_192)
    model = tmp_path / "large-v3"
    model.mkdir(exist_ok=True)
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_whisper_engine_host.py"
    environment = os.environ.copy()
    environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)
    environment.update(environment_overrides or {})
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio.workers.ocr_worker", job_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    assert WorkerMessage.from_json_line(process.stdout.readline()).kind == "ready"
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            job_id,
            "start-request",
            "control",
            {
                "action": "start",
                "command_id": "start-command",
                "job": {
                    "kind": "ocr",
                    "provider_id": "faster-whisper-audio",
                    "model_id": "large-v3",
                    "input_fingerprint": "input",
                    "settings_fingerprint": "settings",
                    "provider_config": {
                        "extraction_method": "whisper_audio",
                        "media_path": str(media),
                        "duration_us": 9_000_000,
                        "sample_interval_us": 800_000,
                        "engine_python": sys.executable,
                        "engine_host_path": str(fake_host),
                        "model_path": str(model),
                        "model_id": "large-v3",
                        "language": "zh",
                        "device": "auto",
                        "output_path": str(output),
                    },
                },
            },
        ).to_json_line()
    )
    process.stdin.flush()
    return process, output


def _read_until_terminal(process: subprocess.Popen[bytes]) -> tuple[list[str], WorkerMessage]:
    assert process.stdout is not None
    kinds: list[str] = []
    while True:
        message = WorkerMessage.from_json_line(process.stdout.readline())
        kinds.append(message.kind)
        if message.kind in {"succeeded", "failed", "stopped"}:
            return kinds, message


def test_whisper_worker_streams_segments_and_publishes_guarded_srt(tmp_path: Path) -> None:
    process, output = _start_fake_whisper_worker(tmp_path, "whisper-success")

    kinds, terminal = _read_until_terminal(process)

    assert terminal.kind == "succeeded", terminal.payload
    assert terminal.payload["cue_count"] == 3
    assert process.wait(timeout=10) == 0
    assert output.read_text(encoding="utf-8-sig").count("-->") == 3
    qa = json.loads(output.with_name(output.stem + "_QA.json").read_text(encoding="utf-8"))
    assert qa["kind"] == "whisper_asr"
    assert qa["output_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert "progress" in kinds


def test_whisper_worker_pause_resume_rewinds_without_duplicate_cues(tmp_path: Path) -> None:
    process, output = _start_fake_whisper_worker(
        tmp_path,
        "whisper-resume",
        environment_overrides={"DUCAI_FAKE_WHISPER_DELAY_MS": "120"},
    )
    assert process.stdin is not None and process.stdout is not None
    while WorkerMessage.from_json_line(process.stdout.readline()).kind != "progress":
        pass
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "whisper-resume",
            "pause-request",
            "control",
            {"action": "pause", "command_id": "pause-command"},
        ).to_json_line()
    )
    process.stdin.flush()
    seen: list[str] = []
    while "paused" not in seen:
        seen.append(WorkerMessage.from_json_line(process.stdout.readline()).kind)
    assert "checkpoint" in seen
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "whisper-resume",
            "resume-request",
            "control",
            {"action": "resume", "command_id": "resume-command"},
        ).to_json_line()
    )
    process.stdin.flush()

    _kinds, terminal = _read_until_terminal(process)

    assert terminal.kind == "succeeded", terminal.payload
    assert process.wait(timeout=10) == 0
    assert output.read_text(encoding="utf-8-sig").count("-->") == 3


def test_whisper_worker_cancel_never_publishes_partial_s1(tmp_path: Path) -> None:
    process, output = _start_fake_whisper_worker(
        tmp_path,
        "whisper-cancel",
        environment_overrides={"DUCAI_FAKE_WHISPER_DELAY_MS": "120"},
    )
    assert process.stdin is not None and process.stdout is not None
    while WorkerMessage.from_json_line(process.stdout.readline()).kind != "progress":
        pass
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "whisper-cancel",
            "cancel-request",
            "control",
            {"action": "cancel", "command_id": "cancel-command"},
        ).to_json_line()
    )
    process.stdin.flush()

    _kinds, terminal = _read_until_terminal(process)

    assert terminal.kind == "stopped"
    assert process.wait(timeout=10) == 0
    assert not output.exists()
    assert not output.with_name(output.stem + "_QA.json").exists()


def test_whisper_worker_falls_back_from_cuda_to_cpu_once(tmp_path: Path) -> None:
    process, output = _start_fake_whisper_worker(
        tmp_path,
        "whisper-fallback",
        environment_overrides={"DUCAI_FAKE_WHISPER_CUDA_FAIL": "1"},
    )

    _kinds, terminal = _read_until_terminal(process)

    assert terminal.kind == "succeeded", terminal.payload
    assert terminal.payload["device"] == "cpu"
    assert terminal.payload["compute_type"] == "int8"
    assert process.wait(timeout=10) == 0
    assert output.is_file()


def test_whisper_checkpoint_rewind_uses_a_complete_segment_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("whisper-boundary")
    worker.whisper_segments_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in (
                {"start_us": 0, "end_us": 3_000_000, "text": "第一句"},
                {"start_us": 3_000_000, "end_us": 6_000_000, "text": "第二句"},
                {"start_us": 6_000_000, "end_us": 9_000_000, "text": "第三句"},
            )
        ),
        encoding="utf-8",
    )
    worker.cursor_us = 9_000_000

    worker._rewind_whisper_segments()

    assert worker.cursor_us == 3_000_000
    assert [row["text"] for row in worker._load_whisper_segments()] == ["第一句"]
