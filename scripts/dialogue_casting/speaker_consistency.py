"""Conservative rescue using acoustic evidence from one diarization analysis."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from .analysis import Settings, interval
from .srt_demo import classify_srt_cue

_RESCUABLE_REASONS = frozenset({"low_confidence", "insufficient_speaker_reference"})
_GENDER_LABELS = frozenset({"male", "female"})


def _independent_samples(evidence: list[dict], label: str, settings: Settings) -> list[dict]:
    # Repeated/global/local crops of the same audio are not independent votes.
    confidence_by_range: dict[tuple[int, int], float] = {}
    for sample in evidence:
        start, end = interval(sample)
        probability = sample["female_probability"]
        confidence = probability if label == "female" else 1 - probability
        key = (start, end)
        confidence_by_range[key] = min(confidence_by_range.get(key, confidence), confidence)

    candidates = [
        {"start_us": start, "end_us": end, "confidence": confidence}
        for (start, end), confidence in confidence_by_range.items()
        if end - start >= settings.min_sample_us and confidence >= settings.confidence
    ]
    # Earliest-finish interval scheduling counts disjoint real samples, without
    # turning overlapping crops or short fragments into additional evidence.
    selected = []
    previous_end = -1
    for sample in sorted(candidates, key=lambda item: (item["end_us"], item["start_us"])):
        if sample["start_us"] >= previous_end:
            selected.append(sample)
            previous_end = sample["end_us"]
    return selected


def _speaker_consensus(rows: list[dict], evidence: list[dict], settings: Settings) -> dict | None:
    references = [
        row["comparison_machine"]
        for row in rows
        if row["comparison_machine"]["label"] in _GENDER_LABELS
        and row["comparison_machine"]["reason"] == "assigned"
        and row["comparison_machine"]["confidence"] >= settings.confidence
    ]
    labels = {reference["label"] for reference in references}
    if len(labels) != 1:
        return None
    label = next(iter(labels))
    consensus = {
        "speaker": references[0]["speaker"],
        "label": label,
        "confidence": min(reference["confidence"] for reference in references),
        "independent_samples": _independent_samples(evidence, label, settings),
    }
    opposing_cues = [
        row["cue_id"]
        for row in rows
        if row["machine"]["label"] in _GENDER_LABELS
        and row["machine"]["label"] != label
        and row["machine"]["confidence"] >= settings.confidence
    ]
    if opposing_cues:
        return {
            **consensus,
            "status": "conflict",
            "reason": "conflicting_local_assignment",
            "opposing_cue_ids": opposing_cues,
        }
    if any(
        (1 - sample["female_probability"] if label == "female" else sample["female_probability"])
        >= settings.contradiction
        for sample in evidence
    ):
        return {**consensus, "status": "conflict", "reason": "opposite_sample"}
    if len(consensus["independent_samples"]) < 2:
        return None
    return {**consensus, "status": "accepted"}


def classify_consistent_cues(
    cues: list[dict],
    analysis: dict,
    settings: Settings,
    probability: Callable[[int, int], float],
) -> list[dict]:
    """Rescue uncertain cues; confident or structurally ambiguous results stay intact.

    Speaker IDs are local to this one analysis. No adjacency, subtitle text,
    manual labels or rescued decisions provide acoustic evidence for another cue.
    """
    decisions = [classify_srt_cue(cue, analysis, settings, probability) for cue in cues]
    rows_by_speaker: dict[str, list[dict]] = defaultdict(list)
    evidence_by_speaker: dict[str, list[dict]] = defaultdict(list)
    for sample in analysis["evidence"]:
        evidence_by_speaker[sample["speaker"]].append(sample)
    for row in decisions:
        speaker = row["machine"]["speaker"]
        if speaker is not None:
            rows_by_speaker[speaker].append(row)
        for sample in row["evidence"]:
            evidence_by_speaker[sample["speaker"]].append(sample)

    for speaker, rows in rows_by_speaker.items():
        candidates = [
            row
            for row in rows
            if row["machine"]["label"] == "unknown"
            and row["machine"]["reason"] in _RESCUABLE_REASONS
        ]
        if not candidates:
            continue
        consensus = _speaker_consensus(rows, evidence_by_speaker[speaker], settings)
        if consensus is None:
            continue
        for row in candidates:
            row["original_machine"] = dict(row["machine"])
            row["speaker_consensus"] = consensus
            if consensus["status"] == "accepted":
                row["machine"] = {
                    **row["machine"],
                    "label": consensus["label"],
                    "reason": "speaker_consensus",
                    "confidence": consensus["confidence"],
                }
    return decisions
