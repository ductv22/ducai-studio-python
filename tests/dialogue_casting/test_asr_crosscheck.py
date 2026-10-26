from __future__ import annotations

import copy

import pytest
from dialogue_casting.analysis import Settings
from dialogue_casting.asr_crosscheck import crosscheck_cues, moss_sample_ranges

SETTINGS = Settings(confidence=0.95)


def cue(
    cue_id: str = "c", start: int = 4_000_000, end: int = 5_000_000, text: str = "你好"
) -> dict:
    return {"id": cue_id, "start_us": start, "end_us": end, "text": text}


def window(
    window_id: str = "w",
    start: int = 0,
    end: int = 10_000_000,
    turns: list[dict] | None = None,
) -> dict:
    return {
        "id": window_id,
        "start_us": start,
        "end_us": end,
        "turns": (
            turns
            if turns is not None
            else [{"speaker": "s1", "start_us": 1_000_000, "end_us": 9_000_000, "text": "你好"}]
        ),
    }


def evidence(start: int, end: int, probability: float, window_id: str = "w") -> dict:
    return {
        "window_id": window_id,
        "start_us": start,
        "end_us": end,
        "female_probability": probability,
    }


def fresh_evidence(
    start: int,
    end: int,
    probability: float,
    *,
    origin: str = "moss_speaker",
    cue_id: str | None = None,
    window_id: str = "w",
) -> dict:
    row = {
        **evidence(start, end, probability, window_id),
        "origin": origin,
        "speaker": "s1",
    }
    if cue_id is not None:
        row["cue_id"] = cue_id
    return row


def base(cue_id: str = "c", label: str = "unknown", reason: str = "low_confidence") -> dict:
    return {cue_id: {"label": label, "reason": reason, "confidence": None}}


def test_rescue_uses_two_disjoint_anchors_outside_a_short_target_cue() -> None:
    rows = crosscheck_cues(
        [cue(start=6_000_000, end=6_500_000)],
        [window()],
        [evidence(1_000_000, 2_000_000, 0.99), evidence(2_500_000, 3_500_000, 0.98)],
        base(),
        SETTINGS,
    )

    assert set(rows[0]) == {"cue_id", "machine", "baseline", "crosscheck"}
    assert rows[0]["machine"]["label"] == "female"
    assert rows[0]["machine"]["reason"] == "asr_speaker_consensus"
    assert rows[0]["crosscheck"]["status"] == "rescued"
    assert len(rows[0]["crosscheck"]["anchors"]) == 2


def test_no_speech_is_never_rescued_or_sent_to_fallback() -> None:
    original = base(reason="no_speech")
    rows = crosscheck_cues([cue()], [window(turns=[])], [], original, SETTINGS)

    assert rows[0]["machine"] == original["c"]
    assert rows[0]["crosscheck"]["reason"] == "structural_no_speech"


@pytest.mark.parametrize(
    "turns,reason",
    [
        (
            [
                {"speaker": "s1", "start_us": 4_000_000, "end_us": 5_000_000},
                {"speaker": "s2", "start_us": 4_000_000, "end_us": 5_000_000},
            ],
            "structural_overlapping_speakers",
        ),
        (
            [
                {"speaker": "s1", "start_us": 4_000_000, "end_us": 4_600_000},
                {"speaker": "s2", "start_us": 4_600_000, "end_us": 5_000_000},
            ],
            "structural_multiple_speakers",
        ),
        (
            [{"speaker": "s1", "start_us": 4_000_000, "end_us": 4_300_000}],
            "structural_insufficient_speech",
        ),
    ],
)
def test_structural_gates_keep_uncertain_decisions(turns: list[dict], reason: str) -> None:
    rows = crosscheck_cues(
        [cue()],
        [window(turns=turns)],
        [evidence(1_000_000, 2_000_000, 0.99), evidence(2_500_000, 3_500_000, 0.99)],
        base(),
        SETTINGS,
    )

    assert rows[0]["machine"] == base()["c"]
    assert rows[0]["crosscheck"]["reason"] == reason


