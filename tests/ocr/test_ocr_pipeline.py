from __future__ import annotations

from dataclasses import replace
from itertools import count
from pathlib import Path

import pytest

from ducai_studio.application import ocr_pipeline as ocr_pipeline_module
from ducai_studio.application.ocr_pipeline import (
    apply_quality_rescue,
    coalesce_rescue_windows,
    consensus_candidate,
    group_visual_evidence,
    measure_hard_sub_coverage,
    merge_band_candidates,
    reinforce_candidate_ranges,
    scene_text_cues_from_evidence,
    targeted_rescue_windows,
)
from ducai_studio.application.subtitle_finalizer import (
    FinalizationStatus,
    finalize_ocr_candidates,
    publish_final_srt,
)
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.subtitles import (
    NormalizedBox,
    RescueWindow,
    SubtitleCandidate,
    SubtitleEvidence,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.providers.ocr import DEFAULT_SUBTITLE_BANDS, OcrMode, OcrRequest
from ducai_studio.workers.ocr_worker import _sampling_intervals


def _evidence(
    identity: str,
    frame_id: str,
    text: str,
    confidence: float,
    *,
    band: str = "upper",
    box: NormalizedBox | None = None,
) -> SubtitleEvidence:
    return SubtitleEvidence(
        identity,
        frame_id,
        int(frame_id.removeprefix("frame-")) * 800_000,
        band,
        text,
        confidence,
        "paddle-fast",
        "PP-OCRv6-small",
        box,
    )


def _candidate(
    identity: str,
    start_us: int,
    end_us: int,
    text: str,
    *,
    confidence: float = 0.95,
    review: tuple[str, ...] = (),
) -> SubtitleCandidate:
    return SubtitleCandidate(
        identity,
        TimeRange(start_us, end_us),
        text,
        confidence,
        (f"evidence-{identity}",),
        "paddle-fast:upper",
        review,
    )


def test_ocr_request_preserves_two_independent_bands_and_strict_interval(
    tmp_path: Path,
) -> None:
    media = tmp_path / "phim.mp4"
    media.write_bytes(b"fixture")

    request = OcrRequest(media, 10_000_000, mode=OcrMode.FAST)

    assert [band.name for band in request.bands] == ["upper", "lower"]
    assert request.bands == DEFAULT_SUBTITLE_BANDS
    with pytest.raises(ValueError, match="sample interval"):
        OcrRequest(media, 10_000_000, sample_interval_us=100_000)


def test_consensus_requires_two_distinct_frames_and_keeps_longer_reading() -> None:
    visual_range = TimeRange(0, 1_600_000)
    evidence = (
        _evidence("a", "frame-1", "必然会有一大堆人来求药买药", 0.91),
        _evidence("b", "frame-2", "必然会有一大堆人来求药买药", 0.94),
    )

    candidate = consensus_candidate(evidence, visual_range, candidate_id="cue")

    assert candidate is not None
    assert candidate.text == "必然会有一大堆人来求药买药"
    assert len(candidate.evidence_ids) == 2
    assert consensus_candidate(evidence[:1], visual_range, candidate_id="single") is None


def test_one_character_dialogue_requires_two_stable_high_confidence_burst_frames() -> None:
    visual_range = TimeRange(1_000_000, 1_500_000)
    box = NormalizedBox(0.46, 0.82, 0.08, 0.09)
    confirmed = (
        SubtitleEvidence(
            "short-a",
            "frame-1000000",
            1_000_000,
            "upper",
            "走",
            0.96,
            "paddle-fast",
            "PP-OCRv6-small",
            box,
            "burst",
        ),
        SubtitleEvidence(
            "short-b",
            "frame-1120000",
            1_120_000,
            "upper",
            "走",
            0.95,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.462, 0.821, 0.079, 0.089),
            "burst",
        ),
    )

    assert consensus_candidate(confirmed, visual_range, candidate_id="short") is not None
    assert (
        consensus_candidate(confirmed[:1], visual_range, candidate_id="short-noise")
        is None
    )
    unstable = (confirmed[0], replace(confirmed[1], box=NormalizedBox(0.12, 0.82, 0.08, 0.09)))
    assert consensus_candidate(unstable, visual_range, candidate_id="short-shift") is None


