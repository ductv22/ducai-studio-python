from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from .analysis import Settings, classify_cue, validate_analysis
from .assets import LOCK_PATH, file_sha256, json_digest, read_json
from .corpus import load_labels, validate_corpus
from .inference import implementation_digest

PREDICTION_SCHEMA = "ducai-dialogue-casting-predictions-v1"
FROZEN_SCHEMA = "ducai-dialogue-casting-calibration-v1"
TARGET_ACCURACY = 0.95
TARGET_COVERAGE = 0.70
MIN_TEST_CUES = 200
MIN_TEST_FILMS = 3


def prediction_rows(predictions: dict, corpus: dict) -> tuple[dict, Settings]:
    validate_corpus(corpus)
    if (
        predictions.get("schema") != PREDICTION_SCHEMA
        or predictions.get("corpus_id") != corpus["corpus_id"]
        or predictions.get("model_lock_sha256") != file_sha256(LOCK_PATH)
        or predictions.get("implementation_sha256") != implementation_digest()
    ):
        raise ValueError("Predictions do not match the corpus, pinned models or implementation")
    settings = Settings(**predictions["settings"])
    known = {cue["id"] for cue in corpus["cues"]}
    rows = {}
    for row in predictions["results"]:
        if row["cue_id"] not in known or row["cue_id"] in rows or row.get("error"):
            raise ValueError("Prediction rows contain an error or unknown/duplicate cue")
        validate_analysis(row["analysis"])
        rows[row["cue_id"]] = row["analysis"]
    return rows, settings


def score(cues: list[dict], rows: dict, labels: dict, settings: Settings) -> dict:
    counts: Counter = Counter()
    confusion: Counter = Counter()
    reasons: Counter = Counter()
    by_film: dict[str, Counter] = {}
    assignments = []
    for cue in cues:
        cue_id = cue["id"]
        if cue_id not in labels:
            counts["missing_labels"] += 1
        if cue_id not in rows:
            counts["missing_predictions"] += 1
            continue
        result = classify_cue(cue, rows[cue_id], settings)
        assignments.append(result)
        predicted = result["label"]
        automatic = predicted in {"male", "female"}
        counts["auto_assigned" if automatic else "fallback"] += 1
        reasons[result["reason"]] += 1
        film_counts = by_film.setdefault(cue["film_id"], Counter())
        film_counts["total"] += 1
        film_counts["auto_assigned" if automatic else "fallback"] += 1
        if cue_id not in labels:
            continue
        truth = labels[cue_id]["label"]
        counts["labelled"] += 1
        counts[f"truth_{truth}"] += 1
        confusion[f"{truth}->{predicted}"] += 1
        if automatic:
            counts["labelled_auto_assigned"] += 1
            if truth == predicted:
                counts["correct_auto_assigned"] += 1
                film_counts["correct_auto_assigned"] += 1
            else:
                counts["incorrect_auto_assigned"] += 1
                film_counts["incorrect_auto_assigned"] += 1
    total = len(cues)
    complete = not counts["missing_labels"] and not counts["missing_predictions"]
    automatic = counts["auto_assigned"]
    return {
        "total": total,
        **dict(counts),
        "complete": complete,
        "accuracy_among_auto_assigned": (
            counts["correct_auto_assigned"] / automatic if complete and automatic else None
        ),
        "automatic_coverage": automatic / total if total else 0.0,
        "fallback_rate": counts["fallback"] / total if total else 0.0,
        "confusion": dict(sorted(confusion.items())),
        "reasons": dict(sorted(reasons.items())),
        "by_film": {key: dict(value) for key, value in sorted(by_film.items())},
        "assignments": assignments,
    }


def calibration_label_digest(corpus: dict, labels: dict) -> str:
    return json_digest(
        {
            cue["id"]: labels[cue["id"]]
            for cue in corpus["cues"]
            if cue["split"] == "calibration" and cue["id"] in labels
        }
    )


