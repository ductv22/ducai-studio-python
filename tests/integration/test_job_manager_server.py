import base64
import hashlib
import json
import subprocess
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtNetwork import QLocalSocket

from ducai_studio.domain.jobs import JobKind, JobSpec, JobState
from ducai_studio.domain.project import MediaAsset, MediaKind, ProjectSnapshot
from ducai_studio.infrastructure.checkpoint_repository import CheckpointRepository
from ducai_studio.infrastructure.database import Database
from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
    encode_frame,
)
from ducai_studio.infrastructure.job_repository import JobRepository
from ducai_studio.job_manager import server as server_module
from ducai_studio.job_manager import service as service_module
from ducai_studio.job_manager.__main__ import _canonical_runtime_path
from ducai_studio.job_manager.server import JobManagerServer, _project_transport_payload
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.media_probe import MediaProbeResult
from ducai_studio.ui.viewmodels.application_view_model import _ProjectSnapshotDecodeTask
from ducai_studio.workers.protocol import WorkerMessage


class StaticMediaProbe:
    def probe(self, _media_path):
        return MediaProbeResult(5_000_000, True, True, 1280, 720, "mp4")


def test_start_tts_job_rebases_packaged_release_host_without_changing_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    old_host = (
        tmp_path
        / "DUCAI-STUDIO-2.0.0a92-win64"
        / "engine_hosts"
        / "tts_engine_host.py"
    )
    runtime_host = tmp_path / "current-release" / "engine_hosts" / "tts_engine_host.py"
    runtime_host.parent.mkdir(parents=True)
    runtime_host.write_text("current tts host", encoding="utf-8")
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    job = service.jobs.create(
        JobSpec(
            JobKind.TTS,
            "project",
            1,
            "vieneu-3.3",
            "3.3.0",
            "v3turbo",
            "tts-source",
            "tts-settings",
            {
                "engine_host_path": str(old_host),
                "engine_host_sha256": "old-release-host",
            },
        )
    )
    service.checkpoints.commit(
        job_id=job.id,
        stage_key="tts",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=3_935_800_000,
        payload={"entries": {"cue-1": "cached"}},
        event_id="tts-checkpoint-before-release-upgrade",
    )
    server = JobManagerServer(name=f"unused-tts-runtime-rebase-{uuid4()}", service=service)
    captured: dict[str, object] = {}
    monkeypatch.setattr(server_module, "_engine_host_path", lambda _name: runtime_host)
    monkeypatch.setattr(server.workers, "start", lambda **values: captured.update(values))
    monkeypatch.setattr(server, "_deliver_control", lambda *_args, **_kwargs: None)

    try:
        server._start_job(
            {
                "job_id": job.id,
                "program": sys.executable,
                "arguments": ["-m", "ducai_studio.workers.tts_worker", job.id],
                "resume_stage_key": "tts",
                "resume_checkpoint_key": "latest",
            }
        )
    finally:
        server.close()

    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["DUCAI_TTS_ENGINE_HOST_PATH"] == str(runtime_host)
    assert environment["DUCAI_TTS_ENGINE_HOST_SHA256"] == hashlib.sha256(
        runtime_host.read_bytes()
    ).hexdigest()
    assert environment["DUCAI_TTS_ENGINE_HOST_MANAGED"] == "1"
    preserved = service.load_worker_checkpoint(
        job.id, stage_key="tts", checkpoint_key="latest"
    )
    assert preserved is not None
    assert preserved["cursor_us"] == 3_935_800_000
    assert service.jobs.get(job.id).spec.provider_config["engine_host_path"] == str(old_host)
    events = service.jobs.list_events(job.id)
    assert any(event.kind == "tts_engine_host_runtime_refreshed" for event in events)


def test_start_tts_job_keeps_custom_engine_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    custom_host = tmp_path / "custom-hosts" / "tts_engine_host.py"
    custom_host.parent.mkdir(parents=True)
    custom_host.write_text("custom tts host", encoding="utf-8")
    runtime_host = tmp_path / "current-release" / "engine_hosts" / "tts_engine_host.py"
    runtime_host.parent.mkdir(parents=True)
    runtime_host.write_text("current tts host", encoding="utf-8")
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    job = service.jobs.create(
        JobSpec(
            JobKind.TTS,
            "project",
            1,
            "vieneu-3.3",
            "3.3.0",
            "v3turbo",
            "tts-source",
            "tts-settings",
            {
                "engine_host_path": str(custom_host),
                "engine_host_sha256": hashlib.sha256(custom_host.read_bytes()).hexdigest(),
            },
        )
    )
    server = JobManagerServer(name=f"unused-custom-tts-host-{uuid4()}", service=service)
    captured: dict[str, object] = {}
    monkeypatch.setattr(server_module, "_engine_host_path", lambda _name: runtime_host)
    monkeypatch.setattr(server.workers, "start", lambda **values: captured.update(values))
    monkeypatch.setattr(server, "_deliver_control", lambda *_args, **_kwargs: None)

    try:
        server._start_job(
            {
                "job_id": job.id,
                "program": sys.executable,
                "arguments": ["-m", "ducai_studio.workers.tts_worker", job.id],
                "resume_stage_key": "tts",
                "resume_checkpoint_key": "latest",
            }
        )
    finally:
        server.close()

    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert "DUCAI_TTS_ENGINE_HOST_PATH" not in environment
    assert "DUCAI_TTS_ENGINE_HOST_SHA256" not in environment
    assert environment["DUCAI_TTS_ENGINE_HOST_MANAGED"] == "0"
    assert service.jobs.get(job.id).spec.provider_config["engine_host_path"] == str(custom_host)


def test_output_promotion_uses_active_volume_temp_and_rolls_back_cross_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "staging" / "candidate.srt"
    active = tmp_path / "media" / "active.srt"
    candidate.parent.mkdir()
    active.parent.mkdir()
    candidate.write_bytes(b"new")
    active.write_bytes(b"old")
    monkeypatch.setattr(service_module, "_same_storage_volume", lambda *_args: False)

    with (
        pytest.raises(RuntimeError, match="project save failed"),
        service_module._promote_output_files(((candidate, active),)),
    ):
        assert active.read_bytes() == b"new"
        assert candidate.read_bytes() == b"new"
        raise RuntimeError("project save failed")

    assert active.read_bytes() == b"old"
    assert candidate.read_bytes() == b"new"

    with service_module._promote_output_files(((candidate, active),)):
        assert active.read_bytes() == b"new"

    assert active.read_bytes() == b"new"
    assert not candidate.exists()


def test_large_project_snapshot_uses_compressed_transport_without_exceeding_frame_limit(
    tmp_path: Path,
) -> None:
    snapshot = {
        "id": "large-project",
        "revision": 17,
        # A realistic cue-heavy snapshot compresses well but is much larger
        # than the 16 MiB IPC frame limit in its expanded JSON form.
        "cues": [{"index": index, "text": "Câu thoại tiếng Việt."} for index in range(250_000)],
    }
    transported = _project_transport_payload(snapshot, staging_root=tmp_path / "staging")
    assert "__project_transport__" in transported
    envelope = transported["__project_transport__"]
    assert envelope["encoding"] == "zlib+base64+json"
    compressed = base64.b64decode(envelope["data"])
    assert len(zlib.decompress(compressed)) == envelope["uncompressed_bytes"]

    decoded = _ProjectSnapshotDecodeTask(
        SimpleNamespace(), transported, {}, artifact_root=(tmp_path / "artifacts").resolve()
    )._decode(transported)
    assert decoded == snapshot