def test_window_with_largest_edge_margin_is_selected() -> None:
    windows = [
        window("narrow", 3_500_000, 6_000_000),
        window("wide", 0, 10_000_000),
    ]
    rows = crosscheck_cues(
        [cue()],
        windows,
        [evidence(1_000_000, 2_000_000, 0.99), evidence(2_500_000, 3_500_000, 0.99)],
        base(),
        SETTINGS,
    )

    assert rows[0]["crosscheck"]["window_id"] == "wide"


def test_overlapping_and_duplicate_crops_do_not_count_as_two_anchors() -> None:
    rows = crosscheck_cues(
        [cue()],
        [window()],
        [
            evidence(1_000_000, 2_000_000, 0.99),
            evidence(1_000_000, 2_000_000, 0.98),
            evidence(1_500_000, 2_500_000, 0.99),
        ],
        base(),
        SETTINGS,
    )

    assert rows[0]["machine"] == base()["c"]
    assert rows[0]["crosscheck"]["reason"] == "insufficient_independent_anchors"


def test_opposing_evidence_blocks_rescue() -> None:
    rows = crosscheck_cues(
        [cue()],
        [window()],
        [
            evidence(1_000_000, 2_000_000, 0.99),
            evidence(2_500_000, 3_500_000, 0.99),
            evidence(6_000_000, 7_000_000, 0.01),
        ],
        base(),
        SETTINGS,
    )

    assert rows[0]["machine"] == base()["c"]
    assert rows[0]["crosscheck"]["reason"] == "contradictory_acoustic_evidence"


def test_text_mismatch_is_advisory_and_cannot_change_acoustic_label() -> None:
    rows = crosscheck_cues(
        [cue(text="今天天气很好")],
        [window()],
        [evidence(1_000_000, 2_000_000, 0.99), evidence(2_500_000, 3_500_000, 0.99)],
        base(),
        SETTINGS,
    )

    assert rows[0]["machine"]["label"] == "female"
    assert rows[0]["crosscheck"]["text"]["status"] == "mismatch"


def test_confident_assignment_is_preserved_but_opposite_group_is_reported() -> None:
    original = base(label="female", reason="assigned")
    rows = crosscheck_cues(
        [cue()],
        [window()],
        [evidence(1_000_000, 2_000_000, 0.01), evidence(2_500_000, 3_500_000, 0.01)],
        original,
        SETTINGS,
    )

    assert rows[0]["machine"] == original["c"]
    assert rows[0]["crosscheck"]["status"] == "conflict"
    assert rows[0]["crosscheck"]["reason"] == "opposing_evidence"


def test_window_local_speaker_ids_are_not_merged_across_windows() -> None:
    windows = [
        window("first", 0, 5_000_000),
        window("second", 5_000_000, 10_000_000),
    ]
    rows = crosscheck_cues(
        [cue(start=5_100_000, end=5_600_000)],
        windows,
        [evidence(1_000_000, 2_000_000, 0.99, "first")],
        base(),
        SETTINGS,
    )

    assert rows[0]["crosscheck"]["window_id"] == "second"
    assert rows[0]["machine"] == base()["c"]


def test_anchor_intersecting_another_speaker_turn_is_rejected() -> None:
    turns = [
        {"speaker": "s1", "start_us": 1_000_000, "end_us": 4_000_000},
        {"speaker": "s2", "start_us": 2_000_000, "end_us": 2_500_000},
    ]
    rows = crosscheck_cues(
        [cue(start=3_000_000, end=4_000_000)],
        [window(turns=turns)],
        [evidence(1_000_000, 2_200_000, 0.99), evidence(2_600_000, 3_600_000, 0.99)],
        base(),
        SETTINGS,
    )

    assert rows[0]["machine"] == base()["c"]
    assert rows[0]["crosscheck"]["reason"] == "insufficient_independent_anchors"


def test_inputs_are_not_mutated_and_missing_window_is_explicit() -> None:
    cues = [cue(start=20_000_000, end=21_000_000)]
    baseline = base()
    original = copy.deepcopy((cues, baseline))
    rows = crosscheck_cues(cues, [window(end=10_000_000)], [], baseline, SETTINGS)

    assert rows[0]["crosscheck"]["reason"] == "no_complete_window"
    assert (cues, baseline) == original


