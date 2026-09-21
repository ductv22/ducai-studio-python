from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from ducai_studio.workers import ocr_worker as ocr_worker_module
from ducai_studio.workers.ocr_worker import EngineHostClient, OcrWorker
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


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
