from __future__ import annotations

import pytest

from ducai_studio.domain.voice_conform import (
    VoiceConformCueInput,
    VoiceConformPlan,
    VoiceConformSegment,
    VoiceConformSettings,
    VoiceTimingMode,
    build_voice_conform_plan,
)


def _cue(audio_duration_us: int) -> VoiceConformCueInput:
    return VoiceConformCueInput("cue-1", 1, 0, 2_000_000, audio_duration_us)


def test_video_stretch_keeps_voice_and_doubles_the_slot() -> None:
    plan = build_voice_conform_plan(
        (_cue(4_000_000),),
        source_duration_us=2_000_000,
        settings=VoiceConformSettings(mode=VoiceTimingMode.STRETCH_VIDEO),
    )

    decision = plan.cues[0]
    assert plan.applicable is True
    assert decision.audio_speed == pytest.approx(1.0)
    assert decision.video_speed == pytest.approx(0.5)
    assert decision.output_start_us == 0
    assert decision.output_end_us == 4_000_000
    assert plan.output_duration_us == 4_000_000


def test_audio_speed_mode_fits_a_four_second_voice_into_two_seconds() -> None:
    plan = build_voice_conform_plan(
        (_cue(4_000_000),),
        source_duration_us=2_000_000,
        settings=VoiceConformSettings(
            mode=VoiceTimingMode.SPEED_AUDIO,
            max_audio_speed=2.0,
        ),
    )

    decision = plan.cues[0]
    assert plan.applicable is True
    assert decision.audio_speed == pytest.approx(2.0)
    assert decision.video_speed == pytest.approx(1.0)
    assert decision.output_end_us == 2_000_000
    assert plan.output_duration_us == 2_000_000


def test_hybrid_uses_slider_as_audio_cap_then_stretches_video() -> None:
    plan = build_voice_conform_plan(
        (_cue(4_000_000),),
        source_duration_us=2_000_000,
        settings=VoiceConformSettings(
            mode=VoiceTimingMode.HYBRID,
            max_audio_speed=1.25,
        ),
    )

    decision = plan.cues[0]
    assert plan.applicable is True
    assert decision.audio_speed == pytest.approx(1.25)
    assert decision.video_speed == pytest.approx(0.625)
    assert decision.output_end_us == 3_200_000
    assert plan.output_duration_us == 3_200_000


def test_hybrid_marks_a_five_times_cue_unresolved() -> None:
    plan = build_voice_conform_plan(
        (_cue(10_000_000),),
        source_duration_us=2_000_000,
        settings=VoiceConformSettings(
            mode=VoiceTimingMode.HYBRID,
            max_audio_speed=2.0,
            min_video_speed=0.5,
        ),
    )

    assert plan.applicable is False
    assert plan.unresolved_cue_ids == ("cue-1",)
    assert plan.cues[0].issue_code == "CUE_SYNC_LIMIT_EXCEEDED"
    assert plan.cues[0].required_video_speed == pytest.approx(0.4)


def test_short_cues_and_gaps_remain_identity_mapped() -> None:
    cues = (
        VoiceConformCueInput("one", 1, 1_000_000, 2_000_000, 500_000),
        VoiceConformCueInput("two", 2, 4_000_000, 5_000_000, 1_000_000),
    )
    plan = build_voice_conform_plan(
        cues,
        source_duration_us=6_000_000,
        settings=VoiceConformSettings(mode=VoiceTimingMode.HYBRID),
    )

    assert plan.output_duration_us == 6_000_000
    assert len(plan.segments) == 1
    assert plan.segments[0].video_speed == pytest.approx(1.0)
    assert plan.source_to_output_us(4_500_000) == 4_500_000
    assert plan.output_to_source_us(4_500_000) == 4_500_000


def test_overlapping_cues_are_serialized_inside_one_conform_block() -> None:
    cues = (
        VoiceConformCueInput("one", 1, 0, 2_000_000, 2_000_000),
        VoiceConformCueInput("two", 2, 1_000_000, 3_000_000, 2_000_000),
    )
    plan = build_voice_conform_plan(
        cues,
        source_duration_us=3_000_000,
        settings=VoiceConformSettings(
            mode=VoiceTimingMode.HYBRID,
            max_audio_speed=1.25,
            min_video_speed=0.5,
        ),
    )

    assert plan.applicable is True
    assert len(plan.segments) == 1
    assert plan.cues[1].voice_start_us >= plan.cues[0].voice_end_us
    assert plan.output_duration_us >= plan.cues[1].voice_end_us


