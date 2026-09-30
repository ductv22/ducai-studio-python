import base64
import hashlib
import json
import subprocess
import sys
import time
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtNetwork import QLocalSocket

from ducai_studio.domain.jobs import JobKind, JobSpec, JobState
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    Overlay,
    OverlayKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
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


def test_ocr_regions_replace_repeat_proven_watermark_masks_without_touching_manual_blur(
) -> None:
    manual = Overlay(
        "manual-blur",
        OverlayKind.BLUR,
        TimeRange(0, 5_000_000),
        0.1,
        0.8,
        0.8,
        0.1,
    )
    old_generated = Overlay(
        "overlay-ocr-watermark-1",
        OverlayKind.BLUR,
        TimeRange(0, 5_000_000),
        0.01,
        0.1,
        0.2,
        0.08,
        tracking_mode="ocr_watermark",
        concealment_mode="cover",
    )
    manual_with_reserved_prefix = Overlay(
        "overlay-ocr-watermark-user",
        OverlayKind.BLUR,
        TimeRange(0, 5_000_000),
        0.2,
        0.2,
        0.2,
        0.08,
        tracking_mode="fixed",
    )
    project = ProjectSnapshot(
        "ocr-mask-project",
        "OCR mask project",
        1,
        source_cues=(
            SubtitleCue("cue-1", 1, TimeRange(0, 1_000_000), "第一句", "zh"),
        ),
        overlays=(manual, manual_with_reserved_prefix, old_generated),
    )

    legacy = service_module._apply_ocr_cue_regions(project, {"cue_regions": []})
    assert legacy.overlays == project.overlays

    updated = service_module._apply_ocr_cue_regions(
        project,
        {
            "cue_regions": [],
            "watermark_regions": [
                {
                    "start_us": 0,
                    "end_us": 5_000_000,
                    "box": [0.72, 0.08, 0.26, 0.09],
                    "sample_count": 3,
                    "source": "ocr_bilibili_anchor",
                }
            ],
        },
    )

    assert updated.overlays[0] == manual
    assert updated.overlays[1] == manual_with_reserved_prefix
    assert len(updated.overlays) == 3
    generated = updated.overlays[2]
    assert generated.id == "overlay-ocr-watermark-1"
    assert generated.tracking_mode == "ocr_watermark"
    assert generated.concealment_mode == "cover"
    assert generated.blur_radius == 46
    assert (generated.x, generated.y, generated.width, generated.height) == (
        0.72,
        0.08,
        0.26,
        0.09,
    )

    cleared = service_module._apply_ocr_cue_regions(
        updated,
        {"cue_regions": [], "watermark_regions": []},
    )
    assert cleared.overlays == (manual, manual_with_reserved_prefix)


def test_ocr_regions_reject_unproven_watermark_masks() -> None:
    project = ProjectSnapshot("ocr-mask-proof", "OCR proof", 1)

    for row in (
        {
            "start_us": 0,
            "end_us": 5_000_000,
            "box": [0.72, 0.08, 0.26, 0.09],
            "sample_count": 1,
            "source": "ocr_bilibili_anchor",
        },
        {
            "start_us": 0,
            "end_us": 5_000_000,
            "box": [0.72, 0.08, 0.26, 0.09],
            "sample_count": 2,
            "source": "ocr_bilibili_anchor",
        },
        {
            "start_us": 0,
            "end_us": 5_000_000,
            "box": [0.72, 0.08, 0.26, 0.09],
            "sample_count": 3,
            "source": "untrusted",
        },
    ):
        updated = service_module._apply_ocr_cue_regions(
            project,
            {"cue_regions": [], "watermark_regions": [row]},
        )
        assert updated.overlays == ()


