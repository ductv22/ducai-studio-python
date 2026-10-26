from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Settings:
    # Experimental starting values. These are not validated production thresholds.
    confidence: float = 0.98
    speech_ratio: float = 0.5
    secondary_speaker_ratio: float = 0.10
    contradiction: float = 0.8
    min_speech_us: int = 200_000
    min_sample_us: int = 1_000_000
    max_overlap_us: int = 100_000
    block_us: int = 60_000_000
    context_us: int = 5_000_000
    max_sample_us: int = 5_000_000
    max_samples_per_speaker: int = 3
    clustering_threshold: float = 0.5
    threads: int = 2

    def __post_init__(self) -> None:
        for name in ("confidence", "speech_ratio", "secondary_speaker_ratio", "contradiction"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"Invalid casting setting: {name}")
        if not 0 < self.clustering_threshold < 1:
            raise ValueError("Invalid speaker clustering threshold")
        for name in (
            "min_speech_us",
            "min_sample_us",
            "max_overlap_us",
            "block_us",
            "context_us",
            "max_sample_us",
            "max_samples_per_speaker",
            "threads",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"Invalid casting setting: {name}")
        if (
            self.block_us + 20_000_000 + 2 * self.context_us > 150_000_000
            or self.context_us > 10_000_000
        ):
            raise ValueError("Casting windows must stay bounded")
        if self.max_sample_us > 5_000_000 or self.min_sample_us > self.max_sample_us:
            raise ValueError("Classifier samples must be between minimum length and five seconds")
        if self.max_samples_per_speaker > 8 or self.threads > 8:
            raise ValueError("Casting CPU/sample budget exceeded")

    def analysis_identity(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != "confidence"}


def interval(value: dict) -> tuple[int, int]:
    start, end = value["start_us"], value["end_us"]
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise ValueError("Invalid original-audio interval")
    return start, end


def merged_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(end, result[-1][1])
        else:
            result.append((start, end))
    return result


def duration(ranges: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in merged_ranges(ranges))


def clear_ranges(turn: dict, other_turns: list[dict]) -> list[tuple[int, int]]:
    """Exclude all competing speakers from classifier reference samples."""
    remaining = [interval(turn)]
    for other in other_turns:
        if other["speaker"] == turn["speaker"]:
            continue
        other_start, other_end = interval(other)
        next_ranges = []
        for start, end in remaining:
            if other_start >= end or other_end <= start:
                next_ranges.append((start, end))
            else:
                if start < other_start:
                    next_ranges.append((start, other_start))
                if other_end < end:
                    next_ranges.append((other_end, end))
        remaining = next_ranges
    return remaining


def validate_analysis(value: dict) -> None:
    if not isinstance(value.get("turns"), list) or not isinstance(value.get("evidence"), list):
        raise ValueError("Invalid diarization result structure")
    speakers = set()
    for turn in value["turns"]:
        interval(turn)
        if not isinstance(turn.get("speaker"), str) or not turn["speaker"]:
            raise ValueError("Invalid diarization speaker identity")
        speakers.add(turn["speaker"])
    for item in value["evidence"]:
        interval(item)
        probability = item["female_probability"]
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (float, int))
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise ValueError("Invalid classifier probability; inference is not an unknown result")
        if item["speaker"] not in speakers:
            raise ValueError("Classifier evidence has no matching diarized speaker")


def classify_cue(cue: dict, analysis: dict, settings: Settings) -> dict:
    validate_analysis(analysis)
    start, end = interval(cue)
    by_speaker: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for turn in analysis["turns"]:
        turn_start, turn_end = interval(turn)
        speaker = turn["speaker"]
        if not isinstance(speaker, str) or not speaker:
            raise ValueError("Invalid diarization speaker identity")
        if turn_start < end and turn_end > start:
            by_speaker[speaker].append((max(start, turn_start), min(end, turn_end)))
    totals = sorted(((duration(ranges), key) for key, ranges in by_speaker.items()), reverse=True)
    union_us = duration([r for ranges in by_speaker.values() for r in ranges])
    overlap_us = sum(item[0] for item in totals) - union_us
    result = {
        "cue_id": cue["id"],
        "label": "unknown",
        "reason": "no_speech",
        "speaker": None,
        "confidence": None,
        "speech_us": union_us,
        "speech_ratio": union_us / (end - start),
        "overlap_us": overlap_us,
    }
    if not totals:
        return result
    _, speaker = totals[0]
    result["speaker"] = speaker
    if overlap_us > settings.max_overlap_us:
        return result | {"reason": "overlapping_speakers"}
    if len(totals) > 1 and totals[1][0] / max(1, union_us) > settings.secondary_speaker_ratio:
        return result | {"reason": "multiple_speakers"}
    if union_us < settings.min_speech_us or result["speech_ratio"] < settings.speech_ratio:
        return result | {"reason": "insufficient_speech"}
    evidence = []
    for item in analysis["evidence"]:
        sample_start, sample_end = interval(item)
        if item["speaker"] == speaker and sample_end - sample_start >= settings.min_sample_us:
            evidence.append(item)
    if not evidence:
        return result | {"reason": "insufficient_speaker_reference"}
    weight = sum(item["end_us"] - item["start_us"] for item in evidence)
    female = (
        sum(item["female_probability"] * (item["end_us"] - item["start_us"]) for item in evidence)
        / weight
    )
    label = "female" if female >= 0.5 else "male"
    confidence = female if label == "female" else 1 - female
    result["confidence"] = confidence
    if any(
        (1 - item["female_probability"] if label == "female" else item["female_probability"])
        >= settings.contradiction
        for item in evidence
    ):
        return result | {"reason": "contradictory_speaker_evidence"}
    if confidence < settings.confidence:
        return result | {"reason": "low_confidence"}
    return result | {"label": label, "reason": "assigned"}
