from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Any

from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.subtitles import RescueWindow, SubtitleCandidate, SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange

_CJK = re.compile(r"[\u3400-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_COMPACT = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")


@dataclass(frozen=True, slots=True)
class CoverageGap:
    timestamp_us: int
    texts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HardSubCoverageReport:
    minimum_coverage: float
    subtitle_positive_timestamps: int
    covered_timestamps: int
    coverage_ratio: float
    continuity_gap_limit_us: int
    maximum_positive_gap_us: int
    timeline_continuity_passes: bool
    unmatched_samples: tuple[CoverageGap, ...]

    @property
    def passes(self) -> bool:
        return bool(
            self.subtitle_positive_timestamps
            and self.coverage_ratio >= self.minimum_coverage
            and self.timeline_continuity_passes
        )

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def cjk_count(value: str) -> int:
    return len(_CJK.findall(str(value or "")))


def compact_text(value: str) -> str:
    return _COMPACT.sub("", str(value or "")).casefold()


def text_similarity(left: str, right: str) -> float:
    left_value = compact_text(left)
    right_value = compact_text(right)
    if not left_value or not right_value:
        return 0.0
    matcher = SequenceMatcher(None, left_value, right_value)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    coverage = matched / max(1, min(len(left_value), len(right_value)))
    containment = float(
        min(len(left_value), len(right_value)) >= 3
        and (left_value in right_value or right_value in left_value)
    )
    return max(matcher.ratio(), coverage, containment)


def _same_visual_reading(left: str, right: str) -> bool:
    left_value = compact_text(left)
    right_value = compact_text(right)
    if not left_value or not right_value:
        return False
    if min(len(left_value), len(right_value)) >= 3 and (
        left_value in right_value or right_value in left_value
    ):
        return True
    return SequenceMatcher(None, left_value, right_value).ratio() >= 0.72


def _same_character_inventory(left: str, right: str) -> bool:
    """Detect the same multi-row subtitle returned in a different row order."""

    left_value = compact_text(left)
    right_value = compact_text(right)
    if min(cjk_count(left_value), cjk_count(right_value)) < 6:
        return False
    if abs(len(left_value) - len(right_value)) > 2:
        return False
    shared = sum((Counter(left_value) & Counter(right_value)).values())
    return shared / min(len(left_value), len(right_value)) >= 0.90


def _coverage_text(value: str, ignored_overlay_tokens: tuple[str, ...]) -> str:
    result = compact_text(value)
    for token in ignored_overlay_tokens:
        if token:
            result = result.replace(token, "")
    return result


def measure_hard_sub_coverage(
    evidence: Iterable[SubtitleEvidence],
    cues: Iterable[SubtitleCandidate | SubtitleCue],
    *,
    minimum_coverage: float = 0.95,
    ignored_overlay_tokens: Iterable[str] = (),
    video_duration_us: int | None = None,
) -> HardSubCoverageReport:
    """Measure finalized cue coverage against subtitle-positive sampled frames.

    Evidence from both subtitle bands at the same timestamp represents one
    sampled frame. A frame is covered only when an overlapping finalized cue
    agrees with at least one valid reading at that timestamp.
    """

    if (
        isinstance(minimum_coverage, bool)
        or not isinstance(minimum_coverage, (int, float))
        or not 0 < float(minimum_coverage) <= 1
    ):
        raise ValueError("OCR coverage threshold must be between zero and one")
    threshold = float(minimum_coverage)
    overlay_tokens = tuple(
        compact_text(token) for token in ignored_overlay_tokens if compact_text(token)
    )
    by_timestamp: dict[int, set[str]] = {}
    for item in evidence:
        if (
            item.confidence < 0.62
            or cjk_count(item.text) < 2
            or _probable_edge_overlay(item)
            or _probable_vertical_scene_text(item)
        ):
            continue
        text = _coverage_text(item.text, overlay_tokens)
        if cjk_count(text) < 2:
            continue
        by_timestamp.setdefault(item.timestamp_us, set()).add(text)

    ordered_cues = tuple(sorted(cues, key=lambda item: item.range.start_us))
    unmatched: list[CoverageGap] = []
    covered = 0
    for timestamp_us, texts in sorted(by_timestamp.items()):
        overlapping = tuple(
            cue
            for cue in ordered_cues
            if cue.range.start_us <= timestamp_us < cue.range.end_us
        )
        if any(
            text_similarity(cue.text, evidence_text) >= 0.72
            for cue in overlapping
            for evidence_text in texts
        ):
            covered += 1
            continue
        unmatched.append(CoverageGap(timestamp_us, tuple(sorted(texts))))
    total = len(by_timestamp)
    ratio = covered / total if total else 0.0
    continuity_gap_limit_us = 0
    maximum_positive_gap_us = 0
    timeline_continuity_passes = True
    if video_duration_us is not None:
        if type(video_duration_us) is not int or video_duration_us <= 0:
            raise ValueError("OCR coverage video duration must be a positive integer")
        continuity_gap_limit_us = max(120_000_000, int(video_duration_us * 0.02))
        timeline_points = [0, *sorted(by_timestamp), video_duration_us]
        maximum_positive_gap_us = max(
            (
                right - left
                for left, right in zip(
                    timeline_points, timeline_points[1:], strict=False
                )
            ),
            default=video_duration_us,
        )
        timeline_continuity_passes = bool(
            by_timestamp and maximum_positive_gap_us <= continuity_gap_limit_us
        )
    return HardSubCoverageReport(
        threshold,
        total,
        covered,
        ratio,
        continuity_gap_limit_us,
        maximum_positive_gap_us,
        timeline_continuity_passes,
        tuple(unmatched),
    )