def test_effect_command_returns_only_compact_overlay_patch(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    overlay = Overlay(
        "overlay-default-blur",
        OverlayKind.BLUR,
        TimeRange(0, 5_000_000),
        0.05,
        0.8,
        0.9,
        0.15,
        blur_radius=22,
    )
    snapshot = ProjectSnapshot(
        "project-effect-patch",
        "Effect patch",
        1,
        source_cues=(
            SubtitleCue("cue-1", 1, TimeRange(0, 1_000_000), "第一句", "zh"),
        ),
        overlays=(overlay,),
    )
    service.projects.save(project_path=tmp_path / "effect.ducai", snapshot=snapshot)

    result = service.effect_command(
        project_id=snapshot.id,
        command="update",
        overlay_id=overlay.id,
        values={"blur_radius": 31, "concealment_mode": "cover"},
    )

    assert result == {
        "_effect_patch": True,
        "id": snapshot.id,
        "revision": 2,
        "overlay_upserts": [
            {
                "id": overlay.id,
                "kind": "blur",
                "range": {"start_us": 0, "end_us": 5_000_000},
                "x": 0.05,
                "y": 0.8,
                "width": 0.9,
                "height": 0.15,
                "asset_id": None,
                "enabled": True,
                "opacity": 1.0,
                "blur_radius": 31,
                "tracking_mode": "fixed",
                "padding_x": 0.012,
                "padding_y": 0.008,
                "concealment_mode": "cover",
            }
        ],
        "removed_overlay_ids": [],
        "media_upserts": [],
        "removed_media_ids": [],
    }
    assert "source_cues" not in result
    assert "tracks" not in result


def test_progress_event_never_decodes_full_job_and_multi_broadcasts_once(tmp_path, monkeypatch):
    _application = QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts")
    created = service.create_job({
        "kind": "tts", "project_id": "project", "input_revision": 1,
        "provider_id": "vieneu-3.8", "provider_version": "3.8.1",
        "model_id": "v3turbo", "input_fingerprint": "fixture",
        "settings_fingerprint": "fixture", "provider_config": {},
    })
    job_id = str(created["id"])
    server = JobManagerServer(name=f"progress-{uuid4()}", service=service)
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("progress must not decode full job configuration")

        monkeypatch.setattr(service.jobs, "get", forbidden)
        server._worker_event(job_id, WorkerMessage(1, job_id, "manual", "progress", {
            "current": 32, "total": 8587, "phase": "synthesis",
        }))
        assert service.jobs.list_events(job_id)[-1].kind == "progress"
        calls = []
        monkeypatch.setattr(
            server.multi, "on_worker_event", lambda *args: calls.append(args) or True
        )
        monkeypatch.setattr(server.multi, "_publish_status", forbidden)
        server._worker_event(job_id, WorkerMessage(1, job_id, "multi", "progress", {
            "current": 64, "total": 8587, "phase": "synthesis",
        }))
        assert len(calls) == 1
        assert service.jobs.list_events(job_id)[-1].payload["current"] == 64
    finally:
        server.close()


def test_export_subtitle_srt_uses_latest_snapshot_and_does_not_mutate_project(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    source = SubtitleCue(
        "source-1",
        7,
        TimeRange(1_234_000, 3_456_000),
        "第一行\n第二行",
        "zh",
    )
    translated = SubtitleCue(
        "translated-1",
        7,
        TimeRange(1_234_000, 3_456_000),
        "Dòng một\nDòng hai đã sửa",
        "vi",
        revision=2,
    )
    scene_source = SubtitleCue(
        "scene-2",
        8,
        TimeRange(2_000_000, 3_000_000),
        "海王兽",
        "zh",
        role="scene_text",
        orientation="vertical",
        style_hint="calligraphy",
    )
    scene_translated = replace(scene_source, text="Hải Vương Thú", language="vi")
    snapshot = ProjectSnapshot(
        "project-srt-export",
        "SRT export",
        4,
        source_cues=(source, scene_source),
        translated_cues=(translated, scene_translated),
    )
    project_path = tmp_path / "project.ducai"
    service.projects.save(project_path=project_path, snapshot=snapshot)
    before = service.projects.load(snapshot.id)
    output = tmp_path / "edited-vi.srt"
    output.write_text("stale", encoding="utf-8")

    result = service.export_subtitle_srt(
        project_id=snapshot.id,
        language="translated",
        output_path=str(output),
    )

    assert result == {
        "project_id": snapshot.id,
        "revision": 4,
        "language": "translated",
        "path": str(output.resolve()),
        "cue_count": 1,
    }
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert output.read_text(encoding="utf-8-sig") == (
        "7\n00:00:01,234 --> 00:00:03,456\nDòng một\nDòng hai đã sửa\n"
    )
    assert service.projects.load(snapshot.id) == before
    assert service.projects.get_summary(snapshot.id)["head_revision"] == 4


def test_export_subtitle_srt_validates_language_extension_and_empty_track(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    snapshot = ProjectSnapshot("project-empty-srt", "Empty SRT", 1)
    service.projects.save(project_path=tmp_path / "empty.ducai", snapshot=snapshot)

    with pytest.raises(ValueError, match="phụ đề nguồn"):
        service.export_subtitle_srt(
            project_id=snapshot.id,
            language="source",
            output_path=str(tmp_path / "source.srt"),
        )
    with pytest.raises(ValueError, match="đuôi .srt"):
        service.export_subtitle_srt(
            project_id=snapshot.id,
            language="source",
            output_path=str(tmp_path / "source.txt"),
        )
    with pytest.raises(ValueError, match="phụ đề nguồn hoặc tiếng Việt"):
        service.export_subtitle_srt(
            project_id=snapshot.id,
            language="english",
            output_path=str(tmp_path / "source.srt"),
        )


def test_server_dispatches_live_srt_export(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    snapshot = ProjectSnapshot(
        "project-server-export",
        "Server export",
        1,
        source_cues=(
            SubtitleCue("source", 1, TimeRange(0, 1_000_000), "你好", "zh"),
        ),
    )
    service.projects.save(project_path=tmp_path / "server.ducai", snapshot=snapshot)
    server = JobManagerServer(name=f"unused-srt-export-{uuid4()}", service=service)
    output = tmp_path / "source.srt"
    try:
        result = server._dispatch(
            "export_subtitle_srt",
            {
                "project_id": snapshot.id,
                "language": "source",
                "output_path": str(output),
            },
        )
    finally:
        server.close()

    assert result["cue_count"] == 1
    assert output.read_text(encoding="utf-8-sig").endswith("你好\n")


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
            "vieneu-3.8",
            "3.8.1",
            "v3turbo",
            "tts-source",
            "tts-settings",
            {
                "engine_host_path": str(old_host),
                "engine_host_sha256": "old-release-host",
                "runtime_python": sys.executable,
                "data_root": str(tmp_path),
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
    assert environment["DUCAI_TTS_WARM_PROTOCOL"] == "1"
    assert environment["DUCAI_TTS_WARM_ENDPOINT"].startswith("127.0.0.1:")
    assert len(environment["DUCAI_TTS_WARM_TOKEN"]) >= 32
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
            "vieneu-3.8",
            "3.8.1",
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
    assert environment["DUCAI_TTS_WARM_PROTOCOL"] == ""
    assert environment["DUCAI_TTS_WARM_TOKEN"] == ""
    assert service.jobs.get(job.id).spec.provider_config["engine_host_path"] == str(custom_host)


def test_start_tts_job_rejects_legacy_vieneu_checkpoint(tmp_path: Path) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
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
            {},
        )
    )
    server = JobManagerServer(name=f"unused-legacy-vieneu-{uuid4()}", service=service)
    try:
        with pytest.raises(ValueError, match="không thể tiếp tục bằng engine 3.8.1"):
            server._start_job(
                {
                    "job_id": job.id,
                    "program": sys.executable,
                    "arguments": ["-m", "ducai_studio.workers.tts_worker", job.id],
                }
            )
    finally:
        server.close()


def test_retry_tts_job_rejects_legacy_vieneu_without_mutating_state(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
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
            {},
        )
    )
    service.transition_job(job.id, JobState.START_REQUESTED.value)
    service.jobs.append_worker_event(
        event_id=str(uuid4()),
        job_id=job.id,
        kind="fixture_failed",
        payload={"message": "fixture"},
        transitions={JobState.START_REQUESTED: JobState.FAILED},
        error_code="fixture",
        error_payload={"message": "fixture"},
    )
    server = JobManagerServer(name=f"unused-legacy-vieneu-retry-{uuid4()}", service=service)
    try:
        with pytest.raises(ValueError, match="không thể tiếp tục bằng engine 3.8.1"):
            server._retry_job(job.id)
    finally:
        server.close()

    assert service.jobs.get(job.id).state is JobState.FAILED


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
            "provider_id": "vieneu-3.8",
            "provider_version": "3.8.1",
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


def test_update_install_reservation_is_global_and_blocks_work_admission(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    server = JobManagerServer(name=f"unused-update-reservation-{uuid4()}", service=service)
    raw_job = {
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
    try:
        reserved = server._dispatch("reserve_update_install", {})
        assert reserved["eligible"] is True
        assert reserved["reserved"] is True
        token = str(reserved["token"])
        with pytest.raises(ValueError, match="bản cập nhật"):
            server._dispatch("create_job", raw_job)

        assert server._dispatch("release_update_install", {"token": token}) == {
            "released": True
        }
        server._dispatch("create_job", raw_job)
        blocked = server._dispatch("reserve_update_install", {})
        assert blocked["eligible"] is False
        assert blocked["active_job_count"] == 1
        assert blocked["reserved"] is False
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


def test_whisper_job_has_separate_fingerprint_and_imports_method_aware_qa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    video = tmp_path / "phim.mp4"
    video.write_bytes(b"fixture")
    model = tmp_path / "large-v3"
    model.mkdir()
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(service_module, "whisper_model_path", lambda **_kwargs: model)
    monkeypatch.setattr(service_module, "whisper_runtime_is_ready", lambda _runtime: True)
    original_which = service_module.shutil.which
    monkeypatch.setattr(
        service_module.shutil,
        "which",
        lambda name: sys.executable if name in {"ffmpeg", "ffprobe"} else original_which(name),
    )
    project = service.create_project(
        project_path=str(tmp_path / "project.ducai"), name="Phim thử"
    )
    service.import_media(project_id=project["id"], media_path=str(video))
    frame_job = service.create_ocr_job(
        project_id=project["id"],
        sample_interval_us=800_000,
        engine_python=sys.executable,
    )
    service.jobs.transition(frame_job["id"], JobState.START_REQUESTED)
    service.jobs.transition(frame_job["id"], JobState.RUNNING)
    service.jobs.transition(frame_job["id"], JobState.SUCCEEDED)

    whisper_job = service.create_ocr_job(
        project_id=project["id"],
        sample_interval_us=800_000,
        engine_python=sys.executable,
        extraction_method="whisper_audio",
        device="auto",
    )

    assert whisper_job["spec"]["provider_id"] == "faster-whisper-audio"
    assert whisper_job["spec"]["provider_config"]["extraction_method"] == "whisper_audio"
    assert (
        service.jobs.get(whisper_job["id"]).spec.resume_fingerprint
        != service.jobs.get(frame_job["id"]).spec.resume_fingerprint
    )
    candidate = Path(whisper_job["spec"]["provider_config"]["staging_output_path"])
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n我要修炼成仙\n",
        encoding="utf-8-sig",
    )
    qa = candidate.with_name(candidate.stem + "_QA.json")
    qa.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "whisper_asr",
                "status": "PASS",
                "job_id": whisper_job["id"],
                "input_fingerprint": whisper_job["spec"]["input_fingerprint"],
                "settings_fingerprint": whisper_job["spec"]["settings_fingerprint"],
                "extraction_method": "whisper_audio",
                "cue_count": 1,
                "output_path": str(candidate),
                "output_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    imported = service.import_final_ocr_srt(whisper_job["id"], str(candidate))

    assert imported["source_cues"][0]["source"] == "whisper-large-v3"
    assert imported["source_cues"][0]["text"] == "我要修炼成仙"


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
    manual = service.subtitle_command(
        project_id=project["id"],
        command="add_scene_text",
        params={
            "text": "Bí cảnh",
            "start_us": 2_000_000,
            "end_us": 3_000_000,
            "visual_box": [0.72, 0.20, 0.16, 0.22],
            "font_size": 21,
            "orientation": "vertical",
            "background_color": "#111111",
        },
    )
    manual_cue_id = next(
        cue["id"]
        for cue in manual["source_cues"]
        if cue["source"] == "manual_scene_text"
    )
    manual = service.subtitle_command(
        project_id=project["id"],
        command="add_subtitle_cue",
        params={
            "text": "Câu thoại bù thủ công",
            "start_us": 4_000_000,
            "end_us": 5_000_000,
            "visual_box": [0.10, 0.74, 0.80, 0.16],
            "cue_role": "dialogue",
        },
    )
    manual_dialogue_id = next(
        cue["id"]
        for cue in manual["source_cues"]
        if cue["source"] == "manual_dialogue"
    )
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
    assert [cue["text"] for cue in imported["source_cues"]] == [
        "新字幕完整",
        "Bí cảnh",
        "Câu thoại bù thủ công",
    ]
    preserved = next(cue for cue in imported["source_cues"] if cue["id"] == manual_cue_id)
    assert preserved["index"] == 2
    assert preserved["text_font_size"] == 21
    assert preserved["orientation"] == "vertical"
    assert preserved["background_color"] == "#111111"
    assert any(
        track["id"] == "S2"
        and any(clip["cue_id"] == manual_cue_id for clip in track["clips"])
        for track in imported["tracks"]
    )
    preserved_dialogue = next(
        cue for cue in imported["source_cues"] if cue["id"] == manual_dialogue_id
    )
    assert preserved_dialogue["role"] == "dialogue"
    assert preserved_dialogue["subtitle_track_id"] == "S1"
    assert any(
        track["id"] == "S1"
        and any(clip["cue_id"] == manual_dialogue_id for clip in track["clips"])
        for track in imported["tracks"]
    )
    assert not candidate.exists()


def test_ocr_job_uses_project_dialogue_and_exclusion_regions(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    video = tmp_path / "phim.mp4"
    video.write_bytes(b"fixture")
    project = service.create_project(
        project_path=str(tmp_path / "project.ducai"),
        name="Phim thử",
    )
    service.import_media(project_id=project["id"], media_path=str(video))
    service.subtitle_command(
        project_id=project["id"],
        command="set_ocr_regions",
        params={
            "dialogue_region": [0.10, 0.72, 0.80, 0.20],
            "exclusion_regions": [[0.82, 0.02, 0.16, 0.08]],
        },
    )

    job = service.create_ocr_job(
        project_id=project["id"],
        sample_interval_us=800_000,
        engine_python=sys.executable,
    )

    config = job["spec"]["provider_config"]
    assert config["dialogue_region"] == [0.10, 0.72, 0.80, 0.20]
    assert config["exclusion_regions"] == [[0.82, 0.02, 0.16, 0.08]]
    assert len(config["bands"]) == 1
    band = config["bands"][0]
    assert band["name"] == "upper"
    assert band["reading_mode"] == "dialogue"
    assert band["scale"] == 1.0
    assert band["x1"] == pytest.approx(0.10)
    assert band["y1"] == pytest.approx(0.72)
    assert band["x2"] == pytest.approx(0.90)
    assert band["y2"] == pytest.approx(0.92)


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


def test_scene_text_font_scale_command_persists_paired_compact_patch(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    source = SubtitleCue(
        "scene-1",
        1,
        TimeRange(0, 2_000_000),
        "韩立",
        "zh",
        role="scene_text",
        visual_box=(0.3, 0.2, 0.1, 0.08),
        visual_box_source="ocr",
    )
    snapshot = ProjectSnapshot(
        "project-scene-scale",
        "Scene scale",
        1,
        source_cues=(source,),
        translated_cues=(replace(source, text="Hàn Lập", language="vi"),),
    )
    service.projects.save(project_path=tmp_path / "scene.ducai", snapshot=snapshot)
    server = JobManagerServer(name=f"unused-scene-scale-{uuid4()}", service=service)
    try:
        result = server._dispatch(
            "subtitle_command",
            {
                "project_id": snapshot.id,
                "command": "set_scene_text_font_scale",
                "cue_id": source.id,
                "compact": True,
                "values": {"scale": 1.25},
            },
        )

        assert result["_subtitle_patch"] is True
        assert result["source_cues"][0]["scene_font_scale"] == 1.25
        assert result["translated_cues"][0]["scene_font_scale"] == 1.25
        persisted = service.projects.load(snapshot.id)
        assert persisted.source_cues[0].scene_font_scale == 1.25

        undone = server._dispatch(
            "subtitle_command",
            {"project_id": snapshot.id, "command": "undo", "compact": True},
        )
        assert undone["source_cues"][0]["scene_font_scale"] == 1.0
    finally:
        server.close()


def test_scene_text_timing_command_persists_compact_clip_patch_and_undo(
    tmp_path: Path,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    source = SubtitleCue(
        "scene-1",
        1,
        TimeRange(1_000_000, 3_000_000),
        "韩立",
        "zh",
        role="scene_text",
    )
    video = TimelineClip("video-1", "V1", TimeRange(0, 10_000_000))
    scene = TimelineClip(
        "scene-clip-1",
        "S2",
        source.range,
        source.range,
        cue_id=source.id,
        cue_index=source.index,
        cue_text="Hàn Lập",
    )
    snapshot = ProjectSnapshot(
        "project-scene-timing",
        "Scene timing",
        1,
        tracks=(
            TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),
            TimelineTrack("S2", TrackKind.SUBTITLE, "Chữ trong cảnh", (scene,)),
        ),
        source_cues=(source,),
        translated_cues=(replace(source, text="Hàn Lập", language="vi"),),
    )
    service.projects.save(project_path=tmp_path / "scene-timing.ducai", snapshot=snapshot)
    server = JobManagerServer(name=f"unused-scene-timing-{uuid4()}", service=service)
    try:
        result = server._dispatch(
            "subtitle_command",
            {
                "project_id": snapshot.id,
                "command": "set_scene_text_timing",
                "cue_id": source.id,
                "compact": True,
                "values": {"start_us": 500_000, "end_us": 4_000_000},
            },
        )

        assert result["_subtitle_patch"] is True
        assert result["source_cues"][0]["range"] == {
            "start_us": 500_000,
            "end_us": 4_000_000,
        }
        assert result["translated_cues"][0]["range"] == {
            "start_us": 500_000,
            "end_us": 4_000_000,
        }
        assert result["subtitle_clip_upserts"][0]["id"] == scene.id
        assert result["subtitle_clip_upserts"][0]["timeline_range"] == {
            "start_us": 500_000,
            "end_us": 4_000_000,
        }
        persisted = service.projects.load(snapshot.id)
        assert persisted.tracks[1].clips[0].timeline_range == TimeRange(
            500_000,
            4_000_000,
        )

        undone = server._dispatch(
            "subtitle_command",
            {"project_id": snapshot.id, "command": "undo", "compact": True},
        )
        assert undone["source_cues"][0]["range"] == {
            "start_us": 1_000_000,
            "end_us": 3_000_000,
        }
        assert undone["subtitle_clip_upserts"][0]["timeline_range"] == {
            "start_us": 1_000_000,
            "end_us": 3_000_000,
        }
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


def test_job_manager_runs_whisper_worker_and_auto_imports_pass_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticMediaProbe(),
    )
    server = JobManagerServer(name=f"unused-whisper-worker-{uuid4()}", service=service)
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    model = tmp_path / "large-v3"
    model.mkdir()
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_whisper_engine_host.py"
    monkeypatch.setattr(service_module, "whisper_model_path", lambda **_kwargs: model)
    monkeypatch.setattr(service_module, "whisper_runtime_is_ready", lambda _runtime: True)
    original_which = service_module.shutil.which
    monkeypatch.setattr(
        service_module.shutil,
        "which",
        lambda name: sys.executable if name in {"ffmpeg", "ffprobe"} else original_which(name),
    )
    try:
        project = service.create_project(
            project_path=str(tmp_path / "project.ducai"), name="Phim thử"
        )
        service.import_media(project_id=project["id"], media_path=str(video))
        job = service.create_ocr_job(
            project_id=project["id"],
            sample_interval_us=800_000,
            engine_python=sys.executable,
            extraction_method="whisper_audio",
            device="auto",
            engine_host_path=str(fake_host),
        )
        job_staging = service.checkpoints.staging_root / job["id"]
        job_staging.mkdir(parents=True, exist_ok=True)
        (job_staging / "dialogue-16k-mono.wav").write_bytes(b"W" * 8_192)
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
            state = service.jobs.get(job["id"]).state
            if state in {JobState.SUCCEEDED, JobState.FAILED}:
                break
            time.sleep(0.01)

        assert service.jobs.get(job["id"]).state is JobState.SUCCEEDED
        loaded = service.get_project(project["id"])
        assert len(loaded["source_cues"]) == 2
        assert {cue["source"] for cue in loaded["source_cues"]} == {
            "whisper-large-v3"
        }
        assert (tmp_path / "fixture_ZH_FINAL.srt").is_file()
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
