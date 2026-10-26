from __future__ import annotations

import copy
import wave

import pytest
from dialogue_casting.assets import file_sha256
from dialogue_casting.demo import (
    speaker_aligned_segments,
    validate_asr,
    write_segment_clip,
)


def excerpt():
    return {
        "start_us": 10_000_000,
        "analysis": {
            "turns": [
                {"start_us": 10_000_000, "end_us": 12_000_000, "speaker": "a"},
                {"start_us": 12_000_000, "end_us": 14_000_000, "speaker": "b"},
            ]
        },
    }


def test_long_whisper_sentence_splits_at_measured_speaker_change_without_losing_words():
    segment = {
        "start_us": 0, "end_us": 4_000_000, "text": "甲乙丙丁",
        "words": [
            {"start_us": i * 1_000_000, "end_us": (i + 1) * 1_000_000, "text": text}
            for i, text in enumerate("甲乙丙丁")
        ],
    }
    result = speaker_aligned_segments([segment], excerpt())
    assert result == [
        {"start_us": 0, "end_us": 2_000_000, "text": "甲乙"},
        {"start_us": 2_000_000, "end_us": 4_000_000, "text": "丙丁"},
    ]


def test_word_alignment_with_missing_text_keeps_raw_sentence_instead_of_dropping_words():
    segment = {
        "start_us": 0, "end_us": 4_000_000, "text": "不能丢字",
        "words": [{"start_us": 0, "end_us": 1_000_000, "text": "丢字"}],
    }
    assert speaker_aligned_segments([segment], excerpt()) == [
        {"start_us": 0, "end_us": 4_000_000, "text": "不能丢字"}
    ]


def test_zero_length_tokens_at_sentence_edges_are_retained_in_positive_length_cues():
    segment = {
        "start_us": 0, "end_us": 4_000_000, "text": "甲乙。丙丁",
        "words": [
            {"start_us": 0, "end_us": 0, "text": "甲"},
            {"start_us": 0, "end_us": 1_000_000, "text": "乙。"},
            {"start_us": 1_000_000, "end_us": 2_000_000, "text": "丙"},
            {"start_us": 2_000_000, "end_us": 2_000_000, "text": "丁"},
        ],
    }
    result = speaker_aligned_segments([segment], excerpt())
    assert "".join(r["text"] for r in result) == "甲乙。丙丁"
    assert all(r["end_us"] > r["start_us"] for r in result)


def test_all_zero_word_times_preserve_raw_valid_segment():
    segment = {
        "start_us": 0, "end_us": 1_000_000, "text": "甲",
        "words": [{"start_us": 0, "end_us": 0, "text": "甲"}],
    }
    assert speaker_aligned_segments([segment], excerpt()) == [
        {"start_us": 0, "end_us": 1_000_000, "text": "甲"}
    ]


def test_original_audio_clip_at_excerpt_edges_is_not_padded_or_truncated(tmp_path):
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x02" * 16000)
    target = tmp_path / "target.wav"
    begin, end = write_segment_clip(source, target, 100_000, 900_000)
    assert (begin, end) == (100_000, 900_000)
    with wave.open(str(target), "rb") as wav:
        assert wav.getnframes() == 16000
        assert wav.readframes(16000) == b"\x01\x02" * 16000


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "hash", "time", "word", "path", "duration"]
)
def test_paired_asr_rejects_incomplete_changed_or_invalid_audio_results(tmp_path, mutation):
    audio = tmp_path / "source.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 16000)
    spec = {
        "experiment_id": "experiment",
        "audio": [{"id": "a", "path": "source.wav", "sha256": file_sha256(audio)}],
    }
    row = {
        "audio_id": "a", "audio_sha256": file_sha256(audio), "duration_us": 1_000_000,
        "segments": [{
            "start_us": 0, "end_us": 500_000, "text": "甲",
            "words": [{"start_us": 0, "end_us": 500_000, "text": "甲"}],
        }],
    }
    result = {
        "schema": "ducai-whisper-comparison-v1", "experiment_id": "experiment",
        "model_id": "belle-zh", "results": [copy.deepcopy(row)],
    }
    if mutation == "missing":
        result["results"] = []
    elif mutation == "duplicate":
        result["results"].append(row)
    elif mutation == "hash":
        audio.write_bytes(b"changed")
    elif mutation == "time":
        result["results"][0]["segments"][0]["end_us"] = 2_000_000
    elif mutation == "word":
        result["results"][0]["segments"][0]["words"][0]["start_us"] = -1
    elif mutation == "path":
        spec["audio"][0]["path"] = "../source.wav"
    elif mutation == "duration":
        result["results"][0]["duration_us"] = 2_000_000
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_asr(result, spec, tmp_path, "belle-zh")
