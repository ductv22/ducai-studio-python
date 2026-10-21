from dataclasses import replace

from ducai_studio.application.ocr_pipeline import (
    scene_text_cues_from_evidence,
    scene_text_rescue_regions,
)
from ducai_studio.domain.subtitles import NormalizedBox, SceneTextProbe, SubtitleEvidence


def reading(timestamp, text="天南", *, confidence=.94, score=.58, scope="one"):
    return SubtitleEvidence(
        f"{scope}-{timestamp}-{text}", f"{scope}-frame-{timestamp}", timestamp,
        "scene", text, confidence, "paddle-fast", "small",
        NormalizedBox(.12, .25, .18, .06), "scene", "horizontal", "display",
        panel_box=NormalizedBox(.10, .23, .22, .10), scene_confidence=score,
        source_scope=scope,
    )


def test_textured_horizontal_label_is_retained_for_review_not_silently_dropped():
    cues = scene_text_cues_from_evidence(
        (reading(1_000_000), reading(1_240_000)),
        video_duration_us=10_000_000, first_index=1,
    )
    assert len(cues) == 1
    assert cues[0].review_state == "review" and cues[0].text_font_size == 40


def test_quality_confirms_single_glyph_scene_label():
    rows = tuple(replace(reading(t, "火", score=.95), provider_id="paddle-quality")
                 for t in (1_000_000, 1_200_000))
    cues = scene_text_cues_from_evidence(rows, video_duration_us=5_000_000, first_index=1)
    assert len(cues) == 1 and cues[0].text == "火"


def test_same_scene_at_different_times_or_across_cuts_is_not_one_long_cue():
    rows = (reading(1_000_000), reading(1_240_000),
            reading(7_000_000), reading(7_240_000),
            reading(7_300_000, scope="two"), reading(7_500_000, scope="two"))
    cues = scene_text_cues_from_evidence(rows, video_duration_us=10_000_000, first_index=1)
    assert len(cues) == 3


def test_scene_quality_rescue_uses_expanded_regions_and_keeps_weak_seed():
    rows = (reading(1_000_000, confidence=.61), reading(1_240_000, confidence=.74))
    regions = scene_text_rescue_regions(rows, video_duration_us=10_000_000)
    assert len(regions) == 1
    region = regions[0]
    assert region.box.x < .12 and region.box.x + region.box.width > .30
    assert region.range.start_us < 1_000_000 < region.range.end_us
    assert region.source_scope == "one"


def test_scene_boundaries_follow_confirmed_absence_not_fixed_one_second_tail():
    box = NormalizedBox(.08, .20, .28, .16)
    rows = tuple(replace(reading(t, score=.9), provider_id="paddle-quality")
                 for t in (1_000_000, 1_200_000, 1_400_000))
    probes = tuple(SceneTextProbe(t, box, "one")
                   for t in (800_000, 1_000_000, 1_200_000, 1_400_000, 1_600_000))
    cues = scene_text_cues_from_evidence(
        rows, video_duration_us=5_000_000, first_index=1, probes=probes,
    )
    assert cues[0].range.start_us == 900_000
    assert cues[0].range.end_us == 1_500_000


def test_two_absent_probes_separate_nearby_repeated_label():
    box = NormalizedBox(.08, .20, .28, .16)
    rows = tuple(reading(t, score=.9) for t in
                 (1_000_000, 1_200_000, 2_000_000, 2_200_000))
    probes = tuple(SceneTextProbe(t, box, "one") for t in (1_400_000, 1_600_000))
    cues = scene_text_cues_from_evidence(
        rows, video_duration_us=5_000_000, first_index=1, probes=probes,
    )
    assert len(cues) == 2


def test_multi_crop_same_frame_does_not_count_as_temporal_confirmation():
    row = reading(1_000_000, confidence=.85, score=.6)
    cues = scene_text_cues_from_evidence(
        (row, replace(row, id="another-crop")), video_duration_us=5_000_000, first_index=1,
    )
    assert not cues


def test_scene_rescue_rejects_mixed_latin_platform_badge_and_unreadable_noise():
    rows = (replace(reading(1_000_000, "Liilili独辑"),
                    box=NormalizedBox(.87, .04, .10, .05)),
            reading(2_000_000, "乱码", confidence=.3))
    assert not scene_text_rescue_regions(rows, video_duration_us=5_000_000)


def test_consecutive_clear_quality_names_remain_independent():
    rows = tuple(replace(reading(t, text, score=.9), provider_id="paddle-quality")
                 for t, text in ((1_000_000, "天星锤"), (1_200_000, "天星锤"),
                                 (1_600_000, "天星珠"), (1_800_000, "天星珠")))
    cues = scene_text_cues_from_evidence(rows, video_duration_us=5_000_000, first_index=1)
    assert [cue.text for cue in cues] == ["天星锤", "天星珠"]


def test_more_quality_frames_do_not_outvote_clearer_fast_consensus():
    rows = tuple(reading(t, "胥国七大派长老", confidence=.97, score=.95)
                 for t in (1_000_000, 1_240_000))
    rows += tuple(replace(reading(t, "胥国士大派长者", confidence=.92, score=.95),
                          provider_id="paddle-quality")
                  for t in (1_000_000, 1_200_000, 1_400_000, 1_600_000))
    cues = scene_text_cues_from_evidence(rows, video_duration_us=5_000_000, first_index=1)
    assert len(cues) == 1
    assert cues[0].text == "胥国七大派长老" and cues[0].review_state == "review"


def test_quality_truncated_name_does_not_replace_two_clear_complete_frames():
    rows = tuple(reading(t, text, confidence=confidence, score=.95)
                 for t, text, confidence in ((1_000_000, "董萱儿", .97),
                                             (1_240_000, "董萱儿", .90)))
    rows += tuple(replace(reading(t, text, confidence=confidence, score=.95),
                          provider_id="paddle-quality")
                  for t, text, confidence in ((1_000_000, "董萱ル", .84),
                                              (1_200_000, "董萱", .99)))
    cues = scene_text_cues_from_evidence(rows, video_duration_us=5_000_000, first_index=1)
    assert len(cues) == 1 and cues[0].text == "董萱儿"
