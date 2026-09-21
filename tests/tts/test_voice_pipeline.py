from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

import pytest

from ducai_studio.application.voice_pipeline import (
    VoicePipelineSettings,
    assemble_voice_timeline,
    classify_cue,
    measured_voice_timing_issues,
    plan_fit,
    process_voice_segment,
    read_pcm16_mono,
    write_pcm16_mono,
)
from ducai_studio.providers.audio_processing import _atempo_filter_chain
from ducai_studio.providers.tts.contracts import TtsCueRequest


def _speech_fixture(*, seconds: float = 0.5, click: bool = False) -> array:
    sample_rate = 48_000
    values = array("f")
    for index in range(int(sample_rate * seconds)):
        envelope = min(1.0, index / 400)
        values.append(0.2 * envelope * math.sin(2 * math.pi * 220 * index / sample_rate))
    if click:
        values[8] = 0.98
    return values


def test_onset_click_is_repaired_without_destroying_speech(tmp_path: Path) -> None:
    source = tmp_path / "raw.wav"
    target = tmp_path / "clean.wav"
    settings = VoicePipelineSettings()
    write_pcm16_mono(source, _speech_fixture(click=True), settings.sample_rate)

    report = process_voice_segment(source, target, settings)
    cleaned, rate = read_pcm16_mono(target)

    assert report.ok is True
    assert report.repaired_onset is True
    assert target.is_file()
    assert rate == 48_000
    assert len(cleaned) > 10_000
    assert max(abs(value) for value in cleaned) < 0.98


def test_total_speed_cap_is_not_compounded_with_base_read_speed() -> None:
    settings = VoicePipelineSettings(
        max_absolute_speed=2.25,
        voice_read_speed=1.5,
        adaptive_timing=True,
        max_reflow_us=0,
    )
    cue = TtsCueRequest("cue-1", 1, 0, 1_000_000, "Một câu thoại.", "hash")

    accepted = plan_fit(
        cue,
        audio_duration_us=2_000_000,
        next_start_us=1_000_000,
        settings=settings,
    )
    rejected = plan_fit(
        cue,
        audio_duration_us=3_000_000,
        next_start_us=1_000_000,
        settings=settings,
    )

    assert accepted.fits is True
    assert accepted.absolute_speed == 2.0
    assert accepted.base_speed == 1.5
    assert accepted.applied_total_speed == 2.0
    assert rejected.fits is False
    assert rejected.absolute_speed == 2.25
    assert rejected.blocker == "CUE_SYNC_LIMIT_EXCEEDED"


def test_adaptive_timing_caps_only_a_long_cue_and_reports_its_overflow() -> None:
    settings = VoicePipelineSettings(
        max_absolute_speed=1.5,
        adaptive_timing=True,
        max_reflow_us=0,
    )
    cue = TtsCueRequest("cue-long", 1, 0, 2_000_000, "Một câu dài.", "hash")

    planned = plan_fit(
        cue,
        audio_duration_us=4_000_000,
        next_start_us=2_000_000,
        settings=settings,
    )

    assert planned.fits is False
    assert planned.absolute_speed == 1.5
    assert planned.timeline_overflow_us == 666_667
    assert planned.adaptive_timing is True


def test_base_speed_and_long_cue_cap_are_total_speeds_not_additive() -> None:
    cue = TtsCueRequest("cue-total", 1, 0, 2_000_000, "Một câu dài.", "hash")
    same_cap = plan_fit(
        cue,
        audio_duration_us=4_000_000,
        next_start_us=None,
        settings=VoicePipelineSettings(
            voice_read_speed=1.5,
            max_absolute_speed=1.5,
            adaptive_timing=True,
        ),
    )
    higher_cap = plan_fit(
        cue,
        audio_duration_us=4_000_000,
        next_start_us=None,
        settings=VoicePipelineSettings(
            voice_read_speed=1.5,
            max_absolute_speed=2.25,
            adaptive_timing=True,
        ),
    )

    assert same_cap.applied_total_speed == 1.5
    assert same_cap.final_duration_us == 2_666_667
    assert same_cap.fits is False
    # The required total is 2.0x. A 2.25x cap must not over-speed the cue.
    assert higher_cap.required_total_speed == 2.0
    assert higher_cap.applied_total_speed == 2.0
    assert higher_cap.final_duration_us == 2_000_000
    assert higher_cap.fits is True