def consensus_candidate(
    evidence: Iterable[SubtitleEvidence],
    visual_range: TimeRange,
    *,
    candidate_id: str,
    minimum_frame_confidence: float = 0.62,
    minimum_consensus_confidence: float = 0.82,
) -> SubtitleCandidate | None:
    valid = [
        item
        for item in evidence
        if item.confidence >= minimum_frame_confidence and cjk_count(item.text) >= 2
    ]
    best_cluster: list[SubtitleEvidence] = []
    for seed in valid:
        cluster = [item for item in valid if _same_visual_reading(seed.text, item.text)]
        cluster_frame_ids = {item.frame_id for item in cluster}
        best_frame_ids = {item.frame_id for item in best_cluster}
        if (len(cluster_frame_ids), sum(item.confidence for item in cluster)) > (
            len(best_frame_ids),
            sum(item.confidence for item in best_cluster),
        ):
            best_cluster = cluster
    frame_ids = {item.frame_id for item in best_cluster}
    if len(frame_ids) < 2:
        return None
    confidence = sum(item.confidence for item in best_cluster) / len(best_cluster)
    if confidence < minimum_consensus_confidence:
        return None
    winner = max(
        best_cluster,
        key=lambda item: (
            sum(text_similarity(item.text, other.text) for other in best_cluster),
            item.confidence,
            cjk_count(item.text),
        ),
    )
    return SubtitleCandidate(
        id=candidate_id,
        range=visual_range,
        text=winner.text.strip(),
        confidence=round(confidence, 4),
        evidence_ids=tuple(sorted(item.id for item in best_cluster)),
        source=f"{winner.provider_id}:{winner.band}",
    )


def _temporal_duplicate(left: SubtitleCandidate, right: SubtitleCandidate) -> bool:
    overlap = min(left.range.end_us, right.range.end_us) - max(
        left.range.start_us, right.range.start_us
    )
    if overlap <= 120_000:
        return False
    shorter = min(left.range.duration_us, right.range.duration_us)
    left_center = (left.range.start_us + left.range.end_us) // 2
    right_center = (right.range.start_us + right.range.end_us) // 2
    overlap_ratio = overlap / max(100_000, shorter)
    center_distance = abs(left_center - right_center)
    if overlap_ratio >= 0.48 and center_distance <= 1_000_000:
        return True
    return (
        text_similarity(left.text, right.text) >= 0.88
        and overlap_ratio >= 0.20
        and center_distance <= 3_000_000
    )


def _clean_contained_reading(
    upper: SubtitleCandidate, lower: SubtitleCandidate
) -> SubtitleCandidate | None:
    upper_text = compact_text(upper.text)
    lower_text = compact_text(lower.text)
    if (
        upper_text
        and not _LATIN.search(upper.text)
        and _LATIN.search(lower.text)
        and (
            upper_text in lower_text
            or (
                text_similarity(upper.text, lower.text) >= 0.72
                and cjk_count(upper.text) + 4 >= cjk_count(lower.text)
            )
        )
    ):
        return upper
    return None


