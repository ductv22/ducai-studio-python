from __future__ import annotations

from copy import deepcopy

import pytest
from dialogue_casting.analysis import Settings
from dialogue_casting.speaker_consistency import classify_consistent_cues
from dialogue_casting.srt_demo import classify_srt_cue

SETTINGS = Settings(confidence=0.95)


def cue(cue_id, start, end, **values):
    return {"id": cue_id, "start_us": start, "end_us": end, **values}


def turn(start, end, speaker="a"):
    return {"start_us": start, "end_us": end, "speaker": speaker}


def sample(start, end, probability=0.99, speaker="a"):
    return {**turn(start, end, speaker), "female_probability": probability}


def same_speaker_case(probability=0.99):
    cues = [
        cue("first", 0, 2_000_000),
        cue("uncertain", 3_000_000, 5_000_000),
        cue("last", 6_000_000, 8_000_000),
    ]
    analysis = {
        "turns": [turn(0, 2_000_000), turn(3_000_000, 5_000_000), turn(6_000_000, 8_000_000)],
        "evidence": [sample(0, 2_000_000, probability), sample(6_000_000, 8_000_000, probability)],
    }
    return cues, analysis


@pytest.mark.parametrize("anchor,uncertain,label", [(0.99, 0.6, "female"), (0.01, 0.4, "male")])
def test_uncertain_cue_uses_two_independent_references_from_its_speaker(anchor, uncertain, label):
    cues, analysis = same_speaker_case(anchor)
    calls = []

    def probability(start, end):
        calls.append((start, end))
        return uncertain if start == 3_000_000 else anchor

    rows = classify_consistent_cues(cues, analysis, SETTINGS, probability)

    assert [row["machine"]["label"] for row in rows] == [label, label, label]
    assert rows[1]["machine"]["reason"] == "speaker_consensus"
    assert rows[1]["original_machine"]["label"] == "unknown"
    assert rows[1]["original_machine"]["reason"] == "low_confidence"
    assert rows[1]["speaker_consensus"] == {
        "speaker": "a",
        "label": label,
        "confidence": pytest.approx(0.99),
        "independent_samples": [
            {"start_us": 0, "end_us": 2_000_000, "confidence": pytest.approx(0.99)},
            {"start_us": 6_000_000, "end_us": 8_000_000, "confidence": pytest.approx(0.99)},
        ],
        "status": "accepted",
    }
    assert calls == [(0, 2_000_000), (3_000_000, 5_000_000), (6_000_000, 8_000_000)]
    assert "original_machine" not in rows[0]
    assert "original_machine" not in rows[2]


def test_alternating_speakers_are_resolved_separately_without_using_adjacency():
    spans = [
        (0, 2_000_000, "f"),
        (3_000_000, 5_000_000, "m"),
        (6_000_000, 8_000_000, "f"),
        (9_000_000, 11_000_000, "m"),
        (12_000_000, 14_000_000, "f"),
        (15_000_000, 17_000_000, "m"),
    ]
    cues = [cue(str(index), start, end) for index, (start, end, _) in enumerate(spans)]
    analysis = {
        "turns": [turn(*span) for span in spans],
        "evidence": [
            sample(start, end, 0.99 if speaker == "f" else 0.01, speaker)
            for start, end, speaker in spans[:4]
        ],
    }
    probabilities = {start: (0.99 if speaker == "f" else 0.01) for start, _, speaker in spans[:4]}
    probabilities.update({12_000_000: 0.6, 15_000_000: 0.4})
    rows = classify_consistent_cues(
        cues, analysis, SETTINGS, lambda start, _end: probabilities[start]
    )

    assert [row["machine"]["label"] for row in rows] == ["female", "male"] * 3
    assert rows[-2]["speaker_consensus"]["speaker"] == "f"
    assert rows[-1]["speaker_consensus"]["speaker"] == "m"