@pytest.mark.parametrize(
    ("audio_duration_us", "expected_fits"),
    ((1_050_000, True), (1_050_001, False)),
)
def test_timing_tolerance_is_exactly_fifty_milliseconds(
    audio_duration_us: int,
    expected_fits: bool,
) -> None:
    settings = VoicePipelineSettings(max_absolute_speed=1.0, max_reflow_us=0)
    cue = TtsCueRequest("cue-tolerance", 1, 0, 1_000_000, "Một câu.", "hash")

    planned = plan_fit(
        cue,
        audio_duration_us=audio_duration_us,
        next_start_us=None,
        settings=settings,
    )

    assert planned.fits is expected_fits


def test_timing_tolerance_never_allows_a_cue_to_cross_the_next_timestamp() -> None:
    settings = VoicePipelineSettings(max_absolute_speed=1.0, max_reflow_us=0)
    cue = TtsCueRequest("cue-collision", 1, 0, 1_000_000, "Một câu.", "hash")

    planned = plan_fit(
        cue,
        audio_duration_us=1_020_000,
        next_start_us=1_000_000,
        settings=settings,
    )

    assert planned.timeline_overflow_us == 20_000
    assert planned.fits is False
    assert planned.blocker == "CUE_SYNC_LIMIT_EXCEEDED"


def test_measured_timing_review_ignores_fit_overflow_within_fifty_ms() -> None:
    cue = TtsCueRequest("cue-rounding", 1, 0, 1_000_000, "Một câu.", "hash")
    entries = {
        cue.cue_id: {
            "fit": {
                "fits": True,
                "timeline_overflow_us": 20_000,
                "required_total_speed": 1.02,
                "applied_total_speed": 1.0,
                "audio_duration_us": 1_020_000,
                "allowed_duration_us": 1_000_000,
            }
        }
    }

    assert measured_voice_timing_issues(
        (cue,), entries, {}, max_absolute_speed=1.5
    ) == ()


def test_non_dialogue_is_explicitly_ineligible() -> None:
    assert classify_cue("[Nhạc nền]").reason == "non_dialogue_marker"
    assert classify_cue("  ").reason == "empty_after_normalization"
    assert classify_cue("Ta sẽ trở về tông môn.").eligible is True


def test_three_times_speed_uses_a_valid_atempo_chain() -> None:
    assert _atempo_filter_chain(3.0) == "atempo=2.00000000,atempo=1.50000000"


def test_timeline_assembly_has_exact_duration(tmp_path: Path) -> None:
    from ducai_studio.application.voice_pipeline import (
        AudioQualityReport,
        VoiceClip,
    )

    wav = tmp_path / "cue.wav"
    write_pcm16_mono(wav, _speech_fixture(seconds=0.25), 48_000)
    qa = AudioQualityReport(True, 250_000, 48_000, -10, -21, 0, 0.1, -12)
    output = tmp_path / "timeline.wav"
    progress: list[tuple[int, int]] = []

    report = assemble_voice_timeline(
        (VoiceClip("cue-1", 500_000, 900_000, wav, qa, "hash"),),
        output,
        total_duration_us=2_000_000,
        progress=lambda current, total: progress.append((current, total)),
    )

    with wave.open(str(output), "rb") as stream:
        assert stream.getnframes() == 96_000
        assert stream.getframerate() == 48_000
    assert report["overlap_count"] == 0
    assert progress == [(1, 1)]


def test_timeline_assembly_streams_the_exact_published_pcm_payload(tmp_path: Path) -> None:
    from ducai_studio.application.voice_pipeline import AudioQualityReport, VoiceClip

    wav = tmp_path / "cue.wav"
    write_pcm16_mono(wav, _speech_fixture(seconds=0.25), 48_000)
    qa = AudioQualityReport(True, 250_000, 48_000, -10, -21, 0, 0.1, -12)
    output = tmp_path / "timeline.wav"
    streamed: list[bytes] = []

    assemble_voice_timeline(
        (VoiceClip("cue-1", 500_000, 750_000, wav, qa, "hash"),),
        output,
        total_duration_us=2_000_000,
        pcm_sink=streamed.append,
    )

    with wave.open(str(output), "rb") as stream:
        published_pcm = stream.readframes(stream.getnframes())
    assert b"".join(streamed) == published_pcm


