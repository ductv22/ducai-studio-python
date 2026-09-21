from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ducai_studio.application.effects_commands import (
    DEFAULT_BLUR_ID,
    DEFAULT_LOGO_ID,
    EffectsEditor,
    ensure_default_overlays,
)
from ducai_studio.application.export_pipeline import build_export_snapshot
from ducai_studio.domain.effects import (
    ExportSettings,
    ExportSnapshot,
    RenderClip,
    RenderOverlay,
    SubtitleStyle,
    VideoFilterSettings,
)
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
from ducai_studio.providers.export.ffmpeg import (
    FfmpegCapabilities,
    _ass_color,
    _video_filter_chain,
    materialize_export_chunk,
)
from ducai_studio.providers.media_probe import MediaProbeResult


class Probe:
    def probe(self, _media_path):
        return MediaProbeResult(10_000_000, True, True, 1920, 1080, "mp4")


def _project(tmp_path: Path) -> ProjectSnapshot:
    video = tmp_path / "movie.mp4"
    voice = tmp_path / "voice.wav"
    video.write_bytes(b"video")
    voice.write_bytes(b"voice")
    timeline = TimeRange(0, 10_000_000)
    media = (
        MediaAsset("video", str(video), MediaKind.VIDEO, timeline.end_us),
        MediaAsset("voice", str(voice), MediaKind.AUDIO, timeline.end_us),
    )
    return ProjectSnapshot(
        "project",
        "Movie",
        1,
        media=media,
        tracks=(
            TimelineTrack(
                "V1",
                TrackKind.VIDEO,
                "Video",
                (TimelineClip("v", "V1", timeline, timeline, "video", volume=0.0),),
            ),
            TimelineTrack(
                "D1",
                TrackKind.VOICE,
                "Voice",
                (TimelineClip("d", "D1", timeline, timeline, "voice", volume=0.9),),
            ),
        ),
        translated_cues=(
            SubtitleCue("cue", 1, TimeRange(1_000_000, 2_000_000), "Xin chào.", "vi"),
        ),
    )


def test_default_overlays_cover_full_video_and_logo_asset_can_be_replaced(
    tmp_path: Path,
) -> None:
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"logo")
    seeded = ensure_default_overlays(_project(tmp_path), logo_path=logo)

    assert [item.id for item in seeded.overlays] == [DEFAULT_BLUR_ID, DEFAULT_LOGO_ID]
    assert all(item.range == TimeRange(0, 10_000_000) for item in seeded.overlays)
    assert seeded.overlays[0].enabled is True
    assert seeded.overlays[1].enabled is True

    updated = EffectsEditor(seeded).update_overlay(
        DEFAULT_BLUR_ID,
        x=0.1,
        width=0.8,
        blur_radius=30,
        enabled=False,
    )
    blur = next(item for item in updated.overlays if item.id == DEFAULT_BLUR_ID)
    assert (blur.x, blur.width, blur.blur_radius, blur.enabled) == (0.1, 0.8, 30, False)


def test_effects_editor_supports_multiple_blur_regions_and_deletes_added_region(
    tmp_path: Path,
) -> None:
    seeded = ensure_default_overlays(_project(tmp_path))
    editor = EffectsEditor(seeded)
    expanded = editor.add_blur("overlay-blur-extra")
    extra = next(item for item in expanded.overlays if item.id == "overlay-blur-extra")
    assert extra.kind.value == "blur"
    assert extra.range == TimeRange(0, 10_000_000)
    assert 0 < extra.x < 1 and 0 < extra.y < 1

    removed = editor.delete_overlay(extra.id)
    assert all(item.id != extra.id for item in removed.overlays)
    with pytest.raises(ValueError, match="disabled"):
        EffectsEditor(removed).delete_overlay(DEFAULT_BLUR_ID)


def test_export_snapshot_keeps_video_audio_subtitle_and_effect_contract(
    tmp_path: Path,
) -> None:
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"logo")
    project = ensure_default_overlays(_project(tmp_path), logo_path=logo)

    snapshot = build_export_snapshot(
        project,
        settings=ExportSettings(subtitle_style=SubtitleStyle(enabled=True)),
        media_probe=Probe(),
    )

    assert snapshot.video.path.endswith("movie.mp4")
    assert [item.track_kind for item in snapshot.audio] == ["voice"]
    assert {item.kind for item in snapshot.overlays} == {"blur", "logo"}
    assert snapshot.subtitles[0].text == "Xin chào."


