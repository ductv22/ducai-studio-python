from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

from ducai_studio.application.voice_pipeline import (
    atomic_write_json,
    file_sha256,
    write_pcm16_mono,
)
from ducai_studio.domain.effects import ExportSettings, ExportSnapshot, RenderClip, SubtitleStyle
from ducai_studio.domain.voice_conform import (
    VoiceConformCueInput,
    VoiceConformSettings,
    VoiceTimingMode,
    build_voice_conform_plan,
)
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.export import probe_ffmpeg_capabilities
from ducai_studio.providers.export.ffmpeg import (
    _chunk_conform_pieces,
    _conform_output_to_source,
    _hidden_subprocess_kwargs,
    materialize_export_chunk,
)
from ducai_studio.providers.media_probe import FfprobeProvider
from ducai_studio.workers import export_worker as export_worker_module
from ducai_studio.workers.export_worker import ExportWorker
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


@pytest.mark.skipif(os.name != "nt", reason="Windows-only subprocess flags")
def test_ffmpeg_subprocesses_hide_console_but_keep_startup_configuration() -> None:
    options = _hidden_subprocess_kwargs()
    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW


def test_export_progress_is_bounded_to_one_event_per_second(tmp_path: Path, monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    monkeypatch.setattr(export_worker_module.time, "monotonic", lambda: clock[0])
    worker = ExportWorker("export-progress-cadence")
    worker.snapshot = SimpleNamespace(duration_us=10_000_000)
    worker.ledger = {"entries": {}}
    worker._last_progress_sent = clock[0]
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]

    clock[0] += 0.999
    worker._send_progress("render", ((0, 10_000_000),), current_chunk_us=1_000_000)

    assert events == []

    clock[0] += 0.001
    worker._send_progress("render", ((0, 10_000_000),), current_chunk_us=1_100_000)

    assert len(events) == 1
    assert events[0][0] == "progress"
    assert events[0][1]["chunk_current"] == 1
    assert events[0][1]["chunk_total"] == 1


def test_export_renderer_change_does_not_adopt_old_chunks_or_delete_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = ExportWorker("renderer-cache")
    old_chunk = tmp_path / "old.mp4"
    old_chunk.write_bytes(b"old renderer cache")
    checkpoint = tmp_path / "ledger.json"
    atomic_write_json(
        checkpoint,
        {
            "signature": "legacy",
            "entries": {"0": {"path": str(old_chunk), "sha256": file_sha256(old_chunk)}},
        },
    )
    reference = {"artifact": {"uri": str(checkpoint)}}
    assert worker._restore_checkpoint(reference, "new", legacy_signature="legacy") is None
    assert worker._restore_checkpoint(reference, "new", previous_signature="legacy") is None
    assert worker._restore_checkpoint(reference, "new", system_fonts_signature="legacy") is None
    assert old_chunk.read_bytes() == b"old renderer cache"
    with pytest.raises(ValueError, match="signature_mismatch"):
        worker._restore_checkpoint(reference, "new", legacy_signature="unrelated")
    assert worker._restore_checkpoint(reference, "legacy")["entries"]["0"]["path"] == str(old_chunk)


def test_full_frame_subtitle_mask_keeps_odd_dimensions() -> None:
    from ducai_studio.domain.effects import RenderOverlay, RenderSubtitleBlur
    from ducai_studio.providers.export.ffmpeg import _overlay_pixels

    mask = RenderSubtitleBlur("c", 1, 0, 1_000_000, 0.1, 0.1, 0.575, 0.385)
    assert _overlay_pixels(mask, 1000, 200) == (100, 20, 575, 77)
    manual = RenderOverlay("o", "blur", 0, 1_000_000, 0.1, 0.1, 0.575, 0.385)
    assert _overlay_pixels(manual, 1000, 200) == (100, 20, 574, 76)