def test_timeline_assembly_streams_without_allocating_full_float_timeline(
    monkeypatch, tmp_path: Path
) -> None:
    import ducai_studio.application.voice_pipeline as voice_module
    from ducai_studio.application.voice_pipeline import AudioQualityReport, VoiceClip

    wav = tmp_path / "cue.wav"
    write_pcm16_mono(wav, _speech_fixture(seconds=0.1), 48_000)
    qa = AudioQualityReport(True, 100_000, 48_000, -10, -21, 0, 0.1, -12)
    output = tmp_path / "streamed.wav"
    monkeypatch.setattr(
        voice_module,
        "array",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("timeline assembly allocated a float mix")
        ),
    )

    assemble_voice_timeline(
        (VoiceClip("cue-1", 500_000, 600_000, wav, qa, "hash"),),
        output,
        total_duration_us=2_000_000,
    )

    with wave.open(str(output), "rb") as stream:
        assert stream.getnframes() == 96_000


def test_timeline_assembly_streaming_limiter_scales_only_voice_frames(
    tmp_path: Path,
) -> None:
    from ducai_studio.application.voice_pipeline import AudioQualityReport, VoiceClip

    wav = tmp_path / "hot.wav"
    write_pcm16_mono(wav, array("f", [0.999] * 4_800), 48_000)
    qa = AudioQualityReport(True, 100_000, 48_000, 0.0, -1.0, 0, 0, 0.0)
    output = tmp_path / "limited.wav"

    report = assemble_voice_timeline(
        (VoiceClip("cue-hot", 500_000, 600_000, wav, qa, "hash"),),
        output,
        total_duration_us=1_000_000,
    )
    samples, _rate = read_pcm16_mono(output)

    assert report["peak_before_limiter"] == 1.0
    assert max(abs(sample) for sample in samples) <= 0.981


def test_timeline_assembly_rejects_overlap_before_publishing_stale_wav(
    tmp_path: Path,
) -> None:
    from ducai_studio.application.voice_pipeline import AudioQualityReport, VoiceClip

    wav = tmp_path / "cue.wav"
    write_pcm16_mono(wav, _speech_fixture(seconds=0.25), 48_000)
    qa = AudioQualityReport(True, 250_000, 48_000, -10, -21, 0, 0.1, -12)
    output = tmp_path / "overlap.wav"
    output.write_bytes(b"stale")
    streamed: list[bytes] = []

    report = assemble_voice_timeline(
        (
            VoiceClip("cue-1", 0, 250_000, wav, qa, "one"),
            VoiceClip("cue-2", 100_000, 350_000, wav, qa, "two"),
        ),
        output,
        total_duration_us=1_000_000,
        pcm_sink=streamed.append,
    )

    assert report["overlap_count"] == 1
    assert output.exists() is False
    assert streamed == []


@pytest.mark.parametrize(
    ("start_us", "error_code"),
    [
        (-100_000, "voice_clip_starts_before_timeline"),
        (1_000_000, "voice_clip_starts_outside_timeline"),
        (900_000, "voice_clip_exceeds_timeline"),
    ],
)
def test_timeline_assembly_rejects_clips_outside_target_without_truncating(
    tmp_path: Path,
    start_us: int,
    error_code: str,
) -> None:
    from ducai_studio.application.voice_pipeline import AudioQualityReport, VoiceClip

    wav = tmp_path / "cue.wav"
    write_pcm16_mono(wav, _speech_fixture(seconds=0.25), 48_000)
    qa = AudioQualityReport(True, 250_000, 48_000, -10, -21, 0, 0.1, -12)
    output = tmp_path / "outside.wav"
    output.write_bytes(b"stale")

    with pytest.raises(ValueError, match=error_code):
        assemble_voice_timeline(
            (
                VoiceClip(
                    "cue-outside",
                    start_us,
                    start_us + 250_000,
                    wav,
                    qa,
                    "hash",
                ),
            ),
            output,
            total_duration_us=1_000_000,
        )

    assert output.exists() is False