def test_large_timeline_command_uses_project_transport_and_keeps_action(
    tmp_path: Path,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    snapshot = {
        "id": "large-timeline-project",
        "revision": 18,
        "tracks": [
            {"id": f"clip-{index}", "text": "Câu thoại tiếng Việt."}
            for index in range(50_000)
        ],
        "_timeline_can_undo": True,
        "_timeline_can_redo": False,
    }

    class StubService:
        checkpoints = SimpleNamespace(staging_root=tmp_path / "staging")

        def timeline_command(self, **kwargs):
            assert kwargs["command"] == "resize"
            assert kwargs["clip_id"] == "voice-1"
            return snapshot

    server = JobManagerServer(
        name=f"unused-large-timeline-{uuid4()}", service=StubService()
    )
    try:
        transported = server._dispatch(
            "timeline_command",
            {
                "project_id": "large-timeline-project",
                "command": "resize",
                "clip_id": "voice-1",
                "timeline_start_us": 1_000_000,
                "timeline_end_us": 2_000_000,
                "apply_linked": False,
            },
        )
    finally:
        server.close()

    assert "__project_transport__" in transported
    emitted: list[tuple] = []
    runtime = SimpleNamespace(
        projectPayloadDecoded=SimpleNamespace(
            emit=lambda *args: emitted.append(args)
        )
    )
    _ProjectSnapshotDecodeTask(
        runtime,
        transported,
        {"request": "resize"},
        action="timeline_command",
        artifact_root=(tmp_path / "artifacts").resolve(),
    ).run()

    assert emitted == [
        (
            "timeline_command",
            True,
            snapshot,
            {"request": "resize"},
        )
    ]


def test_prepare_tts_rerun_is_exposed_as_a_project_transport_action(
    tmp_path: Path,
) -> None:
    snapshot = {"id": "voice-project", "revision": 8, "media": [], "tracks": []}

    class StubService:
        checkpoints = SimpleNamespace(staging_root=tmp_path / "staging")

        def prepare_tts_rerun(self, *, project_id: str):
            assert project_id == "voice-project"
            return snapshot

    server = JobManagerServer(
        name=f"unused-prepare-tts-{uuid4()}", service=StubService()
    )
    try:
        result = server._dispatch(
            "prepare_tts_rerun",
            {"project_id": "voice-project"},
        )
    finally:
        server.close()

    assert result == snapshot


@pytest.mark.parametrize("state", ["running", "pause_requested"])
def test_multi_membership_mutations_are_rejected_until_checkpoint_pause(
    tmp_path: Path, state: str
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    service.multi_videos.update_batch(state=state)

    with pytest.raises(ValueError, match="checkpoint an toàn"):
        service.add_multi_projects([])
    with pytest.raises(ValueError, match="checkpoint an toàn"):
        service.remove_multi_project("project")
    with pytest.raises(ValueError, match="checkpoint an toàn"):
        service.reorder_multi_projects([])
    with pytest.raises(ValueError, match="checkpoint an toàn"):
        service.select_all_multi_projects(False)

    service.multi_videos.update_batch(state="paused")
    assert service.reorder_multi_projects([])["items"] == []


def test_voice_conform_ipc_keeps_estimates_and_create_results_compact() -> None:
    QCoreApplication.instance() or QCoreApplication([])

    class StubService:
        def estimate_voice_conform(self, **kwargs):
            assert kwargs["include_plan"] is False
            assert kwargs["verify_cache"] is False
            return {
                "status": "PASS",
                "plan_fingerprint": "compact-plan",
                "unresolved_count": 0,
            }

        def create_voice_conform_job(self, **_kwargs):
            return {
                "id": "conform-job",
                "state": "queued",
                "spec": {"provider_config": {"cues": ["large"]}},
            }

    server = JobManagerServer(
        name=f"unused-compact-conform-{uuid4()}", service=StubService()
    )
    params = {
        "project_id": "project",
        "parent_tts_job_id": "parent",
        "mode": "hybrid",
        "max_audio_speed": 1.25,
        "min_video_speed": 0.5,
        "allow_duration_growth": True,
    }

    estimate = server._dispatch("estimate_voice_conform", params)
    created = server._dispatch("create_voice_conform_job", params)

    assert "plan" not in estimate
    assert created == {
        "id": "conform-job",
        "state": "queued",
        "kind": "tts",
        "project_id": "project",
        "provider_id": "voice-conform",
    }


def test_multi_status_event_uses_the_versioned_ipc_result_contract(tmp_path: Path) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-multi-event-{uuid4()}", service=service)

    class RecordingSocket:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        def state(self):
            return QLocalSocket.LocalSocketState.ConnectedState

        def write(self, frame: bytes) -> int:
            self.frames.append(bytes(frame))
            return len(frame)

        def abort(self) -> None:
            return

    socket = RecordingSocket()
    server._decoders[socket] = FrameDecoder()  # type: ignore[index]
    try:
        server._broadcast_multi_status()

        messages = FrameDecoder().feed(b"".join(socket.frames))
        assert len(messages) == 1
        assert messages[0].protocol_version == IPC_PROTOCOL_VERSION
        assert messages[0].kind == "event"
        assert messages[0].payload["event"] == "multi_status"
        assert messages[0].payload["result"] == {
            "batch": service.multi_videos.get_batch(),
            "items": [],
        }
    finally:
        server._decoders.clear()
        server.close()


def test_completed_job_reuse_invalidates_only_the_changed_stage_fingerprint(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )

    def raw(stage: str, settings_fingerprint: str) -> dict[str, object]:
        output = tmp_path / f"{stage}.active"
        return {
            "kind": stage,
            "project_id": "project-multi",
            "input_revision": 1,
            "provider_id": f"provider-{stage}",
            "provider_version": "1.0",
            "model_id": f"model-{stage}",
            "input_fingerprint": f"input-{stage}",
            "settings_fingerprint": settings_fingerprint,
            "provider_config": {
                "stage": stage,
                "output_path": str(output),
                "qa_path": str(output.with_suffix(".qa.json")),
            },
        }

    baseline = {
        "ocr": raw("ocr", "ocr-settings-v1"),
        "translate": raw("translate", "translation-settings-v1"),
        "tts": raw("tts", "tts-settings-v1"),
    }
    completed_ids: dict[str, str] = {}
    for stage, spec in baseline.items():
        created = service._create_or_reuse_completed_job(spec, reuse_completed=True)
        completed_ids[stage] = str(created["id"])
        service.jobs.transition(completed_ids[stage], JobState.START_REQUESTED)
        service.jobs.transition(completed_ids[stage], JobState.RUNNING)
        service.jobs.transition(completed_ids[stage], JobState.SUCCEEDED)
        job = service.jobs.get(completed_ids[stage])
        output = Path(str(job.spec.provider_config["output_path"]))
        output.write_bytes(f"active-{stage}".encode())
        Path(str(job.spec.provider_config["qa_path"])).write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "job_id": job.id,
                    "input_fingerprint": job.spec.input_fingerprint,
                    "settings_fingerprint": job.spec.settings_fingerprint,
                    "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )

    changed_translation = {
        **baseline,
        "translate": raw("translate", "translation-settings-v2"),
    }
    results = {
        stage: service._create_or_reuse_completed_job(spec, reuse_completed=True)
        for stage, spec in changed_translation.items()
    }

    assert results["ocr"]["reused"] is True
    assert results["ocr"]["id"] == completed_ids["ocr"]
    assert results["tts"]["reused"] is True
    assert results["tts"]["id"] == completed_ids["tts"]
    assert results["translate"]["reused"] is False
    assert results["translate"]["id"] != completed_ids["translate"]


def test_completed_job_reuse_ignores_an_older_job_after_manual_overwrite(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    output = tmp_path / "voice.active"
    qa_path = tmp_path / "voice.qa.json"
    raw = {
        "kind": "tts",
        "project_id": "project",
        "input_revision": 1,
        "provider_id": "piper",
        "provider_version": "1",
        "model_id": "voice",
        "input_fingerprint": "same-input",
        "settings_fingerprint": "same-settings",
        "provider_config": {
            "purpose": "production",
            "output_path": str(output),
            "qa_path": str(qa_path),
        },
    }

    def succeed(job_id: str, payload: bytes) -> None:
        service.jobs.transition(job_id, JobState.START_REQUESTED)
        service.jobs.transition(job_id, JobState.RUNNING)
        service.jobs.transition(job_id, JobState.SUCCEEDED)
        job = service.jobs.get(job_id)
        output.write_bytes(payload)
        qa_path.write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "job_id": job.id,
                    "input_fingerprint": job.spec.input_fingerprint,
                    "settings_fingerprint": job.spec.settings_fingerprint,
                    "output_sha256": hashlib.sha256(payload).hexdigest(),
                }
            ),
            encoding="utf-8",
        )

    older = service._create_or_reuse_completed_job(raw, reuse_completed=False)
    succeed(str(older["id"]), b"older")
    newer = service._create_or_reuse_completed_job(raw, reuse_completed=False)
    succeed(str(newer["id"]), b"newer")

    reused = service._create_or_reuse_completed_job(raw, reuse_completed=True)

    assert reused["reused"] is True
    assert reused["id"] == newer["id"]


def test_job_manager_canonicalizes_runtime_paths_before_locking(tmp_path: Path) -> None:
    target = tmp_path / "runtime"
    target.mkdir()

    assert _canonical_runtime_path(target / ".." / target.name) == target.resolve()


def send_request(socket: QLocalSocket, message_id: str, action: str, params: dict) -> IpcMessage:
    request = IpcMessage(
        IPC_PROTOCOL_VERSION,
        message_id,
        "request",
        {"action": action, "params": params},
    )
    socket.write(encode_frame(request))
    assert socket.waitForBytesWritten(2_000)
    decoder = FrameDecoder()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if socket.bytesAvailable() or socket.waitForReadyRead(250):
            messages = decoder.feed(bytes(socket.readAll()))
            if messages:
                return messages[0]
    raise AssertionError("Timed out waiting for Job Manager response")


def connect_to_server(socket: QLocalSocket, server_name: str) -> None:
    deadline = time.monotonic() + 8
    while (
        time.monotonic() < deadline
        and socket.state() != QLocalSocket.LocalSocketState.ConnectedState
    ):
        socket.abort()
        socket.connectToServer(server_name)
        socket.waitForConnected(250)
    assert socket.state() == QLocalSocket.LocalSocketState.ConnectedState