def test_overlapping_cues_follow_index_order_when_timestamps_are_out_of_order() -> None:
    cues = (
        VoiceConformCueInput("second-by-time", 1, 900_000, 2_500_000, 1_200_000),
        VoiceConformCueInput("first-by-time", 2, 0, 1_500_000, 1_200_000),
    )

    plan = build_voice_conform_plan(
        cues,
        source_duration_us=2_500_000,
        settings=VoiceConformSettings(
            mode=VoiceTimingMode.HYBRID,
            max_audio_speed=1.25,
            min_video_speed=0.5,
        ),
    )

    by_id = {decision.cue_id: decision for decision in plan.cues}
    assert plan.applicable is True
    assert by_id["second-by-time"].voice_start_us < by_id["first-by-time"].voice_start_us
    assert (
        by_id["first-by-time"].voice_start_us
        >= by_id["second-by-time"].voice_end_us
    )


def test_audio_only_overlap_that_cannot_be_serialized_is_unresolved() -> None:
    cues = (
        VoiceConformCueInput("one", 1, 0, 2_000_000, 2_000_000),
        VoiceConformCueInput("two", 2, 1_000_000, 3_000_000, 2_000_000),
    )
    plan = build_voice_conform_plan(
        cues,
        source_duration_us=3_000_000,
        settings=VoiceConformSettings(
            mode=VoiceTimingMode.SPEED_AUDIO,
            max_audio_speed=1.25,
        ),
    )

    assert plan.applicable is False
    assert set(plan.unresolved_cue_ids) == {"one", "two"}


def test_conform_settings_enforce_selected_quality_limits() -> None:
    with pytest.raises(ValueError, match="between 1.0 and 2.0"):
        VoiceConformSettings(max_audio_speed=2.1)
    with pytest.raises(ValueError, match="between 0.5 and 1.0"):
        VoiceConformSettings(min_video_speed=0.49)


def test_duration_growth_disabled_marks_stretching_plan_unresolved() -> None:
    plan = build_voice_conform_plan(
        (_cue(4_000_000),),
        source_duration_us=2_000_000,
        settings=VoiceConformSettings(
            mode=VoiceTimingMode.HYBRID,
            max_audio_speed=1.25,
            allow_duration_growth=False,
        ),
    )

    assert plan.applicable is False
    assert plan.unresolved_cue_ids == ("cue-1",)
    assert "duration" in plan.cues[0].issue_message


def test_indexed_time_mapping_preserves_all_boundaries_and_rounding() -> None:
    segments = (
        VoiceConformSegment(0, 7, 0, 11, 7 / 11),
        VoiceConformSegment(7, 12, 11, 14, 5 / 3),
        VoiceConformSegment(12, 16, 14, 19, 4 / 5),
    )
    plan = VoiceConformPlan(1, VoiceConformSettings(), 16, 19, segments, (), (), "map")

    def reference(value, source):
        prefix, target = ("source", "output") if source else ("output", "source")
        total = getattr(plan, prefix + "_duration_us")
        target_total = getattr(plan, target + "_duration_us")
        position = max(0, min(total, int(value)))
        if position == total:
            return target_total
        segment = next(s for s in segments if getattr(s, prefix + "_start_us")
                       <= position < getattr(s, prefix + "_end_us"))
        start, end = (getattr(segment, prefix + edge) for edge in ("_start_us", "_end_us"))
        a, b = (getattr(segment, target + edge) for edge in ("_start_us", "_end_us"))
        quotient, remainder = divmod((position - start) * (b - a), end - start)
        return a + quotient + int(2 * remainder >= end - start)

    for value in (-100, *[i / 4 for i in range(85)], 100):
        assert plan.source_to_output_us(value) == reference(value, True)
        assert plan.output_to_source_us(value) == reference(value, False)
    assert set(plan.as_json()) == {
        "schema_version", "settings", "source_duration_us", "output_duration_us",
        "segments", "cues", "unresolved_cue_ids", "fingerprint",
    }


def test_time_queries_do_not_scan_every_conform_segment() -> None:
    class CountedSegments(tuple):
        scans = 0
        reads = 0

        def __iter__(self):
            self.scans += 1
            return super().__iter__()

        def __getitem__(self, index):
            self.reads += 1
            return super().__getitem__(index)

    count = 12_000
    segments = CountedSegments(VoiceConformSegment(i * 10, (i + 1) * 10,
                              i * 20, (i + 1) * 20, 0.5) for i in range(count))
    plan = VoiceConformPlan(1, VoiceConformSettings(), count * 10, count * 20,
                            segments, (), (), "large-map")
    segments.scans = segments.reads = 0
    assert plan.source_to_output_us(count * 10 - 5) == count * 20 - 10
    assert plan.output_to_source_us(count * 20 - 10) == count * 10 - 5
    assert segments.scans == 0
    assert segments.reads <= 32