def _probable_edge_overlay(item: SubtitleEvidence) -> bool:
    box = item.box
    return bool(
        item.band == "lower"
        and box is not None
        and box.x <= 0.08
        and box.width <= 0.34
        and cjk_count(item.text) <= 4
    )


def _probable_vertical_scene_text(item: SubtitleEvidence) -> bool:
    box = item.box
    return bool(
        box is not None
        and box.width <= 0.075
        and box.height >= 0.55
        and box.height / box.width >= 8.0
    )


def merge_band_candidates(
    upper: Iterable[SubtitleCandidate],
    lower: Iterable[SubtitleCandidate],
    *,
    id_factory: Callable[[], str],
) -> tuple[SubtitleCandidate, ...]:
    selected = list(sorted(upper, key=lambda item: (item.range.start_us, item.range.end_us)))
    for candidate in sorted(lower, key=lambda item: (item.range.start_us, item.range.end_us)):
        matching = [
            (index, item)
            for index, item in enumerate(selected)
            if _temporal_duplicate(item, candidate)
        ]
        if not matching:
            selected.append(candidate)
            continue
        index, current = max(
            matching,
            key=lambda item: text_similarity(item[1].text, candidate.text),
        )
        agreement = text_similarity(current.text, candidate.text)
        combined_range = TimeRange(
            min(current.range.start_us, candidate.range.start_us),
            max(current.range.end_us, candidate.range.end_us),
        )
        winner = _clean_contained_reading(current, candidate) or max(
            (current, candidate),
            key=lambda item: (cjk_count(item.text), item.confidence, len(item.evidence_ids)),
        )
        review_reasons = tuple(
            sorted(
                {
                    *current.review_reasons,
                    *candidate.review_reasons,
                    *(("band_conflict",) if agreement < 0.72 else ()),
                }
            )
        )
        selected[index] = SubtitleCandidate(
            id=id_factory(),
            range=combined_range,
            text=winner.text,
            confidence=max(current.confidence, candidate.confidence),
            evidence_ids=tuple(sorted({*current.evidence_ids, *candidate.evidence_ids})),
            source="multiband_agree" if agreement >= 0.72 else "multiband_conflict",
            review_reasons=review_reasons,
        )
    return tuple(sorted(selected, key=lambda item: (item.range.start_us, item.range.end_us)))


def targeted_rescue_windows(
    candidates: Iterable[SubtitleCandidate],
    *,
    duration_us: int,
    minimum_gap_us: int = 650_000,
) -> tuple[RescueWindow, ...]:
    ordered = sorted(candidates, key=lambda item: (item.range.start_us, item.range.end_us))
    raw: list[RescueWindow] = []
    cursor = 0
    for candidate in ordered:
        if candidate.range.start_us - cursor >= minimum_gap_us:
            raw.append(
                RescueWindow(TimeRange(cursor, candidate.range.start_us), ("coverage_gap",))
            )
        if candidate.confidence < 0.86 or candidate.review_reasons:
            reasons = set(candidate.review_reasons)
            if candidate.confidence < 0.86:
                reasons.add("low_confidence")
            raw.append(RescueWindow(candidate.range, tuple(sorted(reasons))))
        cursor = max(cursor, candidate.range.end_us)
    if duration_us - cursor >= minimum_gap_us:
        raw.append(RescueWindow(TimeRange(cursor, duration_us), ("coverage_gap",)))
    return tuple(raw)