def calibrate(corpus: dict, predictions: dict, labels: dict) -> dict:
    rows, base_settings = prediction_rows(predictions, corpus)
    cues = [cue for cue in corpus["cues"] if cue["split"] == "calibration"]
    if not cues or any(cue["id"] not in labels or cue["id"] not in rows for cue in cues):
        raise ValueError("All independent calibration labels and predictions are required")
    candidates = []
    for threshold in (0.5, 0.6, 0.7, 0.8, 0.85, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995, 0.999):
        settings = replace(base_settings, confidence=threshold)
        metrics = score(cues, rows, labels, settings)
        accuracy = metrics["accuracy_among_auto_assigned"]
        if accuracy is not None and accuracy >= TARGET_ACCURACY:
            candidates.append((metrics["automatic_coverage"], accuracy, threshold, metrics))
    if not candidates:
        raise ValueError("No threshold reaches 95% calibration accuracy; revisit the approach")
    _, _, threshold, metrics = max(candidates, key=lambda item: item[:3])
    return {
        "schema": FROZEN_SCHEMA,
        "corpus_id": corpus["corpus_id"],
        "model_lock_sha256": predictions["model_lock_sha256"],
        "implementation_sha256": predictions["implementation_sha256"],
        "settings": asdict(replace(base_settings, confidence=threshold)),
        "calibration_label_digest": calibration_label_digest(corpus, labels),
        "calibration_ids": sorted(cue["id"] for cue in cues),
        "frozen_at": datetime.now(UTC).isoformat(),
        "selection": "maximum calibration coverage at >=95% accuracy; held-out labels unused",
        "calibration_metrics": {
            key: value for key, value in metrics.items() if key != "assignments"
        },
        "product_gate": "NOT_EVALUATED",
    }


def evaluate(
    corpus: dict, predictions: dict, labels: dict, frozen: dict, verified_film_ids: list[str]
) -> dict:
    rows, prediction_settings = prediction_rows(predictions, corpus)
    if (
        frozen.get("schema") != FROZEN_SCHEMA
        or frozen.get("corpus_id") != corpus["corpus_id"]
        or frozen.get("model_lock_sha256") != predictions["model_lock_sha256"]
        or frozen.get("implementation_sha256") != predictions["implementation_sha256"]
        or frozen.get("calibration_label_digest") != calibration_label_digest(corpus, labels)
    ):
        raise ValueError("Calibration is missing/stale or calibration labels changed")
    settings = Settings(**frozen["settings"])
    if settings.analysis_identity() != prediction_settings.analysis_identity():
        raise ValueError("Inference settings changed after calibration")
    calibration_ids = sorted(cue["id"] for cue in corpus["cues"] if cue["split"] == "calibration")
    if not calibration_ids or frozen.get("calibration_ids") != calibration_ids:
        raise ValueError("Calibration split changed after thresholds were frozen")
    test = [cue for cue in corpus["cues"] if cue["split"] == "test"]
    metrics = score(test, rows, labels, settings)
    films = {cue["film_id"] for cue in test}
    verified = set(verified_film_ids)
    eligible = {
        film["id"]
        for film in corpus["films"]
        if film["language"] == "zh" and film["genre"] == "animation" and film["id"] in verified
    }
    reasons = []
    if len(test) < MIN_TEST_CUES:
        reasons.append("fewer_than_200_held_out_cues")
    if len(films) < MIN_TEST_FILMS:
        reasons.append("fewer_than_three_distinct_films")
    if not films <= eligible:
        reasons.append("unverified_chinese_animation_sources")
    if not metrics["complete"]:
        reasons.append("missing_independent_labels_or_predictions")
    if reasons:
        gate = "NOT_EVALUATED"
    else:
        accuracy = metrics["accuracy_among_auto_assigned"]
        if accuracy is None or accuracy < TARGET_ACCURACY:
            reasons.append("auto_assignment_accuracy_below_95_percent")
        if metrics["automatic_coverage"] < TARGET_COVERAGE:
            reasons.append("automatic_coverage_below_70_percent")
        gate = "FAIL" if reasons else "PASS"
    return {
        "schema": "ducai-dialogue-casting-evaluation-v1",
        "gate": gate,
        "reasons": reasons,
        "corpus_id": corpus["corpus_id"],
        "frozen_sha256": json_digest(frozen),
        "predictions_sha256": json_digest(predictions),
        "model_lock_sha256": predictions["model_lock_sha256"],
        "held_out_film_count": len(films),
        "metrics": metrics,
        "targets": {
            "accuracy": TARGET_ACCURACY,
            "coverage": TARGET_COVERAGE,
            "test_cues": MIN_TEST_CUES,
            "test_films": MIN_TEST_FILMS,
        },
    }


def evaluate_files(
    corpus_path: Path, predictions_path: Path, labels_path: Path, frozen_path: Path
) -> dict:
    corpus = read_json(corpus_path)
    labels = load_labels(labels_path, corpus)
    label_document = read_json(labels_path)
    return evaluate(
        corpus,
        read_json(predictions_path),
        labels,
        read_json(frozen_path),
        label_document.get("verified_film_ids", []),
    )
