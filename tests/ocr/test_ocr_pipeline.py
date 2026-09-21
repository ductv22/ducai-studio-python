from __future__ import annotations

from itertools import count
from pathlib import Path

import pytest

from ducai_studio.application.ocr_pipeline import (
    apply_quality_rescue,
    coalesce_rescue_windows,
    consensus_candidate,
    group_visual_evidence,
    measure_hard_sub_coverage,
    merge_band_candidates,
    reinforce_candidate_ranges,
    targeted_rescue_windows,
)
from ducai_studio.application.subtitle_finalizer import (
    FinalizationStatus,
    finalize_ocr_candidates,
    publish_final_srt,
)
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
