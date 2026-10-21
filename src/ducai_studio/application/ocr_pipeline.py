from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from hashlib import sha256
from heapq import heappop, heappush
from typing import Any

from ducai_studio.domain.project import (
    SCENE_TEXT_DEFAULT_BACKGROUND_COLOR,
    SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT,
    SCENE_TEXT_DEFAULT_BACKGROUND_STYLE,
    SCENE_TEXT_DEFAULT_FADE_DURATION_MS,
    SCENE_TEXT_DEFAULT_FADE_ENABLED,
    SCENE_TEXT_DEFAULT_FONT_SIZE,
    SCENE_TEXT_DEFAULT_STYLE_HINT,
    SCENE_TEXT_DEFAULT_TEXT_COLOR,
    SubtitleCue,
)
from ducai_studio.domain.subtitles import (
    NormalizedBox,
    RescueWindow,
    SceneTextProbe,
    SubtitleCandidate,
    SubtitleEvidence,
)
from ducai_studio.domain.timebase import TimeRange

_CJK = re.compile(r"[\u3400-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_COMPACT = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")
OCR_AGGREGATION_VERSION = "evidence-bound-mixed-occurrences-v5"


@dataclass(frozen=True, slots=True)
class SuppressedOcrReading:
    evidence_id: str
    winner_evidence_id: str
    frame_id: str
    timestamp_us: int
    source_scope: str
    reason: str = "same_frame_crop_cut"


@dataclass(frozen=True, slots=True)
class OcrEvidenceReconciliation:
    evidence: tuple[SubtitleEvidence, ...]
    suppressions: tuple[SuppressedOcrReading, ...]
    deferred_suppressions: tuple[SuppressedOcrReading, ...] = ()


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


def reconcile_dialogue_evidence(
    evidence: Iterable[SubtitleEvidence],
    crops: Mapping[str, NormalizedBox],
) -> OcrEvidenceReconciliation:
    """Ignore a clipped read only when complete same-frame glyphs prove it.

    A crop can cut strokes into confident but unrelated Chinese/Latin text.
    Text similarity cannot decide those cases. Require an uncut, corroborated
    reading crossing the clipped crop edge at the same source frame/location.
    Equal-length proofs below the complete-reading confidence floor are deferred
    until a whole candidate can be proved redundant. Keep their consensus votes:
    the uncut crop need not have the better transcription for the occurrence.
    Raw evidence is never rewritten and independent short/scene lines survive.
    """
    rows = tuple(evidence)
    frames: dict[tuple[str, str, int, str, str], list[SubtitleEvidence]] = defaultdict(list)
    support: dict[tuple[str, str, str, str, str], list[int]] = defaultdict(list)
    for item in rows:
        if item.band not in crops or item.evidence_kind == "scene" or item.box is None:
            continue
        frames[
            (item.source_scope, item.frame_id, item.timestamp_us, item.provider_id, item.model_id)
        ].append(item)
        if item.confidence >= 0.90:
            support[
                (
                    item.source_scope,
                    item.provider_id,
                    item.model_id,
                    item.band,
                    compact_text(item.text),
                )
            ].append(item.timestamp_us)
    support = {key: sorted(set(values)) for key, values in support.items()}
    suppressed: dict[str, SuppressedOcrReading] = {}
    for frame in frames.values():
        for partial in frame:
            crop = crops[partial.band]
            partial_characters = cjk_count(partial.text)
            possible = []
            for full in frame:
                full_characters = cjk_count(full.text)
                if (
                    full.band == partial.band
                    or full.confidence < 0.90
                    or full_characters < max(2, partial_characters)
                    or (full_characters == partial_characters and partial.confidence >= 0.90)
                    or not _crop_cut_counterpart(partial, full, crop)
                ):
                    continue
                timestamps = support[
                    (
                        full.source_scope,
                        full.provider_id,
                        full.model_id,
                        full.band,
                        compact_text(full.text),
                    )
                ]
                left = bisect_left(timestamps, full.timestamp_us - 800_000)
                right = bisect_right(timestamps, full.timestamp_us + 800_000)
                if right - left < 2:
                    continue
                own_crop = crops[full.band]
                box = full.box
                if box is None or not (
                    box.x > own_crop.x + 0.001
                    and box.y > own_crop.y + 0.001
                    and box.x + box.width < own_crop.x + own_crop.width - 0.001
                    and box.y + box.height < own_crop.y + own_crop.height - 0.001
                ):
                    continue
                possible.append(full)
            if possible:
                winner = max(possible, key=lambda item: (cjk_count(item.text), item.confidence))
                suppressed[partial.id] = SuppressedOcrReading(
                    partial.id,
                    winner.id,
                    partial.frame_id,
                    partial.timestamp_us,
                    partial.source_scope,
                )
    # A reading shown to be clipped cannot prove another reading is complete.
    # Keep ambiguous chains/cycles rather than lose their only available text.
    clipped_ids = set(suppressed)
    suppressed = {
        evidence_id: proof
        for evidence_id, proof in suppressed.items()
        if proof.winner_evidence_id not in clipped_ids
    }
    by_id = {item.id: item for item in rows}
    deferred = {
        evidence_id: proof
        for evidence_id, proof in suppressed.items()
        if cjk_count(by_id[evidence_id].text)
        == cjk_count(by_id[proof.winner_evidence_id].text)
    }
    immediate = {key: proof for key, proof in suppressed.items() if key not in deferred}
    return OcrEvidenceReconciliation(
        tuple(item for item in rows if item.id not in immediate),
        tuple(immediate.values()),
        tuple(deferred.values()),
    )


