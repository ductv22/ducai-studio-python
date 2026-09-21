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