def test_cross_pass_consensus_recovers_reading_seen_once_by_fast_and_quality() -> None:
    evidence = (
        SubtitleEvidence(
            "fast-short-dialogue",
            "frame-58800000",
            58_800_000,
            "upper",
            "好吧",
            0.997509,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.46, 0.89, 0.08, 0.06),
        ),
        SubtitleEvidence(
            "quality-short-dialogue",
            "frame-58600000",
            58_600_000,
            "lower",
            "好吧",
            0.999855,
            "paddle-quality",
            "PP-OCRv6-medium",
            NormalizedBox(0.462, 0.895, 0.079, 0.058),
        ),
    )

    candidates = ocr_pipeline_module.cross_pass_consensus_candidates(
        evidence,
        video_duration_us=120_000_000,
        evidence_half_interval_us=400_000,
    )

    assert len(candidates) == 1
    assert candidates[0].text == "好吧"
    assert candidates[0].source == "paddle-cross-pass-consensus"
    assert candidates[0].evidence_ids == (
        "fast-short-dialogue",
        "quality-short-dialogue",
    )
    assert not ocr_pipeline_module.cross_pass_consensus_candidates(
        (evidence[0], replace(evidence[1], provider_id="paddle-fast")),
        video_duration_us=120_000_000,
        evidence_half_interval_us=400_000,
    )
    assert not ocr_pipeline_module.cross_pass_consensus_candidates(
        (
            evidence[0],
            replace(
                evidence[1],
                box=NormalizedBox(0.10, 0.40, 0.079, 0.058),
            ),
        ),
        video_duration_us=120_000_000,
        evidence_half_interval_us=400_000,
    )


def test_scene_text_classifier_keeps_transient_vertical_title_and_rejects_persistent_corner(
) -> None:
    panel = NormalizedBox(0.73, 0.18, 0.18, 0.43)
    title_box = NormalizedBox(0.78, 0.24, 0.08, 0.31)
    title = tuple(
        SubtitleEvidence(
            f"title-{index}",
            f"scene-frame-{index}",
            timestamp,
            "scene",
            "海王兽",
            0.96,
            "paddle-fast",
            "PP-OCRv6-small",
            title_box,
            "scene",
            "vertical",
            "calligraphy",
            panel,
            "#111111",
        )
        for index, timestamp in enumerate((1_000_000, 1_240_000), start=1)
    )
    watermark_box = NormalizedBox(0.88, 0.03, 0.10, 0.05)
    watermark = tuple(
        SubtitleEvidence(
            f"wm-{index}",
            f"wm-frame-{index}",
            timestamp,
            "scene",
            "平台独播",
            0.97,
            "paddle-fast",
            "PP-OCRv6-small",
            watermark_box,
            "scene",
            "horizontal",
            "display",
        )
        for index, timestamp in enumerate((0, 10_000_000, 20_000_000, 30_000_000), start=1)
    )

    cues = scene_text_cues_from_evidence(
        (*title, *watermark),
        video_duration_us=60_000_000,
        first_index=11,
    )

    assert len(cues) == 1
    assert cues[0].text == "海王兽"
    assert cues[0].index == 11
    assert cues[0].role == "scene_text"
    assert cues[0].orientation == "vertical"
    assert cues[0].style_hint == "calligraphy_brush"
    assert cues[0].background_style == "ink_splash"
    assert cues[0].text_color == "#FFFFFF"
    assert cues[0].background_color == "#000000"
    assert cues[0].background_opacity_percent == 100


def test_scene_text_rejects_two_frame_corner_platform_badge_without_anchor() -> None:
    badge = tuple(
        SubtitleEvidence(
            f"badge-{index}",
            f"badge-frame-{index}",
            timestamp,
            "scene",
            "独播",
            0.99,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.94, 0.04, 0.03, 0.07),
            "scene",
            "vertical",
            "display",
            scene_confidence=0.99,
        )
        for index, timestamp in enumerate((1_000_000, 1_240_000), start=1)
    )

    assert not scene_text_cues_from_evidence(
        badge,
        video_duration_us=5_000_000,
        first_index=1,
    )


def test_scene_text_clusters_weak_variants_around_one_strong_reading() -> None:
    box = NormalizedBox(0.022, 0.019, 0.135, 0.478)
    variants = tuple(
        SubtitleEvidence(
            f"scene-variant-{index}",
            f"scene-frame-{index}",
            timestamp,
            "scene",
            text,
            confidence,
            "paddle-fast",
            "PP-OCRv6-small",
            box,
            "scene",
            "horizontal",
            "display",
            NormalizedBox(0.0, 0.0, 0.252, 0.574),
            "#101010",
            scene_confidence,
        )
        for index, (timestamp, text, confidence, scene_confidence) in enumerate(
            (
                (7_600_000, "宝星海", 0.611878, 0.82),
                (7_840_000, "北乱星海", 0.813903, 0.90),
                (8_800_000, "北乱星海", 0.590797, 0.90),
                (9_040_000, "乱星海", 0.996099, 0.98),
            ),
            start=1,
        )
    )

    cues = scene_text_cues_from_evidence(
        variants,
        video_duration_us=20_000_000,
        first_index=1,
    )

    assert len(cues) == 1
    assert cues[0].text == "乱星海"
    assert cues[0].review_state == "accepted"
    assert len(cues[0].evidence_ids) == 4