def test_conform_output_time_map_splits_chunk_and_maps_audio_source() -> None:
    snapshot = SimpleNamespace(
        timeline_start_us=0,
        timeline_end_us=4_000_000,
        conform_segments=(
            SimpleNamespace(
                source_start_us=0,
                source_end_us=2_000_000,
                output_start_us=0,
                output_end_us=4_000_000,
                video_speed=0.5,
            ),
        ),
        video=SimpleNamespace(source_start_us=0, source_end_us=2_000_000),
    )

    assert _chunk_conform_pieces(snapshot, 1_000_000, 3_000_000) == ((500_000, 1_500_000, 0.5),)
    assert _conform_output_to_source(snapshot, 3_000_000) == 1_500_000


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is required for the real conform export contract",
)
def test_real_conform_export_has_video_audio_burned_subtitle_and_mapped_duration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    source = tmp_path / "source_ZH_FINAL.srt"
    translated = tmp_path / "source_ZH_FINAL_codex_vi.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n你好。\n",
        encoding="utf-8",
    )
    translated.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nXin chào từ phụ đề conform.\n",
        encoding="utf-8",
    )
    base_voice = tmp_path / "source_voice_vi.wav"
    conformed_voice = tmp_path / "source_voice_vi_conformed.wav"
    from array import array

    write_pcm16_mono(
        base_voice,
        array("f", (0.06 if index % 100 < 50 else -0.06 for index in range(96_000))),
        48_000,
    )
    write_pcm16_mono(
        conformed_voice,
        array("f", (0.06 if index % 100 < 50 else -0.06 for index in range(192_000))),
        48_000,
    )
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FfprobeProvider(),
    )
    project = service.create_project(
        project_path=str(tmp_path / "project.ducai"),
        name="Conform export",
    )
    project_id = str(project["id"])
    service.import_media(project_id=project_id, media_path=str(video))
    service.import_media(project_id=project_id, media_path=str(source))
    snapshot = service.projects.load(project_id)
    snapshot = service.media_imports.import_srt(snapshot, translated, language="vi")
    snapshot = service.media_imports.import_voice_media(snapshot, base_voice)
    cue = snapshot.translated_cues[0]
    plan = build_voice_conform_plan(
        (
            VoiceConformCueInput(
                cue.id,
                cue.index,
                cue.range.start_us,
                cue.range.end_us,
                4_000_000,
            ),
        ),
        source_duration_us=2_000_000,
        settings=VoiceConformSettings(mode=VoiceTimingMode.STRETCH_VIDEO),
    )
    snapshot = service.media_imports.apply_voice_conform_media(
        snapshot,
        conformed_voice,
        plan=plan,
        parent_tts_job_id="tts-production",
    )
    summary = service.projects.get_summary(project_id)
    service.projects.save(project_path=summary["path"], snapshot=snapshot)

    output = tmp_path / "conformed-final.mp4"
    job = service.create_export_job(
        project_id=project_id,
        output_path=str(output),
        export_options={
            "backend": "cpu",
            "chunk_minutes": 1,
            "data_root": str(tmp_path / "data"),
            "subtitle_style": {"font_family": "Arial", "font_size": 26},
        },
    )
    render = job["spec"]["provider_config"]["snapshot"]
    assert render["timeline_end_us"] == plan.output_duration_us == 4_000_000
    assert render["conform_segments"] == [
        {
            "source_start_us": 0,
            "source_end_us": 2_000_000,
            "output_start_us": 0,
            "output_end_us": 4_000_000,
            "video_speed": 0.5,
        }
    ]
    assert render["subtitles"][0]["start_us"] == 0
    assert render["subtitles"][0]["end_us"] == 4_000_000
    assert render["subtitle_blurs"][0]["start_us"] == 0
    assert render["subtitle_blurs"][0]["end_us"] == 4_000_000
    assert {item["track_kind"] for item in render["audio"]} >= {
        "original_audio",
        "voice",
    }

    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = ExportWorker(str(job["id"]))
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure({"job": job["spec"]})
    worker._execute()

    assert events[-1][0] == "succeeded"
    probe = FfprobeProvider(ffprobe).probe(output)
    assert probe.has_video is True
    assert probe.has_audio is True
    assert abs(probe.duration_us - plan.output_duration_us) <= 100_000
    qa = json.loads(output.with_name("conformed-final_QA.json").read_text(encoding="utf-8"))
    assert qa["status"] == "PASS"
    assert qa["has_video"] is True
    assert qa["has_audio"] is True


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is required for the real multi-segment export contract",
)
def test_real_multi_segment_export_preserves_join_frames_audio_and_duration(
    tmp_path: Path,
) -> None:
    ffmpeg = str(shutil.which("ffmpeg"))
    source = tmp_path / "three-segment-source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000:duration=1",
            "-f",
            "lavfi",
            "-i",
            "color=green:s=160x90:r=24:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=160x90:r=24:d=1.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=990:sample_rate=48000:duration=1.5",
            "-filter_complex",
            "[0:v][1:a][2:v][3:a][4:v][5:a]concat=n=3:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    first_video = RenderClip(
        "v-red",
        "V1",
        "video",
        str(source),
        0,
        1_000_000,
        0,
        1_000_000,
    )
    second_video = RenderClip(
        "v-blue",
        "V1",
        "video",
        str(source),
        1_000_000,
        2_500_000,
        1_500_000,
        3_000_000,
    )
    audio = (
        RenderClip(
            "a-red",
            "A1",
            "original_audio",
            str(source),
            0,
            1_000_000,
            0,
            1_000_000,
        ),
        RenderClip(
            "a-blue",
            "A1",
            "original_audio",
            str(source),
            1_000_000,
            2_500_000,
            1_500_000,
            3_000_000,
        ),
    )
    snapshot = ExportSnapshot(
        1,
        "multi-segment",
        1,
        0,
        2_500_000,
        160,
        90,
        first_video,
        audio,
        (),
        (),
        ExportSettings(
            backend="cpu",
            fps=24,
            subtitle_style=SubtitleStyle(enabled=False),
        ),
        video_segments=(first_video, second_video),
    )
    work_dir = tmp_path / "multi-segment-work"
    output = tmp_path / "multi-segment.mp4"
    capabilities = probe_ffmpeg_capabilities(ffmpeg)
    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=2_500_000,
        work_dir=work_dir,
        output_path=output,
        capabilities=capabilities,
    )
    subprocess.run(
        plan.arguments,
        cwd=work_dir,
        check=True,
        capture_output=True,
        timeout=60,
    )

    probe = FfprobeProvider().probe(output)
    assert probe.has_video is True and probe.has_audio is True
    assert abs(probe.duration_us - 2_500_000) <= 100_000

    def sampled_pixel(at_seconds: float) -> tuple[int, int, int]:
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                str(at_seconds),
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-vf",
                "scale=1:1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return tuple(result.stdout[:3])  # type: ignore[return-value]

    red = sampled_pixel(0.5)
    blue = sampled_pixel(1.75)
    assert red[0] > 180 and red[1] < 80 and red[2] < 80
    assert blue[2] > 180 and blue[0] < 80 and blue[1] < 80

    pcm = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(output),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    samples = array("h")
    samples.frombytes(pcm)

    def estimated_frequency(start_s: float, end_s: float) -> float:
        window = samples[round(start_s * 48_000) : round(end_s * 48_000)]
        crossings = sum(
            (left < 0 <= right) or (left >= 0 > right)
            for left, right in zip(window, window[1:], strict=False)
        )
        return crossings / (2 * (end_s - start_s))

    assert estimated_frequency(0.2, 0.8) == pytest.approx(330, abs=15)
    assert estimated_frequency(1.2, 2.2) == pytest.approx(990, abs=20)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="Real plain-video export requires FFmpeg")
