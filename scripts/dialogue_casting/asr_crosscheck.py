"""Conservative cross-check of SRT cues with an offline ASR/diarization run.

MOSS speaker IDs are anonymous and local to one window. They are used only to
associate speech boundaries with acoustic evidence; transcript text is an
advisory alignment signal and never supplies a gender label.
"""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher

from .analysis import Settings, clear_ranges, interval, merged_ranges

_RESCUABLE_REASONS = frozenset(
    {"low_confidence", "insufficient_speaker_reference", "insufficient_speech"}
)
_GENDER_LABELS = frozenset({"male", "female"})
_FRESH_RESCUABLE_REASONS = frozenset({"multiple_speakers", "contradictory_speaker_evidence"})
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")


def _strict_us(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("ASR cross-check timestamps must be non-negative integers")
    return value


def _range(value: Mapping[str, object]) -> tuple[int, int]:
    if "start_us" not in value or "end_us" not in value:
        raise ValueError("ASR cross-check interval requires start_us and end_us")
    start, end = _strict_us(value["start_us"]), _strict_us(value["end_us"])
    if end <= start:
        raise ValueError("ASR cross-check interval is invalid")
    return start, end


def _window_id(window: Mapping[str, object], index: int) -> str:
    value = window.get("id", window.get("window_id", index))
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value):
        raise ValueError("ASR cross-check window ID is invalid")
    return str(value)


def _turns(window: Mapping[str, object]) -> list[dict]:
    raw = window.get("turns")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError("ASR cross-check window turns must be a list")
    result = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("ASR cross-check turn must be an object")
        start, end = _range(item)
        speaker = item.get("speaker")
        if not isinstance(speaker, str) or not speaker:
            raise ValueError("ASR cross-check speaker ID is invalid")
        result.append(
            {
                "start_us": start,
                "end_us": end,
                "speaker": speaker,
                "text": str(item.get("text", "")),
            }
        )
    return result


def _evidence_rows(evidence: Sequence[Mapping[str, object]]) -> list[dict]:
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes, bytearray)):
        raise ValueError("ASR cross-check evidence must be a list")
    result = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("ASR cross-check evidence must be an object")
        start, end = _range(item)
        result.append({**item, "start_us": start, "end_us": end})
    return result


def _baseline_rows(baseline: Mapping[str, Mapping[str, object]]) -> dict[str, dict]:
    if not isinstance(baseline, Mapping):
        raise ValueError("ASR cross-check baseline must be a cue map")
    result = {}
    for cue_id, row in baseline.items():
        if not isinstance(cue_id, str) or not cue_id:
            raise ValueError("ASR cross-check baseline cue ID is invalid")
        if not isinstance(row, Mapping):
            raise ValueError("ASR cross-check baseline row must be an object")
        if row.get("label") not in _GENDER_LABELS | {"unknown"} or not isinstance(
            row.get("reason"), str
        ):
            raise ValueError("ASR cross-check baseline row has an invalid decision")
        result[cue_id] = copy.deepcopy(dict(row))
    return result


def _complete_window(
    cue: Mapping[str, object], windows: list[tuple[str, Mapping[str, object]]]
) -> tuple[str, Mapping[str, object]] | None:
    start, end = interval(cue)
    candidates = []
    for index, (window_id, window) in enumerate(windows):
        window_start, window_end = _range(window)
        if window_start <= start and window_end >= end:
            left, right = start - window_start, window_end - end
            # Largest minimum edge margin wins. Remaining keys make the choice
            # deterministic without linking speaker IDs across windows.
            candidates.append(
                (
                    min(left, right),
                    left + right,
                    -(window_end - window_start),
                    -index,
                    window_id,
                    window,
                )
            )
    if not candidates:
        return None
    _, _, _, _, window_id, window = max(candidates)
    return window_id, window


def _overlap(start: int, end: int, other_start: int, other_end: int) -> int:
    return max(0, min(end, other_end) - max(start, other_start))


