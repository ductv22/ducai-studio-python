from dataclasses import replace

import pytest

from ducai_studio.application.ocr_pipeline import reinforce_candidate_ranges
from ducai_studio.application.subtitle_finalizer import finalize_ocr_candidates
from ducai_studio.domain.subtitles import NormalizedBox, SubtitleCandidate, SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_scene_title_cannot_extend_dialogue_into_the_next_two_lines(orientation):
    rows = tuple(SubtitleCandidate(str(i), TimeRange(start, end), text, .999, (str(i),), "ocr")
                 for i, (start, end, text) in enumerate((
                     (62_000_000, 64_400_000, "在下是这万宝楼的掌柜"),
                     (64_300_000, 66_100_000, "敢问客官尊姓大名"),
                     (65_800_000, 67_000_000, "师承何派"),
                 )))
    scene = tuple(SubtitleEvidence(
        f"scene-{t}", f"frame-{t}", t, "scene", "万宝楼掌柜", .999, "paddle-quality", "model",
        NormalizedBox(.1, .2, .1, .05), evidence_kind="scene", orientation=orientation,
    ) for t in (64_600_000, 65_000_000, 65_400_000, 65_800_000))
    reinforced = reinforce_candidate_ranges(rows, scene, video_duration_us=70_000_000,
                                            evidence_half_interval_us=400_000,
                                            maximum_distance_us=800_000)
    assert reinforced == rows
    result = finalize_ocr_candidates(reinforced, video_duration_us=70_000_000,
                                     evidence=scene)
    assert not result.blockers
    assert [c.text for c in result.cues] == [c.text for c in rows]
    assert all(a.range.end_us <= b.range.start_us
               for a, b in zip(result.cues, result.cues[1:], strict=False))


def mixed_candidates():
    clean = SubtitleCandidate("clean", TimeRange(776_900_000, 780_100_000),
                              "中心区域有一圈环形山脉", .9987,
                              ("clean-0", "clean-1", "clean-2"), "multiband_agree")
    mixed = SubtitleCandidate("mixed", TimeRange(777_700_000, 779_900_000),
                              "11a五指湖区域有一圈环形山脉nH", .8242,
                              ("mixed-0", "mixed-1"), "paddle-fast:upper")
    evidence = tuple(SubtitleEvidence(
        key, f"frame-{timestamp}", timestamp, band, text, confidence, "paddle-fast", "model",
        NormalizedBox(*box), source_scope="video:0",
    ) for key, timestamp, band, text, confidence, box in (
        ("clean-0", 777_600_000, "lower", clean.text, .9999, (.37, .90, .26, .05)),
        ("clean-1", 778_400_000, "lower", clean.text, .99, (.37, .90, .26, .05)),
        ("clean-2", 779_200_000, "lower", clean.text, .9997, (.37, .90, .26, .05)),
        ("mixed-0", 778_400_000, "upper", mixed.text, .824, (.26, .765, .40, .21)),
        ("mixed-1", 779_200_000, "upper", mixed.text, .824, (.26, .765, .40, .21)),
    ))
    return clean, mixed, evidence


def test_mixed_map_glyphs_do_not_create_overlapping_dialogue():
    clean, mixed, evidence = mixed_candidates()
    result = finalize_ocr_candidates((clean, mixed), video_duration_us=800_000_000,
                                     evidence=evidence)
    assert [c.text for c in result.cues] == [clean.text]
    assert result.cues[0].range == clean.range
    assert set(result.cues[0].evidence_ids) == {*clean.evidence_ids, *mixed.evidence_ids}
    assert result.cues[0].visual_box == pytest.approx((.37, .90, .26, .05))


def test_length_first_winner_is_restored_from_repeated_clean_frame_readings():
    clean, mixed, evidence = mixed_candidates()
    clean = replace(clean, text="五指" + clean.text)
    result = finalize_ocr_candidates((clean, mixed), video_duration_us=800_000_000,
                                     evidence=evidence)
    assert [c.text for c in result.cues] == [evidence[0].text]
    assert result.cues[0].range == clean.range
    assert result.cues[0].visual_box == pytest.approx((.37, .90, .26, .05))


@pytest.mark.parametrize("boundary", ["scope", "frame", "region", "confidence", "short"])
def test_weak_longer_reading_is_not_removed_without_occurrence_proof(boundary):
    clean, mixed, evidence = mixed_candidates()
    if boundary == "confidence":
        mixed = replace(mixed, confidence=.99)
    elif boundary == "short":
        clean = replace(clean, text="娘")
    else:
        evidence = tuple(replace(e, **(
            {"source_scope": "other:0"} if boundary == "scope" else
            {"frame_id": e.frame_id + "-other"} if boundary == "frame" else
            {"box": NormalizedBox(.01, .1, .4, .21)}
        )) if e.id.startswith("mixed") else e for e in evidence)
    result = finalize_ocr_candidates((clean, mixed), video_duration_us=800_000_000,
                                     evidence=evidence)
    assert len(result.cues) == 2
    assert result.status == "REVIEW"


def test_nested_conflicting_overlaps_all_receive_review_state():
    rows = tuple(SubtitleCandidate(str(i), TimeRange(start, end), text, .99, (str(i),), "ocr")
                 for i, (start, end, text) in enumerate((
                     (0, 10_000_000, "第一句保持完整"),
                     (1_000_000, 2_000_000, "另外一句"),
                     (3_000_000, 4_000_000, "第三句"),
                 )))
    result = finalize_ocr_candidates(rows, video_duration_us=12_000_000)
    assert all(c.review_state == "review" for c in result.cues)
    assert len([b for b in result.blockers if b.startswith("cue_overlap:")]) == 2


@pytest.mark.parametrize("long_frames,expected", [(1, "中心区域有一圈环形山脉"),
                                                  (2, "中心区域有一一圈环形山脉")])
def test_repeated_glyph_requires_independent_frame_support(long_frames, expected):
    clean, _, rows = mixed_candidates()
    long = "中心区域有一一圈环形山脉"
    more = tuple(replace(rows[i], id=f"long-{i}", text=long)
                 for i in range(long_frames))
    candidate = replace(clean, text=long, evidence_ids=(*clean.evidence_ids,
                                                      *(r.id for r in more)))
    result = finalize_ocr_candidates((candidate,), video_duration_us=800_000_000,
                                     evidence=(*rows[:3], *more))
    assert result.cues[0].text == expected
    assert result.cues[0].range == candidate.range


@pytest.mark.parametrize("boundary", ["scope", "frame", "region"])
def test_repeated_glyph_in_other_occurrence_is_not_normalized(boundary):
    clean, _, rows = mixed_candidates()
    long = "中心区域有一一圈环形山脉"
    original = replace(rows[0], id="original", text=long, **(
        {"source_scope": "other"} if boundary == "scope" else
        {"frame_id": "other"} if boundary == "frame" else
        {"box": NormalizedBox(.01, .1, .3, .05)}
    ))
    candidate = replace(clean, text=long, evidence_ids=(*clean.evidence_ids, original.id))
    result = finalize_ocr_candidates((candidate,), video_duration_us=800_000_000,
                                     evidence=(*rows[:3], original))
    assert result.cues[0].text == long
