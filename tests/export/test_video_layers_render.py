"""Real layer frames and audio must agree with Preview and the OCR scan plan."""

import shutil
import subprocess
from array import array
from dataclasses import replace
from math import cos, pi, sin, sqrt
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from ducai_studio.application.export_pipeline import build_export_snapshot
from ducai_studio.domain.effects import ExportSettings, SubtitleStyle
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.domain.timeline_edit import build_scan_plan
from ducai_studio.providers.export.ffmpeg import materialize_export_chunk, probe_ffmpeg_capabilities
from ducai_studio.providers.media_probe import FfprobeProvider
from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel

S = 1_000_000


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Real render requires FFmpeg")
@pytest.mark.parametrize("layer_state", ["visible", "muted", "hidden"])
def test_layer_frame_audio_preview_and_ocr_share_winner(tmp_path: Path, layer_state):
    import json
    from dataclasses import asdict

    ffmpeg = shutil.which("ffmpeg")
    paths = []
    for color, hz in (("red", 440), ("blue", 880)):
        path = tmp_path / (color + ".mp4")
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x180:r=24",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={hz}:sample_rate=48000",
                "-t",
                "3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-y",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        paths.append(path)
    clips = [
        TimelineClip("v1", "V1", TimeRange(0, 3 * S), TimeRange(0, 3 * S), "m1", "g1"),
        TimelineClip("v2", "V2", TimeRange(S, 2 * S), TimeRange(0, S), "m2", "g2"),
    ]
    project = ProjectSnapshot(
        "layers",
        "Layers",
        1,
        media=tuple(
            MediaAsset(f"m{i + 1}", str(path), MediaKind.VIDEO, 3 * S)
            for i, path in enumerate(paths)
        ),
        tracks=(
            TimelineTrack("V1", TrackKind.VIDEO, "V1", (clips[0],)),
            TimelineTrack(
                "A1", TrackKind.ORIGINAL_AUDIO, "A1", (replace(clips[0], id="a1", track_id="A1"),)
            ),
            TimelineTrack(
                "V2",
                TrackKind.VIDEO,
                "V2",
                (clips[1],),
                muted=layer_state == "muted",
                visible=layer_state != "hidden",
            ),
            TimelineTrack(
                "A2", TrackKind.ORIGINAL_AUDIO, "A2", (replace(clips[1], id="a2", track_id="A2"),)
            ),
        ),
    )
    scan = build_scan_plan(project)
    assert [row["clip_id"] for row in scan["segments"]] == (
        ["v1"] if layer_state == "hidden" else ["v1", "v2", "v1"]
    )
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(json.loads(json.dumps(asdict(project))))
    assert model.previewDescriptor(1.5 * S)["clipId"] == ("v1" if layer_state == "hidden" else "v2")
    render = build_export_snapshot(
        project,
        settings=ExportSettings(backend="cpu", fps=24, subtitle_style=SubtitleStyle(enabled=False)),
        media_probe=FfprobeProvider(),
    )
    output = tmp_path / "export.mp4"
    plan = materialize_export_chunk(
        render,
        index=0,
        start_us=0,
        end_us=3 * S,
        work_dir=tmp_path / "work",
        output_path=output,
        capabilities=probe_ffmpeg_capabilities(ffmpeg),
    )
    subprocess.run(
        plan.arguments, cwd=tmp_path / "work", capture_output=True, check=True, timeout=30
    )
    for at, expected_color, expected_tone in (
        (0.5, "red", 440),
        (
            1.5,
            "red" if layer_state == "hidden" else "blue",
            440 if layer_state == "hidden" else None if layer_state == "muted" else 880,
        ),
        (2.5, "red", 440),
    ):
        frame = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                str(at),
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
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        assert (
            (frame[0] > 180 and frame[2] < 70)
            if expected_color == "red"
            else (frame[2] > 180 and frame[0] < 70)
        )
        pcm = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                str(at),
                "-i",
                str(output),
                "-t",
                "0.15",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "s16le",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        samples = array("h", pcm)
        rms = sqrt(sum(x * x for x in samples) / len(samples))
        if expected_tone is None:
            assert rms < 10, rms
        else:
            tones = {
                hz: abs(
                    sum(value * cos(2 * pi * hz * i / 48000) for i, value in enumerate(samples))
                )
                + abs(sum(value * sin(2 * pi * hz * i / 48000) for i, value in enumerate(samples)))
                for hz in (440, 880)
            }
            assert rms > 100 and tones[expected_tone] > 5 * tones[1320 - expected_tone], tones