def test_export_snapshot_preserves_cue_scoped_d1_clips(tmp_path: Path) -> None:
    project = _project(tmp_path)
    d1 = next(track for track in project.tracks if track.id == "D1")
    cue_ranges = (TimeRange(1_000_000, 1_900_000), TimeRange(3_000_000, 4_250_000))
    split_d1 = replace(
        d1,
        clips=tuple(
            TimelineClip(
                f"d{index}",
                "D1",
                cue_range,
                cue_range,
                "voice",
                volume=0.9,
            )
            for index, cue_range in enumerate(cue_ranges, 1)
        ),
    )
    split_project = replace(
        project,
        tracks=tuple(split_d1 if track.id == "D1" else track for track in project.tracks),
    )

    snapshot = build_export_snapshot(
        split_project,
        settings=ExportSettings(),
        media_probe=Probe(),
    )

    assert len(snapshot.audio) == 2
    assert [item.timeline_start_us for item in snapshot.audio] == [
        1_000_000,
        3_000_000,
    ]
    assert all(item.path.endswith("voice.wav") for item in snapshot.audio)


def test_chunk_plan_opens_cue_scoped_master_wav_once_on_windows(tmp_path: Path) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    voice = (tmp_path / "voice.wav").resolve()
    source.write_bytes(b"source")
    voice.write_bytes(b"voice")
    cue_duration_us = 600_000
    cue_count = 1_000
    duration_us = cue_duration_us * cue_count
    voice_clips = tuple(
        RenderClip(
            f"voice-{index}",
            "D1",
            "voice",
            str(voice),
            index * cue_duration_us,
            (index + 1) * cue_duration_us,
            index * cue_duration_us,
            (index + 1) * cue_duration_us,
        )
        for index in range(cue_count)
    )
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        RenderClip(
            "video",
            "V1",
            "video",
            str(source),
            0,
            duration_us,
            0,
            duration_us,
        ),
        voice_clips,
        (),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "work-many-cues",
        output_path=tmp_path / "many-cues.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")

    assert plan.arguments.count(str(voice)) == 1
    assert len(subprocess.list2cmdline(plan.arguments)) < 32_767
    assert graph.count("atrim=start=") >= cue_count
    assert graph.count("apad=whole_dur=") == 1
    assert ",apad," not in graph


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
def test_reused_voice_master_keeps_a_late_cue_after_a_timeline_gap(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    source = (tmp_path / "source.mp4").resolve()
    voice = (tmp_path / "voice.wav").resolve()
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=12:d=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-y",
            str(source),
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
            "sine=frequency=880:sample_rate=48000:duration=4",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(voice),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    cue_ranges = (
        (100_000, 500_000, 0, 400_000),
        (600_000, 1_000_000, 400_000, 800_000),
        (1_100_000, 1_500_000, 800_000, 1_200_000),
        (3_000_000, 3_400_000, 2_000_000, 2_400_000),
    )
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        4_000_000,
        160,
        90,
        RenderClip("video", "V1", "video", str(source), 0, 4_000_000, 0, 4_000_000),
        tuple(
            RenderClip(
                f"voice-{index}",
                "D1",
                "voice",
                str(voice),
                timeline_start,
                timeline_end,
                source_start,
                source_end,
            )
            for index, (
                timeline_start,
                timeline_end,
                source_start,
                source_end,
            ) in enumerate(cue_ranges)
        ),
        (),
        (),
        ExportSettings(
            backend="cpu",
            video_bitrate="300k",
            audio_bitrate="96k",
            fps=12,
            subtitle_style=SubtitleStyle(enabled=False),
        ),
    )
    output = tmp_path / "late-cue.mp4"
    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=4_000_000,
        work_dir=tmp_path / "work-late-cue",
        output_path=output,
        capabilities=FfmpegCapabilities(ffmpeg, "ffmpeg test", False, True, True),
    )

    subprocess.run(
        plan.arguments,
        check=True,
        capture_output=True,
        timeout=30,
    )
    decoded = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            "3.1",
            "-t",
            "0.2",
            "-i",
            str(output),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    samples = [
        int.from_bytes(decoded.stdout[index : index + 2], "little", signed=True)
        for index in range(0, len(decoded.stdout) - 1, 2)
    ]

    assert samples
    assert max(abs(sample) for sample in samples) > 500


