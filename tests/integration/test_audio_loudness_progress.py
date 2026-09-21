from __future__ import annotations

import math
import shutil
import wave
from array import array

import pytest

from ducai_studio.application.voice_pipeline import write_pcm16_mono
from ducai_studio.providers.audio_processing import FfmpegAudioProcessor


def test_master_ebu_r128_reports_progress_for_measure_and_apply(tmp_path) -> None:
    executable = shutil.which("ffmpeg")
    if not executable:
        pytest.skip("FFmpeg is unavailable")
    source = tmp_path / "source.wav"
    target = tmp_path / "mastered.wav"
    sample_rate = 48_000
    samples = array(
        "f",
        (0.12 * math.sin(2 * math.pi * 220 * index / sample_rate) for index in range(96_000)),
    )
    write_pcm16_mono(source, samples, sample_rate)
    events: list[tuple[str, int, int, int | None]] = []

    FfmpegAudioProcessor(executable).master_ebu_r128(
        source,
        target,
        progress=lambda stage, current, total, eta: events.append(
            (stage, current, total, eta)
        ),
    )

    assert target.is_file()
    assert {stage for stage, _current, _total, _eta in events} == {"measure", "apply"}
    assert events[-1] == ("apply", 2_000_000, 2_000_000, 0)
    assert any(
        stage == "measure" and 0 < current <= total and eta is not None
        for stage, current, total, eta in events
    )


def test_streamed_pcm_measurement_matches_the_file_first_pass(tmp_path) -> None:
    executable = shutil.which("ffmpeg")
    if not executable:
        pytest.skip("FFmpeg is unavailable")
    source = tmp_path / "source.wav"
    sample_rate = 48_000
    samples = array(
        "f",
        (0.12 * math.sin(2 * math.pi * 220 * index / sample_rate) for index in range(96_000)),
    )
    write_pcm16_mono(source, samples, sample_rate)
    processor = FfmpegAudioProcessor(executable)
    expected = processor.measure_ebu_r128(source)
    events: list[tuple[str, int, int, int | None]] = []
    session = processor.open_pcm16_ebu_r128_measure(
        sample_rate=sample_rate,
        duration_us=2_000_000,
        progress=lambda stage, current, total, eta: events.append(
            (stage, current, total, eta)
        ),
    )
    try:
        with wave.open(str(source), "rb") as stream:
            while raw := stream.readframes(16_384):
                session.write(raw)
        measured = session.finish()
    finally:
        session.abort()

    assert measured == expected
    assert events[-1] == ("measure", 2_000_000, 2_000_000, 0)
