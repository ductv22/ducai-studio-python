from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ducai_studio.application.media_commands import MediaImportService
from ducai_studio.domain.project import (
    OverlayKind,
    ProjectSnapshot,
    SubtitleCue,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.providers.media_probe import FfprobeProvider, MediaProbeResult

SECOND = 1_000_000


class FakeProbe:
    def __init__(self, result: MediaProbeResult) -> None:
        self.result = result
        self.paths: list[Path] = []

    def probe(self, media_path: str | Path) -> MediaProbeResult:
        self.paths.append(Path(media_path))
        return self.result


def _ids():
    value = 0
    while True:
        value += 1
        yield str(value)


def test_video_import_auto_creates_detached_linked_video_and_audio_tracks(
    tmp_path: Path,
) -> None:
    video = tmp_path / "phim có dấu cách.mp4"
    video.write_bytes(b"fixture")
    probe = FakeProbe(MediaProbeResult(12 * SECOND, True, True, 1920, 1080, "mp4"))
    ids = _ids()
    service = MediaImportService(probe, id_factory=lambda: next(ids))

    snapshot = service.import_media(ProjectSnapshot("p", "Phim", 1), video)

    assert snapshot.revision == 2
    assert [track.kind for track in snapshot.tracks] == [
        TrackKind.VIDEO,
        TrackKind.ORIGINAL_AUDIO,
    ]
    video_clip = snapshot.tracks[0].clips[0]
    audio_clip = snapshot.tracks[1].clips[0]
    assert video_clip.volume == 0
    assert audio_clip.volume == 1
    assert video_clip.link_group_id == audio_clip.link_group_id
    assert video_clip.timeline_range == audio_clip.timeline_range
    assert probe.paths == [video.resolve()]


def test_video_import_seeds_saved_blur_and_logo_preset_for_full_timeline(
    tmp_path: Path,
) -> None:
    video = tmp_path / "phim.mp4"
    logo = tmp_path / "logo.png"
    video.write_bytes(b"fixture")
    logo.write_bytes(b"image")
    probe = FakeProbe(MediaProbeResult(12 * SECOND, True, True, 1920, 1080, "mp4"))
    ids = _ids()
    service = MediaImportService(probe, id_factory=lambda: next(ids))

    snapshot = service.import_media(
        ProjectSnapshot("p", "Phim", 1),
        video,
        default_logo_path=logo,
        overlay_preset={
            "blur": {
                "x": 0.12,
                "y": 0.79,
                "width": 0.71,
                "height": 0.13,
                "enabled": True,
                "blur_radius": 31,
            },
            "logo": {
                "x": 0.76,
                "y": 0.04,
                "width": 0.19,
                "height": 0.1,
                "enabled": True,
                "opacity": 0.72,
            },
        },
    )

    blur = next(item for item in snapshot.overlays if item.kind is OverlayKind.BLUR)
    seeded_logo = next(item for item in snapshot.overlays if item.kind is OverlayKind.LOGO)
    assert (blur.x, blur.y, blur.width, blur.height, blur.blur_radius) == (
        0.12,
        0.79,
        0.71,
        0.13,
        31,
    )
    assert (seeded_logo.x, seeded_logo.y, seeded_logo.opacity) == (0.76, 0.04, 0.72)
    assert seeded_logo.enabled is True
    assert blur.range.start_us == seeded_logo.range.start_us == 0
    assert blur.range.end_us == seeded_logo.range.end_us == 12 * SECOND
    assert next(item.path for item in snapshot.media if item.id == seeded_logo.asset_id) == str(
        logo.resolve()
    )


def test_second_video_appends_after_project_end_without_overwriting_tracks(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    probe = FakeProbe(MediaProbeResult(5 * SECOND, True, False, 1280, 720, "mp4"))
    ids = _ids()
    service = MediaImportService(probe, id_factory=lambda: next(ids))

    snapshot = service.import_media(ProjectSnapshot("p", "Phim", 1), first)
    snapshot = service.import_media(snapshot, second)

    clips = snapshot.tracks[0].clips
    assert len(clips) == 2
    assert clips[0].timeline_range.end_us == clips[1].timeline_range.start_us


def test_audio_only_import_auto_creates_independent_a2_track(tmp_path: Path) -> None:
    audio = tmp_path / "nhạc nền.wav"
    audio.write_bytes(b"fixture")
    probe = FakeProbe(MediaProbeResult(7 * SECOND, False, True, format_name="wav"))
    ids = _ids()
    service = MediaImportService(probe, id_factory=lambda: next(ids))

    snapshot = service.import_media(ProjectSnapshot("p", "Phim", 1), audio)

    assert [track.kind for track in snapshot.tracks] == [TrackKind.BACKGROUND_MUSIC]
    clip = snapshot.tracks[0].clips[0]
    assert clip.track_id == "A2"
    assert clip.link_group_id is None
    assert clip.volume == 1.0


def test_voice_import_replaces_d1_instead_of_accumulating_tracks(tmp_path: Path) -> None:
    first = tmp_path / "first_voice.wav"
    second = tmp_path / "second_voice.wav"
    first.write_bytes(b"fixture")
    second.write_bytes(b"fixture")
    probe = FakeProbe(MediaProbeResult(7 * SECOND, False, True, format_name="wav"))
    ids = _ids()
    service = MediaImportService(probe, id_factory=lambda: next(ids))

    snapshot = service.import_voice_media(ProjectSnapshot("p", "Phim", 1), first)
    updated = service.import_voice_media(snapshot, second)

    voice_tracks = [track for track in updated.tracks if track.kind is TrackKind.VOICE]
    assert len(voice_tracks) == 1
    assert voice_tracks[0].id == "D1"
    assert len(voice_tracks[0].clips) == 1
    referenced = voice_tracks[0].clips[0].media_id
    assert next(item.path for item in updated.media if item.id == referenced) == str(
        second.resolve()
    )
    assert all(item.path != str(first.resolve()) for item in updated.media)


def test_replacing_upstream_subtitles_invalidates_only_active_downstream_tracks(
    tmp_path: Path,
) -> None:
    source_range = TimeRange(0, SECOND)
    project = ProjectSnapshot(
        "p",
        "Phim",
        1,
        source_cues=(SubtitleCue("cue-1", 1, source_range, "旧字幕", "zh"),),
        translated_cues=(
            SubtitleCue("cue-1", 1, source_range, "Phụ đề cũ", "vi"),
        ),
    )
    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"voice-cache-must-survive")
    service = MediaImportService(
        FakeProbe(MediaProbeResult(SECOND, False, True, format_name="wav"))
    )
    voiced = service.import_voice_media(project, voice)

    replacement_source = tmp_path / "film_ZH_FINAL.srt"
    replacement_source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n新字幕\n",
        encoding="utf-8-sig",
    )
    replaced_source = service.import_srt(voiced, replacement_source, language="zh")

    assert [cue.text for cue in replaced_source.source_cues] == ["新字幕"]
    assert replaced_source.translated_cues == ()
    assert all(track.id not in {"D1"} for track in replaced_source.tracks)
    assert voice.read_bytes() == b"voice-cache-must-survive"

    replacement_translation = tmp_path / "film_ZH_FINAL_codex_vi.srt"
    replacement_translation.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nBản dịch mới\n",
        encoding="utf-8-sig",
    )
    translated_again = service.import_srt(
        voiced,
        replacement_translation,
        language="vi",
    )

    assert [cue.text for cue in translated_again.translated_cues] == ["Bản dịch mới"]
    assert all(track.id != "D1" for track in translated_again.tracks)
    assert voice.read_bytes() == b"voice-cache-must-survive"