def test_subtitle_opacity_and_video_filter_compile_to_real_ffmpeg_values() -> None:
    style = SubtitleStyle(
        primary_color="#FFFFFF",
        outline_color="#102030",
        alignment=3,
        opacity_percent=60,
    )
    video_filter = VideoFilterSettings(
        preset_id="cinematic-warm",
        intensity_percent=50,
        brightness_percent=10,
        contrast_percent=110,
        saturation_percent=120,
    )

    filters = _video_filter_chain(video_filter)

    assert _ass_color(style.primary_color, style.opacity_percent) == "&H66FFFFFF"
    assert filters[0].startswith("colorbalance=")
    assert filters[1].startswith("eq=brightness=0.1000:")
    assert "contrast=1.1440" in filters[1]
    assert "saturation=1.2720" in filters[1]


def test_grayscale_filter_blends_with_user_saturation() -> None:
    filters = _video_filter_chain(
        VideoFilterSettings(
            preset_id="grayscale",
            intensity_percent=50,
            saturation_percent=120,
        )
    )

    assert filters == ["eq=brightness=0.0000:contrast=1.0000:saturation=0.6000"]


def test_export_snapshot_rejects_multi_video_without_silently_flattening(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    track = project.tracks[0]
    second = TimelineClip(
        "v2",
        "V1",
        TimeRange(10_000_000, 20_000_000),
        TimeRange(0, 10_000_000),
        "video",
    )
    project = ProjectSnapshot(
        project.id,
        project.name,
        project.revision,
        media=project.media,
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (*track.clips, second)),),
    )

    with pytest.raises(ValueError, match="exactly one"):
        build_export_snapshot(
            project,
            settings=ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
            media_probe=Probe(),
        )


def test_chunk_plan_reuses_aligned_source_audio_and_skips_same_size_scale(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    voice = (tmp_path / "voice.wav").resolve()
    source.write_bytes(b"source")
    voice.write_bytes(b"voice")
    duration_us = 10_000_000
    video = RenderClip(
        "video",
        "V1",
        "video",
        str(source),
        0,
        duration_us,
        0,
        duration_us,
    )
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        video,
        (
            RenderClip(
                "original",
                "A1",
                "original_audio",
                str(source),
                0,
                duration_us,
                0,
                duration_us,
                volume=0.2,
            ),
            RenderClip(
                "voice",
                "D1",
                "voice",
                str(voice),
                1_234_567,
                duration_us,
                1_234_567,
                duration_us,
            ),
        ),
        (),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
    )
    capabilities = FfmpegCapabilities("ffmpeg", "ffmpeg test", True, True, True)

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "work",
        output_path=tmp_path / "chunk.mp4",
        capabilities=capabilities,
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")

    assert plan.arguments.count(str(source)) == 1
    assert plan.arguments.count(str(voice)) == 1
    assert "[0:a]" in graph
    assert "[1:a]" in graph
    assert "amix=inputs=2:duration=longest:normalize=0" in graph
    assert "alimiter=limit=0.84139514:level=0:latency=1" in graph
    assert "aresample=48000" in graph
    assert "adelay=59259S:all=1" in graph
    assert "scale=852:480:flags=lanczos" not in graph

    misaligned = replace(
        snapshot,
        audio=(
            replace(snapshot.audio[0], timeline_start_us=1_000_000),
            snapshot.audio[1],
        ),
    )
    separate_plan = materialize_export_chunk(
        misaligned,
        index=1,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "separate-work",
        output_path=tmp_path / "separate-chunk.mp4",
        capabilities=capabilities,
    )

    assert separate_plan.arguments.count(str(source)) == 2


def test_chunk_plan_decodes_static_logo_once_and_avoids_chunk_faststart(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    logo = (tmp_path / "logo.png").resolve()
    source.write_bytes(b"source")
    logo.write_bytes(b"logo")
    duration_us = 10_000_000
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        RenderClip(
            "video",
            "V1",
            "video",
            str(source),
            0,
            duration_us,
            0,
            duration_us,
        ),
        (),
        (
            RenderOverlay(
                "logo",
                "logo",
                0,
                duration_us,
                0.8,
                0.05,
                0.15,
                0.1,
                str(logo),
            ),
        ),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "work",
        output_path=tmp_path / "chunk.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", True, True, True),
    )

    assert plan.arguments.count(str(logo)) == 1
    assert "-loop" not in plan.arguments
    assert "-framerate" not in plan.arguments
    assert "-movflags" not in plan.arguments
    assert "eof_action=repeat" in plan.filter_script_path.read_text(encoding="utf-8")