def test_confident_local_opposite_assignment_disqualifies_a_merged_speaker_group():
    cues, analysis = same_speaker_case()
    cues.append(cue("opposite", 9_000_000, 11_000_000))
    analysis["turns"].append(turn(9_000_000, 11_000_000))
    probabilities = {0: 0.99, 3_000_000: 0.6, 6_000_000: 0.99, 9_000_000: 0.01}

    def probability(start, _end):
        return probabilities[start]

    baseline = [classify_srt_cue(item, analysis, SETTINGS, probability) for item in cues]

    rows = classify_consistent_cues(cues, analysis, SETTINGS, probability)

    assert rows[-1] == baseline[-1]
    assert rows[1]["machine"] == baseline[1]["machine"]
    assert rows[1]["speaker_consensus"]["status"] == "conflict"
    assert rows[1]["speaker_consensus"]["reason"] == "conflicting_local_assignment"
    assert rows[1]["speaker_consensus"]["opposing_cue_ids"] == ["opposite"]


@pytest.mark.parametrize("probability", [0.01, 0.15, 0.2])
def test_opposite_local_sample_blocks_rescue_even_below_assignment_confidence(probability):
    cues, analysis = same_speaker_case()
    rows = classify_consistent_cues(
        cues,
        analysis,
        SETTINGS,
        lambda start, _end: probability if start == 3_000_000 else 0.99,
    )

    assert rows[1]["machine"]["label"] == ("male" if probability == 0.01 else "unknown")
    if probability != 0.01:
        assert rows[1]["speaker_consensus"]["reason"] == "opposite_sample"


def test_conflicting_global_references_do_not_become_a_majority_anchor():
    cues, analysis = same_speaker_case()
    analysis["evidence"].append(sample(9_000_000, 11_000_000, 0.01))
    analysis["turns"].append(turn(9_000_000, 11_000_000))

    def probability(start, _end):
        return 0.6 if start == 3_000_000 else 0.99

    baseline = [classify_srt_cue(item, analysis, SETTINGS, probability) for item in cues]

    assert classify_consistent_cues(cues, analysis, SETTINGS, probability) == baseline
    assert baseline[1]["comparison_machine"]["reason"] == "contradictory_speaker_evidence"


@pytest.mark.parametrize(
    "ranges",
    [
        [(0, 2_000_000), (0, 2_000_000)],
        [(0, 2_000_000), (1_000_000, 3_000_000)],
        [(0, 5_000_000), (1_000_000, 3_000_000)],
        [(0, 800_000), (2_000_000, 3_000_000)],
    ],
)
def test_duplicates_overlaps_and_short_samples_are_not_two_independent_references(ranges):
    item = cue("uncertain", 6_000_000, 8_000_000)
    analysis = {
        "turns": [turn(0, 8_000_000)],
        "evidence": [sample(start, end) for start, end in ranges],
    }
    baseline = classify_srt_cue(item, analysis, SETTINGS, lambda *_: 0.6)

    assert classify_consistent_cues([item], analysis, SETTINGS, lambda *_: 0.6) == [baseline]


def test_duplicate_global_and_local_crop_is_not_an_extra_reference():
    cues = [cue("strong", 0, 2_000_000), cue("uncertain", 3_000_000, 5_000_000)]
    analysis = {"turns": [turn(0, 5_000_000)], "evidence": [sample(0, 2_000_000)]}

    def probability(start, _end):
        return 0.99 if start == 0 else 0.6

    baseline = [classify_srt_cue(item, analysis, SETTINGS, probability) for item in cues]

    assert classify_consistent_cues(cues, analysis, SETTINGS, probability) == baseline


def test_a_disjoint_local_sample_can_support_an_existing_global_anchor():
    cues, analysis = same_speaker_case()
    analysis["evidence"] = analysis["evidence"][:1]
    rows = classify_consistent_cues(
        cues,
        analysis,
        SETTINGS,
        lambda start, _end: 0.6 if start == 3_000_000 else 0.99,
    )

    assert rows[1]["machine"]["label"] == "female"
    assert len(rows[1]["speaker_consensus"]["independent_samples"]) == 2


def test_touching_samples_at_the_minimum_length_are_independent():
    item = cue("uncertain", 3_000_000, 5_000_000)
    analysis = {
        "turns": [turn(0, 5_000_000)],
        "evidence": [sample(0, 1_000_000), sample(1_000_000, 2_000_000)],
    }

    row = classify_consistent_cues([item], analysis, SETTINGS, lambda *_: 0.6)[0]

    assert row["machine"]["reason"] == "speaker_consensus"
    assert len(row["speaker_consensus"]["independent_samples"]) == 2