def group_visual_evidence(
    evidence: Iterable[SubtitleEvidence],
    *,
    band: str,
    duration_us: int,
    sample_interval_us: int,
    provider_prefix: str | None = None,
) -> tuple[SubtitleCandidate, ...]:
    """Turn repeated sampled readings into stable visual subtitle states.

    Empty output at one sampled frame is deliberately not treated as evidence
    that the subtitle disappeared. A state is closed only by a conflicting
    reading or a gap larger than two sample intervals.
    """

    if duration_us <= 0 or sample_interval_us <= 0:
        raise ValueError("OCR grouping duration and interval must be positive")
    ordered = sorted(
        (
            item
            for item in evidence
            if item.band == band
            and (provider_prefix is None or item.provider_id.startswith(provider_prefix))
            and not _probable_edge_overlay(item)
            and not _probable_vertical_scene_text(item)
        ),
        key=lambda item: (item.timestamp_us, item.frame_id, item.id),
    )
    if not ordered:
        return ()

    clusters: list[list[SubtitleEvidence]] = []
    current: list[SubtitleEvidence] = []
    for item in ordered:
        if not current:
            current = [item]
            continue
        recent = current[-1]
        close_enough = item.timestamp_us - recent.timestamp_us <= sample_interval_us * 2
        agrees = any(_same_visual_reading(item.text, member.text) for member in current[-3:])
        if close_enough and agrees:
            current.append(item)
            continue
        clusters.append(current)
        current = [item]
    if current:
        clusters.append(current)

    candidates: list[SubtitleCandidate] = []
    half_interval = sample_interval_us // 2
    for cluster in clusters:
        start_us = max(0, cluster[0].timestamp_us - half_interval)
        end_us = min(duration_us, cluster[-1].timestamp_us + half_interval)
        if end_us <= start_us:
            continue
        digest = sha256("|".join(item.id for item in cluster).encode("utf-8")).hexdigest()[:20]
        candidate = consensus_candidate(
            cluster,
            TimeRange(start_us, end_us),
            candidate_id=f"candidate-{band}-{digest}",
        )
        if candidate is not None:
            winning_evidence = [
                item for item in cluster if item.id in candidate.evidence_ids
            ]
            winning_range = TimeRange(
                max(0, winning_evidence[0].timestamp_us - half_interval),
                min(duration_us, winning_evidence[-1].timestamp_us + half_interval),
            )
            candidates.append(replace(candidate, range=winning_range))
    return tuple(candidates)


def coalesce_rescue_windows(
    windows: Iterable[RescueWindow],
    *,
    join_gap_us: int = 200_000,
) -> tuple[RescueWindow, ...]:
    if join_gap_us < 0:
        raise ValueError("Rescue join gap cannot be negative")
    ordered = sorted(windows, key=lambda item: (item.range.start_us, item.range.end_us))
    result: list[RescueWindow] = []
    for window in ordered:
        if not result or window.range.start_us > result[-1].range.end_us + join_gap_us:
            result.append(window)
            continue
        previous = result[-1]
        result[-1] = RescueWindow(
            TimeRange(
                previous.range.start_us,
                max(previous.range.end_us, window.range.end_us),
            ),
            tuple(sorted({*previous.reasons, *window.reasons})),
        )
    return tuple(result)


def apply_quality_rescue(
    fast_candidates: Iterable[SubtitleCandidate],
    quality_candidates: Iterable[SubtitleCandidate],
    rescue_windows: Iterable[RescueWindow],
) -> tuple[SubtitleCandidate, ...]:
    """Apply Quality OCR only where Fast explicitly requested help.

    Quality evidence may add a missing cue or reinforce a matching cue. It may
    not silently replace a conflicting Fast reading; conflicts remain visible
    as REVIEW evidence.
    """

    selected = list(sorted(fast_candidates, key=lambda item: item.range.start_us))
    windows = tuple(rescue_windows)
    for quality in sorted(quality_candidates, key=lambda item: item.range.start_us):
        if not any(_ranges_overlap(quality.range, window.range) for window in windows):
            continue
        matches = [
            (
                index,
                current,
                text_similarity(current.text, quality.text),
                _same_character_inventory(current.text, quality.text),
            )
            for index, current in enumerate(selected)
            if _ranges_touch_or_overlap(current.range, quality.range)
        ]
        if not matches:
            selected.append(quality)
            continue
        agreeing = [item for item in matches if item[2] >= 0.72 or item[3]]
        if agreeing:
            index, current, agreement, layout_order_variant = max(
                agreeing,
                key=lambda item: (
                    item[3],
                    item[2],
                    _overlap_duration(item[1].range, quality.range),
                ),
            )
            winner = (
                quality
                if layout_order_variant and agreement < 0.72
                else max(
                    (current, quality),
                    key=lambda item: (
                        cjk_count(item.text),
                        item.confidence,
                        len(item.evidence_ids),
                    ),
                )
            )
            resolved_reasons = {"low_confidence"}
            if not quality.review_reasons:
                resolved_reasons.add("band_conflict")
            selected[index] = SubtitleCandidate(
                id=current.id,
                range=TimeRange(
                    min(current.range.start_us, quality.range.start_us),
                    max(current.range.end_us, quality.range.end_us),
                ),
                text=winner.text,
                confidence=max(current.confidence, quality.confidence),
                evidence_ids=tuple(sorted({*current.evidence_ids, *quality.evidence_ids})),
                source="paddle-quality-rescue",
                review_reasons=tuple(
                    reason
                    for reason in current.review_reasons
                    if reason not in resolved_reasons
                ),
            )
            continue
        conflicts = [item for item in matches if _temporal_duplicate(item[1], quality)]
        if not conflicts:
            selected.append(quality)
            continue
        index, current, _agreement, _layout_order_variant = max(
            conflicts,
            key=lambda item: _overlap_duration(item[1].range, quality.range),
        )
        selected[index] = SubtitleCandidate(
            id=current.id,
            range=current.range,
            text=current.text,
            confidence=current.confidence,
            evidence_ids=tuple(sorted({*current.evidence_ids, *quality.evidence_ids})),
            source=current.source,
            review_reasons=tuple(sorted({*current.review_reasons, "quality_conflict"})),
        )
    return tuple(sorted(selected, key=lambda item: (item.range.start_us, item.range.end_us)))


