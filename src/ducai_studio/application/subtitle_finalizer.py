from __future__ import annotations

import math
import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import overload

from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.subtitles import SubtitleCandidate, SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange, format_srt_timestamp

from .ocr_pipeline import candidates_share_visual_frame, cjk_count, compact_text, text_similarity

_CHANNEL_SUFFIXES = ("看漫剧", "说漫剧", "漫剧解说", "漫画解说", "动漫解说")


class FinalizationStatus(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    status: FinalizationStatus
    cues: tuple[SubtitleCue, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    overlay_token: str = ""
    overlay_rows_stripped: int = 0
    overlay_only_rows_removed: int = 0
    overlay_tokens: tuple[str, ...] = ()


def _known_channel_overlay_signature(token: str) -> bool:
    compact = compact_text(token)
    suffix = next((value for value in _CHANNEL_SUFFIXES if compact.endswith(value)), "")
    if not suffix or not 4 <= cjk_count(compact) <= 10:
        return False
    prefix = compact[: -len(suffix)]
    return bool(2 <= len(prefix) <= 4 and prefix[0] in "小老阿")


def _known_channel_prefix(value: str) -> str:
    compact = compact_text(value)
    match = re.match(
        r"([\u3400-\u9fff]{1,4}(?:看漫剧|说漫剧|漫剧解说|漫画解说|动漫解说))",
        compact,
    )
    if not match:
        return ""
    token = match.group(1)
    return token if _known_channel_overlay_signature(token) else ""


def _learn_evidence_channel_overlays(
    evidence: tuple[SubtitleEvidence, ...],
) -> tuple[str, ...]:
    """Learn a persistent channel label before temporal grouping collapses it.

    A pure-CJK watermark can become one long candidate, so candidate frequency
    is no longer evidence of repetition.  OCR samples retain that evidence and
    full-frame geometry: require at least six observations, broad timeline
    distribution and a compact region touching a horizontal frame edge.
    """

    rows = tuple(
        item
        for item in evidence
        if item.box is not None
        and item.box.height <= 0.18
        and (item.box.x <= 0.25 or item.box.x + item.box.width >= 0.75)
    )
    if len(rows) < 6:
        return ()
    occurrences: dict[str, list[SubtitleEvidence]] = {}
    for item in rows:
        token = _known_channel_prefix(item.text)
        if token:
            occurrences.setdefault(token, []).append(item)
    timeline_start = min(item.timestamp_us for item in rows)
    timeline_end = max(item.timestamp_us for item in rows)
    span = max(1, timeline_end - timeline_start)
    result: list[tuple[int, int, str]] = []
    for token, matches in occurrences.items():
        frame_count = len({item.frame_id for item in matches})
        bins = {min(7, int(8 * (item.timestamp_us - timeline_start) / span)) for item in matches}
        if frame_count >= 6 and len(bins) >= 4:
            result.append((frame_count, len(token), token))
    return tuple(token for _count, _length, token in sorted(result, reverse=True)[:4])


def _timeline_bin_count(items: list[tuple[int, str]], token: str, *, suffix: bool) -> int:
    matching = [start for start, text in items if text.endswith(token) if suffix or text == token]
    if not matching:
        return 0
    lower = min(start for start, _text in items)
    upper = max(start for start, _text in items)
    span = max(1, upper - lower)
    return len({min(7, int(8 * max(0, start - lower) / span)) for start in matching})


def _learn_exact_overlay(candidates: tuple[SubtitleCandidate, ...]) -> str:
    items = [
        (item.range.start_us, compact_text(item.text))
        for item in candidates
        if compact_text(item.text)
    ]
    total = len(items)
    if total < 8:
        return ""
    exact = Counter(text for _start, text in items)
    minimum_exact = max(5, math.ceil(total * 0.012))
    ranked: list[tuple[int, int, str]] = []
    for token, exact_count in exact.items():
        if not 3 <= len(token) <= 10 or cjk_count(token) < 2:
            continue
        if exact_count < minimum_exact:
            continue
        suffix_count = sum(text.endswith(token) for _start, text in items)
        attached_count = suffix_count - exact_count
        distributed = _timeline_bin_count(items, token, suffix=False) >= min(4, max(2, total // 4))
        proven_suffix = suffix_count >= max(6, math.ceil(total * 0.04)) and (
            attached_count >= max(3, math.ceil(total * 0.005))
        )
        proven_known_channel = _known_channel_overlay_signature(token) and (
            exact_count >= max(6, math.ceil(total * 0.02))
        )
        if distributed and (proven_suffix or proven_known_channel):
            ranked.append((exact_count * 2 + attached_count, len(token), token))
    return max(ranked)[2] if ranked else ""


def _timeline_bins_for_rows(items: list[tuple[int, str]], rows: set[int]) -> int:
    starts = [items[index][0] for index in rows]
    if not starts:
        return 0
    lower = min(start for start, _text in items)
    upper = max(start for start, _text in items)
    span = max(1, upper - lower)
    return len({min(7, int(8 * max(0, start - lower) / span)) for start in starts})


def _learn_alphanumeric_overlays(
    candidates: tuple[SubtitleCandidate, ...],
) -> tuple[str, ...]:
    items = [
        (item.range.start_us, compact_text(item.text))
        for item in candidates
        if compact_text(item.text)
    ]
    if len(items) < 6:
        return ()
    occurrences: dict[str, set[int]] = {}
    for row, (_start, text) in enumerate(items):
        seen: set[str] = set()
        for start in range(len(text)):
            for length in range(4, min(12, len(text) - start) + 1):
                token = text[start : start + length]
                latin_count = sum(
                    character.isascii() and character.isalpha() for character in token
                )
                if not any(character.isdigit() for character in token) and latin_count < 5:
                    continue
                seen.add(token)
        for token in seen:
            occurrences.setdefault(token, set()).add(row)
    minimum = max(6, math.ceil(len(items) * 0.015))
    ranked = sorted(
        (
            (len(rows), len(token), token, rows)
            for token, rows in occurrences.items()
            if len(rows) >= minimum and _timeline_bins_for_rows(items, rows) >= 4
        ),
        reverse=True,
    )
    selected: list[str] = []
    for _count, _length, token, _rows in ranked:
        if any(token in current or current in token for current in selected):
            continue
        selected.append(token)
        if len(selected) == 4:
            break
    return tuple(selected)


def learn_repeated_overlays(
    candidates: tuple[SubtitleCandidate, ...],
) -> tuple[str, ...]:
    result: list[str] = []
    exact = _learn_exact_overlay(candidates)
    if exact:
        result.append(exact)
    for token in _learn_alphanumeric_overlays(candidates):
        if not any(token in current or current in token for current in result):
            result.append(token)
    return tuple(result)


def learn_repeated_overlay(candidates: tuple[SubtitleCandidate, ...]) -> str:
    overlays = learn_repeated_overlays(candidates)
    return overlays[0] if overlays else ""


def _remove_compact_token(value: str, token: str) -> str:
    result = value
    while True:
        compact: list[str] = []
        positions: list[int] = []
        for index, character in enumerate(result):
            if re.match(r"[0-9A-Za-z\u3400-\u9fff]", character):
                compact.append(character.casefold())
                positions.append(index)
        compact_value = "".join(compact)
        start_index = compact_value.find(token)
        if start_index < 0:
            break
        end_index = start_index + len(token) - 1
        result = result[: positions[start_index]] + result[positions[end_index] + 1 :]
    return result


def _strip_fuzzy_latin_variants(value: str, overlays: tuple[str, ...]) -> str:
    latin = tuple(
        token for token in overlays if len(token) >= 5 and token.isascii() and token.isalpha()
    )
    if not latin:
        return value
    result = value
    matches = list(re.finditer(r"[A-Za-z]{5,12}", result))
    for match in reversed(matches):
        if max(text_similarity(match.group(), token) for token in latin) < 0.72:
            continue
        result = result[: match.start()] + result[match.end() :]
    return result


def strip_proven_overlays(
    candidates: tuple[SubtitleCandidate, ...],
    tokens: tuple[str, ...],
) -> tuple[tuple[SubtitleCandidate, ...], int, int]:
    if not tokens:
        return candidates, 0, 0
    result: list[SubtitleCandidate] = []
    stripped = 0
    removed = 0
    for candidate in candidates:
        cleaned = candidate.text
        for token in tokens:
            if not (token.isascii() and token.isalpha()):
                cleaned = _remove_compact_token(cleaned, token)
        cleaned = _strip_fuzzy_latin_variants(cleaned, tokens).strip(
            " \t\r\n—，。！？、：；·|丨【】()[]（）"
        )
        if cleaned == candidate.text:
            result.append(candidate)
            continue
        if cjk_count(cleaned) == 0:
            removed += 1
            continue
        result.append(replace(candidate, text=cleaned))
        stripped += 1
    return tuple(result), stripped, removed


def strip_proven_overlay(
    candidates: tuple[SubtitleCandidate, ...],
    token: str,
) -> tuple[tuple[SubtitleCandidate, ...], int, int]:
    return strip_proven_overlays(candidates, (token,) if token else ())


def _resolve_sampling_overlaps(
    candidates: tuple[SubtitleCandidate, ...],
    *,
    maximum_overlap_us: int = 800_000,
    evidence: tuple[SubtitleEvidence, ...] = (),
) -> tuple[tuple[SubtitleCandidate, ...], int]:
    by_id = {item.id: item for item in evidence}

    def same_frame(left: SubtitleCandidate, right: SubtitleCandidate) -> bool:
        if not evidence:
            return compact_text(left.text) == compact_text(right.text)
        return candidates_share_visual_frame(left, right, by_id)

    result: list[SubtitleCandidate] = []
    resolved = 0
    for candidate in sorted(candidates, key=lambda item: (item.range.start_us, item.range.end_us)):
        if not result or result[-1].range.end_us <= candidate.range.start_us:
            result.append(candidate)
            continue
        previous = result[-1]
        overlap_us = previous.range.end_us - candidate.range.start_us
        if text_similarity(previous.text, candidate.text) >= 0.88 and same_frame(
            previous, candidate
        ):
            winner = max(
                (previous, candidate),
                key=lambda item: (cjk_count(item.text), item.confidence, len(item.text)),
            )
            result[-1] = replace(
                winner,
                range=TimeRange(
                    min(previous.range.start_us, candidate.range.start_us),
                    max(previous.range.end_us, candidate.range.end_us),
                ),
                evidence_ids=tuple(sorted({*previous.evidence_ids, *candidate.evidence_ids})),
                review_reasons=tuple(sorted({*previous.review_reasons, *candidate.review_reasons})),
                source="sampling_duplicate_collapsed",
            )
            resolved += 1
            continue
        boundary = (previous.range.end_us + candidate.range.start_us) // 2
        if (
            overlap_us <= maximum_overlap_us
            and boundary > previous.range.start_us
            and candidate.range.end_us > boundary
        ):
            result[-1] = replace(
                previous,
                range=TimeRange(previous.range.start_us, boundary),
            )
            result.append(replace(candidate, range=TimeRange(boundary, candidate.range.end_us)))
            resolved += 1
            continue
        result.append(candidate)
    return tuple(result), resolved


def collapse_partial_ocr_cues(
    cues: tuple[SubtitleCue, ...],
    *,
    suppressed_evidence: Mapping[str, str] | None = None,
) -> tuple[SubtitleCue, ...]:
    """Collapse overlapping partial OCR of the same line, never manual edits.

    Reviewed SRT imports can bypass candidate finalization. Geometry and strong
    temporal containment are required here; similarity alone is not evidence
    that two separate lines/characters are the same dialogue.
    """
    cues = _collapse_proven_crop_occurrences(cues, suppressed_evidence or {})
    result: list[SubtitleCue] = []
    active: list[int] = []
    for cue in sorted(cues, key=lambda item: (item.range.start_us, item.range.end_us)):
        active = [index for index in active if result[index].range.end_us > cue.range.start_us]
        merged = False
        for index in reversed(active):
            previous = result[index]
            if previous.range.end_us <= cue.range.start_us:
                continue
            if (
                "ocr" not in previous.source.casefold()
                or "ocr" not in cue.source.casefold()
                or previous.role != "dialogue"
                or cue.role != "dialogue"
                or previous.subtitle_track_id != cue.subtitle_track_id
                or previous.visual_box is None
                or cue.visual_box is None
            ):
                continue
            short, long = sorted(
                (compact_text(previous.text), compact_text(cue.text)),
                key=lambda text: (cjk_count(text), len(text)),
            )
            if cjk_count(short) < 3:
                continue
            if not _contained_ocr_reading(short, long):
                continue
            overlap = min(previous.range.end_us, cue.range.end_us) - max(
                previous.range.start_us, cue.range.start_us
            )
            if overlap < 0.65 * min(previous.range.duration_us, cue.range.duration_us):
                continue
            ax, ay, aw, ah = previous.visual_box
            bx, by, bw, bh = cue.visual_box
            area = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
                0, min(ay + ah, by + bh) - max(ay, by)
            )
            if area < 0.75 * min(aw * ah, bw * bh):
                continue
            winner = max(
                (previous, cue),
                key=lambda item: (
                    cjk_count(item.text),
                    item.confidence or 0,
                    len(item.evidence_ids),
                ),
            )
            result[index] = replace(
                winner,
                range=TimeRange(
                    min(previous.range.start_us, cue.range.start_us),
                    max(previous.range.end_us, cue.range.end_us),
                ),
                evidence_ids=tuple(sorted({*previous.evidence_ids, *cue.evidence_ids})),
            )
            merged = True
            break
        if not merged:
            active.append(len(result))
            result.append(cue)
    return tuple(replace(cue, index=index) for index, cue in enumerate(result, 1))


def collapse_proven_ocr_cues(
    cues: tuple[SubtitleCue, ...], *, suppressed_evidence: Mapping[str, str]
) -> tuple[SubtitleCue, ...]:
    """Strict reviewed import: do not run the legacy containment heuristic."""
    return tuple(
        replace(cue, index=index)
        for index, cue in enumerate(_collapse_proven_crop_occurrences(cues, suppressed_evidence), 1)
    )


def collapse_proven_ocr_candidates(
    candidates: tuple[SubtitleCandidate, ...],
    *,
    evidence: tuple[SubtitleEvidence, ...],
    suppressed_evidence: Mapping[str, str],
) -> tuple[SubtitleCandidate, ...]:
    return _collapse_proven_crop_occurrences(
        candidates, suppressed_evidence,
        source_scopes={item.id: item.source_scope for item in evidence},
    )


def _crop_occurrence_scope(item: SubtitleCue | SubtitleCandidate) -> str | None:
    if isinstance(item, SubtitleCandidate):
        # Candidates passed to finalization are dialogue; scene cues are built
        # separately. The worker's proof already binds source/frame/geometry.
        return "dialogue-candidate"
    if item.is_dialogue and "ocr" in item.source.casefold():
        return item.subtitle_track_id
    return None


@overload
def _collapse_proven_crop_occurrences(
    cues: tuple[SubtitleCue, ...],
    proofs: Mapping[str, str],
    *, source_scopes: Mapping[str, str] | None = None,
) -> tuple[SubtitleCue, ...]: ...


@overload
def _collapse_proven_crop_occurrences(
    cues: tuple[SubtitleCandidate, ...],
    proofs: Mapping[str, str],
    *, source_scopes: Mapping[str, str] | None = None,
) -> tuple[SubtitleCandidate, ...]: ...


def _collapse_proven_crop_occurrences(
    cues: tuple[SubtitleCue | SubtitleCandidate, ...],
    proofs: Mapping[str, str],
    *, source_scopes: Mapping[str, str] | None = None,
) -> tuple[SubtitleCue | SubtitleCandidate, ...]:
    """Remove a whole occurrence only with proofs to one unambiguous survivor."""
    if not proofs:
        return cues
    owners: dict[str, list[SubtitleCue | SubtitleCandidate]] = {}
    for cue in cues:
        if _crop_occurrence_scope(cue) is None:
            continue
        for key in cue.evidence_ids:
            owners.setdefault(key, []).append(cue)
    by_evidence = {
        key: values[0] for key, values in owners.items() if len(values) == 1 and key not in proofs
    }
    replacements: dict[str, SubtitleCue | SubtitleCandidate] = {}
    removed: set[str] = set()
    for cue in cues:
        if (
            _crop_occurrence_scope(cue) is None
            or not cue.evidence_ids
            or not all(key in proofs for key in cue.evidence_ids)
            or any(len(owners.get(key, ())) != 1 for key in cue.evidence_ids)
        ):
            continue
        winners = {
            by_evidence[proofs[key]].id for key in cue.evidence_ids if proofs[key] in by_evidence
        }
        if len(winners) != 1 or any(proofs[key] not in by_evidence for key in cue.evidence_ids):
            continue
        winner = by_evidence[proofs[cue.evidence_ids[0]]]
        if winner.id == cue.id or _crop_occurrence_scope(winner) != _crop_occurrence_scope(cue):
            continue
        if source_scopes is not None:
            loser_scopes = {source_scopes.get(key) for key in cue.evidence_ids}
            winner_scopes = {source_scopes.get(key) for key in winner.evidence_ids}
            if None in loser_scopes or len(loser_scopes) != 1 or loser_scopes != winner_scopes:
                continue
        winner = replacements.get(winner.id, winner)
        replacements[winner.id] = replace(
            winner,
            range=TimeRange(
                min(winner.range.start_us, cue.range.start_us),
                max(winner.range.end_us, cue.range.end_us),
            ),
        )
        removed.add(cue.id)
    return tuple(replacements.get(cue.id, cue) for cue in cues if cue.id not in removed)


def _contained_ocr_reading(short: str, long: str) -> bool:
    def contained(value: str, reading: str) -> bool:
        letters = iter(reading)
        return all(any(char == candidate for candidate in letters) for char in value)

    if contained(short, long):
        return True
    # Earlier engines could rotate fragments from one row. Only circular order
    # with the same glyphs is eligible, under the caller's geometry/time gates.
    if len(short) >= len(long) * 0.6 and any(
        contained(short, long[offset:] + long[:offset]) for offset in range(1, len(long))
    ):
        return True
    if cjk_count(short) >= cjk_count(long) or len(short) < 4 or len(short) < len(long) * 0.65:
        return False
    # A cropped, shorter reading may also contain a few recognition errors.
    # Match it in order with bounded substitutions; do not treat different
    # complete sentences as duplicates just because they share most words.
    costs = list(range(len(short) + 1))
    for character in long:
        previous = costs
        costs = [0]
        for index, glyph in enumerate(short, 1):
            costs.append(min(previous[index], previous[index - 1] + (glyph != character)))
    return costs[-1] <= max(1, len(short) // 6)


def _candidate_visual_box(
    candidate: SubtitleCandidate,
    evidence_by_id: dict[str, SubtitleEvidence],
) -> tuple[float, float, float, float] | None:
    boxes = [
        evidence_by_id[key].box
        for key in candidate.evidence_ids
        if key in evidence_by_id and evidence_by_id[key].box is not None
    ]
    if not boxes:
        return None
    left = min(box.x for box in boxes)
    top = min(box.y for box in boxes)
    right = max(box.x + box.width for box in boxes)
    bottom = max(box.y + box.height for box in boxes)
    return (left, top, right - left, bottom - top)


def _collapse_candidate_occurrences(
    candidates: tuple[SubtitleCandidate, ...],
    evidence: tuple[SubtitleEvidence, ...],
    proven_boxes: dict[str, tuple[float, float, float, float]],
) -> tuple[tuple[SubtitleCandidate, ...], int]:
    evidence_by_id = {item.id: item for item in evidence}
    by_id = {item.id: item for item in candidates}
    temporary = tuple(
        SubtitleCue(
            id=item.id,
            index=index,
            range=item.range,
            text=item.text,
            language="zh",
            source="ocr_candidate",
            confidence=item.confidence,
            evidence_ids=item.evidence_ids,
            visual_box=proven_boxes.get(item.id) or _candidate_visual_box(item, evidence_by_id),
            visual_box_source="ocr" if (item.id in proven_boxes
                                       or _candidate_visual_box(item, evidence_by_id)) else "",
        )
        for index, item in enumerate(candidates, 1)
    )
    collapsed = collapse_partial_ocr_cues(temporary)
    merged = tuple(
        replace(by_id[cue.id], range=cue.range, evidence_ids=cue.evidence_ids) for cue in collapsed
    )
    return merged, len(candidates) - len(merged)


def _corroborated_dialogue_row(
    candidate: SubtitleCandidate, rows: list[SubtitleEvidence],
) -> list[SubtitleEvidence]:
    """Select observed text, not a length-first union polluted by scene glyphs."""
    groups: dict[tuple[str, str], list[SubtitleEvidence]] = {}
    text = compact_text(candidate.text)
    for row in rows:
        value = compact_text(row.text)
        if (row.box is None or row.orientation != "horizontal" or row.confidence < .97
                or cjk_count(value) < 6 or re.search(r"[0-9A-Za-z]", value)):
            continue
        matcher = SequenceMatcher(None, text, value)
        match = matcher.find_longest_match()
        if ((match.size < 6 or match.size < min(len(text), len(value)) * .72)
                and matcher.ratio() < .90):
            continue
        groups.setdefault((row.source_scope, value), []).append(row)
    supported = [group for group in groups.values()
                 if len({(row.frame_id, row.timestamp_us) for row in group}) >= 2]
    return max(supported, key=lambda group: (
        len({(row.frame_id, row.timestamp_us) for row in group}),
        sum(row.confidence for row in group) / len(group),
    ), default=[])


def _collapse_mixed_row_occurrences(
    candidates: tuple[SubtitleCandidate, ...],
    evidence: tuple[SubtitleEvidence, ...],
) -> tuple[tuple[SubtitleCandidate, ...], int, dict[str, tuple[float, float, float, float]]]:
    """Reconcile noisy map/scene rows only with corroborated same-frame dialogue.

    A longer mixed reading is not a more complete sentence. Require a strong
    repeated clean reading, a weak alphanumeric contaminated variant and two
    shared source frames whose taller mixed box contains the dialogue glyphs.
    Independent short lines and uncertain conflicting sentences stay reviewable.
    """
    by_id = {item.id: item for item in evidence}
    ordered = sorted(candidates, key=lambda item: item.range.start_us)
    removed: set[str] = set()
    replacements: dict[str, SubtitleCandidate] = {}
    proven_boxes: dict[str, tuple[float, float, float, float]] = {}
    active: list[SubtitleCandidate] = []
    for candidate in ordered:
        active = [item for item in active if item.range.end_us > candidate.range.start_us]
        for other in active:
            if other.id in removed or candidate.id in removed:
                continue
            clean, mixed = sorted((candidate, other), key=lambda item: item.confidence,
                                  reverse=True)
            mixed_text = compact_text(mixed.text)
            overlap = min(clean.range.end_us, mixed.range.end_us) - max(
                clean.range.start_us, mixed.range.start_us
            )
            if (clean.confidence < .97 or mixed.confidence > .90
                    or clean.confidence - mixed.confidence < .10
                    or not re.search(r"[0-9A-Za-z]", mixed_text)
                    or overlap < min(clean.range.duration_us, mixed.range.duration_us) * .65):
                continue
            clean_rows = [by_id[key] for key in clean.evidence_ids if key in by_id]
            strong = _corroborated_dialogue_row(clean, clean_rows)
            if not strong:
                continue
            clean_text = compact_text(strong[0].text)
            match = SequenceMatcher(None, clean_text, mixed_text).find_longest_match()
            if match.size < 6 or match.size < len(clean_text) * .72:
                continue
            frames = set()
            for key in mixed.evidence_ids:
                row = by_id.get(key)
                if row is None or row.box is None:
                    continue
                for full in clean_rows:
                    if (full.box is None or full.source_scope != row.source_scope
                            or full.frame_id != row.frame_id
                            or full.timestamp_us != row.timestamp_us
                            or full.confidence < .90
                            or full.source_scope != strong[0].source_scope
                            or row.box.height < full.box.height * 1.6):
                        continue
                    common = SequenceMatcher(None, clean_text,
                                             compact_text(full.text)).find_longest_match()
                    if common.size < 6 or common.size < len(clean_text) * .72:
                        continue
                    a, b = row.box, full.box
                    area = max(0, min(a.x+a.width, b.x+b.width) - max(a.x, b.x)) * max(
                        0, min(a.y+a.height, b.y+b.height) - max(a.y, b.y)
                    )
                    if area >= b.width * b.height * .80:
                        frames.add((row.source_scope, row.frame_id, row.timestamp_us))
            if len(frames) < 2:
                continue
            winner = replacements.get(clean.id, clean)
            replacements[clean.id] = replace(
                winner,
                text=strong[0].text,
                evidence_ids=tuple(sorted({*winner.evidence_ids, *mixed.evidence_ids})),
            )
            box = _candidate_visual_box(
                replace(clean, evidence_ids=tuple(row.id for row in strong)), by_id,
            )
            if box is not None:
                proven_boxes[clean.id] = box
            removed.add(mixed.id)
        active.append(candidate)
    return (tuple(replacements.get(c.id, c) for c in ordered if c.id not in removed),
            len(removed), proven_boxes)


def _restore_uncorroborated_repeated_glyphs(
    candidates: tuple[SubtitleCandidate, ...], evidence: tuple[SubtitleEvidence, ...],
) -> tuple[tuple[SubtitleCandidate, ...], int]:
    """Do not let a one-frame repeated-glyph insertion win a multiband merge.

    Restore only an observed, repeated reading. Genuine repeated words, short
    dialogue and variants supported by multiple source frames remain untouched.
    """
    by_id = {row.id: row for row in evidence}
    result = []
    restored = 0
    for candidate in candidates:
        original = compact_text(candidate.text)
        if not re.search(r"([\u3400-\u9fff])\1", original):
            result.append(candidate)
            continue
        rows = [by_id[key] for key in candidate.evidence_ids if key in by_id]
        strong = _corroborated_dialogue_row(candidate, rows)
        replacement = compact_text(strong[0].text) if strong else original
        edits = [op for op in SequenceMatcher(None, original, replacement).get_opcodes()
                 if op[0] != "equal"]
        supported_frames = {(row.source_scope, row.frame_id, row.timestamp_us) for row in strong}
        original_rows = [row for row in rows if compact_text(row.text) == original]
        original_frames = {(row.source_scope, row.frame_id, row.timestamp_us)
                           for row in original_rows}
        if (len(edits) == 1 and edits[0][0] == "delete"
                and edits[0][2] - edits[0][1] == 1
                and ((edits[0][1] > 0
                      and original[edits[0][1]] == original[edits[0][1] - 1])
                     or (edits[0][2] < len(original)
                         and original[edits[0][1]] == original[edits[0][2]]))
                and len(supported_frames) >= 3 and len(original_frames) == 1
                and candidates_share_visual_frame(
                    replace(candidate, evidence_ids=tuple(row.id for row in original_rows)),
                    replace(candidate, evidence_ids=tuple(row.id for row in strong)), by_id,
                )):
            candidate = replace(candidate, text=strong[0].text)
            restored += 1
        result.append(candidate)
    return tuple(result), restored


def finalize_ocr_candidates(
    candidates: tuple[SubtitleCandidate, ...],
    *,
    video_duration_us: int,
    language: str = "zh",
    evidence: tuple[SubtitleEvidence, ...] = (),
    suppressed_evidence: Mapping[str, str] | None = None,
) -> FinalizationResult:
    overlays = list(learn_repeated_overlays(candidates))
    for token in _learn_evidence_channel_overlays(evidence):
        if not any(token in current or current in token for current in overlays):
            overlays.append(token)
    overlay_tokens = tuple(overlays)
    overlay = overlay_tokens[0] if overlay_tokens else ""
    cleaned, stripped, removed = strip_proven_overlays(candidates, overlay_tokens)
    cleaned, glyph_restorations = _restore_uncorroborated_repeated_glyphs(cleaned, evidence)
    cleaned, mixed_count, proven_boxes = _collapse_mixed_row_occurrences(cleaned, evidence)
    cleaned, duplicate_count = _collapse_candidate_occurrences(cleaned, evidence, proven_boxes)
    normalized, overlap_resolutions = _resolve_sampling_overlaps(cleaned, evidence=evidence)
    before_proof_collapse = len(normalized)
    normalized = collapse_proven_ocr_candidates(
        normalized, evidence=evidence, suppressed_evidence=suppressed_evidence or {}
    )
    proof_collapses = before_proof_collapse - len(normalized)
    ordered = sorted(normalized, key=lambda item: (item.range.start_us, item.range.end_us))
    blockers: list[str] = []
    warnings: list[str] = []
    if proof_collapses:
        warnings.append(f"same_frame_crop_occurrences_collapsed:{proof_collapses}")
    if glyph_restorations:
        warnings.append(f"uncorroborated_repeated_glyphs_restored:{glyph_restorations}")
    if duplicate_count:
        warnings.append(f"same_visual_occurrences_collapsed:{duplicate_count}")
    if mixed_count:
        warnings.append(f"same_frame_mixed_rows_collapsed:{mixed_count}")
    if overlap_resolutions:
        warnings.append(f"sampling_overlaps_resolved:{overlap_resolutions}")
    active: list[SubtitleCandidate] = []
    review_ids: set[str] = set()
    for candidate in ordered:
        if candidate.range.end_us > video_duration_us:
            blockers.append(f"cue_outside_video:{candidate.id}")
        if cjk_count(candidate.text) == 0:
            blockers.append(f"cue_without_chinese:{candidate.id}")
        if candidate.review_reasons:
            review_ids.add(candidate.id)
            blockers.append(
                f"cue_requires_review:{candidate.id}:{','.join(candidate.review_reasons)}"
            )
        active = [previous for previous in active
                  if previous.range.end_us > candidate.range.start_us]
        for previous in active:
            review_ids.update((previous.id, candidate.id))
            blockers.append(f"cue_overlap:{previous.id}:{candidate.id}")
        active.append(candidate)
    if not ordered:
        blockers.append("no_final_cues")
    if any(re.search(r"[A-Za-z]", item.text) for item in ordered):
        warnings.append("latin_characters_present")
    evidence_by_id = {item.id: item for item in evidence}

    cue_rows: list[SubtitleCue] = []
    for index, item in enumerate(ordered, start=1):
        box = proven_boxes.get(item.id) or _candidate_visual_box(item, evidence_by_id)
        cue_rows.append(
            SubtitleCue(
                id=f"ocr-final-{index}",
                index=index,
                range=item.range,
                text=item.text,
                language=language,
                source=item.source,
                confidence=item.confidence,
                evidence_ids=item.evidence_ids,
                visual_box=box,
                visual_box_source="ocr" if box is not None else "",
                review_state="review" if item.id in review_ids else "accepted",
            )
        )
    cues = tuple(cue_rows)
    if blockers:
        return FinalizationResult(
            FinalizationStatus.REVIEW,
            cues,
            tuple(blockers),
            tuple(warnings),
            overlay,
            stripped,
            removed,
            overlay_tokens,
        )
    return FinalizationResult(
        FinalizationStatus.PASS,
        cues,
        (),
        tuple(warnings),
        overlay,
        stripped,
        removed,
        overlay_tokens,
    )


def render_srt(cues: tuple[SubtitleCue, ...]) -> str:
    blocks = [
        (
            f"{cue.index}\n{format_srt_timestamp(cue.range.start_us)} --> "
            f"{format_srt_timestamp(cue.range.end_us)}\n{cue.text}"
        )
        for cue in cues
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def publish_final_srt(path: str | Path, result: FinalizationResult) -> Path:
    target = Path(path).expanduser().resolve()
    if result.status is not FinalizationStatus.PASS or not result.cues:
        raise ValueError("Only a non-empty PASS result can be published")
    if not target.name.endswith("_ZH_FINAL.srt"):
        raise ValueError("Final OCR output must end with _ZH_FINAL.srt")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text("\ufeff" + render_srt(result.cues), encoding="utf-8")
    os.replace(temporary, target)
    return target