def test_voice_import_splits_master_into_measured_cue_clips(tmp_path: Path) -> None:
    voice = tmp_path / "voice_master.wav"
    voice.write_bytes(b"fixture")
    probe = FakeProbe(MediaProbeResult(7 * SECOND, False, True, format_name="wav"))
    ids = _ids()
    service = MediaImportService(probe, id_factory=lambda: next(ids))

    project = ProjectSnapshot(
        "p",
        "Phim",
        1,
        source_cues=(
            SubtitleCue("cue-1", 1, TimeRange(500_000, 1_700_000), "一", "zh"),
            SubtitleCue("cue-2", 2, TimeRange(3_000_000, 4_250_000), "二", "zh"),
        ),
    )
    snapshot = service.import_voice_media(
        project,
        voice,
        cue_timing=(
            {
                "cue_id": "cue-1",
                "index": 1,
                "timeline_start_us": 500_000,
                "timeline_end_us": 1_700_000,
                "source_start_us": 500_000,
                "source_end_us": 1_700_000,
            },
            {
                "cue_id": "cue-2",
                "index": 2,
                "timeline_start_us": 3_000_000,
                "timeline_end_us": 4_250_000,
                "source_start_us": 3_000_000,
                "source_end_us": 4_250_000,
            },
        ),
    )

    voice_track = next(track for track in snapshot.tracks if track.id == "D1")
    assert len(voice_track.clips) == 2
    assert [clip.timeline_range.start_us for clip in voice_track.clips] == [
        500_000,
        3_000_000,
    ]
    assert [clip.timeline_range.end_us for clip in voice_track.clips] == [
        1_700_000,
        4_250_000,
    ]
    assert all(
        clip.source_range == clip.timeline_range for clip in voice_track.clips
    )
    assert len({clip.media_id for clip in voice_track.clips}) == 1