def reinforce_candidate_ranges(
    candidates: Iterable[SubtitleCandidate],
    evidence: Iterable[SubtitleEvidence],
    *,
    video_duration_us: int,
    evidence_half_interval_us: int,
    maximum_distance_us: int,
) -> tuple[SubtitleCandidate, ...]:
    """Extend an existing cue with agreeing sparse Quality evidence.

    Quality probes may contain a single valid frame at a cue boundary.  Such a
    frame cannot create a cue by itself because consensus requires two frames,
    but it can safely reinforce a nearby cue with the same reading.
    """

    if video_duration_us <= 0:
        raise ValueError("OCR reinforcement duration must be positive")
    if evidence_half_interval_us <= 0 or maximum_distance_us < 0:
        raise ValueError("OCR reinforcement intervals are invalid")
    selected = list(sorted(candidates, key=lambda item: item.range.start_us))
    if not selected:
        return ()
    ordered_evidence = sorted(evidence, key=lambda item: (item.timestamp_us, item.id))
    active: list[int] = []
    next_candidate = 0
    for item in ordered_evidence:
        if (
            item.confidence < 0.62
            or cjk_count(item.text) < 2
            or _probable_edge_overlay(item)
            or _probable_vertical_scene_text(item)
        ):
            continue
        upper_bound = item.timestamp_us + maximum_distance_us
        while (
            next_candidate < len(selected)
            and selected[next_candidate].range.start_us <= upper_bound
        ):
            active.append(next_candidate)
            next_candidate += 1
        lower_bound = item.timestamp_us - maximum_distance_us
        active = [
            index for index in active if selected[index].range.end_us >= lower_bound
        ]
        matches: list[tuple[float, int, float, int]] = []
        for index in active:
            candidate = selected[index]
            agreement = text_similarity(candidate.text, item.text)
            if agreement < 0.72:
                continue
            distance = (
                0
                if candidate.range.start_us
                <= item.timestamp_us
                < candidate.range.end_us
                else min(
                    abs(item.timestamp_us - candidate.range.start_us),
                    abs(item.timestamp_us - candidate.range.end_us),
                )
            )
            matches.append((agreement, -distance, candidate.confidence, index))
        if not matches:
            continue
        _agreement, _distance, _confidence, index = max(matches)
        candidate = selected[index]
        reinforced_range = TimeRange(
            max(
                0,
                min(
                    candidate.range.start_us,
                    item.timestamp_us - evidence_half_interval_us,
                ),
            ),
            min(
                video_duration_us,
                max(
                    candidate.range.end_us,
                    item.timestamp_us + evidence_half_interval_us,
                ),
            ),
        )
        selected[index] = replace(
            candidate,
            range=reinforced_range,
            evidence_ids=tuple(sorted({*candidate.evidence_ids, item.id})),
        )
    return tuple(sorted(selected, key=lambda item: (item.range.start_us, item.range.end_us)))


def _overlap_duration(left: TimeRange, right: TimeRange) -> int:
    return max(0, min(left.end_us, right.end_us) - max(left.start_us, right.start_us))


def _ranges_overlap(left: TimeRange, right: TimeRange) -> bool:
    return _overlap_duration(left, right) > 0


def _ranges_touch_or_overlap(left: TimeRange, right: TimeRange) -> bool:
    return left.start_us <= right.end_us and right.start_us <= left.end_us