def test_scene_text_accepts_one_extremely_strong_non_watermark_card() -> None:
    card = SubtitleEvidence(
        "scene-strong-single",
        "scene-frame-118240000",
        118_240_000,
        "scene",
        "海王兽",
        0.995393,
        "paddle-fast",
        "PP-OCRv6-small",
        NormalizedBox(0.77, 0.23, 0.07, 0.28),
        "scene",
        "vertical",
        "calligraphy",
        NormalizedBox(0.72, 0.18, 0.17, 0.38),
        "#111111",
        1.0,
    )

    cues = scene_text_cues_from_evidence(
        (card,),
        video_duration_us=180_000_000,
        first_index=1,
    )

    assert len(cues) == 1
    assert cues[0].text == "海王兽"
    assert cues[0].review_state == "accepted"


def test_scene_text_does_not_promote_a_cluster_made_only_of_weak_readings() -> None:
    weak = tuple(
        SubtitleEvidence(
            f"scene-weak-{index}",
            f"scene-weak-frame-{index}",
            timestamp,
            "scene",
            "疑似文字",
            confidence,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.72, 0.20, 0.08, 0.28),
            "scene",
            "vertical",
            "calligraphy",
            scene_confidence=0.98,
        )
        for index, (timestamp, confidence) in enumerate(
            ((1_000_000, 0.58), (1_240_000, 0.61)),
            start=1,
        )
    )

    assert not scene_text_cues_from_evidence(
        weak,
        video_duration_us=5_000_000,
        first_index=1,
    )


def test_scene_text_classifier_rejects_ambiguous_middle_dialogue_without_panel() -> None:
    dialogue = tuple(
        SubtitleEvidence(
            f"dialogue-{index}",
            f"scene-dialogue-frame-{index}",
            timestamp,
            "scene",
            "你在做什么",
            0.97,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.28, 0.36, 0.44, 0.10),
            "scene",
            "horizontal",
            "display",
            scene_confidence=0.58,
        )
        for index, timestamp in enumerate((1_000_000, 1_240_000), start=1)
    )

    assert not scene_text_cues_from_evidence(
        dialogue,
        video_duration_us=5_000_000,
        first_index=1,
    )


def test_scene_text_classifier_removes_full_frame_duplicate_of_dialogue() -> None:
    dialogue_box = NormalizedBox(0.44, 0.89, 0.12, 0.06)
    duplicate = tuple(
        SubtitleEvidence(
            f"scene-dialogue-{index}",
            f"scene-dialogue-frame-{index}",
            timestamp,
            "scene",
            "海王兽",
            0.98,
            "paddle-fast",
            "PP-OCRv6-small",
            dialogue_box,
            "scene",
            "horizontal",
            "display",
            scene_confidence=0.98,
        )
        for index, timestamp in enumerate((1_000_000, 1_240_000), start=1)
    )
    real_title = tuple(
        SubtitleEvidence(
            f"scene-title-{index}",
            f"scene-title-frame-{index}",
            timestamp,
            "scene",
            "海王兽",
            0.98,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.78, 0.20, 0.07, 0.28),
            "scene",
            "vertical",
            "calligraphy",
            scene_confidence=0.98,
        )
        for index, timestamp in enumerate((1_000_000, 1_240_000), start=1)
    )
    dialogue = SubtitleCue(
        id="dialogue-1",
        index=1,
        range=TimeRange(800_000, 1_600_000),
        text="海王兽",
        language="zh",
        visual_box=(0.439, 0.891, 0.121, 0.061),
        visual_box_source="ocr",
    )

    cues = scene_text_cues_from_evidence(
        (*duplicate, *real_title),
        video_duration_us=5_000_000,
        first_index=2,
        dialogue_cues=(dialogue,),
    )

    assert [cue.text for cue in cues] == ["海王兽"]
    assert cues[0].visual_box == (0.78, 0.20, 0.07, 0.28)


def test_horizontal_scene_panel_stays_review_only_until_user_accepts_it() -> None:
    title = tuple(
        SubtitleEvidence(
            f"horizontal-title-{index}",
            f"horizontal-title-frame-{index}",
            timestamp,
            "scene",
            "玄天宗",
            0.96,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.38, 0.22, 0.24, 0.09),
            "scene",
            "horizontal",
            "display",
            NormalizedBox(0.35, 0.18, 0.30, 0.17),
            "#101010",
            0.78,
        )
        for index, timestamp in enumerate((1_000_000, 1_240_000), start=1)
    )

    cues = scene_text_cues_from_evidence(
        title,
        video_duration_us=5_000_000,
        first_index=1,
    )

    assert len(cues) == 1
    assert cues[0].role == "scene_text"
    assert cues[0].review_state == "review"