def test_voice_import_ignores_scene_text_when_validating_dialogue_ledger(
    tmp_path: Path,
) -> None:
    voice = tmp_path / "voice_master.wav"
    voice.write_bytes(b"fixture")
    service = MediaImportService(
        FakeProbe(MediaProbeResult(2 * SECOND, False, True, format_name="wav"))
    )
    project = ProjectSnapshot(
        "p",
        "Phim",
        1,
        source_cues=(
            SubtitleCue("dialogue-1", 1, TimeRange(0, SECOND), "你好", "zh"),
            SubtitleCue(
                "scene-2",
                2,
                TimeRange(200_000, 1_200_000),
                "海王兽",
                "zh",
                role="scene_text",
                orientation="vertical",
                style_hint="calligraphy",
            ),
        ),
    )

    snapshot = service.import_voice_media(
        project,
        voice,
        cue_timing=(
            {
                "cue_id": "dialogue-1",
                "index": 1,
                "timeline_start_us": 0,
                "timeline_end_us": SECOND,
                "source_start_us": 0,
                "source_end_us": SECOND,
            },
        ),
    )

    voice_track = next(track for track in snapshot.tracks if track.id == "D1")
    assert [clip.cue_id for clip in voice_track.clips] == ["dialogue-1"]


def test_voice_import_rejects_timing_for_a_different_source_cue_set(
    tmp_path: Path,
) -> None:
    voice = tmp_path / "voice_master.wav"
    voice.write_bytes(b"fixture")
    service = MediaImportService(
        FakeProbe(MediaProbeResult(2 * SECOND, False, True, format_name="wav"))
    )
    project = ProjectSnapshot(
        "p",
        "Phim",
        1,
        source_cues=(
            SubtitleCue("cue-current", 1, TimeRange(0, SECOND), "一", "zh"),
        ),
    )

    with pytest.raises(ValueError, match="ledger không khớp phụ đề nguồn"):
        service.import_voice_media(
            project,
            voice,
            cue_timing=(
                {
                    "cue_id": "cue-stale",
                    "index": 1,
                    "timeline_start_us": 0,
                    "timeline_end_us": SECOND,
                    "source_start_us": 0,
                    "source_end_us": SECOND,
                },
            ),
        )


