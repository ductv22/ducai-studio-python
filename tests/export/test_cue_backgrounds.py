import json
from dataclasses import asdict, replace

import pytest

from ducai_studio.application.export_pipeline import build_export_snapshot
from ducai_studio.domain.effects import ExportSettings, SubtitleStyle
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.infrastructure.project_repository import decode_project_snapshot
from ducai_studio.providers.export.ffmpeg import decode_export_snapshot
from ducai_studio.providers.media_probe import MediaProbeResult
from ducai_studio.providers.subtitle_artwork import artwork_payload, editor_payload, layout_batch


def background_project(tmp_path, background, role="dialogue"):
    video = tmp_path / "source.mp4"
    video.touch()
    interval = TimeRange(0, 3_000_000)
    cue = SubtitleCue(
        "cue", 1, TimeRange(500_000, 2_500_000), "Một câu đang chọn", "vi",
        role=role, background_style=background, background_color="#123456",
        background_opacity_percent=65, panel_box=(.1, .3, .8, .3),
        visual_box=(.1, .3, .8, .3), visual_box_source="estimated", review_state="accepted",
    )
    project = ProjectSnapshot(
        "background", "Background", 1,
        media=(MediaAsset("m", str(video), MediaKind.VIDEO, interval.duration_us),),
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (
            TimelineClip("v", "V1", interval, interval, "m"),)),),
        source_cues=() if role == "title_text" else (cue,),
        title_cues=(cue,) if role == "title_text" else (),
    )
    return decode_project_snapshot(json.loads(json.dumps(asdict(project))))


class BackgroundProbe:
    def probe(self, _path):
        return MediaProbeResult(3_000_000, True, False, 640, 360, "mp4")


@pytest.mark.parametrize("background", ["none", "blur"])
@pytest.mark.parametrize("role", ["dialogue", "scene_text", "title_text"])
@pytest.mark.parametrize("enabled,mode", [(True, "black"), (True, "blur"), (False, "black")])
def test_explicit_cue_background_overrides_default_and_roundtrips(
    tmp_path, background, role, enabled, mode,
):
    project = background_project(tmp_path, background, role)
    assert decode_project_snapshot(json.loads(json.dumps(asdict(project)))) == project
    style = SubtitleStyle(background_enabled=enabled, background_mode=mode)
    snapshot = build_export_snapshot(project, settings=ExportSettings(subtitle_style=style),
                                     media_probe=BackgroundProbe())
    assert decode_export_snapshot(json.loads(json.dumps(asdict(snapshot)))) == snapshot
    rendered = (*snapshot.subtitles, *snapshot.scene_texts, *snapshot.title_texts)[0]
    assert rendered.background_style == background
    payload = artwork_payload(rendered, 640, 360, style)
    layout = layout_batch([payload])[0]
    assert layout["background_path"] == ""
    assert layout["background_color"] == ""
    assert layout["text_path"]
    assert len(snapshot.subtitle_blurs) == (1 if background == "blur" else 0)
    if background == "blur":
        blur = snapshot.subtitle_blurs[0]
        assert (blur.start_us, blur.end_us, blur.opacity) == (500_000, 2_500_000, .65)
        assert blur.tracking_mode == "translated_text"
    # Editor and worker must pass the same explicit style into the renderer.
    cue = project.title_cues[0] if role == "title_text" else project.source_cues[0]
    editor = {**asdict(cue), "cue_id": cue.id, "start_us": cue.range.start_us,
              "end_us": cue.range.end_us, "source_text": cue.text, "cue_role": role}
    assert editor_payload(editor, 640, 360, style) == payload


def test_old_dialogue_retains_global_black_background(tmp_path):
    project = background_project(tmp_path, "solid")
    project = replace(project, source_cues=(replace(project.source_cues[0], background_color=""),))
    style = SubtitleStyle(background_mode="black")
    snapshot = build_export_snapshot(project, settings=ExportSettings(subtitle_style=style),
                                     media_probe=BackgroundProbe())
    payload = artwork_payload(snapshot.subtitles[0], 640, 360, style)
    layout = layout_batch([payload])[0]
    assert layout["background_color"] == "#000000"
    assert layout["background_path"]


def test_pending_scene_does_not_blur_before_acceptance(tmp_path):
    project = background_project(tmp_path, "blur", "scene_text")
    pending = replace(project.source_cues[0], review_state="review")
    project = replace(project, source_cues=(pending,))
    snapshot = build_export_snapshot(project, settings=ExportSettings(),
                                     media_probe=BackgroundProbe())
    assert not snapshot.subtitle_blurs


@pytest.mark.parametrize("role,count", [("dialogue", 0), ("scene_text", 0), ("title_text", 1)])
def test_cue_blur_follows_text_visibility(tmp_path, role, count):
    project = background_project(tmp_path, "blur", role)
    snapshot = build_export_snapshot(
        project, settings=ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
        media_probe=BackgroundProbe(),
    )
    assert len(snapshot.subtitle_blurs) == count


def test_old_export_snapshot_decodes_without_new_background_fields(tmp_path):
    project = background_project(tmp_path, "solid")
    style = SubtitleStyle(background_mode="blur")
    snapshot = build_export_snapshot(project, settings=ExportSettings(subtitle_style=style),
                                     media_probe=BackgroundProbe())
    raw = json.loads(json.dumps(asdict(snapshot)))
    for cue in raw["subtitles"]:
        cue.pop("background_style")
    for blur in raw["subtitle_blurs"]:
        blur.pop("fade_enabled")
        blur.pop("fade_duration_ms")
    assert decode_export_snapshot(raw) == snapshot


def test_opaque_blur_mask_has_full_white_instead_of_video_limited_white(tmp_path):
    import shutil
    import subprocess

    from ducai_studio.providers.export.ffmpeg import FfmpegCapabilities, materialize_export_chunk

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "FFmpeg required to verify the real blur opacity mask"
    project = background_project(tmp_path, "blur")
    project = replace(project, source_cues=(replace(
        project.source_cues[0], background_opacity_percent=100,
    ),))
    snapshot = build_export_snapshot(project, settings=ExportSettings(),
                                     media_probe=BackgroundProbe())
    work = tmp_path / "work"
    plan = materialize_export_chunk(
        snapshot, index=0, start_us=0, end_us=3_000_000, work_dir=work,
        output_path=tmp_path / "output.mp4",
        capabilities=FfmpegCapabilities(ffmpeg, "test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")
    mask_filter = next(line for line in graph.splitlines() if line.startswith("color="))
    mask_filter = mask_filter.rsplit("[", 1)[0].rstrip(";")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", mask_filter, "-ss", "1",
         "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"],
        cwd=work, capture_output=True, timeout=15, check=True,
    )
    mask = result.stdout
    assert len(mask) == 640 * 360
    assert min(mask) == 0 and max(mask) == 255
    assert mask.count(255) > 1000  # Solid interior must replace 100% of original pixels.
    assert mask.count(0) > len(mask) // 2  # Outside region remains untouched.