def test_multiband_union_keeps_unobserved_cues_and_marks_conflicts_for_review() -> None:
    ids = (f"merged-{value}" for value in count(1))
    upper_only = _candidate("upper-only", 0, 1_000_000, "上排台词")
    upper_conflict = _candidate("upper", 2_000_000, 3_000_000, "你必须留下")
    lower_conflict = _candidate("lower", 2_050_000, 3_100_000, "他们立刻离开")
    lower_only = _candidate("lower-only", 4_000_000, 5_000_000, "下排台词")

    merged = merge_band_candidates(
        (upper_only, upper_conflict),
        (lower_conflict, lower_only),
        id_factory=lambda: next(ids),
    )

    assert [item.text for item in merged if not item.review_reasons] == [
        "上排台词",
        "下排台词",
    ]
    conflict = next(item for item in merged if item.review_reasons)
    assert conflict.review_reasons == ("band_conflict",)


def test_multiband_prefers_clean_upper_text_over_latin_watermark_suffix() -> None:
    upper = _candidate("upper", 0, 1_000_000, "再拒绝就生分了")
    lower = _candidate("lower", 0, 1_000_000, "再拒绝就生分了修复2802bilibili")

    merged = merge_band_candidates((upper,), (lower,), id_factory=lambda: "merged")

    assert merged[0].text == "再拒绝就生分了"
    assert merged[0].review_reasons == ()


def test_multiband_prefers_clean_upper_when_watermarked_band_drops_one_character() -> None:
    upper = _candidate("upper", 0, 1_000_000, "是我刚刚在青云坊精挑细选的小")
    lower = _candidate(
        "lower",
        0,
        1_000_000,
        "是我刚刚在青云坊精挑细选的bilibili修复2802",
    )

    merged = merge_band_candidates((upper,), (lower,), id_factory=lambda: "merged")

    assert merged[0].text == "是我刚刚在青云坊精挑细选的小"
    assert merged[0].review_reasons == ()


def test_multiband_merges_same_reading_with_cross_band_timing_skew() -> None:
    upper = _candidate("upper", 0, 4_400_000, "冰锥攻击速度极快")
    lower = _candidate("lower", 0, 2_000_000, "冰锥攻击速度极快")

    merged = merge_band_candidates((upper,), (lower,), id_factory=lambda: "merged")

    assert len(merged) == 1
    assert merged[0].text == "冰锥攻击速度极快"
    assert merged[0].range == TimeRange(0, 4_400_000)


def test_fast_and_quality_grouping_use_the_intervals_they_actually_scan() -> None:
    assert _sampling_intervals(800_000) == (400_000, 800_000)
    assert _sampling_intervals(1_500_000) == (750_000, 1_500_000)


def test_lower_band_discards_small_left_edge_overlay_without_density_filtering() -> None:
    evidence = (
        _evidence(
            "overlay-1",
            "frame-1",
            "修复2802bilibili",
            0.95,
            band="lower",
            box=NormalizedBox(0.0, 0.1, 0.22, 0.8),
        ),
        _evidence(
            "overlay-2",
            "frame-2",
            "修复2802bilibili",
            0.96,
            band="lower",
            box=NormalizedBox(0.0, 0.1, 0.22, 0.8),
        ),
        _evidence(
            "dialogue-1",
            "frame-3",
            "真正的左侧长台词不会被删掉",
            0.96,
            band="lower",
            box=NormalizedBox(0.0, 0.1, 0.65, 0.8),
        ),
        _evidence(
            "dialogue-2",
            "frame-4",
            "真正的左侧长台词不会被删掉",
            0.97,
            band="lower",
            box=NormalizedBox(0.0, 0.1, 0.65, 0.8),
        ),
    )

    candidates = group_visual_evidence(
        evidence,
        band="lower",
        duration_us=5_000_000,
        sample_interval_us=800_000,
    )

    assert [item.text for item in candidates] == ["真正的左侧长台词不会被删掉"]


def test_visual_grouping_discards_narrow_vertical_scene_text_in_both_bands() -> None:
    evidence = (
        _evidence(
            "vertical-upper-1",
            "frame-1",
            "元然石",
            0.99,
            box=NormalizedBox(0.27, 0.35, 0.04, 0.65),
        ),
        _evidence(
            "vertical-upper-2",
            "frame-2",
            "元然石",
            0.99,
            box=NormalizedBox(0.27, 0.35, 0.04, 0.65),
        ),
        _evidence(
            "dialogue-1",
            "frame-3",
            "哎哎",
            0.99,
            box=NormalizedBox(0.45, 0.0, 0.10, 0.73),
        ),
        _evidence(
            "dialogue-2",
            "frame-4",
            "哎哎",
            0.99,
            box=NormalizedBox(0.45, 0.0, 0.10, 0.73),
        ),
    )

    candidates = group_visual_evidence(
        evidence,
        band="upper",
        duration_us=5_000_000,
        sample_interval_us=800_000,
    )

    assert [item.text for item in candidates] == ["哎哎"]


