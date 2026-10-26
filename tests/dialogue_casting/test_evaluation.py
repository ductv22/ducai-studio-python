"""Synthetic gate tests are never evidence of real animation recognition accuracy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

import pytest
from dialogue_casting.analysis import Settings
from dialogue_casting.assets import LOCK_PATH, file_sha256, json_digest
from dialogue_casting.corpus import CORPUS_SCHEMA
from dialogue_casting.evaluation import PREDICTION_SCHEMA, calibrate, evaluate
from dialogue_casting.inference import implementation_digest


def benchmark(test_counts=(70, 70, 60)):
    corpus = {"schema": CORPUS_SCHEMA, "films": [], "cues": []}
    labels = {}
    rows = []
    for number, count in enumerate(test_counts, 1):
        film_id = f"film-{number}"
        corpus["films"].append(
            {
                "id": film_id,
                "source_sha256": str(number) * 64,
                "duration_us": 1_000_000_000,
                "language": "zh",
                "genre": "animation",
            }
        )
        for i in range(count + 4):
            cue_id = f"{film_id}-{i}"
            start = i * 4_000_000
            cue = {
                "id": cue_id,
                "film_id": film_id,
                "start_us": start,
                "end_us": start + 2_000_000,
                "clip_sha256": "a" * 64,
                "split": "calibration" if i < 4 else "test",
            }
            label = "male" if i % 2 else "female"
            raw_turn = {"start_us": start, "end_us": start + 2_000_000, "speaker": "a"}
            rows.append(
                {
                    "cue_id": cue_id,
                    "analysis": {
                        "turns": [raw_turn],
                        "evidence": [
                            raw_turn | {"female_probability": 0.0001 if label == "male" else 0.9999}
                        ],
                    },
                }
            )
            corpus["cues"].append(cue)
            labels[cue_id] = {"cue_id": cue_id, "label": label, "clip_sha256": "a" * 64}
    corpus["corpus_id"] = json_digest(corpus)
    predictions = {
        "schema": PREDICTION_SCHEMA,
        "corpus_id": corpus["corpus_id"],
        "model_lock_sha256": file_sha256(LOCK_PATH),
        "implementation_sha256": implementation_digest(),
        "settings": asdict(Settings()),
        "results": rows,
    }
    return corpus, predictions, labels


def run_gate(corpus, predictions, labels, frozen=None):
    frozen = frozen or calibrate(corpus, predictions, labels)
    return evaluate(corpus, predictions, labels, frozen, [film["id"] for film in corpus["films"]])


def test_both_targets_can_pass_with_enough_independent_test_data():
    result = run_gate(*benchmark())
    assert result["gate"] == "PASS"
    assert result["metrics"]["total"] == 200
    assert result["metrics"]["accuracy_among_auto_assigned"] == 1
    assert result["metrics"]["automatic_coverage"] == 1


@pytest.mark.parametrize("wrong,gate", [(10, "PASS"), (11, "FAIL")])
def test_95_percent_accuracy_boundary(wrong, gate):
    corpus, predictions, labels = benchmark()
    test_ids = [cue["id"] for cue in corpus["cues"] if cue["split"] == "test"]
    for cue_id in test_ids[:wrong]:
        labels[cue_id]["label"] = "male" if labels[cue_id]["label"] == "female" else "female"
    result = run_gate(corpus, predictions, labels)
    assert result["gate"] == gate
    assert result["metrics"]["incorrect_auto_assigned"] == wrong


@pytest.mark.parametrize("fallback_count,gate", [(60, "PASS"), (61, "FAIL"), (200, "FAIL")])
def test_coverage_is_required_and_all_fallback_cannot_pass(fallback_count, gate):
    corpus, predictions, labels = benchmark()
    ids = {cue["id"] for cue in corpus["cues"] if cue["split"] == "test"}
    test_rows = [row for row in predictions["results"] if row["cue_id"] in ids]
    for row in test_rows[:fallback_count]:
        row["analysis"] = {"turns": [], "evidence": []}
    result = run_gate(corpus, predictions, labels)
    assert result["gate"] == gate
    assert result["metrics"]["fallback_rate"] == fallback_count / 200


@pytest.mark.parametrize("truth", ["mixed", "unclear"])
def test_ambiguous_truth_is_kept_in_denominator_and_auto_assignment_is_wrong(truth):
    corpus, predictions, labels = benchmark()
    for cue in [cue for cue in corpus["cues"] if cue["split"] == "test"][:11]:
        labels[cue["id"]]["label"] = truth
    result = run_gate(corpus, predictions, labels)
    assert result["gate"] == "FAIL"
    assert result["metrics"]["total"] == 200


@pytest.mark.parametrize(
    "test_counts,reason",
    [((99, 100), "fewer_than_200_held_out_cues"), ((100, 100), "fewer_than_three_distinct_films")],
)
def test_insufficient_films_or_cues_are_not_an_evaluation_pass(test_counts, reason):
    result = run_gate(*benchmark(test_counts))
    assert result["gate"] == "NOT_EVALUATED"
    assert reason in result["reasons"]


@pytest.mark.parametrize("missing", ["label", "prediction"])
def test_missing_held_out_rows_cannot_disappear_from_gate(missing):
    corpus, predictions, labels = benchmark()
    frozen = calibrate(corpus, predictions, labels)
    target = next(cue["id"] for cue in corpus["cues"] if cue["split"] == "test")
    if missing == "label":
        del labels[target]
    else:
        predictions["results"] = [row for row in predictions["results"] if row["cue_id"] != target]
    result = run_gate(corpus, predictions, labels, frozen)
    assert result["gate"] == "NOT_EVALUATED"
    assert result["metrics"]["total"] == 200
    assert result["metrics"]["accuracy_among_auto_assigned"] is None


def test_threshold_selection_does_not_read_held_out_labels():
    corpus, predictions, labels = benchmark()
    first = calibrate(corpus, predictions, labels)
    for cue in corpus["cues"]:
        if cue["split"] == "test":
            labels[cue["id"]]["label"] = "unclear"
    second = calibrate(corpus, predictions, labels)
    assert first["settings"] == second["settings"]
    assert first["calibration_label_digest"] == second["calibration_label_digest"]


def test_changed_calibration_evidence_requires_recalibration():
    corpus, predictions, labels = benchmark()
    frozen = calibrate(corpus, predictions, labels)
    labels[corpus["cues"][0]["id"]]["label"] = "unclear"
    with pytest.raises(ValueError, match="Calibration"):
        run_gate(corpus, predictions, labels, frozen)


@pytest.mark.parametrize("mutation", ["model", "code", "settings", "duplicate", "error"])
def test_stale_provenance_and_error_rows_are_rejected(mutation):
    corpus, predictions, labels = benchmark()
    frozen = calibrate(corpus, predictions, labels)
    if mutation == "model":
        predictions["model_lock_sha256"] = "stale"
    elif mutation == "code":
        predictions["implementation_sha256"] = "stale"
    elif mutation == "settings":
        predictions["settings"]["clustering_threshold"] = 0.7
    elif mutation == "duplicate":
        predictions["results"].append(deepcopy(predictions["results"][0]))
    else:
        predictions["results"][0]["error"] = "model load failed"
    with pytest.raises(ValueError):
        run_gate(corpus, predictions, labels, frozen)


def test_sources_require_independent_verification_as_chinese_animation():
    corpus, predictions, labels = benchmark()
    frozen = calibrate(corpus, predictions, labels)
    result = evaluate(corpus, predictions, labels, frozen, [])
    assert result["gate"] == "NOT_EVALUATED"
    assert "unverified_chinese_animation_sources" in result["reasons"]