def test_a_duplicate_crop_with_weaker_evidence_does_not_add_a_strong_vote():
    cues, analysis = same_speaker_case()

    def probability(start, _end):
        return 0.99 if start == 6_000_000 else 0.6

    baseline = [classify_srt_cue(item, analysis, SETTINGS, probability) for item in cues]

    assert classify_consistent_cues(cues, analysis, SETTINGS, probability) == baseline


def test_an_opposite_sample_disqualifies_a_group_even_when_too_short_to_anchor_it():
    cues, analysis = same_speaker_case()
    analysis["evidence"].append(sample(9_000_000, 9_500_000, 0.01))
    analysis["turns"].append(turn(9_000_000, 9_500_000))

    def probability(start, _end):
        return 0.6 if start == 3_000_000 else 0.99

    rows = classify_consistent_cues(cues, analysis, SETTINGS, probability)

    assert rows[1]["comparison_machine"]["label"] == "female"
    assert rows[1]["machine"]["label"] == "unknown"
    assert rows[1]["speaker_consensus"]["reason"] == "opposite_sample"


def test_local_samples_without_an_assigned_comparison_reference_cannot_start_a_rescue():
    cues, analysis = same_speaker_case()
    analysis["evidence"] = []
    cues.append(cue("short", 8_100_000, 8_600_000))
    analysis["turns"].append(turn(8_100_000, 8_600_000))

    def probability(start, _end):
        return 0.6 if start == 3_000_000 else 0.99

    baseline = [classify_srt_cue(item, analysis, SETTINGS, probability) for item in cues]

    assert classify_consistent_cues(cues, analysis, SETTINGS, probability) == baseline
    assert baseline[-1]["machine"]["reason"] == "insufficient_speaker_reference"


@pytest.mark.parametrize(
    "reason",
    [
        "no_speech",
        "insufficient_speech",
        "overlapping_speakers",
        "multiple_speakers",
        "overlapping_subtitles",
        "contradictory_speaker_evidence",
    ],
)
def test_structural_and_contradictory_results_remain_unchanged(reason):
    item = cue("target", 4_000_000, 6_000_000)
    analysis = {
        "turns": [turn(0, 2_000_000), turn(4_000_000, 6_000_000), turn(10_000_000, 12_000_000)],
        "evidence": [sample(0, 2_000_000), sample(10_000_000, 12_000_000)],
    }

    def probability(*_):
        return 0.99

    if reason == "no_speech":
        item.update(start_us=2_500_000, end_us=3_000_000)
    elif reason == "insufficient_speech":
        item.update(start_us=3_000_000, end_us=4_200_000)
    elif reason == "overlapping_speakers":
        analysis["turns"].append(turn(4_000_000, 6_000_000, "b"))
    elif reason == "multiple_speakers":
        analysis["turns"][1] = turn(4_000_000, 5_000_000)
        analysis["turns"].append(turn(5_000_000, 6_000_000, "b"))
    elif reason == "overlapping_subtitles":
        item["srt_overlap_ids"] = ["other"]
    else:
        item["end_us"] = 6_500_000
        analysis["turns"][1:2] = [turn(4_000_000, 5_200_000), turn(5_300_000, 6_500_000)]

        def probability(start, _end):
            return 0.99 if start == 4_000_000 else 0.01

    baseline = classify_srt_cue(item, analysis, SETTINGS, probability)

    assert baseline["machine"]["reason"] == reason
    assert classify_consistent_cues([item], analysis, SETTINGS, probability) == [baseline]


def test_text_and_cue_order_do_not_supply_evidence_or_mutate_the_inputs():
    cues, analysis = same_speaker_case()
    cues = [cues[2], cues[0], cues[1]]
    original = deepcopy((cues, analysis))

    def probability(start, _end):
        return 0.6 if start == 3_000_000 else 0.99

    expected = classify_consistent_cues(cues, analysis, SETTINGS, probability)
    changed_text = [{**item, "text": "他说她说 different speaker"} for item in cues]

    assert classify_consistent_cues(changed_text, analysis, SETTINGS, probability) == expected
    assert [row["cue_id"] for row in expected] == ["last", "first", "uncertain"]
    assert (cues, analysis) == original
