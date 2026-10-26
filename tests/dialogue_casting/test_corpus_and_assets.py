from __future__ import annotations

import json
import wave
from collections import Counter

import pytest
from dialogue_casting.assets import (
    checked_path,
    file_sha256,
    install_models,
    model_files,
    verify_file,
)
from dialogue_casting.corpus import (
    LABEL_SCHEMA,
    load_labels,
    sample_cues,
    srt_cues,
    validate_corpus,
)
from dialogue_casting.inference import decode_audio
from test_evaluation import benchmark


def test_sampling_is_reproducible_disjoint_and_not_filtered_by_prediction():
    cues = [
        {"id": str(i), "start_us": i * 2_000_000, "end_us": i * 2_000_000 + 300_000}
        for i in range(100)
    ]
    first = sample_cues(cues, seed="fixed", calibration=15, test=70)
    assert first == sample_cues(list(reversed(cues)), seed="fixed", calibration=15, test=70)
    assert Counter(cue["split"] for cue in first) == {"calibration": 15, "test": 70}
    assert len({cue["id"] for cue in first}) == 85


def test_sampler_refuses_to_fill_counts_with_duplicate_audio():
    cues = [{"id": str(i), "start_us": 0, "end_us": 2_000_000} for i in range(10)]
    with pytest.raises(ValueError, match="disjoint"):
        sample_cues(cues, seed="fixed", calibration=1, test=2)


def test_srt_timestamps_stay_in_integer_microseconds_and_no_text_enters_pack(tmp_path):
    source = tmp_path / "câu.srt"
    source.write_text("1\n00:01:02,345 --> 00:01:04,678\n宝儿別睡\n", encoding="utf-8-sig")
    assert srt_cues(source) == [{"id": "srt-1", "start_us": 62_345_000, "end_us": 64_678_000}]


@pytest.mark.parametrize(
    "value", ["00:00:04,000 --> 00:00:03,000", "00:61:00,000 --> 01:03:00,000"]
)
def test_invalid_srt_is_an_error(tmp_path, value):
    path = tmp_path / "source.srt"
    path.write_text(f"1\n{value}\ntext\n", encoding="utf-8")
    with pytest.raises(ValueError):
        srt_cues(path)


def test_corpus_rejects_mutated_cues_and_duplicate_media():
    from dialogue_casting.assets import json_digest

    corpus, _, _ = benchmark()
    corpus["cues"][0]["end_us"] += 1
    with pytest.raises(ValueError, match="modified"):
        validate_corpus(corpus)
    corpus["films"][1]["source_sha256"] = corpus["films"][0]["source_sha256"]
    corpus["corpus_id"] = json_digest({k: v for k, v in corpus.items() if k != "corpus_id"})
    with pytest.raises(ValueError, match="same original media"):
        validate_corpus(corpus)


@pytest.mark.parametrize("mutation", ["source", "checksum", "duplicate", "unknown"])
def test_listening_label_identity_and_independence_are_required(tmp_path, mutation):
    corpus, _, labels = benchmark()
    document = {
        "schema": LABEL_SCHEMA,
        "corpus_id": corpus["corpus_id"],
        "annotator": "independent-listener",
        "label_source": "human-listening",
        "labels": list(labels.values()),
    }
    if mutation == "source":
        document["label_source"] = "model-predictions"
    elif mutation == "checksum":
        document["labels"][0]["clip_sha256"] = "different"
    elif mutation == "duplicate":
        document["labels"].append(document["labels"][0])
    else:
        document["labels"][0]["cue_id"] = "not-in-corpus"
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_labels(path, corpus)


def test_corrupt_model_is_an_actionable_error(tmp_path):
    path = tmp_path / "model.onnx"
    path.write_bytes(b"model")
    entry = {"size": 5, "sha256": file_sha256(path)}
    verify_file(path, entry)
    path.write_bytes(b"wrong")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        verify_file(path, entry)
    with pytest.raises(RuntimeError, match="Missing/incomplete"):
        model_files(tmp_path)


def test_offline_setup_never_downloads_missing_models(tmp_path):
    with pytest.raises(RuntimeError, match="Offline installer cache is incomplete"):
        install_models(tmp_path / "models", tmp_path / "downloads", offline=True)


def test_model_paths_cannot_escape_runtime(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        checked_path(tmp_path, "../model.onnx")


def test_real_ffmpeg_decodes_only_requested_audio_and_missing_audio_errors(tmp_path):
    path = tmp_path / "âm thanh gốc.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000)
    result = decode_audio(path, 100_000, 350_000, "ffmpeg")
    assert len(result) == 4000 * 4
    with pytest.raises(RuntimeError, match="missing/truncated"):
        decode_audio(path, 2_000_000, 3_000_000, "ffmpeg")
    with pytest.raises(ValueError, match="150 seconds"):
        decode_audio(path, 0, 151_000_000, "ffmpeg")
