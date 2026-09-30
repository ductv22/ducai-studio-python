from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.subtitles import SubtitleCandidate, SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange, format_srt_timestamp

from .ocr_pipeline import cjk_count, compact_text, text_similarity

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
        bins = {
            min(7, int(8 * (item.timestamp_us - timeline_start) / span))
            for item in matches
        }
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
        distributed = _timeline_bin_count(items, token, suffix=False) >= min(
            4, max(2, total // 4)
        )
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
) -> tuple[tuple[SubtitleCandidate, ...], int]:
    result: list[SubtitleCandidate] = []
    resolved = 0
    for candidate in sorted(candidates, key=lambda item: (item.range.start_us, item.range.end_us)):
        if not result or result[-1].range.end_us <= candidate.range.start_us:
            result.append(candidate)
            continue
        previous = result[-1]
        overlap_us = previous.range.end_us - candidate.range.start_us
        if text_similarity(previous.text, candidate.text) >= 0.88:
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
                evidence_ids=tuple(
                    sorted({*previous.evidence_ids, *candidate.evidence_ids})
                ),
                review_reasons=tuple(
                    sorted({*previous.review_reasons, *candidate.review_reasons})
                ),
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
            result.append(
                replace(candidate, range=TimeRange(boundary, candidate.range.end_us))
            )
            resolved += 1
            continue
        result.append(candidate)
    return tuple(result), resolved


def finalize_ocr_candidates(
    candidates: tuple[SubtitleCandidate, ...],
    *,
    video_duration_us: int,
    language: str = "zh",
    evidence: tuple[SubtitleEvidence, ...] = (),
) -> FinalizationResult:
    overlays = list(learn_repeated_overlays(candidates))
    for token in _learn_evidence_channel_overlays(evidence):
        if not any(token in current or current in token for current in overlays):
            overlays.append(token)
    overlay_tokens = tuple(overlays)
    overlay = overlay_tokens[0] if overlay_tokens else ""
    cleaned, stripped, removed = strip_proven_overlays(candidates, overlay_tokens)
    normalized, overlap_resolutions = _resolve_sampling_overlaps(cleaned)
    ordered = sorted(normalized, key=lambda item: (item.range.start_us, item.range.end_us))
    blockers: list[str] = []
    warnings: list[str] = []
    if overlap_resolutions:
        warnings.append(f"sampling_overlaps_resolved:{overlap_resolutions}")
    previous: SubtitleCandidate | None = None
    for candidate in ordered:
        if candidate.range.end_us > video_duration_us:
            blockers.append(f"cue_outside_video:{candidate.id}")
        if cjk_count(candidate.text) == 0:
            blockers.append(f"cue_without_chinese:{candidate.id}")
        if candidate.review_reasons:
            blockers.append(
                f"cue_requires_review:{candidate.id}:{','.join(candidate.review_reasons)}"
            )
        if previous is not None and previous.range.end_us > candidate.range.start_us:
            blockers.append(f"cue_overlap:{previous.id}:{candidate.id}")
        previous = candidate
    if not ordered:
        blockers.append("no_final_cues")
    if any(re.search(r"[A-Za-z]", item.text) for item in ordered):
        warnings.append("latin_characters_present")
    evidence_by_id = {item.id: item for item in evidence}

    def visual_box(item: SubtitleCandidate) -> tuple[float, float, float, float] | None:
        boxes = [
            evidence_by_id[evidence_id].box
            for evidence_id in item.evidence_ids
            if evidence_id in evidence_by_id and evidence_by_id[evidence_id].box is not None
        ]
        if not boxes:
            return None
        left = min(box.x for box in boxes)
        top = min(box.y for box in boxes)
        right = max(box.x + box.width for box in boxes)
        bottom = max(box.y + box.height for box in boxes)
        return (left, top, right - left, bottom - top)

    cue_rows: list[SubtitleCue] = []
    for index, item in enumerate(ordered, start=1):
        box = visual_box(item)
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