def test_real_import_split_delete_speed_export_without_ai(tmp_path, monkeypatch):
    from ducai_studio.domain.project import TrackKind

    ffmpeg = shutil.which("ffmpeg")
    source = tmp_path / "source.mp4"
    subprocess.run([
        ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=red:s=320x180:r=24:d=1",
        "-f", "lavfi", "-i", "color=green:s=320x180:r=24:d=1",
        "-f", "lavfi", "-i", "color=blue:s=320x180:r=24:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-y", str(source),
    ], check=True, capture_output=True, timeout=20, **_hidden_subprocess_kwargs())
    original_hash = file_sha256(source)
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts", media_probe=FfprobeProvider())
    project = service.create_project(project_path=str(tmp_path / "project.ducai"), name="No AI")
    project_id = project["id"]
    service.import_media(project_id=project_id, media_path=str(source))
    for point in (1_000_000, 2_000_000):
        service.timeline_command(project_id=project_id, command="split_video", timeline_us=point)
    split = service.projects.load(project_id)
    clips = next(track.clips for track in split.tracks if track.kind is TrackKind.VIDEO)
    blue = next(clip for clip in clips if clip.timeline_range.start_us == 2_000_000)
    middle = next(clip for clip in clips if clip.timeline_range.start_us == 1_000_000)
    service.timeline_command(project_id=project_id, command="speed", clip_id=blue.id, value=200)
    service.timeline_command(
        project_id=project_id, command="delete_video_segment", clip_id=middle.id)
    edited = service.projects.load(project_id)
    assert not edited.source_cues and not edited.translated_cues
    output = tmp_path / "plain-final.mp4"
    job = service.create_export_job(project_id=project_id, output_path=str(output),
        export_options={"backend": "cpu", "data_root": str(tmp_path / "data")})
    render = job["spec"]["provider_config"]["snapshot"]
    assert render["subtitles"] == [] and render["timeline_end_us"] == 2_000_000
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = ExportWorker(job["id"])
    events = []
    worker.send = lambda kind, payload: events.append((kind, payload))
    worker._configure({"job": job["spec"]})
    worker._execute()
    assert events[-1][0] == "succeeded"
    qa = service.validate_final_export(job["id"], str(output))
    assert qa["status"] == "PASS"
    probe = FfprobeProvider().probe(output)
    assert probe.has_video and probe.has_audio
    assert abs(probe.duration_us - 2_000_000) <= 100_000
    for at, channel in (("0.5", 0), ("1.5", 2)):
        frame = subprocess.run([
            ffmpeg, "-v", "error", "-ss", at, "-i", str(output), "-frames:v", "1",
            "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ], check=True, capture_output=True, timeout=10, **_hidden_subprocess_kwargs()).stdout
        assert frame[channel] > 180 and frame[1] < 80
    assert file_sha256(source) == original_hash
    assert {item["spec"]["kind"] for item in service.list_jobs()} == {"export"}


def test_export_resume_eta_ignores_instantly_restored_cache(tmp_path: Path, monkeypatch) -> None:
    clock = [200.0]
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    monkeypatch.setattr(export_worker_module.time, "monotonic", lambda: clock[0])
    worker = ExportWorker("export-resume-eta")
    worker.snapshot = SimpleNamespace(duration_us=20_000_000)
    worker.ledger = {"entries": {"0": {"path": "cached"}}}
    worker._job_started = clock[0]
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    ranges = ((0, 10_000_000), (10_000_000, 20_000_000))

    clock[0] += 1.0
    worker._send_progress("render", ranges, force=True)

    assert events[-1][1]["current"] == 10_000_000
    assert events[-1][1]["eta_ms"] is None

    clock[0] += 1.0
    worker._send_progress("render", ranges, current_chunk_us=2_000_000, force=True)

    assert events[-1][1]["eta_ms"] == 8_000


def test_export_pause_suspends_owned_ffmpeg_and_resume_continues_it(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    worker = ExportWorker("export-pause-resume")
    process = SimpleNamespace(pid=4321)
    process_calls: list[tuple[str, int]] = []
    acknowledgements: list[tuple[str, str, dict]] = []
    checkpoints: list[tuple[tuple[int, int], ...]] = []
    monkeypatch.setattr(
        export_worker_module,
        "_suspend_process",
        lambda pid: process_calls.append(("pause", pid)),
    )
    monkeypatch.setattr(
        export_worker_module,
        "_resume_process",
        lambda pid: process_calls.append(("resume", pid)),
    )
    monkeypatch.setattr(worker, "_checkpoint", lambda ranges: checkpoints.append(ranges))
    monkeypatch.setattr(
        worker,
        "acknowledge",
        lambda command_id, kind, payload: acknowledgements.append((command_id, kind, payload)),
    )
    worker.commands.put(
        WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            "pause-event",
            "control",
            {"action": "pause", "command_id": "pause-command"},
        )
    )
    worker.commands.put(
        WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            "resume-event",
            "control",
            {"action": "resume", "command_id": "resume-command"},
        )
    )
    ranges = ((0, 600_000_000),)

    worker._control_running_process(process, ranges)

    assert process_calls == [("pause", 4321), ("resume", 4321)]
    assert checkpoints == [ranges]
    assert acknowledgements == [
        ("pause-command", "paused", {"status": "paused"}),
        ("resume-command", "resumed", {"status": "running"}),
    ]


def test_export_duplicate_control_replays_cached_acknowledgement(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    worker = ExportWorker("export-duplicate-control")
    cached = WorkerMessage(
        PROTOCOL_VERSION,
        worker.job_id,
        "paused-event",
        "paused",
        {"command_id": "pause-command", "status": "paused"},
    )
    worker.acknowledgements["pause-command"] = cached
    worker.commands.put(
        WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            "duplicate-pause-event",
            "control",
            {"action": "pause", "command_id": "pause-command"},
        )
    )
    emitted: list[WorkerMessage] = []
    monkeypatch.setattr(worker, "_emit_message", emitted.append)

    worker._control_between_chunks()

    assert emitted == [cached]


def test_export_finalize_is_pauseable_reports_real_phases_and_skips_faststart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = ExportWorker("export-fast-finalize")
    chunk = tmp_path / "chunk.mp4"
    chunk.write_bytes(b"cached-chunk")
    output = tmp_path / "final.mp4"
    worker.snapshot = SimpleNamespace(
        duration_us=10_000_000,
        timeline_start_us=0,
        settings=SimpleNamespace(overwrite=True),
    )
    worker.config = {
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "output_path": str(output),
    }
    worker.ledger = {
        "signature": "fixture-signature",
        "entries": {"0": {"path": str(chunk)}},
    }
    process_calls: list[dict[str, object]] = []
    events: list[tuple[str, dict]] = []

    def run_ffmpeg(
        arguments,
        duration_us,
        ranges,
        *,
        cwd,
        phase="render",
        failure_code="export_ffmpeg_failed",
    ) -> None:
        process_calls.append(
            {
                "arguments": list(arguments),
                "duration_us": duration_us,
                "ranges": ranges,
                "cwd": cwd,
                "phase": phase,
                "failure_code": failure_code,
            }
        )
        Path(arguments[-1]).write_bytes(b"final-mp4")

    monkeypatch.setattr(worker, "_run_ffmpeg", run_ffmpeg)
    monkeypatch.setattr(
        worker,
        "_probe_media",
        lambda _path, expected: {
            "playable": True,
            "duration_us": expected,
            "expected_duration_us": expected,
            "has_video": True,
            "has_audio": True,
            "size_bytes": 9,
        },
    )
    monkeypatch.setattr(worker, "send", lambda kind, payload: events.append((kind, payload)))

    worker._finalize(((0, 10_000_000),))

    assert output.read_bytes() == b"final-mp4"
    assert len(process_calls) == 1
    assert process_calls[0]["phase"] == "finalize_concat"
    arguments = process_calls[0]["arguments"]
    assert isinstance(arguments, list)
    assert "-progress" in arguments
    assert "pipe:1" in arguments
    assert "-movflags" not in arguments
    phases = [payload.get("phase") for kind, payload in events if kind == "progress"]
    assert "finalize_concat" in phases
    assert "finalize_probe" in phases
    assert "finalize_checksum" in phases
    assert "finalize_publish" in phases
    assert events[-1][0] == "succeeded"


def test_export_checksum_progress_honors_pause_and_resumes_same_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = ExportWorker("export-checksum-pause")
    target = tmp_path / "large.mp4"
    target.write_bytes(b"0123456789" * 1024)
    worker.snapshot = SimpleNamespace(
        duration_us=1_000_000,
        timeline_start_us=0,
        timeline_end_us=1_000_000,
        settings=SimpleNamespace(chunk_minutes=1, chunk_duration_us=60_000_000),
    )
    worker.ledger = {"entries": {}}
    checkpoints: list[tuple[tuple[int, int], ...]] = []
    acknowledgements: list[tuple[str, str, dict]] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(worker, "_checkpoint", lambda ranges: checkpoints.append(ranges))
    monkeypatch.setattr(
        worker,
        "acknowledge",
        lambda command_id, kind, payload: acknowledgements.append((command_id, kind, payload)),
    )
    monkeypatch.setattr(worker, "send", lambda kind, payload: events.append((kind, payload)))
    worker.commands.put(
        WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            "pause-checksum-event",
            "control",
            {"action": "pause", "command_id": "pause-checksum"},
        )
    )
    worker.commands.put(
        WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            "resume-checksum-event",
            "control",
            {"action": "resume", "command_id": "resume-checksum"},
        )
    )
    ranges = ((0, 1_000_000),)

    digest = worker._sha256_final_output(target, ranges)

    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    assert checkpoints == [ranges]
    assert acknowledgements == [
        ("pause-checksum", "paused", {"status": "paused"}),
        ("resume-checksum", "resumed", {"status": "running"}),
    ]
    checksum_events = [
        payload
        for kind, payload in events
        if kind == "progress" and payload.get("phase") == "finalize_checksum"
    ]
    assert checksum_events[-1]["phase_current"] == target.stat().st_size
    assert checksum_events[-1]["phase_total"] == target.stat().st_size


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is required for the real export contract",
)
def test_real_one_pass_export_blur_logo_subtitle_audio_and_atomic_qa(
    tmp_path: Path, monkeypatch
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    video = tmp_path / "movie.mp4"
    logo = tmp_path / "logo.png"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x32",
            "-frames:v",
            "1",
            "-y",
            str(logo),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    source = tmp_path / "source.srt"
    translated = tmp_path / "source_codex_vi.srt"
    source.write_text(
        "1\n00:00:00,300 --> 00:00:01,400\n你好。\n",
        encoding="utf-8",
    )
    translated.write_text(
        "1\n00:00:00,300 --> 00:00:01,400\nXin chào thế giới.\n",
        encoding="utf-8",
    )
    voice = tmp_path / "movie_voice_vi.wav"
    from array import array

    samples = array("f", (0.08 if index % 100 < 50 else -0.08 for index in range(144_000)))
    write_pcm16_mono(voice, samples, 48_000)

    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FfprobeProvider(),
    )
    project = service.create_project(project_path=str(tmp_path / "project.ducai"), name="Movie")
    project_id = project["id"]
    service.import_media(project_id=project_id, media_path=str(video))
    service.import_media(project_id=project_id, media_path=str(source))
    snapshot = service.projects.load(project_id)
    snapshot = service.media_imports.import_srt(snapshot, translated, language="vi")
    snapshot = service.media_imports.import_voice_media(snapshot, voice)
    summary = service.projects.get_summary(project_id)
    service.projects.save(project_path=summary["path"], snapshot=snapshot)
    service.effect_command(
        project_id=project_id,
        command="set_logo_asset",
        values={"path": str(logo)},
    )
    output = tmp_path / "final.mp4"
    job = service.create_export_job(
        project_id=project_id,
        output_path=str(output),
        export_options={
            "backend": "cpu",
            "chunk_minutes": 1,
            "data_root": str(tmp_path / "data"),
            "subtitle_style": {
                "font_family": "Arial",
                "font_size": 28,
                "primary_color": "#FFE6A3",
                "outline_color": "#101820",
                "outline_width": 2,
                "opacity_percent": 85,
                "alignment": 2,
            },
            "video_filter": {
                "preset_id": "cinematic-warm",
                "intensity_percent": 35,
                "brightness_percent": 2,
                "contrast_percent": 105,
                "saturation_percent": 110,
            },
        },
    )
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = ExportWorker(job["id"])
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure({"job": job["spec"]})

    worker._execute()

    assert events[-1][0] == "succeeded"
    assert output.is_file() and output.stat().st_size > 20_000
    qa = json.loads(output.with_name("final_QA.json").read_text(encoding="utf-8"))
    assert qa["status"] == "PASS"
    assert qa["has_video"] is True
    assert qa["has_audio"] is True
    assert service.validate_final_export(job["id"], str(output))["status"] == "PASS"

    output.unlink()
    output.with_name("final_QA.json").unlink()
    resumed = ExportWorker(job["id"])
    resumed_events: list[tuple[str, dict]] = []
    resumed.send = lambda kind, payload: resumed_events.append((kind, payload))  # type: ignore[method-assign]
    resumed._configure({"job": job["spec"]})

    resumed_run_ffmpeg = resumed._run_ffmpeg

    def must_not_render(*args, **kwargs):
        if kwargs.get("phase", "render") == "render":
            raise AssertionError("Approved global chunk cache was not reused")
        return resumed_run_ffmpeg(*args, **kwargs)

    resumed._run_ffmpeg = must_not_render  # type: ignore[method-assign]
    resumed._execute()

    assert resumed_events[-1][0] == "succeeded"
    assert output.is_file()

    output.with_name("final_QA.json").unlink()
    marker = output.with_name(f".{output.name}.{job['id']}.finalizing.json")
    atomic_write_json(
        marker,
        {
            "schema_version": 1,
            "job_id": job["id"],
            "ledger_signature": resumed.ledger["signature"],
            "output_sha256": file_sha256(output),
        },
    )
    recovered = ExportWorker(job["id"])
    recovered_events: list[tuple[str, dict]] = []
    recovered.send = lambda kind, payload: recovered_events.append((kind, payload))  # type: ignore[method-assign]
    recovered._configure({"job": job["spec"]})
    recovered_run_ffmpeg = recovered._run_ffmpeg

    def recovered_must_not_render(*args, **kwargs):
        if kwargs.get("phase", "render") == "render":
            raise AssertionError("Approved global chunk cache was not reused")
        return recovered_run_ffmpeg(*args, **kwargs)

    recovered._run_ffmpeg = recovered_must_not_render  # type: ignore[method-assign]

    recovered._execute()

    assert recovered_events[-1][0] == "succeeded"
    recovered_qa = json.loads(output.with_name("final_QA.json").read_text(encoding="utf-8"))
    assert recovered_qa["recovered_finalization"] is True
    assert not marker.exists()

    if probe_ffmpeg_capabilities(ffmpeg).h264_nvenc:
        nvenc_output = tmp_path / "final-nvenc.mp4"
        nvenc_job = service.create_export_job(
            project_id=project_id,
            output_path=str(nvenc_output),
            export_options={
                "backend": "nvenc",
                "chunk_minutes": 1,
                "data_root": str(tmp_path / "nvenc-data"),
                "subtitle_style": {"font_family": "Arial", "font_size": 28},
            },
        )
        nvenc_worker = ExportWorker(nvenc_job["id"])
        nvenc_events: list[tuple[str, dict]] = []
        nvenc_worker.send = lambda kind, payload: nvenc_events.append((kind, payload))  # type: ignore[method-assign]
        nvenc_worker._configure({"job": nvenc_job["spec"]})

        nvenc_worker._execute()

        assert nvenc_events[-1][0] == "succeeded"
        assert nvenc_output.is_file()