def create_job(socket: QLocalSocket, message_id: str = "create") -> str:
    created = send_request(
        socket,
        message_id,
        "create_job",
        {
            "kind": "ocr",
            "project_id": "project",
            "input_revision": 1,
            "provider_id": "paddle-fast",
            "provider_version": "3.2.0",
            "model_id": "PP-OCRv5-server",
            "input_fingerprint": "fixture",
            "settings_fingerprint": "settings-fixture",
            "provider_config": {"mode": "fast"},
        },
    )
    assert created.payload["ok"] is True, created.payload
    return str(created.payload["result"]["id"])


def wait_for_job_state(
    socket: QLocalSocket, job_id: str, expected: str, timeout: float = 5
) -> IpcMessage:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = send_request(socket, str(uuid4()), "get_job", {"job_id": job_id})
        if current.payload["result"]["state"] == expected:
            return current
    raise AssertionError(f"Job {job_id} did not reach {expected}")


def test_job_manager_process_serves_and_stops_cleanly(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    server_name = f"ducai-test-{uuid4()}"
    command = [
        sys.executable,
        "-m",
        "ducai_studio.job_manager",
        "--server-name",
        server_name,
        "--database",
        str(tmp_path / "state.db"),
        "--artifacts",
        str(tmp_path / "artifacts"),
    ]
    process = subprocess.Popen(command)
    socket = QLocalSocket()
    try:
        connect_to_server(socket, server_name)

        ping = send_request(socket, "ping", "ping", {})
        assert ping.payload["ok"] is True
        assert ping.payload["result"] == {"status": "ready"}

        job_id = create_job(socket)
        listed_jobs = send_request(socket, "list-jobs", "list_jobs", {})
        assert [item["id"] for item in listed_jobs.payload["result"]] == [job_id]
        listed_projects = send_request(socket, "list-projects", "list_projects", {})
        assert listed_projects.payload["result"] == []
        worker = Path(__file__).parents[1] / "fixtures" / "checkpoint_worker.py"
        running = send_request(
            socket,
            "run",
            "start_job",
            {
                "job_id": job_id,
                "program": sys.executable,
                "arguments": [str(worker), job_id],
            },
        )
        assert running.payload["ok"] is True
        assert running.payload["result"]["state"] == "start_requested"
        wait_for_job_state(socket, job_id, "running")

        shutdown = send_request(socket, "shutdown", "shutdown", {})
        assert shutdown.payload["result"] == {"status": "stopping"}
        assert process.wait(timeout=8) == 0

        jobs = JobRepository(Database(tmp_path / "state.db"))
        assert jobs.get(job_id).state is JobState.INTERRUPTED
        event_kinds = [event.kind for event in jobs.list_events(job_id)]
        worker_events = [
            kind for kind in event_kinds if kind in {"ready", "checkpoint", "stopped"}
        ]
        assert worker_events == ["ready", "checkpoint", "stopped"]
        job = jobs.get(job_id)
        checkpoint = CheckpointRepository(
            Database(tmp_path / "state.db"), tmp_path / "artifacts"
        ).load(
            job_id=job_id,
            stage_key="runtime",
            checkpoint_key="latest",
            resume_fingerprint=job.spec.resume_fingerprint,
        )
        assert checkpoint is not None
        assert checkpoint.cursor_us == 42_000_000

        resume_server = f"ducai-resume-{uuid4()}"
        resume_command = command.copy()
        resume_command[resume_command.index(server_name)] = resume_server
        resumed_process = subprocess.Popen(resume_command)
        resume_socket = QLocalSocket()
        try:
            connect_to_server(resume_socket, resume_server)
            retry = send_request(
                resume_socket,
                "retry-after-restart",
                "retry_job",
                {"job_id": job_id},
            )
            assert retry.payload["result"]["state"] == "queued"
            restarted = send_request(
                resume_socket,
                "start-after-restart",
                "start_job",
                {
                    "job_id": job_id,
                    "program": sys.executable,
                    "arguments": [str(worker), job_id],
                },
            )
            assert restarted.payload["result"]["state"] == "start_requested"
            wait_for_job_state(resume_socket, job_id, "running")
            events = send_request(
                resume_socket,
                "events-after-restart",
                "list_events",
                {"job_id": job_id},
            ).payload["result"]
            started_events = [event for event in events if event["kind"] == "started"]
            assert started_events[-1]["payload"]["resumed_cursor_us"] == 42_000_000
            send_request(resume_socket, "shutdown-after-restart", "shutdown", {})
            assert resumed_process.wait(timeout=8) == 0
        finally:
            resume_socket.abort()
            if resumed_process.poll() is None:
                resumed_process.terminate()
                resumed_process.wait(timeout=5)
    finally:
        socket.abort()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_delete_project_removes_only_internal_records_and_rejects_active_jobs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "video-source.mp4"
    source.write_bytes(b"source")
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    project = ProjectSnapshot(
        "project-delete",
        "Temporary project",
        1,
        media=(MediaAsset("video", str(source), MediaKind.VIDEO, 5_000_000),),
    )
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=project)
    spec = JobSpec(
        JobKind.OCR,
        project.id,
        1,
        "paddle-fast",
        "test",
        "model",
        "input",
        "settings",
        {"mode": "test"},
    )
    job = service.jobs.create(spec)
    (service.checkpoints.staging_root / job.id).write_bytes(b"partial-file")
    (service.checkpoints.immutable_root / job.id).mkdir(parents=True)

    with pytest.raises(ValueError, match="tác vụ đang hoạt động"):
        service.delete_project(project.id)

    service.jobs.transition(job.id, JobState.START_REQUESTED)
    service.jobs.transition(job.id, JobState.RUNNING)
    service.jobs.transition(job.id, JobState.SUCCEEDED)
    result = service.delete_project(project.id)

    assert result["deleted_jobs"] == 1
    assert not (service.checkpoints.staging_root / job.id).exists()
    assert not (service.checkpoints.immutable_root / job.id).exists()
    assert source.read_bytes() == b"source"
    with pytest.raises(LookupError):
        service.projects.get_summary(project.id)


def test_delete_project_keeps_database_records_when_artifact_cleanup_fails(
    tmp_path: Path, monkeypatch
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    project = ProjectSnapshot("project-cleanup-failure", "Keep me", 1)
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=project)
    job = service.jobs.create(
        JobSpec(
            JobKind.OCR,
            project.id,
            1,
            "paddle-fast",
            "test",
            "model",
            "input",
            "settings",
            {"mode": "test"},
        )
    )
    service.jobs.transition(job.id, JobState.START_REQUESTED)
    service.jobs.transition(job.id, JobState.RUNNING)
    service.jobs.transition(job.id, JobState.SUCCEEDED)

    def fail_cleanup(_path: Path, _root: Path) -> None:
        raise PermissionError("artifact locked")

    monkeypatch.setattr(service_module, "_remove_internal_artifact_path", fail_cleanup)

    with pytest.raises(PermissionError, match="artifact locked"):
        service.delete_project(project.id)

    assert service.projects.get_summary(project.id)["name"] == "Keep me"
    assert service.jobs.get(job.id).spec.project_id == project.id