def _crop_cut_counterpart(
    partial: SubtitleEvidence,
    full: SubtitleEvidence,
    crop: NormalizedBox,
) -> bool:
    short_box, full_box = partial.box, full.box
    if short_box is None or full_box is None:
        return False
    intersection = max(
        0,
        min(short_box.x + short_box.width, full_box.x + full_box.width)
        - max(short_box.x, full_box.x),
    ) * max(
        0,
        min(short_box.y + short_box.height, full_box.y + full_box.height)
        - max(short_box.y, full_box.y),
    )
    if intersection < short_box.width * short_box.height * 0.80:
        return False
    if abs((short_box.y + short_box.height / 2) - (full_box.y + full_box.height / 2)) > (
        full_box.height * 0.60
    ):
        return False
    # Bounding polygons can stop a few pixels inside the crop after scaling.
    # Require the independent full box to extend past that very same edge.
    return any(
        (
            short_box.x - crop.x <= crop.width * 0.03 and full_box.x < crop.x - 0.003,
            crop.x + crop.width - short_box.x - short_box.width <= crop.width * 0.03
            and full_box.x + full_box.width > crop.x + crop.width + 0.003,
            short_box.y - crop.y <= crop.height * 0.03 and full_box.y < crop.y - 0.003,
            crop.y + crop.height - short_box.y - short_box.height <= crop.height * 0.03
            and full_box.y + full_box.height > crop.y + crop.height + 0.003,
        )
    )


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
    progress: Callable[[int, int], None] | None = None,
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
            item.evidence_kind == "scene"
            or item.band == "scene"
            or item.confidence < 0.62
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
    active: dict[int, SubtitleCandidate | SubtitleCue] = {}
    endings: list[tuple[int, int]] = []
    cursor = 0
    total = len(by_timestamp)
    for completed, (timestamp_us, texts) in enumerate(sorted(by_timestamp.items()), 1):
        while cursor < len(ordered_cues) and ordered_cues[cursor].range.start_us <= timestamp_us:
            cue = ordered_cues[cursor]
            active[cursor] = cue
            heappush(endings, (cue.range.end_us, cursor))
            cursor += 1
        while endings and endings[0][0] <= timestamp_us:
            _end, index = heappop(endings)
            active.pop(index, None)
        if progress and (completed == 1 or completed % 512 == 0 or completed == total):
            progress(completed, total)
        if any(
            text_similarity(cue.text, evidence_text) >= 0.72
            for cue in active.values()
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
                for left, right in zip(timeline_points, timeline_points[1:], strict=False)
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
        if item.confidence >= minimum_frame_confidence and cjk_count(item.text) >= 1
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
    shortest = min(cjk_count(item.text) for item in best_cluster)
    if shortest == 1:
        burst_cluster = [item for item in best_cluster if item.evidence_kind == "burst"]
        if len({item.frame_id for item in burst_cluster}) < 2 or not _short_burst_geometry_stable(
            burst_cluster
        ):
            return None
    confidence = sum(item.confidence for item in best_cluster) / len(best_cluster)
    required_confidence = max(
        minimum_consensus_confidence,
        0.90 if shortest == 1 else minimum_consensus_confidence,
    )
    if confidence < required_confidence:
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


def _short_burst_geometry_stable(items: list[SubtitleEvidence]) -> bool:
    boxes = [item.box for item in items if item.box is not None]
    if len(boxes) < 2:
        return False
    reference = boxes[0]
    for box in boxes[1:]:
        left = max(reference.x, box.x)
        top = max(reference.y, box.y)
        right = min(reference.x + reference.width, box.x + box.width)
        bottom = min(reference.y + reference.height, box.y + box.height)
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        minimum_area = min(
            reference.width * reference.height,
            box.width * box.height,
        )
        if intersection / max(1e-9, minimum_area) < 0.72:
            return False
    return True


def _subtitle_geometry_agrees(
    left: SubtitleEvidence,
    right: SubtitleEvidence,
) -> bool:
    if left.box is None or right.box is None:
        return False
    intersection_left = max(left.box.x, right.box.x)
    intersection_top = max(left.box.y, right.box.y)
    intersection_right = min(
        left.box.x + left.box.width,
        right.box.x + right.box.width,
    )
    intersection_bottom = min(
        left.box.y + left.box.height,
        right.box.y + right.box.height,
    )
    intersection = max(0.0, intersection_right - intersection_left) * max(
        0.0, intersection_bottom - intersection_top
    )
    smaller_area = min(
        left.box.width * left.box.height,
        right.box.width * right.box.height,
    )
    if intersection / max(1e-9, smaller_area) >= 0.55:
        return True
    left_center = (
        left.box.x + left.box.width / 2,
        left.box.y + left.box.height / 2,
    )
    right_center = (
        right.box.x + right.box.width / 2,
        right.box.y + right.box.height / 2,
    )
    return (
        abs(left_center[0] - right_center[0]) <= max(left.box.width, right.box.width) * 0.60
        and abs(left_center[1] - right_center[1]) <= max(left.box.height, right.box.height) * 0.80
    )


def cross_pass_consensus_candidates(
    evidence: Iterable[SubtitleEvidence],
    *,
    video_duration_us: int,
    evidence_half_interval_us: int,
    maximum_gap_us: int = 1_000_000,
) -> tuple[SubtitleCandidate, ...]:
    """Recover a short dialogue seen once by each independent OCR pass.

    Fast and Quality are intentionally grouped independently elsewhere.  A
    short-lived subtitle can therefore land on one timestamp in each pass and
    fail both two-frame gates.  This narrow bridge accepts only high-confidence
    multi-character readings corroborated by the other pass at a nearby time
    and the same full-frame geometry.
    """

    if video_duration_us <= 0 or evidence_half_interval_us <= 0 or maximum_gap_us <= 0:
        raise ValueError("Cross-pass OCR timing values must be positive")
    fast_providers = {"paddle-fast", "rapid-compatibility"}
    quality_providers = {"paddle-quality", "rapid-quality-fallback"}
    valid = sorted(
        (
            item
            for item in evidence
            if item.provider_id in fast_providers | quality_providers
            and item.band in {"upper", "lower"}
            and item.box is not None
            and item.confidence >= 0.90
            and cjk_count(item.text) >= 2
            and item.evidence_kind != "scene"
            and not _probable_edge_overlay(item)
            and not _probable_vertical_scene_text(item)
        ),
        key=lambda item: (item.timestamp_us, item.id),
    )
    used_pairs: set[tuple[str, str]] = set()
    candidates: list[SubtitleCandidate] = []
    quality_rows = tuple(item for item in valid if item.provider_id in quality_providers)
    quality_timestamps = tuple(item.timestamp_us for item in quality_rows)
    for fast in (item for item in valid if item.provider_id in fast_providers):
        window_start = bisect_left(
            quality_timestamps,
            fast.timestamp_us - maximum_gap_us,
        )
        window_end = bisect_right(
            quality_timestamps,
            fast.timestamp_us + maximum_gap_us,
        )
        matches = [
            quality
            for quality in quality_rows[window_start:window_end]
            if quality.frame_id != fast.frame_id
            and abs(quality.timestamp_us - fast.timestamp_us) <= maximum_gap_us
            and _same_visual_reading(fast.text, quality.text)
            and _subtitle_geometry_agrees(fast, quality)
        ]
        if not matches:
            continue
        quality = max(
            matches,
            key=lambda item: (
                text_similarity(fast.text, item.text),
                item.confidence,
                -abs(item.timestamp_us - fast.timestamp_us),
            ),
        )
        pair_key = tuple(sorted((fast.id, quality.id)))
        if pair_key in used_pairs:
            continue
        used_pairs.add(pair_key)
        rows = (fast, quality)
        winner = max(rows, key=lambda item: (cjk_count(item.text), item.confidence))
        start_us = max(
            0,
            min(item.timestamp_us for item in rows) - evidence_half_interval_us,
        )
        end_us = min(
            video_duration_us,
            max(item.timestamp_us for item in rows) + evidence_half_interval_us,
        )
        if end_us <= start_us:
            continue
        digest = sha256("|".join(pair_key).encode("utf-8")).hexdigest()[:20]
        candidates.append(
            SubtitleCandidate(
                id=f"cross-pass-{digest}",
                range=TimeRange(start_us, end_us),
                text=winner.text.strip(),
                confidence=round(sum(item.confidence for item in rows) / len(rows), 4),
                evidence_ids=pair_key,
                source="paddle-cross-pass-consensus",
            )
        )
    return tuple(sorted(candidates, key=lambda item: (item.range.start_us, item.range.end_us)))


def _scene_score(item: SubtitleEvidence) -> float:
    return float(item.scene_confidence) or (0.86 if item.orientation == "vertical" else 0.55)


def _scene_probe_covers(probe: SceneTextProbe, item: SubtitleEvidence) -> bool:
    if item.box is None or probe.source_scope != item.source_scope:
        return False
    p, b = probe.box, item.box
    area = max(0.0, min(p.x + p.width, b.x + b.width) - max(p.x, b.x)) * max(
        0.0, min(p.y + p.height, b.y + b.height) - max(p.y, b.y)
    )
    return area >= b.width * b.height * .8


def _scene_clusters(
    evidence: Iterable[SubtitleEvidence],
    dialogue: tuple[SubtitleCue, ...] = (),
    probes: tuple[SceneTextProbe, ...] = (),
    progress: Callable[[int, int], None] | None = None,
) -> list[list[SubtitleEvidence]]:
    scene = [item for item in evidence
             if item.evidence_kind == "scene" and item.band == "scene"
             and item.box is not None and item.confidence >= .50
             and cjk_count(item.text) >= 1 and _scene_score(item) >= .45
             and not _scene_platform_badge(item)
             and not (item.orientation == "horizontal" and _scene_score(item) < .65
                      and item.panel_box is None and item.box.width >= .35)]
    clusters: list[list[SubtitleEvidence]] = []
    cluster_times: list[list[int]] = []
    cluster_present: list[set[int]] = []
    active_clusters: dict[int, list[SubtitleEvidence]] = {}
    cluster_endings: list[tuple[int, int]] = []
    ordered_dialogue = sorted(dialogue, key=lambda cue: cue.range.start_us)
    dialogue_cursor = 0
    active_dialogue = {}
    dialogue_endings = []
    probe_groups: dict[str, list[SceneTextProbe]] = defaultdict(list)
    for probe in sorted(probes, key=lambda probe: probe.timestamp_us):
        probe_groups[probe.source_scope].append(probe)
    probe_times = {scope: [p.timestamp_us for p in rows] for scope, rows in probe_groups.items()}
    for completed, item in enumerate(sorted(scene, key=lambda row: (row.timestamp_us, row.id)), 1):
        if progress and (completed == 1 or completed % 128 == 0 or completed == len(scene)):
            progress(completed, len(scene))
        while (dialogue_cursor < len(ordered_dialogue)
               and ordered_dialogue[dialogue_cursor].range.start_us - 250_000 <= item.timestamp_us):
            cue = ordered_dialogue[dialogue_cursor]
            active_dialogue[dialogue_cursor] = cue
            heappush(dialogue_endings, (cue.range.end_us + 250_000, dialogue_cursor))
            dialogue_cursor += 1
        while dialogue_endings and dialogue_endings[0][0] < item.timestamp_us:
            _end, index = heappop(dialogue_endings)
            active_dialogue.pop(index, None)
        if _scene_duplicate_of_dialogue(item, active_dialogue.values()):
            continue
        while cluster_endings and cluster_endings[0][0] < item.timestamp_us:
            _end, index = heappop(cluster_endings)
            if clusters[index][-1].timestamp_us + 3_500_000 < item.timestamp_us:
                active_clusters.pop(index, None)
        match = None
        for index in reversed(active_clusters):
            cluster = active_clusters[index]
            last = cluster[-1]
            if item.source_scope != last.source_scope:
                continue
            if item.timestamp_us - last.timestamp_us > 3_500_000:
                continue
            first_recent = bisect_left(cluster_times[index], item.timestamp_us - 3_500_000)
            if not any(_scene_text_agrees(member, item) and _scene_geometry_agrees(member, item)
                       for member in cluster[first_recent:]):
                continue
            present = cluster_present[index]
            rows = probe_groups.get(item.source_scope, [])
            times = probe_times.get(item.source_scope, [])
            local_probes = rows[bisect_right(times, last.timestamp_us):
                               bisect_left(times, item.timestamp_us)]
            missing = {probe.timestamp_us for probe in local_probes
                       if last.timestamp_us < probe.timestamp_us < item.timestamp_us
                       and _scene_probe_covers(probe, last)
                       and probe.timestamp_us not in present}
            if len(missing) >= 2:
                continue
            match = index
            break
        if match is None:
            match = len(clusters)
            clusters.append([item])
            cluster_times.append([item.timestamp_us])
            cluster_present.append({item.timestamp_us})
            active_clusters[match] = clusters[match]
        else:
            clusters[match].append(item)
            cluster_times[match].append(item.timestamp_us)
            cluster_present[match].add(item.timestamp_us)
        heappush(cluster_endings, (item.timestamp_us + 3_500_000, match))
    return clusters


@dataclass(frozen=True, slots=True)
class SceneTextRescueRegion:
    range: TimeRange
    box: NormalizedBox
    source_scope: str
    target_box: NormalizedBox


def scene_text_rescue_regions(
    evidence: Iterable[SubtitleEvidence], *, video_duration_us: int,
) -> tuple[SceneTextRescueRegion, ...]:
    """Read plausible discovered labels locally, never rescan the whole movie."""
    result = []
    for cluster in _scene_clusters(evidence):
        if max(item.confidence for item in cluster) < .60:
            continue
        boxes = [item.box for item in cluster if item.box is not None]
        left = max(0.0, min(b.x for b in boxes) - .025)
        top = max(0.0, min(b.y for b in boxes) - .025)
        right = min(1.0, max(b.x + b.width for b in boxes) + .025)
        bottom = min(1.0, max(b.y + b.height for b in boxes) + .025)
        # Very persistent corner text is more likely an undetected watermark.
        if (_scene_corner_overlay(cluster[0])
                and cluster[-1].timestamp_us - cluster[0].timestamp_us > 8_000_000):
            continue
        result.append(SceneTextRescueRegion(
            TimeRange(max(0, cluster[0].timestamp_us - 800_000),
                      min(video_duration_us, cluster[-1].timestamp_us + 800_001)),
            NormalizedBox(left, top, right - left, bottom - top), cluster[0].source_scope,
            max(cluster, key=lambda item: (cjk_count(item.text), item.confidence)).box,
        ))
    return tuple(result)


def scene_text_cues_from_evidence(
    evidence: Iterable[SubtitleEvidence],
    *,
    video_duration_us: int,
    first_index: int,
    dialogue_cues: Iterable[SubtitleCue] = (),
    probes: Iterable[SceneTextProbe] = (),
    progress: Callable[[int, int], None] | None = None,
) -> tuple[SubtitleCue, ...]:
    """Build stable non-dialogue text cues from sparse full-frame evidence."""

    dialogue = tuple(cue for cue in dialogue_cues if cue.is_dialogue)
    observations = tuple(sorted(probes, key=lambda probe: probe.timestamp_us))
    observation_times = [probe.timestamp_us for probe in observations]
    clusters = _scene_clusters(evidence, dialogue, observations, progress)
    cues: list[SubtitleCue] = []
    for completed, cluster in enumerate(clusters, 1):
        if progress and (completed == 1 or completed % 128 == 0 or completed == len(clusters)):
            progress(completed, len(clusters))
        quality_frames = {item.frame_id for item in cluster
                          if "quality" in item.provider_id and item.confidence >= .68}
        if max(item.confidence for item in cluster) < 0.82 and len(quality_frames) < 2:
            continue
        confidence = sum(item.confidence for item in cluster) / len(cluster)
        visual_confidence = sum(_scene_score(item) for item in cluster) / len(cluster)
        first = cluster[0]
        last = cluster[-1]
        assert first.box is not None
        reliable = [item for item in cluster if item.confidence >= .82]
        provider_totals = Counter(item.provider_id for item in reliable)
        votes: dict[str, float] = defaultdict(float)
        for item in reliable:
            votes[compact_text(item.text)] += item.confidence / provider_totals[item.provider_id]
        winner = max(reliable or cluster, key=lambda item: (
            votes[compact_text(item.text)] * max(1, cjk_count(item.text)) ** .5,
            item.confidence, cjk_count(item.text),
        ))
        complete = [item for item in reliable
                    if compact_text(winner.text) in compact_text(item.text)
                    and cjk_count(item.text) > cjk_count(winner.text)
                    and len({other.frame_id for other in reliable
                             if compact_text(other.text) == compact_text(item.text)}) >= 2]
        if complete:
            winner = max(complete, key=lambda item: (cjk_count(item.text), item.confidence))
        assert winner.box is not None
        winner_has_extremely_strong_visual_evidence = bool(
            winner.confidence >= 0.97 and _scene_score(winner) >= 0.95
        )
        winner_is_safe_single_frame = bool(
            winner_has_extremely_strong_visual_evidence and not _scene_corner_overlay(winner)
        )
        frame_count = len({item.frame_id for item in cluster})
        if frame_count < 2 and not winner_is_safe_single_frame:
            continue
        timeline_span = last.timestamp_us - first.timestamp_us
        corner = (first.box.x <= 0.12 or first.box.x + first.box.width >= 0.88) and (
            first.box.y <= 0.16 or first.box.y + first.box.height >= 0.84
        )
        if (
            corner
            and len({item.frame_id for item in cluster}) >= 4
            and timeline_span >= max(5_000_000, int(video_duration_us * 0.12))
        ):
            continue
        panel = winner.panel_box or winner.box
        digest = sha256("|".join(sorted(item.id for item in cluster)).encode("utf-8")).hexdigest()[
            :20
        ]
        start_us = max(0, first.timestamp_us - 100_000)
        end_us = min(video_duration_us, max(start_us + 300_000, last.timestamp_us + 1_500_000))
        present = {item.timestamp_us for item in cluster}
        local_probes = observations[
            bisect_left(observation_times, first.timestamp_us - 1_000_000):
            bisect_right(observation_times, last.timestamp_us + 1_000_000)
        ]
        absent = [probe.timestamp_us for probe in local_probes
                  if _scene_probe_covers(probe, winner) and probe.timestamp_us not in present]
        before = [t for t in absent if first.timestamp_us - 1_000_000 <= t < first.timestamp_us]
        after = [t for t in absent if last.timestamp_us < t <= last.timestamp_us + 1_000_000]
        if before:
            start_us = (max(before) + first.timestamp_us) // 2
        if after:
            end_us = (last.timestamp_us + min(after)) // 2
        conflicting_quality = any(
            item.confidence >= .9
            and text_similarity(item.text, winner.text) < .88
            and compact_text(item.text) not in compact_text(winner.text)
            and compact_text(winner.text) not in compact_text(item.text)
            for item in cluster
        )
        compact_corner = (_scene_corner_overlay(winner) and winner.box.width <= .20
                          and winner.box.height <= .15)
        safe = not compact_corner and not conflicting_quality
        if cjk_count(winner.text) == 1:
            safe = safe and len(quality_frames) >= 2 and winner.box.height <= winner.box.width * 2
        cues.append(
            SubtitleCue(
                id=f"ocr-scene-{digest}",
                index=first_index + len(cues),
                range=TimeRange(start_us, end_us),
                text=winner.text.strip(),
                language="zh",
                source=f"{winner.provider_id}:scene",
                confidence=round(confidence, 4),
                evidence_ids=tuple(sorted(item.id for item in cluster)),
                visual_box=(
                    winner.box.x,
                    winner.box.y,
                    winner.box.width,
                    winner.box.height,
                ),
                visual_box_source="ocr",
                role="scene_text",
                orientation=winner.orientation,
                style_hint=SCENE_TEXT_DEFAULT_STYLE_HINT,
                text_font_size=SCENE_TEXT_DEFAULT_FONT_SIZE,
                panel_box=(panel.x, panel.y, panel.width, panel.height),
                background_color=SCENE_TEXT_DEFAULT_BACKGROUND_COLOR,
                text_color=SCENE_TEXT_DEFAULT_TEXT_COLOR,
                background_style=SCENE_TEXT_DEFAULT_BACKGROUND_STYLE,
                background_opacity_percent=(SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT),
                fade_enabled=SCENE_TEXT_DEFAULT_FADE_ENABLED,
                fade_duration_ms=SCENE_TEXT_DEFAULT_FADE_DURATION_MS,
                review_state=(
                    "accepted"
                    if safe and (winner_is_safe_single_frame
                    or (frame_count >= 2 and winner_has_extremely_strong_visual_evidence)
                    or (confidence >= 0.88 and visual_confidence >= 0.82))
                    else "review"
                ),
            )
        )
    return tuple(cues)


def _scene_duplicate_of_dialogue(
    item: SubtitleEvidence,
    dialogue_cues: tuple[SubtitleCue, ...],
) -> bool:
    """Discard full-frame OCR readings already owned by the S1 dialogue track.

    The sparse scene scanner sees the whole frame, including the ordinary
    hard-subtitle band.  Text and time alone are not enough because a name card
    may deliberately repeat a spoken name.  Requiring the OCR boxes to occupy
    the same region removes the duplicated S1 reading while preserving a real
    label elsewhere in the picture.
    """

    if item.box is None:
        return False
    item_box = (item.box.x, item.box.y, item.box.width, item.box.height)
    for cue in dialogue_cues:
        if not cue.range.start_us - 250_000 <= item.timestamp_us <= cue.range.end_us + 250_000:
            continue
        if text_similarity(item.text, cue.text) < 0.72:
            continue
        if cue.visual_box is not None and _same_visual_region(item_box, cue.visual_box):
            return True
    return False


def _same_visual_region(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    left_x, left_y, left_width, left_height = left
    right_x, right_y, right_width, right_height = right
    intersection_width = max(
        0.0,
        min(left_x + left_width, right_x + right_width) - max(left_x, right_x),
    )
    intersection_height = max(
        0.0,
        min(left_y + left_height, right_y + right_height) - max(left_y, right_y),
    )
    intersection = intersection_width * intersection_height
    smaller_area = min(left_width * left_height, right_width * right_height)
    return smaller_area > 0 and intersection / smaller_area >= 0.50


def _scene_text_agrees(left: SubtitleEvidence, right: SubtitleEvidence) -> bool:
    similarity = text_similarity(left.text, right.text)
    if similarity >= 0.88:
        return True
    if ("quality" in left.provider_id and "quality" in right.provider_id
            and min(left.confidence, right.confidence) >= .90
            and compact_text(left.text) not in compact_text(right.text)
            and compact_text(right.text) not in compact_text(left.text)):
        # Two clear names can share most characters but still be separate cards.
        return False
    return similarity >= 0.64 and abs(left.timestamp_us - right.timestamp_us) <= 2_000_000


def _scene_corner_overlay(item: SubtitleEvidence) -> bool:
    if item.box is None:
        return True
    horizontal_edge = item.box.x <= 0.08 or item.box.x + item.box.width >= 0.92
    vertical_edge = item.box.y <= 0.12 or item.box.y + item.box.height >= 0.88
    return horizontal_edge and vertical_edge


def _scene_platform_badge(item: SubtitleEvidence) -> bool:
    """Reject compact platform badges even when their anchor was missed.

    Paddle sometimes detects the Chinese ``独播`` companion in two frames but
    misses the adjacent Bilibili wordmark.  Persistence therefore cannot make
    that corner badge safe scene text.  Keep the rule deliberately narrow and
    geometry-bound so ordinary dialogue containing the same word is preserved.
    """

    value = compact_text(item.text)
    return bool(_scene_corner_overlay(item) and (
        value in {"独播", "全网独播", "平台独播"}
        or any(re.fullmatch(r"[bil]{5,12}", word, re.IGNORECASE)
               for word in re.findall(r"[A-Za-z]+", item.text))
    ))


def _scene_geometry_agrees(left: SubtitleEvidence, right: SubtitleEvidence) -> bool:
    if left.box is None or right.box is None:
        return False
    left_center = (
        left.box.x + left.box.width / 2,
        left.box.y + left.box.height / 2,
    )
    right_center = (
        right.box.x + right.box.width / 2,
        right.box.y + right.box.height / 2,
    )
    return (
        abs(left_center[0] - right_center[0]) <= max(left.box.width, right.box.width) * 0.65
        and abs(left_center[1] - right_center[1]) <= max(left.box.height, right.box.height) * 0.65
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
        # Sampling padding can overlap two consecutive, different subtitles.
        # Time alone establishes a conflict only for almost coextensive reads,
        # not a short caption touching the next long caption at its boundary.
        return text_similarity(left.text, right.text) >= 0.72 or overlap_ratio >= 0.85
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


def candidates_share_visual_frame(
    left: SubtitleCandidate,
    right: SubtitleCandidate,
    evidence_by_id: Mapping[str, SubtitleEvidence],
) -> bool:
    """Prove the readings occupy one source frame region, not just padded time."""
    right_frames: dict[tuple[str, str, int], list[SubtitleEvidence]] = defaultdict(list)
    for key in right.evidence_ids:
        if (row := evidence_by_id.get(key)) is not None:
            right_frames[(row.source_scope, row.frame_id, row.timestamp_us)].append(row)
    for key in left.evidence_ids:
        row = evidence_by_id.get(key)
        if row is None or row.box is None:
            continue
        for other in right_frames.get((row.source_scope, row.frame_id, row.timestamp_us), ()):
            if other.box is not None and _same_visual_region(
                (row.box.x, row.box.y, row.box.width, row.box.height),
                (other.box.x, other.box.y, other.box.width, other.box.height),
            ):
                return True
    return False


def merge_band_candidates(
    upper: Iterable[SubtitleCandidate],
    lower: Iterable[SubtitleCandidate],
    *,
    id_factory: Callable[[], str],
    evidence: Iterable[SubtitleEvidence] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[SubtitleCandidate, ...]:
    evidence_by_id = None if evidence is None else {item.id: item for item in evidence}

    def same_occurrence(left: SubtitleCandidate, right: SubtitleCandidate) -> bool:
        if evidence_by_id is None:
            return True  # Legacy callers have no source/frame geometry ledger.
        if not _same_visual_reading(left.text, right.text):
            return False
        return candidates_share_visual_frame(left, right, evidence_by_id)

    selected = list(sorted(upper, key=lambda item: (item.range.start_us, item.range.end_us)))
    ordered_lower = sorted(lower, key=lambda item: (item.range.start_us, item.range.end_us))
    for processed, candidate in enumerate(ordered_lower):
        if progress is not None and processed % 32 == 0:
            progress(processed, len(ordered_lower))
        matching = [
            (index, item)
            for index, item in enumerate(selected)
            if _temporal_duplicate(item, candidate) and same_occurrence(item, candidate)
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
        # A full read can have several temporally overlapping upper fragments.
        # Consume every contained fragment, not only the best initial match.
        full = selected[index]
        consumed = []
        for other_index, fragment in matching:
            if other_index == index:
                continue
            short_text, full_text = compact_text(fragment.text), compact_text(full.text)
            overlap = _overlap_duration(fragment.range, full.range)
            if (
                cjk_count(short_text) >= 3
                and len(short_text) < len(full_text)
                and short_text in full_text
                and overlap >= fragment.range.duration_us * 0.65
            ):
                full = replace(
                    full,
                    range=TimeRange(
                        min(full.range.start_us, fragment.range.start_us),
                        max(full.range.end_us, fragment.range.end_us),
                    ),
                    evidence_ids=tuple(sorted({*full.evidence_ids, *fragment.evidence_ids})),
                    review_reasons=tuple(sorted({*full.review_reasons, *fragment.review_reasons})),
                )
                consumed.append(other_index)
        selected[index] = full
        for other_index in reversed(sorted(consumed)):
            selected.pop(other_index)
    if progress is not None:
        progress(len(ordered_lower), len(ordered_lower))
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
            raw.append(RescueWindow(TimeRange(cursor, candidate.range.start_us), ("coverage_gap",)))
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
            winning_evidence = [item for item in cluster if item.id in candidate.evidence_ids]
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
                    reason for reason in current.review_reasons if reason not in resolved_reasons
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
    but it can safely reinforce a nearby cue with the same dialogue reading.
    Scene labels remain independent even when their names occur in dialogue.
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
            item.evidence_kind == "scene"
            or item.confidence < 0.62
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
        active = [index for index in active if selected[index].range.end_us >= lower_bound]
        matches: list[tuple[float, int, float, int]] = []
        for index in active:
            candidate = selected[index]
            agreement = text_similarity(candidate.text, item.text)
            if agreement < 0.72:
                continue
            distance = (
                0
                if candidate.range.start_us <= item.timestamp_us < candidate.range.end_us
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