@pytest.mark.parametrize(
    "bad", [{"c": {"label": "unknown"}}, {"c": {"label": "robot", "reason": "x"}}]
)
def test_invalid_baseline_is_rejected(bad: dict) -> None:
    with pytest.raises(ValueError):
        crosscheck_cues([cue()], [window()], [], bad, SETTINGS)


def test_moss_samples_are_disjoint_bounded_and_do_not_cross_turn_boundaries() -> None:
    turns = [
        {"speaker": "s1", "start_us": 0, "end_us": 4_000_000},
        # Touching same-speaker turns remain distinct provenance boundaries.
        {"speaker": "s1", "start_us": 4_000_000, "end_us": 8_000_000},
        {"speaker": "s1", "start_us": 8_000_000, "end_us": 14_000_000},
    ]

    rows = moss_sample_ranges(window(end=14_000_000, turns=turns), [], SETTINGS)

    assert len(rows) == SETTINGS.max_samples_per_speaker
    assert all(row["origin"] == "moss_speaker" for row in rows)
    assert all(
        SETTINGS.min_sample_us <= row["end_us"] - row["start_us"] <= SETTINGS.max_sample_us
        for row in rows
    )
    assert all(
        any(
            turn["start_us"] <= row["start_us"] < row["end_us"] <= turn["end_us"]
            for turn in turns
        )
        for row in rows
    )
    ordered = sorted(rows, key=lambda row: row["start_us"])
    assert all(
        left["end_us"] <= right["start_us"]
        for left, right in zip(ordered, ordered[1:], strict=False)
    )


def test_moss_samples_clip_competing_speaker_and_record_cue_provenance() -> None:
    target = cue("target", start=6_000_000, end=7_500_000)
    turns = [
        {"speaker": "s1", "start_us": 0, "end_us": 10_000_000},
        {"speaker": "s2", "start_us": 4_000_000, "end_us": 5_500_000},
    ]

    rows = moss_sample_ranges(window(end=10_000_000, turns=turns), [target], SETTINGS)
    speaker_rows = [
        row for row in rows if row["origin"] == "moss_speaker" and row["speaker"] == "s1"
    ]
    cue_rows = [row for row in rows if row["origin"] == "moss_cue"]

    assert speaker_rows
    assert all(row["end_us"] <= 4_000_000 or row["start_us"] >= 5_500_000 for row in speaker_rows)
    assert cue_rows == [{
        "window_id": "w",
        "speaker": "s1",
        "start_us": 6_000_000,
        "end_us": 7_500_000,
        "origin": "moss_cue",
        "cue_id": "target",
    }]