def test_visual_grouping_separates_consecutive_cues_with_a_shared_prefix() -> None:
    evidence = (
        _evidence("first-1", "frame-1", "如果效果好我就接着学", 0.99),
        _evidence("first-2", "frame-2", "如果效果好我就接着学", 0.99),
        _evidence("second-1", "frame-3", "如果效果差", 0.99),
        _evidence("second-2", "frame-4", "如果效果差", 0.99),
    )

    candidates = group_visual_evidence(
        evidence,
        band="upper",
        duration_us=5_000_000,
        sample_interval_us=800_000,
    )

    assert [item.text for item in candidates] == [
        "如果效果好我就接着学",
        "如果效果差",
    ]
    assert candidates[0].range.end_us <= candidates[1].range.start_us


def test_targeted_rescue_uses_gaps_low_confidence_and_conflicts_only() -> None:
    candidates = (
        _candidate("one", 1_000_000, 2_000_000, "第一句"),
        _candidate("two", 3_000_000, 4_000_000, "第二句", confidence=0.80),
        _candidate("three", 4_100_000, 5_000_000, "第三句", review=("band_conflict",)),
    )

    windows = targeted_rescue_windows(candidates, duration_us=6_000_000)

    assert [window.reasons for window in windows] == [
        ("coverage_gap",),
        ("coverage_gap",),
        ("low_confidence",),
        ("band_conflict",),
        ("coverage_gap",),
    ]


def test_visual_grouping_survives_one_missing_frame_but_requires_consensus() -> None:
    evidence = (
        _evidence("a", "frame-1", "我要修炼成仙", 0.95),
        _evidence("b", "frame-3", "我要修炼成仙", 0.93),
        _evidence("c", "frame-4", "下一句话", 0.97),
    )

    candidates = group_visual_evidence(
        evidence,
        band="upper",
        duration_us=5_000_000,
        sample_interval_us=800_000,
    )

    assert [item.text for item in candidates] == ["我要修炼成仙"]
    assert candidates[0].range == TimeRange(400_000, 2_800_000)


def test_quality_rescue_reinforces_matches_and_exposes_conflicts() -> None:
    fast = (
        _candidate("fast-one", 0, 1_000_000, "我要修炼", confidence=0.80),
        _candidate(
            "fast-two",
            2_000_000,
            3_000_000,
            "必须留下",
            review=("band_conflict",),
        ),
    )
    quality = (
        _candidate("quality-one", 0, 1_100_000, "我要修炼成仙"),
        _candidate("quality-two", 2_000_000, 3_000_000, "立刻离开"),
        _candidate("outside", 5_000_000, 6_000_000, "不应加入"),
    )
    windows = coalesce_rescue_windows(
        targeted_rescue_windows(fast, duration_us=4_000_000)
    )

    rescued = apply_quality_rescue(fast, quality, windows)

    assert rescued[0].text == "我要修炼成仙"
    assert rescued[0].source == "paddle-quality-rescue"
    assert rescued[1].text == "必须留下"
    assert rescued[1].review_reasons == ("band_conflict", "quality_conflict")
    assert all(item.text != "不应加入" for item in rescued)


def test_clean_quality_consensus_clears_a_stale_fast_band_conflict() -> None:
    fast = (
        _candidate(
            "fast",
            0,
            2_000_000,
            "自己怎么就遇不到这样一位舔狗呢",
            review=("band_conflict",),
        ),
    )
    quality = (
        _candidate(
            "quality",
            0,
            2_000_000,
            "自己怎么就遇不到这样一位舔狗呢",
        ),
    )
    windows = (RescueWindow(TimeRange(0, 2_000_000), ("band_conflict",)),)

    rescued = apply_quality_rescue(fast, quality, windows)

    assert rescued[0].review_reasons == ()
    assert rescued[0].source == "paddle-quality-rescue"


def test_quality_rescue_matches_the_agreeing_adjacent_cue_before_overlap() -> None:
    fast = (
        _candidate("first", 0, 1_600_000, "师弟你没事"),
        _candidate("second", 2_000_000, 3_600_000, "真是太好了"),
    )
    quality = (_candidate("quality", 1_200_000, 2_800_000, "真是太好了"),)
    windows = (RescueWindow(TimeRange(1_000_000, 3_000_000), ("coverage_gap",)),)

    rescued = apply_quality_rescue(fast, quality, windows)

    assert [item.text for item in rescued] == ["师弟你没事", "真是太好了"]
    assert all(not item.review_reasons for item in rescued)
    assert rescued[1].range == TimeRange(1_200_000, 3_600_000)


