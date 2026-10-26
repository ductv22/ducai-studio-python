from __future__ import annotations

from dataclasses import replace

import pytest
from dialogue_casting.analysis import Settings, classify_cue, clear_ranges, validate_analysis
from dialogue_casting.inference import analysis_windows


def turn(start=0, end=4_000_000, speaker="a"):
    return {"start_us": start, "end_us": end, "speaker": speaker}


def evidence(probability=0.999, start=0, end=4_000_000, speaker="a"):
    return turn(start, end, speaker) | {"female_probability": probability}


def cue(start=0, end=4_000_000):
    return {"id": "cue-1", "start_us": start, "end_us": end}


@pytest.mark.parametrize("probability,label", [(0.999, "female"), (0.001, "male")])
def test_cue_uses_audible_speaker_evidence(probability, label):
    result = classify_cue(
        cue(), {"turns": [turn()], "evidence": [evidence(probability)]}, Settings()
    )
    assert result["label"] == label
    assert result["reason"] == "assigned"


def test_short_cue_can_use_long_reference_from_same_speaker():
    raw = {"turns": [turn(), turn(6_000_000, 6_400_000)], "evidence": [evidence()]}
    result = classify_cue(cue(6_000_000, 6_400_000), raw, Settings())
    assert result["label"] == "female"


def test_short_speaker_without_long_reference_needs_review():
    raw = {"turns": [turn(0, 400_000)], "evidence": []}
    assert (
        classify_cue(cue(0, 400_000), raw, Settings())["reason"] == "insufficient_speaker_reference"
    )


@pytest.mark.parametrize("overlap", [False, True])
def test_multiple_speakers_need_review_even_when_both_sound_female(overlap):
    other = turn(1_000_000 if overlap else 2_000_000, 4_000_000, "b")
    raw = {
        "turns": [turn(0, 2_000_000), other],
        "evidence": [evidence(end=2_000_000), evidence(start=other["start_us"], speaker="b")],
    }
    result = classify_cue(cue(), raw, Settings())
    assert result["label"] == "unknown"
    assert result["reason"] == ("overlapping_speakers" if overlap else "multiple_speakers")


def test_contradictory_speaker_references_are_not_averaged_into_a_label():
    raw = {"turns": [turn()], "evidence": [evidence(0.999), evidence(0.01)]}
    assert classify_cue(cue(), raw, Settings())["reason"] == "contradictory_speaker_evidence"


def test_sparse_speech_and_low_confidence_are_explicit_fallback_reasons():
    raw = {"turns": [turn(0, 1_000_000)], "evidence": [evidence(0.8, end=1_000_000)]}
    assert classify_cue(cue(), raw, Settings())["reason"] == "insufficient_speech"
    assert classify_cue(cue(0, 1_000_000), raw, Settings())["reason"] == "low_confidence"


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), -0.1, 1.1, True, "male"])
def test_malformed_classifier_output_is_an_error_even_outside_the_cue(probability):
    raw = {"turns": [turn()], "evidence": [evidence(probability)]}
    with pytest.raises(ValueError, match="probability"):
        classify_cue(cue(8_000_000, 9_000_000), raw, Settings())


def test_orphaned_classifier_evidence_is_an_error():
    with pytest.raises(ValueError, match="matching"):
        validate_analysis({"turns": [], "evidence": [evidence()]})


def test_reference_samples_exclude_overlapped_audio():
    assert clear_ranges(turn(), [turn(), turn(1_000_000, 2_000_000, "b")]) == [
        (0, 1_000_000),
        (2_000_000, 4_000_000),
    ]


def test_windows_include_cross_boundary_cues_and_are_bounded():
    item = cue(59_000_000, 63_000_000)
    windows = analysis_windows([item], 120_000_000, Settings())
    assert windows == [{"start_us": 0, "end_us": 68_000_000, "cues": [item]}]
    assert replace(Settings(), confidence=0.8).analysis_identity() == Settings().analysis_identity()


@pytest.mark.parametrize("item", [cue(0, 21_000_000), cue(119_000_000, 121_000_000)])
def test_unsupported_source_ranges_raise_instead_of_skipping_cues(item):
    with pytest.raises(ValueError):
        analysis_windows([item], 120_000_000, Settings())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"block_us": 140_000_000},
        {"threads": 20},
        {"confidence": float("nan")},
        {"min_speech_us": True},
    ],
)
def test_invalid_resource_or_confidence_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)