def test_job_history_can_delete_only_finished_ocr_and_translation_jobs(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    project = ProjectSnapshot("history-project", "History", 1)
    service.projects.save(project_path=tmp_path / "history.ducai", snapshot=project)
    jobs = []
    for kind in (JobKind.OCR, JobKind.TRANSLATE):
        job = service.jobs.create(
            JobSpec(kind, project.id, 1, "provider", "v1", "model", kind.value, "settings", {})
        )
        service.jobs.transition(job.id, JobState.START_REQUESTED)
        service.jobs.transition(job.id, JobState.RUNNING)
        service.jobs.transition(job.id, JobState.SUCCEEDED)
        (service.checkpoints.staging_root / job.id).mkdir(parents=True)
        (service.checkpoints.staging_root / job.id / "partial").write_bytes(b"x")
        immutable = service.checkpoints.immutable_root / job.id
        immutable.mkdir(parents=True)
        committed = immutable / "checkpoint.json"
        committed.write_bytes(b"immutable")
        committed.chmod(0o444)
        jobs.append(job)

    result = service.clear_project_job_history(project.id, ("ocr", "translate"))

    assert result["deleted_jobs"] == 2
    assert all(not (service.checkpoints.staging_root / job.id).exists() for job in jobs)
    assert all(not (service.checkpoints.immutable_root / job.id).exists() for job in jobs)
    assert all(
        not any(record.id == job.id for record in service.jobs.list_recent(limit=20))
        for job in jobs
    )


def test_job_history_rejects_active_job_without_touching_artifacts(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    project = ProjectSnapshot("history-active", "Active", 1)
    service.projects.save(project_path=tmp_path / "active.ducai", snapshot=project)
    job = service.jobs.create(
        JobSpec(JobKind.OCR, project.id, 1, "provider", "v1", "model", "ocr", "settings", {})
    )
    (service.checkpoints.staging_root / job.id).mkdir(parents=True)
    with pytest.raises(ValueError, match="đã kết thúc"):
        service.delete_job_history(job.id)
    assert (service.checkpoints.staging_root / job.id).exists()


def test_server_cancel_then_deletes_active_history_after_worker_exit(
    tmp_path: Path, monkeypatch
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    job = service.jobs.create(
        JobSpec(
            JobKind.TRANSLATE,
            "history-active-delete",
            1,
            "gemini-web",
            "v1",
            "selected-on-web",
            "source",
            "settings",
            {},
        )
    )
    service.jobs.transition(job.id, JobState.START_REQUESTED)
    service.jobs.transition(job.id, JobState.RUNNING)
    artifact_root = service.checkpoints.staging_root / job.id
    artifact_root.mkdir(parents=True)
    (artifact_root / "ledger.partial").write_bytes(b"cache")
    server = JobManagerServer(name=f"unused-delete-active-{uuid4()}", service=service)
    monkeypatch.setattr(server.workers, "has_worker", lambda current: current == job.id)
    monkeypatch.setattr(server.workers, "request_stop", lambda *_args, **_kwargs: None)
    try:
        result = server._delete_job_history(job.id)

        assert result["pending"] is True
        assert service.jobs.get(job.id).state is JobState.CANCEL_REQUESTED
        assert artifact_root.exists()

        server._worker_finished(job.id, 0)

        with pytest.raises(LookupError):
            service.jobs.get(job.id)
        assert not artifact_root.exists()
    finally:
        server.close()


def test_late_worker_success_after_cancel_never_publishes_project_state(
    tmp_path: Path, monkeypatch
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    job = service.jobs.create(
        JobSpec(
            JobKind.OCR,
            "late-success-project",
            1,
            "ocr",
            "v1",
            "model",
            "source",
            "settings",
            {},
        )
    )
    service.jobs.transition(job.id, JobState.START_REQUESTED)
    service.jobs.transition(job.id, JobState.RUNNING)
    service.jobs.transition(job.id, JobState.CANCEL_REQUESTED)
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service,
        "import_final_ocr_srt",
        lambda job_id, path: published.append((job_id, path)),
    )
    server = JobManagerServer(name=f"unused-late-success-{uuid4()}", service=service)
    try:
        server._persist_worker_event(
            job.id,
            WorkerMessage(
                1,
                job.id,
                "late-success-event",
                "succeeded",
                {"status": "PASS", "output_path": str(tmp_path / "late.srt")},
            ),
        )

        assert published == []
        assert service.jobs.get(job.id).state is JobState.CANCEL_REQUESTED
        ignored = service.jobs.latest_event(job.id, kind="late_success_ignored")
        assert ignored is not None
        assert ignored.payload["job_state"] == "cancel_requested"
    finally:
        server.close()


def test_server_rejects_retry_when_another_translation_job_is_active(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    project_id = "one-active-translation"
    candidate = service.jobs.create(
        JobSpec(
            JobKind.TRANSLATE,
            project_id,
            1,
            "gemini-web",
            "v1",
            "selected-on-web",
            "candidate-source",
            "candidate-settings",
            {},
        )
    )
    service.jobs.transition(candidate.id, JobState.START_REQUESTED)
    service.jobs.transition(candidate.id, JobState.RUNNING)
    service.jobs.transition(candidate.id, JobState.FAILED)
    active = service.jobs.create(
        JobSpec(
            JobKind.TRANSLATE,
            project_id,
            1,
            "gemini-web",
            "v1",
            "selected-on-web",
            "active-source",
            "active-settings",
            {},
        )
    )
    service.jobs.transition(active.id, JobState.START_REQUESTED)
    service.jobs.transition(active.id, JobState.RUNNING)
    server = JobManagerServer(name=f"unused-single-translate-{uuid4()}", service=service)
    try:
        with pytest.raises(ValueError, match="tác vụ dịch khác đang hoạt động"):
            server._retry_job(candidate.id)

        assert service.jobs.get(candidate.id).state is JobState.FAILED
        assert service.jobs.get(active.id).state is JobState.RUNNING
    finally:
        server.close()


def test_startup_abandons_lost_control_and_allows_checkpoint_retry(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    database_path = tmp_path / "state.db"
    artifact_root = tmp_path / "artifacts"
    original = JobManagerService(
        database_path=database_path,
        artifact_root=artifact_root,
    )
    created = original.create_job(
        {
            "kind": "ocr",
            "project_id": "project",
            "input_revision": 1,
            "provider_id": "paddle-fast",
            "provider_version": "3.2.0",
            "model_id": "PP-OCRv5-server",
            "input_fingerprint": "fixture",
            "settings_fingerprint": "settings-fixture",
            "provider_config": {"mode": "fast"},
        }
    )
    job_id = str(created["id"])
    job = original.jobs.get(job_id)
    original.checkpoints.commit(
        job_id=job_id,
        stage_key="runtime",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=job.spec.resume_fingerprint,
        cursor_us=73_000_000,
        payload={"completed_units": 73},
        event_id="checkpoint-before-manager-crash",
    )
    command_id, requested = original.jobs.request_control(
        job_id,
        action="start",
        target=JobState.START_REQUESTED,
    )
    assert requested.state is JobState.START_REQUESTED

    restarted = JobManagerService(
        database_path=database_path,
        artifact_root=artifact_root,
    )
    assert restarted.recover_startup() == 1
    assert restarted.recover_startup() == 0
    assert restarted.jobs.get(job_id).state is JobState.INTERRUPTED
    abandoned = [
        event for event in restarted.jobs.list_events(job_id) if event.kind == "control_abandoned"
    ]
    assert len(abandoned) == 1
    assert abandoned[0].payload == {
        "command_id": command_id,
        "action": "start",
        "reason": "worker_process_lost_on_restart",
    }

    server = JobManagerServer(name=f"unused-recovery-{uuid4()}", service=restarted)
    try:
        retried = server._dispatch("retry_job", {"job_id": job_id})
        assert retried["state"] == "queued"
        checkpoint = restarted.load_worker_checkpoint(job_id)
        assert checkpoint is not None
        assert checkpoint["cursor_us"] == 73_000_000
    finally:
        server.close()


def test_database_lock_rejects_a_second_job_manager(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    database = tmp_path / "state.db"
    first_server = f"ducai-lock-owner-{uuid4()}"
    first_command = [
        sys.executable,
        "-m",
        "ducai_studio.job_manager",
        "--server-name",
        first_server,
        "--database",
        str(database),
        "--artifacts",
        str(tmp_path / "artifacts"),
    ]
    first = subprocess.Popen(first_command)
    socket = QLocalSocket()
    try:
        connect_to_server(socket, first_server)

        second_command = first_command.copy()
        second_command[second_command.index(first_server)] = f"ducai-lock-contender-{uuid4()}"
        second = subprocess.run(second_command, timeout=5, check=False)
        assert second.returncode == 4

        send_request(socket, "shutdown-lock-owner", "shutdown", {})
        assert first.wait(timeout=8) == 0
    finally:
        socket.abort()
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)


def test_worker_controls_own_pause_resume_and_cancel_state(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    server_name = f"ducai-controls-{uuid4()}"
    command = [
        sys.executable,
        "-m",
        "ducai_studio.job_manager",
        "--server-name",
        server_name,
        "--database",
        str(tmp_path / "state.db"),
        "--artifacts",
        str(tmp_path / "artifacts"),
    ]
    process = subprocess.Popen(command)
    socket = QLocalSocket()
    try:
        connect_to_server(socket, server_name)
        job_id = create_job(socket, "create-controls")
        worker = Path(__file__).parents[1] / "fixtures" / "control_worker.py"
        started = send_request(
            socket,
            "start-controls",
            "start_job",
            {
                "job_id": job_id,
                "program": sys.executable,
                "arguments": [str(worker), job_id],
            },
        )
        assert started.payload["result"]["state"] == "start_requested"
        wait_for_job_state(socket, job_id, "running")

        requested_pause = send_request(
            socket, "pause-controls", "pause_job", {"job_id": job_id}
        )
        assert requested_pause.payload["result"]["state"] == "pause_requested"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            paused = send_request(socket, str(uuid4()), "get_job", {"job_id": job_id})
            if paused.payload["result"]["state"] == "paused":
                break
        assert paused.payload["result"]["state"] == "paused"

        resumed = send_request(socket, "resume-controls", "resume_job", {"job_id": job_id})
        assert resumed.payload["result"]["state"] == "resume_requested"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            running = send_request(socket, str(uuid4()), "get_job", {"job_id": job_id})
            if running.payload["result"]["state"] == "running":
                break
        assert running.payload["result"]["state"] == "running"
        requested_cancel = send_request(
            socket, "cancel-controls", "cancel_job", {"job_id": job_id}
        )
        assert requested_cancel.payload["result"]["state"] == "cancel_requested"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            cancelled = send_request(socket, str(uuid4()), "get_job", {"job_id": job_id})
            if cancelled.payload["result"]["state"] == "cancelled":
                break
        assert cancelled.payload["result"]["state"] == "cancelled"

        send_request(socket, "shutdown-controls", "shutdown", {})
        assert process.wait(timeout=8) == 0
    finally:
        socket.abort()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_cancelled_job_stays_cancelled_when_blocking_worker_misses_ack(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    server_name = f"ducai-cancel-ack-{uuid4()}"
    command = [
        sys.executable,
        "-m",
        "ducai_studio.job_manager",
        "--server-name",
        server_name,
        "--database",
        str(tmp_path / "state.db"),
        "--artifacts",
        str(tmp_path / "artifacts"),
    ]
    process = subprocess.Popen(command)
    socket = QLocalSocket()
    try:
        connect_to_server(socket, server_name)
        job_id = create_job(socket, "create-cancel-ack")
        worker = Path(__file__).parents[1] / "fixtures" / "exit_without_ack_worker.py"
        send_request(
            socket,
            "start-cancel-ack",
            "start_job",
            {
                "job_id": job_id,
                "program": sys.executable,
                "arguments": [str(worker), job_id],
            },
        )
        wait_for_job_state(socket, job_id, "running")
        requested = send_request(
            socket, "cancel-without-ack", "cancel_job", {"job_id": job_id}
        )
        assert requested.payload["result"]["state"] == "cancel_requested"

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            finished = send_request(socket, str(uuid4()), "get_job", {"job_id": job_id})
            if finished.payload["result"]["state"] != "cancel_requested":
                break
        assert finished.payload["result"]["state"] == "cancelled"
        assert finished.payload["result"]["error_code"] is None

        send_request(socket, "shutdown-cancel-ack", "shutdown", {})
        assert process.wait(timeout=8) == 0
    finally:
        socket.abort()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_control_outbox_retries_same_command_id_until_ack(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    server_name = f"ducai-control-retry-{uuid4()}"
    command = [
        sys.executable,
        "-m",
        "ducai_studio.job_manager",
        "--server-name",
        server_name,
        "--database",
        str(tmp_path / "state.db"),
        "--artifacts",
        str(tmp_path / "artifacts"),
    ]
    process = subprocess.Popen(command)
    socket = QLocalSocket()
    try:
        connect_to_server(socket, server_name)
        job_id = create_job(socket, "create-control-retry")
        worker = Path(__file__).parents[1] / "fixtures" / "retry_ack_worker.py"
        send_request(
            socket,
            "start-control-retry",
            "start_job",
            {
                "job_id": job_id,
                "program": sys.executable,
                "arguments": [str(worker), job_id],
            },
        )
        wait_for_job_state(socket, job_id, "running")
        requested = send_request(
            socket, "pause-control-retry", "pause_job", {"job_id": job_id}
        )
        assert requested.payload["result"]["state"] == "pause_requested"

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = send_request(socket, str(uuid4()), "get_job", {"job_id": job_id})
            if current.payload["result"]["state"] == "paused":
                break
        assert current.payload["result"]["state"] == "paused"

        events = send_request(
            socket, "control-retry-events", "list_events", {"job_id": job_id}
        ).payload["result"]
        requested_event = next(
            item
            for item in events
            if item["kind"] == "control_requested"
            and item["payload"]["action"] == "pause"
        )
        paused_event = next(item for item in events if item["kind"] == "paused")
        assert requested_event["event_id"] == paused_event["payload"]["command_id"]

        send_request(socket, "cancel-control-retry", "cancel_job", {"job_id": job_id})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = send_request(socket, str(uuid4()), "get_job", {"job_id": job_id})
            if current.payload["result"]["state"] == "cancelled":
                break
        assert current.payload["result"]["state"] == "cancelled"
        send_request(socket, "shutdown-control-retry", "shutdown", {})
        assert process.wait(timeout=8) == 0
    finally:
        socket.abort()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_pause_retry_never_force_stops_a_live_worker_during_blocking_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    created = service.create_job(
        {
            "kind": "export",
            "project_id": "project",
            "input_revision": 1,
            "provider_id": "ffmpeg",
            "provider_version": "8.1",
            "model_id": "h264_nvenc",
            "input_fingerprint": "fixture",
            "settings_fingerprint": "settings-fixture",
            "provider_config": {},
        }
    )
    job_id = str(created["id"])
    service.jobs.transition(job_id, JobState.START_REQUESTED)
    service.jobs.transition(job_id, JobState.RUNNING)
    command_id, _job = service.jobs.request_control(
        job_id,
        action="pause",
        target=JobState.PAUSE_REQUESTED,
    )
    server = JobManagerServer(name=f"unused-pause-blocking-{uuid4()}", service=service)
    sent: list[tuple[str, str, dict]] = []
    stopped: list[str] = []
    monkeypatch.setattr(server.workers, "has_worker", lambda _job_id: True)
    monkeypatch.setattr(
        server.workers,
        "send",
        lambda worker_job_id, *, kind, payload: sent.append(
            (worker_job_id, kind, payload)
        ),
    )
    monkeypatch.setattr(
        server.workers,
        "request_stop",
        lambda worker_job_id, **_kwargs: stopped.append(worker_job_id),
    )
    monkeypatch.setattr(QTimer, "singleShot", lambda *_args: None)
    server._control_retry_attempts[command_id] = 3

    try:
        server._retry_control(
            job_id,
            command_id,
            "pause",
            JobState.PAUSE_REQUESTED,
        )
        attempts = server._control_retry_attempts[command_id]
    finally:
        server.close()

    assert stopped == []
    assert sent == [
        (
            job_id,
            "control",
            {"action": "pause", "command_id": command_id},
        )
    ]
    assert attempts == 4


def test_start_retry_waits_for_slow_live_worker_without_sending_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    created = service.create_job(
        {
            "kind": "tts",
            "project_id": "project",
            "input_revision": 1,
            "provider_id": "vieneu-3.3",
            "provider_version": "3.3",
            "model_id": "voice",
            "input_fingerprint": "fixture",
            "settings_fingerprint": "settings-fixture",
            "provider_config": {},
        }
    )
    job_id = str(created["id"])
    command_id, _job = service.jobs.request_control(
        job_id,
        action="start",
        target=JobState.START_REQUESTED,
    )
    server = JobManagerServer(name=f"unused-slow-start-{uuid4()}", service=service)
    sent: list[dict[str, object]] = []
    stopped: list[str] = []
    scheduled: list[int] = []
    monkeypatch.setattr(server.workers, "has_worker", lambda _job_id: True)
    monkeypatch.setattr(
        server.workers,
        "send",
        lambda _job_id, *, kind, payload: sent.append({"kind": kind, **payload}),
    )
    monkeypatch.setattr(
        server.workers,
        "request_stop",
        lambda worker_job_id, **_kwargs: stopped.append(worker_job_id),
    )
    monkeypatch.setattr(
        QTimer,
        "singleShot",
        lambda delay, _callback: scheduled.append(int(delay)),
    )
    server._control_retry_attempts[command_id] = 12
    server._control_retry_started_at[command_id] = time.monotonic() - 31

    try:
        server._retry_control(
            job_id,
            command_id,
            "start",
            JobState.START_REQUESTED,
            {"job": {"kind": "tts"}},
        )
    finally:
        server.close()

    assert stopped == []
    assert sent == [
        {
            "kind": "control",
            "action": "start",
            "command_id": command_id,
            "job": {"kind": "tts"},
        }
    ]
    assert server._control_retry_attempts[command_id] == 13
    assert scheduled[-1] == 5_000
    delayed = [
        event
        for event in service.jobs.list_events(job_id)
        if event.kind == "worker_start_ack_delayed"
    ]
    assert len(delayed) == 1


def test_failed_control_dispatch_does_not_change_durable_state(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    raw_job = {
        "kind": "ocr",
        "project_id": "project",
        "input_revision": 1,
        "provider_id": "paddle-fast",
        "provider_version": "3.2.0",
        "model_id": "PP-OCRv5-server",
        "input_fingerprint": "fixture",
        "settings_fingerprint": "settings-fixture",
        "provider_config": {"mode": "fast"},
    }
    created = service.create_job(raw_job)
    job_id = str(created["id"])
    server = JobManagerServer(name=f"unused-{uuid4()}", service=service)
    try:
        service.jobs.transition(job_id, JobState.START_REQUESTED)
        service.jobs.transition(job_id, JobState.RUNNING)
        with pytest.raises(LookupError, match="Unknown worker"):
            server._pause_job(job_id)
        assert service.jobs.get(job_id).state is JobState.RUNNING

        service.jobs.transition(job_id, JobState.PAUSE_REQUESTED)
        service.jobs.transition(job_id, JobState.PAUSED)
        with pytest.raises(LookupError, match="Unknown worker"):
            server._resume_job(job_id)
        assert service.jobs.get(job_id).state is JobState.PAUSED

        active_without_worker = str(service.create_job(raw_job)["id"])
        service.jobs.transition(active_without_worker, JobState.START_REQUESTED)
        service.jobs.transition(active_without_worker, JobState.RUNNING)
        cancelled = server._cancel_job(active_without_worker)
        assert cancelled["state"] == "cancelled"
        assert "manager_reconciled_missing_worker" in {
            event.kind for event in service.jobs.list_events(active_without_worker)
        }
    finally:
        server.close()


def test_stderr_contents_are_not_persisted_and_prestart_failure_is_terminal(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    created = service.create_job(
        {
            "kind": "ocr",
            "project_id": "project",
            "input_revision": 1,
            "provider_id": "paddle-fast",
            "provider_version": "3.2.0",
            "model_id": "PP-OCRv5-server",
            "input_fingerprint": "fixture",
            "settings_fingerprint": "settings-fixture",
            "provider_config": {},
        }
    )
    job_id = str(created["id"])
    command_id, _job = service.jobs.request_control(
        job_id,
        action="start",
        target=JobState.START_REQUESTED,
    )
    server = JobManagerServer(name=f"unused-log-redaction-{uuid4()}", service=service)
    try:
        secret = "api_key=must-never-reach-sqlite"
        server._worker_log(job_id, f"provider warning: {secret}")
        log_event = next(
            event for event in service.jobs.list_events(job_id) if event.kind == "worker_stderr"
        )
        assert secret not in str(log_event.payload)
        assert log_event.payload["characters"] == len(f"provider warning: {secret}")

        server._worker_event(
            job_id,
            WorkerMessage(
                1,
                job_id,
                "failed-before-started",
                "failed",
                {"code": "provider_boot_failed", "command_id": command_id},
            ),
        )
        failed = service.jobs.get(job_id)
        assert failed.state is JobState.FAILED
        assert failed.error_code == "provider_boot_failed"
    finally:
        server.close()


def test_ipc_actions_and_worker_events_reject_silent_type_coercion(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    created = service.create_job(
        {
            "kind": "ocr",
            "project_id": "project",
            "input_revision": 1,
            "provider_id": "paddle-fast",
            "provider_version": "3.2.0",
            "model_id": "PP-OCRv5-server",
            "input_fingerprint": "fixture",
            "provider_config": {},
        }
    )
    job_id = str(created["id"])
    server = JobManagerServer(name=f"unused-strict-ipc-{uuid4()}", service=service)
    try:
        with pytest.raises(ValueError, match="job_id"):
            server._dispatch("get_job", {"job_id": None})
        with pytest.raises(ValueError, match="limit"):
            server._dispatch("list_jobs", {"limit": 1.9})

        service.jobs.request_control(
            job_id,
            action="start",
            target=JobState.START_REQUESTED,
        )
        server._worker_event(
            job_id,
            WorkerMessage(1, job_id, "bad-checkpoint", "checkpoint", {"cursor_us": 1.9}),
        )
        failed = service.jobs.get(job_id)
        assert failed.state is JobState.FAILED
        assert failed.error_code == "worker_error"
        assert "Invalid worker event" not in str(failed.error_payload)
        assert "Không thể xử lý phản hồi của tác vụ" in str(failed.error_payload)
    finally:
        server.close()


def test_secret_shaped_worker_failure_code_is_rejected_before_sqlite(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    created = service.create_job(
        {
            "kind": "ocr",
            "project_id": "project",
            "input_revision": 1,
            "provider_id": "paddle-fast",
            "provider_version": "3.2.0",
            "model_id": "PP-OCRv5-server",
            "input_fingerprint": "fixture",
            "provider_config": {},
        }
    )
    job_id = str(created["id"])
    service.jobs.request_control(
        job_id,
        action="start",
        target=JobState.START_REQUESTED,
    )
    server = JobManagerServer(name=f"unused-secret-error-code-{uuid4()}", service=service)
    try:
        secret_code = "sk-AAAAAAAAAAAAAAAAAAAA12345678"
        server._worker_event(
            job_id,
            WorkerMessage(1, job_id, "secret-code", "failed", {"code": secret_code}),
        )
        failed = service.jobs.get(job_id)
        assert failed.state is JobState.FAILED
        assert failed.error_code == "worker_error"
        with service.database.connect(read_only=True) as connection:
            stored = connection.execute(
                "SELECT error_code, error_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert secret_code not in f"{stored['error_code']} {stored['error_json']}"
    finally:
        server.close()


def test_project_import_actions_persist_auto_video_audio_tracks(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-project-import-{uuid4()}", service=service)
    video = tmp_path / "video có dấu cách.mp4"
    video.write_bytes(b"fixture")
    try:
        created = server._dispatch(
            "create_project",
            {"project_path": str(tmp_path / "project.ducai"), "name": "Phim thử"},
        )
        updated = server._dispatch(
            "import_media",
            {"project_id": created["id"], "media_path": str(video)},
        )
        loaded = server._dispatch("get_project", {"project_id": created["id"]})

        assert updated == loaded
        assert updated["revision"] == 2
        assert [track["kind"] for track in updated["tracks"]] == [
            "video",
            "original_audio",
        ]
        assert updated["tracks"][0]["clips"][0]["volume"] == 0.0
        assert updated["tracks"][0]["clips"][0]["link_group_id"] == (
            updated["tracks"][1]["clips"][0]["link_group_id"]
        )
        clip_id = updated["tracks"][0]["clips"][0]["id"]
        slowed = server._dispatch(
            "timeline_command",
            {
                "project_id": created["id"],
                "command": "speed",
                "clip_id": clip_id,
                "value": 85,
                "apply_linked": True,
            },
        )
        assert slowed["tracks"][0]["clips"][0]["speed"] == {
            "numerator": 17,
            "denominator": 20,
        }
        assert slowed["tracks"][0]["clips"][0]["timeline_range"] == (
            slowed["tracks"][1]["clips"][0]["timeline_range"]
        )
        muted = server._dispatch(
            "timeline_command",
            {
                "project_id": created["id"],
                "command": "track_mute",
                "track_id": "A1",
                "value": True,
            },
        )
        assert muted["tracks"][1]["muted"] is True
        locked = server._dispatch(
            "timeline_command",
            {
                "project_id": created["id"],
                "command": "track_lock",
                "track_id": "V1",
                "value": True,
            },
        )
        assert locked["tracks"][0]["locked"] is True
        with pytest.raises(ValueError, match="track_mute requires track_id"):
            server._dispatch(
                "timeline_command",
                {
                    "project_id": created["id"],
                    "command": "track_mute",
                    "value": True,
                },
            )
        unlocked = server._dispatch(
            "timeline_command",
            {"project_id": created["id"], "command": "undo"},
        )
        assert unlocked["tracks"][0]["locked"] is False
        unmuted = server._dispatch(
            "timeline_command",
            {"project_id": created["id"], "command": "undo"},
        )
        assert unmuted["tracks"][1]["muted"] is False
        speed_undone = server._dispatch(
            "timeline_command",
            {"project_id": created["id"], "command": "undo"},
        )
        assert speed_undone["tracks"][0]["clips"][0]["speed"] == {
            "numerator": 1,
            "denominator": 1,
        }
        with pytest.raises(ValueError, match="timeline_start_us"):
            server._dispatch(
                "import_media",
                {
                    "project_id": created["id"],
                    "media_path": str(video),
                    "timeline_start_us": 1.9,
                },
            )
    finally:
        server.close()


def test_legacy_project_actions_offer_read_only_dry_run_then_one_way_import(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-legacy-import-{uuid4()}", service=service)
    video = tmp_path / "legacy-video.mp4"
    video.write_bytes(b"fixture")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        '{"version":"0.1.99","media_items":[{"path":"legacy-video.mp4"}]}',
        encoding="utf-8",
    )
    before = legacy.read_bytes()
    try:
        report = server._dispatch(
            "inspect_legacy_project", {"source_path": str(legacy)}
        )
        imported = server._dispatch(
            "import_legacy_project",
            {
                "source_path": str(legacy),
                "project_path": str(tmp_path / "imported.ducai"),
                "name": "Dự án cũ",
            },
        )

        assert report["dry_run"] is True
        assert report["source_unchanged"] is True
        assert imported["report"]["dry_run"] is False
        assert imported["project"]["name"] == "Dự án cũ"
        assert imported["project"]["tracks"][0]["kind"] == "video"
        assert legacy.read_bytes() == before
        assert service.projects.load(imported["project"]["id"]).name == "Dự án cũ"
    finally:
        server.close()


def test_ocr_job_uses_external_runtime_and_imports_only_guarded_final_srt(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-ocr-create-{uuid4()}", service=service)
    video = tmp_path / "phim.mp4"
    video.write_bytes(b"fixture")
    final = tmp_path / "phim_ZH_FINAL.srt"
    try:
        project = server._dispatch(
            "create_project",
            {"project_path": str(tmp_path / "project.ducai"), "name": "Phim thử"},
        )
        server._dispatch(
            "import_media", {"project_id": project["id"], "media_path": str(video)}
        )
        job = server._dispatch(
            "create_ocr_job",
            {
                "project_id": project["id"],
                "sample_interval_us": 800_000,
                "engine_python": sys.executable,
                "device": "gpu:0",
            },
        )
        config = job["spec"]["provider_config"]
        assert config["output_path"] == str(final)
        assert [band["name"] for band in config["bands"]] == ["upper", "lower"]
        assert len(config["engine_runtime_fingerprint"]) == 64
        assert len(config["model_fingerprint"]) == 64
        assert len(config["engine_host_sha256"]) == 64
        final.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n我要修炼成仙\n",
            encoding="utf-8-sig",
        )
        qa = final.with_name("phim_ZH_FINAL_QA.json")
        qa.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": "ocr_hard_sub_coverage",
                    "status": "PASS",
                    "job_id": job["id"],
                    "input_fingerprint": job["spec"]["input_fingerprint"],
                    "settings_fingerprint": job["spec"]["settings_fingerprint"],
                    "coverage_threshold": 0.95,
                    "coverage_ratio": 1.0,
                    "subtitle_positive_timestamps": 2,
                    "covered_timestamps": 2,
                    "continuity_gap_limit_us": 120_000_000,
                    "maximum_positive_gap_us": 2_000_000,
                    "timeline_continuity_passes": True,
                    "output_path": str(final),
                    "output_sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        imported = service.import_final_ocr_srt(job["id"], str(final))

        assert len(imported["source_cues"]) == 1
        assert imported["source_cues"][0]["text"] == "我要修炼成仙"
        cue_id = imported["source_cues"][0]["id"]
        rescan = server._dispatch(
            "create_ocr_rescan_job",
            {
                "project_id": project["id"],
                "cue_id": cue_id,
                "sample_interval_us": 400_000,
                "engine_python": sys.executable,
                "device": "gpu:0",
            },
        )
        rescanned = service.apply_ocr_rescan(
            rescan["id"],
            {
                "text": "我要认真修炼成仙",
                "confidence": 0.98,
                "evidence_ids": ["ocr-rescan-a", "ocr-rescan-b"],
                "source": "paddle-quality:targeted-rescan",
            },
        )
        assert rescanned["source_cues"][0]["text"] == "我要认真修炼成仙"
        assert rescanned["source_cues"][0]["range"] == {
            "start_us": 0,
            "end_us": 2_000_000,
        }
        with pytest.raises(ValueError, match="guarded final output"):
            service.import_final_ocr_srt(job["id"], str(video))
    finally:
        server.close()


def test_ocr_rerun_promotes_candidate_over_existing_s1_without_duplicate_track(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    video = tmp_path / "phim.mp4"
    video.write_bytes(b"fixture")
    active = tmp_path / "phim_ZH_FINAL.srt"
    active.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n",
        encoding="utf-8-sig",
    )
    project = service.create_project(
        project_path=str(tmp_path / "project.ducai"),
        name="Phim thử",
    )
    service.import_media(project_id=project["id"], media_path=str(video))
    service.import_media(project_id=project["id"], media_path=str(active))
    job = service.create_ocr_job(
        project_id=project["id"],
        sample_interval_us=800_000,
        engine_python=sys.executable,
    )
    config = job["spec"]["provider_config"]
    candidate = Path(config["staging_output_path"])
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n新字幕完整\n",
        encoding="utf-8-sig",
    )
    qa = candidate.with_name(candidate.stem + "_QA.json")
    qa.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "ocr_hard_sub_coverage",
                "status": "PASS",
                "job_id": job["id"],
                "input_fingerprint": job["spec"]["input_fingerprint"],
                "settings_fingerprint": job["spec"]["settings_fingerprint"],
                "coverage_threshold": 0.95,
                "coverage_ratio": 1.0,
                "subtitle_positive_timestamps": 2,
                "covered_timestamps": 2,
                "continuity_gap_limit_us": 120_000_000,
                "maximum_positive_gap_us": 2_000_000,
                "timeline_continuity_passes": True,
                "output_path": str(candidate),
                "output_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    imported = service.import_final_ocr_srt(job["id"], str(candidate))

    assert "新字幕完整" in active.read_text(encoding="utf-8-sig")
    assert len([track for track in imported["tracks"] if track["id"] == "S1"]) == 1
    assert [cue["text"] for cue in imported["source_cues"]] == ["新字幕完整"]
    assert not candidate.exists()


def test_job_manager_rejects_final_ocr_srt_without_matching_coverage_qa(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    video = tmp_path / "phim.mp4"
    video.write_bytes(b"fixture")
    project = service.create_project(
        project_path=str(tmp_path / "project.ducai"), name="Phim thử"
    )
    service.import_media(project_id=project["id"], media_path=str(video))
    job = service.create_ocr_job(
        project_id=project["id"],
        sample_interval_us=800_000,
        engine_python=sys.executable,
    )
    final = tmp_path / "phim_ZH_FINAL.srt"
    final.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n我要修炼成仙\n",
        encoding="utf-8-sig",
    )

    with pytest.raises(ValueError, match="coverage QA"):
        service.import_final_ocr_srt(job["id"], str(final))

    qa = final.with_name("phim_ZH_FINAL_QA.json")
    qa.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ocr_hard_sub_coverage",
                "status": "PASS",
                "job_id": job["id"],
                "input_fingerprint": job["spec"]["input_fingerprint"],
                "settings_fingerprint": job["spec"]["settings_fingerprint"],
                "coverage_threshold": 0.95,
                "coverage_ratio": 0.94,
                "subtitle_positive_timestamps": 100,
                "covered_timestamps": 94,
                "output_path": str(final),
                "output_sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="coverage QA"):
        service.import_final_ocr_srt(job["id"], str(final))

    qa_payload = json.loads(qa.read_text(encoding="utf-8"))
    qa_payload["coverage_ratio"] = 1.0
    qa_payload["covered_timestamps"] = 100
    qa_payload["output_sha256"] = "0" * 64
    qa.write_text(json.dumps(qa_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="coverage QA"):
        service.import_final_ocr_srt(job["id"], str(final))


def test_media_usage_inspection_finds_the_existing_project_without_mutation(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-media-usage-{uuid4()}", service=service)
    video = tmp_path / "Phim đã có.mp4"
    video.write_bytes(b"fixture")
    try:
        project = server._dispatch(
            "create_project",
            {"project_path": str(tmp_path / "project.ducai"), "name": "Bản dựng cũ"},
        )
        imported = server._dispatch(
            "import_media", {"project_id": project["id"], "media_path": str(video)}
        )
        revision_before = imported["revision"]

        report = server._dispatch(
            "inspect_media_usage", {"media_path": str(video)}
        )

        assert report["exists"] is True
        assert report["media_path"] == str(video.resolve())
        assert report["usages"] == [
            {
                "project_id": project["id"],
                "project_name": "Bản dựng cũ",
                "project_path": str((tmp_path / "project.ducai").resolve()),
                "updated_at": report["usages"][0]["updated_at"],
                "media_count": 1,
                "media_kinds": ["video"],
            }
        ]
        assert service.projects.load(project["id"]).revision == revision_before
    finally:
        server.close()


def test_subtitle_commands_persist_split_text_time_and_undo(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-subtitle-edit-{uuid4()}", service=service)
    subtitle = tmp_path / "source.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n我要开始修炼，然后成仙\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n第二句话\n",
        encoding="utf-8-sig",
    )
    try:
        project = server._dispatch(
            "create_project",
            {"project_path": str(tmp_path / "project.ducai"), "name": "Phim thử"},
        )
        imported = server._dispatch(
            "import_media",
            {"project_id": project["id"], "media_path": str(subtitle)},
        )
        cue_id = imported["source_cues"][0]["id"]
        split = server._dispatch(
            "subtitle_command",
            {
                "project_id": project["id"],
                "command": "split",
                "cue_id": cue_id,
                "language": "source",
                "values": {"split_us": 1_000_000},
            },
        )
        assert len(split["source_cues"]) == 3
        assert split["source_cues"][0]["id"] == cue_id
        assert split["_subtitle_can_undo"] is True

        updated = server._dispatch(
            "subtitle_command",
            {
                "project_id": project["id"],
                "command": "update_text",
                "cue_id": cue_id,
                "values": {"text": "我要认真修炼"},
            },
        )
        assert updated["source_cues"][0]["text"] == "我要认真修炼"
        undone = server._dispatch(
            "subtitle_command",
            {"project_id": project["id"], "command": "undo"},
        )
        assert undone["source_cues"][0]["text"] != "我要认真修炼"
        assert undone["revision"] > updated["revision"]
        head_after_undo = service.projects.load(project["id"])
        assert head_after_undo.revision == undone["revision"]
        redone = server._dispatch(
            "subtitle_command",
            {"project_id": project["id"], "command": "redo"},
        )
        assert redone["source_cues"][0]["text"] == "我要认真修炼"
        assert redone["revision"] > undone["revision"]
        assert service.projects.load(project["id"]).revision == redone["revision"]
    finally:
        server.close()


def test_subtitle_find_replace_and_delete_use_compact_paired_patch(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-subtitle-compact-{uuid4()}", service=service)
    source = tmp_path / "source.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n第一句话\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n第二句话\n",
        encoding="utf-8-sig",
    )
    translated = tmp_path / "translated.srt"
    translated.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nĐây là một chiếnhạm\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nMột hạm đội mới\n",
        encoding="utf-8-sig",
    )
    try:
        project = server._dispatch(
            "create_project",
            {"project_path": str(tmp_path / "project.ducai"), "name": "Phim thử"},
        )
        imported = server._dispatch(
            "import_media",
            {"project_id": project["id"], "media_path": str(source)},
        )
        server._dispatch(
            "import_translation_srt",
            {"project_id": project["id"], "subtitle_path": str(translated)},
        )

        replaced = server._dispatch(
            "subtitle_command",
            {
                "project_id": project["id"],
                "command": "find_replace",
                "language": "translated",
                "compact": True,
                "values": {
                    "find": "hạm",
                    "replacement": " hạm",
                    "expected_count": 2,
                },
            },
        )

        assert replaced["_subtitle_patch"] is True
        assert "tracks" not in replaced
        assert [cue["text"] for cue in replaced["source_cues"]] == [
            "第一句话",
            "第二句话",
        ]
        assert [cue["text"] for cue in replaced["translated_cues"]] == [
            "Đây là một chiến hạm",
            "Một  hạm đội mới",
        ]

        deleted = server._dispatch(
            "subtitle_command",
            {
                "project_id": project["id"],
                "command": "delete",
                "cue_id": imported["source_cues"][0]["id"],
                "compact": True,
            },
        )

        assert len(deleted["source_cues"]) == 1
        assert len(deleted["translated_cues"]) == 1
        assert deleted["source_cues"][0]["index"] == 1
        assert deleted["translated_cues"][0]["index"] == 1
        undone = server._dispatch(
            "subtitle_command",
            {
                "project_id": project["id"],
                "command": "undo",
                "compact": True,
            },
        )
        assert len(undone["source_cues"]) == 2
        assert len(undone["translated_cues"]) == 2
    finally:
        server.close()


def test_job_manager_runs_ocr_worker_and_auto_imports_pass_result(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-ocr-worker-{uuid4()}", service=service)
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_ocr_engine_host.py"
    try:
        project = service.create_project(
            project_path=str(tmp_path / "project.ducai"), name="Phim thử"
        )
        service.import_media(project_id=project["id"], media_path=str(video))
        job = service.create_ocr_job(
            project_id=project["id"],
            sample_interval_us=800_000,
            engine_python=sys.executable,
            engine_host_path=str(fake_host),
        )
        server._dispatch(
            "start_job",
            {
                "job_id": job["id"],
                "program": sys.executable,
                "arguments": [
                    "-m",
                    "ducai_studio.workers.ocr_worker",
                    job["id"],
                ],
                "resume_stage_key": "ocr",
            },
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            application.processEvents()
            state = service.jobs.get(job["id"]).state
            if state in {JobState.SUCCEEDED, JobState.FAILED}:
                break
            time.sleep(0.01)

        assert service.jobs.get(job["id"]).state is JobState.SUCCEEDED
        loaded = service.get_project(project["id"])
        assert len(loaded["source_cues"]) == 1
        assert loaded["source_cues"][0]["text"] == "我要修炼成仙"
        assert (tmp_path / "fixture_ZH_FINAL.srt").is_file()
        qa_path = tmp_path / "fixture_ZH_FINAL_QA.json"
        assert qa_path.is_file()
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        assert qa["status"] == "PASS"
        assert qa["coverage_ratio"] >= 0.95
        checkpoint = service.load_worker_checkpoint(
            job["id"], stage_key="ocr", checkpoint_key="latest"
        )
        assert checkpoint is not None
        assert Path(checkpoint["artifact"]["uri"]).is_file()
    finally:
        server.close()


def test_job_manager_runs_ocr_worker_and_publishes_review_srt_with_immutable_qa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-ocr-review-worker-{uuid4()}", service=service)
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_ocr_engine_host.py"
    monkeypatch.setenv("DUCAI_FAKE_OCR_UNMATCHED_LAST", "1")
    try:
        project = service.create_project(
            project_path=str(tmp_path / "project.ducai"), name="Phim thử"
        )
        service.import_media(project_id=project["id"], media_path=str(video))
        job = service.create_ocr_job(
            project_id=project["id"],
            sample_interval_us=800_000,
            engine_python=sys.executable,
            engine_host_path=str(fake_host),
        )
        server._dispatch(
            "start_job",
            {
                "job_id": job["id"],
                "program": sys.executable,
                "arguments": ["-m", "ducai_studio.workers.ocr_worker", job["id"]],
                "resume_stage_key": "ocr",
            },
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            application.processEvents()
            if service.jobs.get(job["id"]).state in {JobState.SUCCEEDED, JobState.FAILED}:
                break
            time.sleep(0.01)

        assert service.jobs.get(job["id"]).state is JobState.FAILED
        assert not (tmp_path / "fixture_ZH_FINAL.srt").exists()
        review = service.load_worker_checkpoint(
            job["id"], stage_key="ocr", checkpoint_key="review"
        )
        review_qa = service.load_worker_checkpoint(
            job["id"], stage_key="ocr", checkpoint_key="review-qa"
        )
        assert review is not None
        assert review_qa is not None
        assert review["artifact"]["kind"] == "ocr_review_srt"
        assert review_qa["artifact"]["kind"] == "ocr_qa_json"
        qa = json.loads(Path(review_qa["artifact"]["uri"]).read_text(encoding="utf-8"))
        assert qa["status"] == "REVIEW"
        assert qa["coverage_ratio"] < 0.95
        assert any(item.startswith("ocr_coverage_below_threshold") for item in qa["blockers"])
        loaded = service.get_project(project["id"])
        assert loaded["source_cues"][0]["source"] == "ocr_review"
    finally:
        server.close()


def test_review_checkpoint_is_imported_for_manual_qa_without_final_name(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-review-import-{uuid4()}", service=service)
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    try:
        project = service.create_project(
            project_path=str(tmp_path / "project.ducai"), name="Phim thử"
        )
        service.import_media(project_id=project["id"], media_path=str(video))
        job = service.create_ocr_job(
            project_id=project["id"],
            sample_interval_us=800_000,
            engine_python=sys.executable,
        )
        service.jobs.transition(job["id"], JobState.START_REQUESTED)
        service.jobs.transition(job["id"], JobState.RUNNING)
        review = service.checkpoints.staging_root / "review.srt.partial"
        review.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n需要人工检查\n",
            encoding="utf-8-sig",
        )
        review_sha256 = hashlib.sha256(review.read_bytes()).hexdigest()
        service.checkpoints.commit(
            job_id=job["id"],
            stage_key="ocr",
            checkpoint_key="review",
            schema_version=1,
            resume_fingerprint=service.jobs.get(job["id"]).spec.resume_fingerprint,
            cursor_us=2_000_000,
            payload={"stage": "review", "blockers": ["cue_requires_review"]},
            artifact_path=review,
            artifact_kind="ocr_review_srt",
        )
        review_qa = service.checkpoints.staging_root / "review_QA.json.partial"
        review_qa.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": "ocr_hard_sub_coverage",
                    "status": "REVIEW",
                    "job_id": job["id"],
                    "input_fingerprint": job["spec"]["input_fingerprint"],
                    "settings_fingerprint": job["spec"]["settings_fingerprint"],
                    "coverage_threshold": 0.95,
                    "coverage_ratio": 0.9,
                    "subtitle_positive_timestamps": 10,
                    "covered_timestamps": 9,
                    "blockers": ["cue_requires_review"],
                    "output_path": str(review),
                    "output_sha256": review_sha256,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        service.checkpoints.commit(
            job_id=job["id"],
            stage_key="ocr",
            checkpoint_key="review-qa",
            schema_version=1,
            resume_fingerprint=service.jobs.get(job["id"]).spec.resume_fingerprint,
            cursor_us=2_000_000,
            payload={"stage": "review", "status": "REVIEW"},
            artifact_path=review_qa,
            artifact_kind="ocr_qa_json",
        )

        server._persist_worker_event(
            job["id"],
            WorkerMessage(
                1,
                job["id"],
                "review-failed",
                "failed",
                {"code": "ocr_review_required"},
            ),
        )

        assert service.jobs.get(job["id"]).state is JobState.FAILED
        loaded = service.get_project(project["id"])
        assert loaded["source_cues"][0]["text"] == "需要人工检查"
        assert loaded["source_cues"][0]["source"] == "ocr_review"
        assert not (tmp_path / "fixture_ZH_FINAL.srt").exists()
    finally:
        server.close()