def test_quality_rescue_prefers_quality_for_multirow_order_variant() -> None:
    fast = (_candidate("fast", 2_000_000, 3_000_000, "旦破功筑基你修炼的功法"),)
    quality = (_candidate("quality", 400_000, 2_000_000, "你修炼的功法一旦破功筑基"),)
    windows = (RescueWindow(TimeRange(0, 2_400_000), ("coverage_gap",)),)

    rescued = apply_quality_rescue(fast, quality, windows)

    assert len(rescued) == 1
    assert rescued[0].text == "你修炼的功法一旦破功筑基"
    assert rescued[0].review_reasons == ()
    assert rescued[0].source == "paddle-quality-rescue"


def test_sparse_quality_evidence_reinforces_matching_cue_boundary() -> None:
    candidate = _candidate("cue", 0, 1_000_000, "但是我绝对不会")
    quality = SubtitleEvidence(
        "quality-boundary",
        "frame-quality",
        1_000_000,
        "upper",
        "但是我绝对不会",
        0.99,
        "paddle-quality",
        "PP-OCRv6-medium",
    )

    reinforced = reinforce_candidate_ranges(
        (candidate,),
        (quality,),
        video_duration_us=3_000_000,
        evidence_half_interval_us=400_000,
        maximum_distance_us=800_000,
    )

    assert reinforced[0].range == TimeRange(0, 1_400_000)
    assert "quality-boundary" in reinforced[0].evidence_ids
    coverage = measure_hard_sub_coverage((quality,), reinforced)
    assert coverage.coverage_ratio == 1.0


def test_sparse_quality_evidence_does_not_extend_a_different_adjacent_cue() -> None:
    candidate = _candidate("cue", 0, 1_000_000, "师弟你没事")
    quality = SubtitleEvidence(
        "quality-next",
        "frame-quality",
        1_000_000,
        "upper",
        "真是太好了",
        0.99,
        "paddle-quality",
        "PP-OCRv6-medium",
    )

    reinforced = reinforce_candidate_ranges(
        (candidate,),
        (quality,),
        video_duration_us=3_000_000,
        evidence_half_interval_us=400_000,
        maximum_distance_us=800_000,
    )

    assert reinforced == (candidate,)


def test_coverage_counts_upper_and_lower_evidence_at_one_timestamp_once() -> None:
    evidence = (
        _evidence("upper", "frame-1", "我要修炼成仙", 0.96),
        _evidence("lower", "frame-1", "我要修炼成仙", 0.94, band="lower"),
    )
    cues = (_candidate("cue", 400_000, 1_200_000, "我要修炼成仙"),)

    report = measure_hard_sub_coverage(evidence, cues)

    assert report.subtitle_positive_timestamps == 1
    assert report.covered_timestamps == 1
    assert report.coverage_ratio == 1.0
    assert report.passes is True


def test_coverage_requires_both_timing_overlap_and_text_agreement() -> None:
    evidence = (
        _evidence("one", "frame-1", "第一句真实台词", 0.96),
        _evidence("two", "frame-2", "第二句真实台词", 0.96),
        _evidence("three", "frame-3", "第三句真实台词", 0.96),
    )
    cues = (
        _candidate("covered", 400_000, 1_200_000, "第一句真实台词"),
        _candidate("wrong-text", 1_200_000, 2_000_000, "完全不同内容"),
        _candidate("wrong-time", 3_000_000, 3_800_000, "第三句真实台词"),
    )

    report = measure_hard_sub_coverage(evidence, cues, minimum_coverage=0.95)

    assert report.subtitle_positive_timestamps == 3
    assert report.covered_timestamps == 1
    assert report.coverage_ratio == pytest.approx(1 / 3)
    assert [gap.timestamp_us for gap in report.unmatched_samples] == [1_600_000, 2_400_000]
    assert report.passes is False


def test_coverage_passes_at_exactly_ninety_five_percent() -> None:
    evidence = tuple(
        SubtitleEvidence(
            f"evidence-{index}",
            f"frame-{index}",
            index * 1_000_000,
            "upper",
            f"第{index}句真实台词",
            0.97,
            "paddle-fast",
            "PP-OCRv6-small",
        )
        for index in range(20)
    )
    cues = tuple(
        _candidate(
            f"cue-{index}",
            max(0, index * 1_000_000 - 100_000),
            index * 1_000_000 + 100_000,
            f"第{index}句真实台词",
        )
        for index in range(19)
    )

    report = measure_hard_sub_coverage(evidence, cues, minimum_coverage=0.95)

    assert report.subtitle_positive_timestamps == 20
    assert report.covered_timestamps == 19
    assert report.coverage_ratio == 0.95
    assert report.passes is True