@pytest.mark.parametrize(
    "bad_window,cues,match",
    [
        (window(window_id=True), [], "window ID"),
        (window(turns=[{"speaker": "s1", "start_us": 0, "end_us": 11_000_000}]), [], "outside"),
        (window(), [cue(start=9_500_000, end=10_500_000)], "cue lies outside"),
        (window(turns=[{"speaker": "", "start_us": 0, "end_us": 2_000_000}]), [], "speaker ID"),
    ],
)
def test_moss_sample_ranges_reject_invalid_window_cue_and_speaker_contracts(
    bad_window: dict, cues: list[dict], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        moss_sample_ranges(bad_window, cues, SETTINGS)


def test_fresh_structural_rescue_requires_cue_local_and_two_outside_references() -> None:
    target = cue(start=6_000_000, end=7_000_000)
    rows = crosscheck_cues(
        [target],
        [window()],
        [
            fresh_evidence(1_000_000, 2_000_000, 0.99),
            fresh_evidence(3_000_000, 4_000_000, 0.98),
            fresh_evidence(6_000_000, 7_000_000, 0.97, origin="moss_cue", cue_id="c"),
        ],
        base(reason="multiple_speakers"),
        SETTINGS,
    )

    assert rows[0]["machine"]["label"] == "female"
    assert rows[0]["machine"]["reason"] == "moss_acoustic_consensus"
    assert rows[0]["machine"]["confidence"] == pytest.approx(0.97)
    assert rows[0]["crosscheck"]["status"] == "rescued"


@pytest.mark.parametrize(
    "extra",
    [
        [],
        [fresh_evidence(6_000_000, 7_000_000, 0.97, origin="moss_cue", cue_id="other")],
        [fresh_evidence(6_000_000, 7_000_000, 0.94, origin="moss_cue", cue_id="c")],
    ],
)
def test_fresh_structural_rescue_rejects_missing_mismatched_or_weak_cue_support(
    extra: list[dict],
) -> None:
    rows = crosscheck_cues(
        [cue(start=6_000_000, end=7_000_000)],
        [window()],
        [
            fresh_evidence(1_000_000, 2_000_000, 0.99),
            fresh_evidence(3_000_000, 4_000_000, 0.98),
            *extra,
        ],
        base(reason="contradictory_speaker_evidence"),
        SETTINGS,
    )

    assert rows[0]["machine"] == base(reason="contradictory_speaker_evidence")["c"]
    assert rows[0]["crosscheck"]["reason"] == "insufficient_fresh_speaker_support"


def test_old_duplicate_and_target_overlapping_fresh_crop_do_not_shadow_reference_count() -> None:
    rows = crosscheck_cues(
        [cue(start=6_000_000, end=7_000_000)],
        [window()],
        [
            fresh_evidence(1_000_000, 2_000_000, 0.99),
            evidence(1_000_000, 2_000_000, 0.995),
            fresh_evidence(5_500_000, 6_500_000, 0.99),
            fresh_evidence(6_000_000, 7_000_000, 0.98, origin="moss_cue", cue_id="c"),
        ],
        base(reason="multiple_speakers"),
        SETTINGS,
    )

    assert rows[0]["machine"] == base(reason="multiple_speakers")["c"]
    assert rows[0]["crosscheck"]["reason"] == "insufficient_fresh_speaker_support"


@pytest.mark.parametrize("opposite_origin", [None, "moss_speaker", "moss_cue"])
def test_old_or_fresh_opposite_evidence_vetoes_structural_rescue(
    opposite_origin: str | None,
) -> None:
    opposite = evidence(8_000_000, 9_000_000, 0.15)
    if opposite_origin is not None:
        opposite.update(origin=opposite_origin, speaker="s1")
        if opposite_origin == "moss_cue":
            opposite.update(start_us=6_000_000, end_us=7_000_000, cue_id="c")
    rows = crosscheck_cues(
        [cue(start=6_000_000, end=7_000_000)],
        [window()],
        [
            fresh_evidence(1_000_000, 2_000_000, 0.99),
            fresh_evidence(3_000_000, 4_000_000, 0.98),
            fresh_evidence(6_000_000, 7_000_000, 0.97, origin="moss_cue", cue_id="c"),
            opposite,
        ],
        base(reason="multiple_speakers"),
        SETTINGS,
    )

    assert rows[0]["machine"] == base(reason="multiple_speakers")["c"]
    assert rows[0]["crosscheck"]["reason"] == "contradictory_acoustic_evidence"


@pytest.mark.parametrize("reason", ["no_speech", "overlapping_speakers"])
def test_fresh_evidence_does_not_rescope_retained_unknown_reasons(reason: str) -> None:
    rows = crosscheck_cues(
        [cue(start=6_000_000, end=7_000_000)],
        [window()],
        [
            fresh_evidence(1_000_000, 2_000_000, 0.99),
            fresh_evidence(3_000_000, 4_000_000, 0.98),
            fresh_evidence(6_000_000, 7_000_000, 0.97, origin="moss_cue", cue_id="c"),
        ],
        base(reason=reason),
        SETTINGS,
    )

    assert rows[0]["machine"] == base(reason=reason)["c"]
    assert rows[0]["crosscheck"]["reason"] == "baseline_not_rescuable"


@pytest.mark.parametrize("probability", [float("nan"), -0.01, 1.01, True])
def test_invalid_acoustic_probability_is_rejected(probability) -> None:
    with pytest.raises(ValueError, match="probability"):
        crosscheck_cues(
            [cue()], [window()], [evidence(1_000_000, 2_000_000, probability)], base(), SETTINGS
        )


def test_probability_and_explicit_label_must_agree() -> None:
    row = evidence(1_000_000, 2_000_000, 0.99)
    row["label"] = "male"
    with pytest.raises(ValueError, match="disagrees"):
        crosscheck_cues([cue()], [window()], [row], base(), SETTINGS)