def test_voice_import_rejects_timeline_interval_beyond_master_duration(
    tmp_path: Path,
) -> None:
    voice = tmp_path / "voice_master.wav"
    voice.write_bytes(b"fixture")
    service = MediaImportService(
        FakeProbe(MediaProbeResult(2 * SECOND, False, True, format_name="wav"))
    )
    project = ProjectSnapshot(
        "p",
        "Phim",
        1,
        source_cues=(
            SubtitleCue("cue-1", 1, TimeRange(0, SECOND), "一", "zh"),
        ),
    )

    with pytest.raises(ValueError, match="vượt quá thời lượng WAV master"):
        service.import_voice_media(
            project,
            voice,
            cue_timing=(
                {
                    "cue_id": "cue-1",
                    "index": 1,
                    "timeline_start_us": 2 * SECOND,
                    "timeline_end_us": 3 * SECOND,
                    "source_start_us": 0,
                    "source_end_us": SECOND,
                },
            ),
        )


def test_srt_import_replaces_source_track_with_contiguous_ids_and_real_timing(
    tmp_path: Path,
) -> None:
    srt = tmp_path / "phụ đề.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n你好。\n\n2\n00:00:04.250 --> 00:00:05.500\n下一句。\n",
        encoding="utf-8",
    )
    ids = _ids()
    service = MediaImportService(
        FakeProbe(MediaProbeResult(SECOND, False, True)),
        id_factory=lambda: next(ids),
    )

    snapshot = service.import_srt(ProjectSnapshot("p", "Phim", 1), srt)

    assert [cue.index for cue in snapshot.source_cues] == [1, 2]
    assert [cue.text for cue in snapshot.source_cues] == ["你好。", "下一句。"]
    assert snapshot.source_cues[1].range.start_us == 4_250_000
    assert snapshot.tracks[-1].kind is TrackKind.SUBTITLE
    assert len(snapshot.tracks[-1].clips) == 2


def test_vietnamese_srt_import_preserves_source_asset_ids_and_exact_timing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    translated = tmp_path / "source_codex_vi.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n你好。\n\n2\n00:00:04,250 --> 00:00:05,500\n下一句。\n",
        encoding="utf-8",
    )
    translated.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nXin chào.\n\n"
        "2\n00:00:04,250 --> 00:00:05,500\nCâu tiếp theo.\n",
        encoding="utf-8",
    )
    ids = _ids()
    service = MediaImportService(
        FakeProbe(MediaProbeResult(SECOND, False, True)),
        id_factory=lambda: next(ids),
    )
    snapshot = service.import_srt(ProjectSnapshot("p", "Phim", 1), source)
    source_media = snapshot.media
    source_track = snapshot.tracks

    updated = service.import_srt(snapshot, translated, language="vi")

    assert updated.media == source_media
    assert updated.tracks == source_track
    assert [cue.id for cue in updated.translated_cues] == [cue.id for cue in updated.source_cues]
    assert [cue.range for cue in updated.translated_cues] == [
        cue.range for cue in updated.source_cues
    ]
    assert [cue.text for cue in updated.translated_cues] == [
        "Xin chào.",
        "Câu tiếp theo.",
    ]


def test_vietnamese_srt_import_rejects_missing_or_retimed_cues(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    translated = tmp_path / "bad_codex_vi.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n你好。\n\n2\n00:00:04,250 --> 00:00:05,500\n下一句。\n",
        encoding="utf-8",
    )
    translated.write_text(
        "1\n00:00:01,100 --> 00:00:03,000\nXin chào.\n",
        encoding="utf-8",
    )
    ids = _ids()
    service = MediaImportService(
        FakeProbe(MediaProbeResult(SECOND, False, True)),
        id_factory=lambda: next(ids),
    )
    snapshot = service.import_srt(ProjectSnapshot("p", "Phim", 1), source)

    with pytest.raises(ValueError, match="cue count"):
        service.import_srt(snapshot, translated, language="vi")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools are unavailable",
)
def test_ffprobe_provider_reads_real_video_and_audio_without_shell(tmp_path: Path) -> None:
    output = tmp_path / "đường dẫn có dấu cách.mp4"
    command = [
        shutil.which("ffmpeg"),
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=160x90:r=10",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        "0.3",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-y",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=20)

    result = FfprobeProvider().probe(output)

    assert result.has_video is True
    assert result.has_audio is True
    assert (result.width, result.height) == (160, 90)
    assert 250_000 <= result.duration_us <= 500_000