def test_coverage_rejects_a_large_unobserved_timeline_gap() -> None:
    evidence = (
        SubtitleEvidence(
            "start",
            "frame-start",
            10_000_000,
            "upper",
            "第一句真实台词",
            0.97,
            "paddle-fast",
            "PP-OCRv6-small",
        ),
        SubtitleEvidence(
            "end",
            "frame-end",
            14_390_000_000,
            "upper",
            "最后一句真实台词",
            0.97,
            "paddle-fast",
            "PP-OCRv6-small",
        ),
    )
    cues = (
        _candidate("start", 9_000_000, 11_000_000, "第一句真实台词"),
        _candidate("end", 14_389_000_000, 14_391_000_000, "最后一句真实台词"),
    )

    report = measure_hard_sub_coverage(
        evidence,
        cues,
        video_duration_us=14_567_508_186,
    )

    assert report.coverage_ratio == 1.0
    assert report.maximum_positive_gap_us > report.continuity_gap_limit_us
    assert report.timeline_continuity_passes is False
    assert report.passes is False


def test_coverage_excludes_proven_overlay_only_samples() -> None:
    evidence = (
        _evidence("overlay", "frame-1", "修复2802bilibili", 0.98),
        _evidence("dialogue", "frame-2", "我要修炼成仙修复2802bilibili", 0.98),
    )
    cues = (_candidate("cue", 1_200_000, 2_000_000, "我要修炼成仙"),)

    report = measure_hard_sub_coverage(
        evidence,
        cues,
        ignored_overlay_tokens=("修复2802bilibili",),
    )

    assert report.subtitle_positive_timestamps == 1
    assert report.covered_timestamps == 1
    assert report.passes is True


def test_finalizer_removes_only_proven_overlay_and_never_density_filters_dialogue(
    tmp_path: Path,
) -> None:
    items: list[SubtitleCandidate] = []
    for index in range(12):
        start = index * 10_000_000
        items.append(_candidate(f"overlay-{index}", start, start + 1_000_000, "修复超清"))
        items.append(
            _candidate(
                f"dialogue-{index}",
                start + 2_000_000,
                start + 9_500_000,
                f"第{index}句真实台词修复超清",
            )
        )
    items.append(_candidate("short", 121_000_000, 130_000_000, "慢着"))

    result = finalize_ocr_candidates(tuple(items), video_duration_us=140_000_000)

    assert result.status is FinalizationStatus.PASS
    assert result.overlay_token == "修复超清"
    assert result.overlay_only_rows_removed == 12
    assert any(cue.text == "慢着" for cue in result.cues)
    assert all("修复超清" not in cue.text for cue in result.cues)
    output = publish_final_srt(tmp_path / "sample_ZH_FINAL.srt", result)
    assert output.read_text(encoding="utf-8-sig").count("-->") == len(result.cues)


def test_finalizer_removes_repeated_known_channel_marker_without_broad_keyword_filter(
    tmp_path: Path,
) -> None:
    items: list[SubtitleCandidate] = []
    for index in range(8):
        start = index * 10_000_000
        items.append(
            _candidate(
                f"channel-{index}", start, start + 900_000, "小李看漫剧"
            )
        )
        items.append(
            _candidate(
                f"dialogue-{index}",
                start + 1_000_000,
                start + 4_000_000,
                f"第{index}句真正台词",
            )
        )
    items.append(_candidate("legitimate", 82_000_000, 84_000_000, "我也想看漫剧"))

    result = finalize_ocr_candidates(tuple(items), video_duration_us=90_000_000)

    assert result.status is FinalizationStatus.PASS
    assert "小李看漫剧" in result.overlay_tokens
    assert all(cue.text != "小李看漫剧" for cue in result.cues)
    assert any(cue.text == "我也想看漫剧" for cue in result.cues)
    output = publish_final_srt(tmp_path / "channel_ZH_FINAL.srt", result)
    assert "小李看漫剧" not in output.read_text(encoding="utf-8-sig")


def test_finalizer_learns_persistent_channel_marker_from_pre_grouped_evidence() -> None:
    watermark = tuple(
        _evidence(
            f"watermark-{index}",
            f"frame-{index}",
            "小李看漫剧",
            0.98,
            band="lower",
            box=NormalizedBox(0.73, 0.84, 0.22, 0.07),
        )
        for index in range(20)
    )
    dialogue = (
        _evidence(
            "dialogue-20",
            "frame-20",
            "真正的台词",
            0.99,
            band="lower",
            box=NormalizedBox(0.34, 0.82, 0.32, 0.08),
        ),
        _evidence(
            "dialogue-21",
            "frame-21",
            "真正的台词",
            0.99,
            band="lower",
            box=NormalizedBox(0.34, 0.82, 0.32, 0.08),
        ),
    )
    evidence = watermark + dialogue
    candidates = group_visual_evidence(
        evidence,
        band="lower",
        duration_us=18_000_000,
        sample_interval_us=800_000,
    )

    assert [item.text for item in candidates] == ["小李看漫剧", "真正的台词"]

    result = finalize_ocr_candidates(
        candidates,
        video_duration_us=18_000_000,
        evidence=evidence,
    )

    assert result.status is FinalizationStatus.PASS
    assert result.overlay_tokens == ("小李看漫剧",)
    assert [cue.text for cue in result.cues] == ["真正的台词"]