def moss_sample_ranges(window: Mapping[str, object], cues: Sequence[Mapping[str, object]],
                       settings: Settings) -> list[dict]:
    """Choose bounded original-audio crops from clear, window-local MOSS turns.

    Speaker references are disjoint; cue crops are separate observations and
    never multiply the independent vote count when they overlap a reference.
    """
    start, end = _range(window)
    window_id = _window_id(window, 0)
    turns = _turns(window)
    if any(not start <= row["start_us"] < row["end_us"] <= end for row in turns):
        raise ValueError("MOSS reference turn lies outside its audio window")
    by_speaker: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for turn in turns:
        by_speaker[turn["speaker"]].extend(clear_ranges(turn, turns))
    samples = []
    for speaker, spans in sorted(by_speaker.items()):
        candidates = []
        for first, last in sorted(set(spans)):
            length = last - first
            if length < settings.min_sample_us:
                continue
            # Split long speech evenly instead of generating overlapping crops
            # or one five-second crop with unusable short remnants at its edges.
            count = min(settings.max_samples_per_speaker,
                        max(1, length // (2 * settings.min_sample_us)))
            for index in range(count):
                a = first + length * index // count
                b = first + length * (index + 1) // count
                size = min(b - a, settings.max_sample_us)
                a += (b - a - size) // 2
                candidates.append((a, a + size))
        selected = []
        for candidate in sorted(candidates, key=lambda span: (-(span[1] - span[0]), span[0])):
            if not any(_overlap(*candidate, *previous) for previous in selected):
                selected.append(candidate)
            if len(selected) == settings.max_samples_per_speaker:
                break
        samples.extend({"window_id": window_id, "speaker": speaker,
                        "start_us": a, "end_us": b, "origin": "moss_speaker"}
                       for a, b in sorted(selected))
    for cue in cues:
        a, b = interval(cue)
        if not start <= a < b <= end:
            raise ValueError("MOSS reference cue lies outside its audio window")
        geometry = _speech_geometry(cue, turns, settings)
        if geometry["reason"] != "speech_ok":
            continue
        speaker = geometry["speaker"]
        spans = [(max(a, first), min(b, last))
                 for first, last in sorted(set(by_speaker[speaker]))
                 if min(b, last) - max(a, first) >= settings.min_sample_us]
        if not spans:
            continue
        first, last = max(spans, key=lambda span: (span[1] - span[0], -span[0]))
        size = min(last - first, settings.max_sample_us)
        first += (last - first - size) // 2
        samples.append({"window_id": window_id, "speaker": speaker,
                        "start_us": first, "end_us": first + size,
                        "origin": "moss_cue", "cue_id": cue["id"]})
    return samples


def _speech_geometry(cue: Mapping[str, object], turns: list[dict], settings: Settings) -> dict:
    start, end = interval(cue)
    clipped = [
        {**turn, "start_us": max(start, turn["start_us"]), "end_us": min(end, turn["end_us"])}
        for turn in turns
        if turn["start_us"] < end and turn["end_us"] > start
    ]
    by_speaker: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for turn in clipped:
        by_speaker[turn["speaker"]].append((turn["start_us"], turn["end_us"]))
    totals = sorted(
        (
            (sum(end - start for start, end in merged_ranges(ranges)), speaker)
            for speaker, ranges in by_speaker.items()
        ),
        reverse=True,
    )
    union = sum(
        end - start
        for start, end in merged_ranges([span for ranges in by_speaker.values() for span in ranges])
    )
    overlap = sum(total for total, _ in totals) - union
    result = {
        "speech_us": union,
        "speech_ratio": union / (end - start),
        "overlap_us": overlap,
        "speakers": [speaker for _, speaker in totals],
        "speaker": totals[0][1] if totals else None,
    }
    if not totals:
        result["reason"] = "no_speech"
    elif overlap > settings.max_overlap_us:
        result["reason"] = "overlapping_speakers"
    elif len(totals) > 1 and totals[1][0] / max(1, union) > settings.secondary_speaker_ratio:
        result["reason"] = "multiple_speakers"
    elif union < settings.min_speech_us or result["speech_ratio"] < settings.speech_ratio:
        result["reason"] = "insufficient_speech"
    else:
        result["reason"] = "speech_ok"
    return result


def _label_confidence(item: Mapping[str, object]) -> tuple[str | None, float | None]:
    label = item.get("label")
    probability = item.get("female_probability")
    confidence = item.get("confidence", item.get("acoustic_confidence"))
    if probability is not None:
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError("ASR cross-check acoustic probability is invalid")
        probability = float(probability)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("ASR cross-check acoustic probability is invalid")
        inferred = "female" if probability >= 0.5 else "male"
        if label is None:
            label = inferred
        elif label != inferred and label in _GENDER_LABELS:
            raise ValueError("ASR cross-check evidence label disagrees with probability")
        confidence = probability if inferred == "female" else 1 - probability
    if label not in _GENDER_LABELS:
        return None, None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return label, None
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("ASR cross-check acoustic confidence is invalid")
    return label, confidence


def _anchors(
    window_id: str,
    speaker: str,
    turns: list[dict],
    evidence: list[dict],
    settings: Settings,
) -> tuple[list[dict], list[dict]]:
    speaker_turns = [turn for turn in turns if turn["speaker"] == speaker]
    candidates, strong = [], []
    for item in evidence:
        item_window = item.get("window_id", item.get("analysis_id"))
        if item_window is not None and str(item_window) != window_id:
            continue
        item_start, item_end = item["start_us"], item["end_us"]
        label, confidence = _label_confidence(item)
        if label is None or confidence is None:
            continue
        containing = [
            turn
            for turn in speaker_turns
            if turn["start_us"] <= item_start and item_end <= turn["end_us"]
        ]
        if not containing:
            continue
        # A crop intersecting a different speaker turn cannot be safely
        # attributed even if it is contained in the target speaker's turn.
        if any(
            other["speaker"] != speaker
            and _overlap(item_start, item_end, other["start_us"], other["end_us"]) > 0
            for other in turns
        ):
            continue
        sample = {
            "start_us": item_start,
            "end_us": item_end,
            "label": label,
            "confidence": confidence,
            "speaker": speaker,
        }
        if item.get("origin") in {"moss_speaker", "moss_cue"}:
            sample["origin"] = item["origin"]
            if "cue_id" in item:
                sample["cue_id"] = item["cue_id"]
        if confidence >= settings.contradiction:
            strong.append(sample)
        if item_end - item_start >= settings.min_sample_us and confidence >= settings.confidence:
            candidates.append(sample)
    unique: dict[tuple[int, int], dict] = {}
    for candidate in candidates:
        key = (candidate["start_us"], candidate["end_us"])
        previous = unique.get(key)
        if previous is None or candidate["confidence"] > previous["confidence"]:
            unique[key] = candidate
    selected, previous_end = [], -1
    for candidate in sorted(unique.values(), key=lambda item: (item["end_us"], item["start_us"])):
        if candidate["start_us"] >= previous_end:
            selected.append(candidate)
            previous_end = candidate["end_us"]
    return selected, strong


def _lcs_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    previous = [0] * (len(right) + 1)
    for first in left:
        current = [0]
        for index, second in enumerate(right, 1):
            current.append(
                previous[index - 1] + 1 if first == second else max(previous[index], current[-1])
            )
        previous = current
    return previous[-1] / max(len(left), len(right))


def _text_crosscheck(cue: Mapping[str, object], turns: list[dict]) -> dict:
    start, end = interval(cue)
    source = "".join(_CJK_RE.findall(unicodedata.normalize("NFKC", str(cue.get("text", "")))))
    recognized = "".join(
        _CJK_RE.findall(
            unicodedata.normalize(
                "NFKC",
                "".join(
                    turn["text"]
                    for turn in turns
                    if _overlap(start, end, turn["start_us"], turn["end_us"]) > 0
                ),
            )
        )
    )
    if not source or not recognized:
        return {
            "status": "unavailable",
            "source": source,
            "recognized": recognized,
            "similarity": None,
        }
    ratio = SequenceMatcher(None, source, recognized, autojunk=False).ratio()
    lcs_ratio = _lcs_ratio(source, recognized)
    return {
        "status": "match"
        if source == recognized
        else ("close" if ratio >= 0.6 or lcs_ratio >= 0.6 else "mismatch"),
        "source": source,
        "recognized": recognized,
        "similarity": round(ratio, 6),
        "lcs_ratio": round(lcs_ratio, 6),
    }


def _diagnostic() -> dict:
    return {
        "status": "kept",
        "reason": "baseline_not_rescuable",
        "window_id": None,
        "speech": None,
        "anchors": [],
        "opposing_evidence": [],
        "text": {
            "status": "unavailable",
            "source": "",
            "recognized": "",
            "similarity": None,
        },
    }


def crosscheck_cues(
    cues: Sequence[Mapping[str, object]],
    windows: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
    baseline: Mapping[str, Mapping[str, object]],
    settings: Settings,
) -> list[dict]:
    """Return ``{cue_id, machine, baseline, crosscheck}`` rows.

    Only uncertain baseline reasons may be rescued. Confident assignments are
    retained, but an opposite high-confidence crop is surfaced as a conflict.
    Speaker IDs are always scoped to the selected window.
    """
    if not isinstance(settings, Settings):
        raise TypeError("ASR cross-check requires dialogue-casting Settings")
    if not isinstance(cues, Sequence) or isinstance(cues, (str, bytes, bytearray)):
        raise ValueError("ASR cross-check cues must be a list")
    if not isinstance(windows, Sequence) or isinstance(windows, (str, bytes, bytearray)):
        raise ValueError("ASR cross-check windows must be a list")
    normalized_windows = []
    for index, window in enumerate(windows):
        if not isinstance(window, Mapping):
            raise ValueError("ASR cross-check window must be an object")
        _range(window)
        normalized_windows.append((_window_id(window, index), window))
    evidence_rows = _evidence_rows(evidence)
    baseline_rows = _baseline_rows(baseline)
    output, seen = [], set()
    for cue_value in cues:
        if not isinstance(cue_value, Mapping) or not isinstance(cue_value.get("id"), str):
            raise ValueError("ASR cross-check cue must have a string ID")
        cue = dict(cue_value)
        cue_id = cue["id"]
        if cue_id in seen:
            raise ValueError(f"Duplicate ASR cross-check cue: {cue_id}")
        seen.add(cue_id)
        interval(cue)
        if cue_id not in baseline_rows:
            raise ValueError(f"Missing ASR cross-check baseline cue: {cue_id}")
        baseline_row = baseline_rows[cue_id]
        machine = copy.deepcopy(baseline_row)
        diagnostic = _diagnostic()
        selected = _complete_window(cue, normalized_windows)
        if selected is None:
            diagnostic["reason"] = "no_complete_window"
        else:
            window_id, window = selected
            turns = _turns(window)
            diagnostic["window_id"] = window_id
            diagnostic["text"] = _text_crosscheck(cue, turns)
            speech = _speech_geometry(cue, turns, settings)
            diagnostic["speech"] = speech
            if speech["reason"] != "speech_ok":
                diagnostic["reason"] = f"structural_{speech['reason']}"
            else:
                speaker = speech["speaker"]
                anchors, strong = _anchors(window_id, speaker, turns, evidence_rows, settings)
                diagnostic["anchors"] = anchors
                diagnostic["opposing_evidence"] = strong
                anchor_labels = {item["label"] for item in anchors}
                strong_labels = {item["label"] for item in strong}
                baseline_label = baseline_row["label"]
                fresh_rescue = (baseline_label == "unknown"
                                and baseline_row["reason"] in _FRESH_RESCUABLE_REASONS)
                if fresh_rescue:
                    start, end = interval(cue)
                    local = [item for item in strong
                             if item.get("origin") == "moss_cue"
                             and item.get("cue_id") == cue_id
                             and item["confidence"] >= settings.confidence]
                    fresh_pool = [item for item in evidence_rows
                                  if item.get("origin") == "moss_speaker"
                                  and _overlap(start, end, item["start_us"], item["end_us"]) == 0]
                    references, _ = _anchors(window_id, speaker, turns, fresh_pool, settings)
                    labels = {item["label"] for item in references}
                    if len(labels) != 1 or len(references) < 2 or not local:
                        diagnostic["reason"] = "insufficient_fresh_speaker_support"
                    elif strong_labels != labels or {item["label"] for item in local} != labels:
                        diagnostic["reason"] = "contradictory_acoustic_evidence"
                    else:
                        machine.update(label=next(iter(labels)), reason="moss_acoustic_consensus",
                                       speaker=speaker,
                                       confidence=min(item["confidence"]
                                                      for item in [*references, *local]),
                                       speech_us=speech["speech_us"],
                                       speech_ratio=speech["speech_ratio"],
                                       overlap_us=speech["overlap_us"])
                        diagnostic.update(status="rescued", reason="moss_acoustic_consensus")
                elif baseline_row["reason"] not in _RESCUABLE_REASONS:
                    if baseline_label in _GENDER_LABELS and strong_labels - {baseline_label}:
                        diagnostic.update(status="conflict", reason="opposing_evidence")
                    else:
                        diagnostic["reason"] = "baseline_not_rescuable"
                elif strong_labels and len(strong_labels) > 1:
                    diagnostic["reason"] = "contradictory_acoustic_evidence"
                elif len(anchor_labels) != 1 or len(anchors) < 2:
                    diagnostic["reason"] = "insufficient_independent_anchors"
                elif strong_labels and strong_labels != anchor_labels:
                    diagnostic["reason"] = "opposing_evidence"
                else:
                    label = next(iter(anchor_labels))
                    confidence = min(item["confidence"] for item in anchors)
                    machine.update(
                        {
                            "label": label,
                            "reason": "asr_speaker_consensus",
                            "speaker": speaker,
                            "confidence": confidence,
                            "speech_us": speech["speech_us"],
                            "speech_ratio": speech["speech_ratio"],
                            "overlap_us": speech["overlap_us"],
                        }
                    )
                    diagnostic.update(status="rescued", reason="asr_speaker_consensus")
        output.append(
            {
                "cue_id": cue_id,
                "machine": machine,
                "baseline": copy.deepcopy(baseline_row),
                "crosscheck": diagnostic,
            }
        )
    return output


__all__ = ["crosscheck_cues", "moss_sample_ranges"]