def test_finalizer_does_not_learn_centered_repeated_dialogue_as_channel_marker() -> None:
    evidence = tuple(
        _evidence(
            f"dialogue-{index}",
            f"frame-{index}",
            "我也想看漫剧",
            0.99,
            band="lower",
            box=NormalizedBox(0.28, 0.82, 0.44, 0.08),
        )
        for index in range(8)
    )
    candidates = group_visual_evidence(
        evidence,
        band="lower",
        duration_us=7_000_000,
        sample_interval_us=800_000,
    )

    result = finalize_ocr_candidates(
        candidates,
        video_duration_us=7_000_000,
        evidence=evidence,
    )

    assert result.overlay_tokens == ()
    assert [cue.text for cue in result.cues] == ["我也想看漫剧"]


def test_finalizer_does_not_learn_edge_repeated_sentence_from_keyword_alone() -> None:
    evidence = tuple(
        _evidence(
            f"dialogue-{index}",
            f"frame-{index}",
            "今天看漫剧",
            0.99,
            band="lower",
            box=NormalizedBox(0.73, 0.84, 0.22, 0.07),
        )
        for index in range(8)
    )
    candidates = group_visual_evidence(
        evidence,
        band="lower",
        duration_us=7_000_000,
        sample_interval_us=800_000,
    )

    result = finalize_ocr_candidates(
        candidates,
        video_duration_us=7_000_000,
        evidence=evidence,
    )

    assert result.overlay_tokens == ()
    assert [cue.text for cue in result.cues] == ["今天看漫剧"]


def test_finalizer_learns_reordered_alphanumeric_overlay_and_ocr_variants() -> None:
    clean_texts = (
        "叶道友终于出来了",
        "系统就会给自己十倍回馈",
        "我这边还不好搞了呢",
        "额外奖励一品炼丹术圆满感悟",
        "到底是谁在搞鬼呢",
        "怕是要等任务期限的末尾",
        "才能上交丹药了",
        "这叶家只要出现筑基修士",
        "地球上的美女都往大城市跑",
        "倍率岂不是二十倍三十倍甚至上百倍",
        "自己都练不出几颗丹药",
        "所以偷了点丹药出来卖",
    )
    variants = (
        "修复2802 bilibili",
        "bilibili修复2802",
        "修复2802bilibili",
        "Lilibili修复2802",
        "bilibilr修复2802",
        "修复2802 billbili",
    )
    items = tuple(
        _candidate(
            f"dirty-{index}",
            index * 2_000_000,
            index * 2_000_000 + 1_500_000,
            (
                variants[index % len(variants)] + text
                if index == 9
                else text + variants[index % len(variants)]
            ),
        )
        for index, text in enumerate(clean_texts)
    ) + (
        _candidate("legitimate-latin", 25_000_000, 26_000_000, "开启VIP通道"),
    )

    result = finalize_ocr_candidates(items, video_duration_us=30_000_000)

    assert result.status is FinalizationStatus.PASS
    assert [cue.text for cue in result.cues[:-1]] == list(clean_texts)
    assert result.cues[-1].text == "开启VIP通道"
    assert result.overlay_rows_stripped == len(clean_texts)


def test_finalizer_resolves_small_sampling_overlap_at_shared_midpoint() -> None:
    result = finalize_ocr_candidates(
        (
            _candidate("one", 0, 1_400_000, "第一句"),
            _candidate("two", 1_000_000, 2_000_000, "第二句"),
        ),
        video_duration_us=3_000_000,
    )

    assert result.status is FinalizationStatus.PASS
    assert result.cues[0].range == TimeRange(0, 1_200_000)
    assert result.cues[1].range == TimeRange(1_200_000, 2_000_000)
    assert "sampling_overlaps_resolved:1" in result.warnings


def test_finalizer_refuses_conflicts_overlap_and_ambiguous_output_name(tmp_path: Path) -> None:
    result = finalize_ocr_candidates(
        (
            _candidate("one", 0, 2_000_000, "第一句"),
            _candidate(
                "two",
                500_000,
                3_000_000,
                "第二句",
                review=("band_conflict",),
            ),
        ),
        video_duration_us=4_000_000,
    )

    assert result.status is FinalizationStatus.REVIEW
    assert len(result.cues) == 2
    assert any(item.startswith("cue_overlap") for item in result.blockers)
    with pytest.raises(ValueError, match="PASS"):
        publish_final_srt(tmp_path / "partial.srt", result)
