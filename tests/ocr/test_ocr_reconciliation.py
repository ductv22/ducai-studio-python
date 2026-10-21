from __future__ import annotations

import json
from dataclasses import asdict, replace
from itertools import count

import pytest

from ducai_studio.application.ocr_pipeline import measure_hard_sub_coverage, merge_band_candidates
from ducai_studio.application.subtitle_finalizer import (
    collapse_partial_ocr_cues,
    collapse_proven_ocr_candidates,
    collapse_proven_ocr_cues,
    finalize_ocr_candidates,
    render_srt,
)
from ducai_studio.domain.project import ProjectSnapshot, SubtitleCue
from ducai_studio.domain.subtitles import NormalizedBox, SubtitleCandidate, SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.job_manager.service import _apply_ocr_cue_regions
from ducai_studio.providers.ocr.contracts import DEFAULT_SUBTITLE_BANDS
from ducai_studio.workers.ocr_worker import OcrWorker

LEGACY_BANDS = [
    {"name": "upper", "x1": 0.06, "y1": 0.765, "x2": 0.94, "y2": 0.935, "scale": 0.9},
    {"name": "lower", "x1": 0.08, "y1": 0.885, "x2": 0.92, "y2": 0.982, "scale": 0.75},
]


def _worker(tmp_path, monkeypatch, rows, *, bands=LEGACY_BANDS):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    worker = OcrWorker("reconciliation")
    worker.config = {"bands": bands}
    worker.evidence_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    return worker


def _rows(short, full, short_box, full_box, *, start=4_200_000):
    result = []
    for timestamp in (start, start + 100_000):
        for band, text, box, confidence in (
            ("upper", short, short_box, 0.949627),
            ("lower", full, full_box, 0.99996),
        ):
            result.append(
                {
                    "id": f"{band}-{timestamp}",
                    "frame_id": f"frame-{timestamp}",
                    "timestamp_us": timestamp,
                    "band": band,
                    "text": text,
                    "confidence": confidence,
                    "provider_id": "paddle-fast",
                    "model_id": "PP-OCRv6_small_rec",
                    "box": box,
                    "evidence_kind": "burst",
                    "media_id": "media-one",
                    "scan_segment_index": 0,
                }
            )
    return result


def _arrow_frog_crop_cut_rows():
    rows = []
    boxes = {
        "upper": [0.44016, 0.8505480769230769, 0.1185066666666667, 0.081375],
        "lower": [0.44290502793296094, 0.885, 0.11418994413407824, 0.04279411764705876],
    }
    for timestamp in range(540_800_000, 541_400_000, 100_000):
        for band, text, confidence in (
            ("upper", "箭毒蛙", 0.998804),
            ("lower", "前毒蛙", 0.889924),
        ):
            rows.append(
                {
                    "id": f"{band}-{timestamp}",
                    "frame_id": f"frame-{timestamp}",
                    "timestamp_us": timestamp,
                    "band": band,
                    "text": text,
                    "confidence": confidence,
                    "provider_id": "paddle-fast",
                    "model_id": "PP-OCRv6_small_rec",
                    "box": boxes[band],
                    "evidence_kind": "burst",
                    "media_id": "frog-source",
                    "scan_segment_index": 0,
                }
            )
    return rows


def _reciprocal_overlap_crop_rows():
    rows = []
    boxes = {
        "A": [0.29, 0.4, 0.40, 0.10],
        "B": [0.31, 0.4, 0.40, 0.10],
    }
    for timestamp in (1_000_000, 1_100_000):
        for band, text in (("A", "箭毒蛙"), ("B", "前毒蛙")):
            rows.append(
                {
                    "id": f"{band}-{timestamp}",
                    "frame_id": f"frame-{timestamp}",
                    "timestamp_us": timestamp,
                    "band": band,
                    "text": text,
                    "confidence": 0.99,
                    "provider_id": "paddle-fast",
                    "model_id": "PP-OCRv6_small_rec",
                    "box": boxes[band],
                    "evidence_kind": "burst",
                    "media_id": "reciprocal-source",
                    "scan_segment_index": 0,
                }
            )
    return rows


def _current_subtitle_bands():
    return [asdict(band) for band in DEFAULT_SUBTITLE_BANDS]


def _candidate_evidence(candidates):
    return tuple(
        SubtitleEvidence(
            key, f"frame-{offset}", 200_000 + offset * 100_000, "upper",
            candidate.text, candidate.confidence, "paddle", "model",
            source_scope="source:0",
        )
        for candidate in candidates
        for offset, key in enumerate(candidate.evidence_ids)
    )


def test_worker_reconciles_actual_equal_length_arrow_frog_crop_cut_readings(tmp_path, monkeypatch):
    rows = _arrow_frog_crop_cut_rows()
    worker = _worker(tmp_path, monkeypatch, rows, bands=_current_subtitle_bands())

    evidence = worker._load_evidence()
    timestamps = range(540_800_000, 541_400_000, 100_000)

    assert len(evidence) == 12
    assert [
        (item.evidence_id, item.winner_evidence_id, item.frame_id, item.timestamp_us)
        for item in worker._suppressed_readings
    ] == [
        (f"lower-{timestamp}", f"upper-{timestamp}", f"frame-{timestamp}", timestamp)
        for timestamp in timestamps
    ]
    assert len(worker.evidence_path.read_text(encoding="utf-8").splitlines()) == 12
    candidates, _ = worker._fast_candidates(550_000_000, 200_000)
    proofs = {item.evidence_id: item.winner_evidence_id for item in worker._suppressed_readings}
    before = finalize_ocr_candidates(
        candidates, video_duration_us=550_000_000, evidence=evidence
    )
    after = finalize_ocr_candidates(
        candidates,
        video_duration_us=550_000_000,
        evidence=evidence,
        suppressed_evidence=proofs,
    )
    assert sorted(cue.text for cue in before.cues) == ["前毒蛙", "箭毒蛙"]
    assert [cue.text for cue in after.cues] == ["箭毒蛙"]
    assert not after.blockers
    assert after.cues[0].evidence_ids == next(
        cue.evidence_ids for cue in before.cues if cue.text == "箭毒蛙"
    )


@pytest.mark.parametrize("confidence", [0.90, 0.95, 0.9999])
@pytest.mark.parametrize(
    "partial_text,full_text",
    [
        ("前毒蛙", "箭毒蛙"),
        ("什么都改变不了啊你", "什么都改变不了啊你"),
        ("加热后的尸臭味可以飘向更远的地方", "加热后的尺臭味可以飘向更远的地方"),
    ],
)
def test_reliable_equal_length_readings_keep_their_consensus_votes(
    tmp_path, monkeypatch, confidence, partial_text, full_text
):
    rows = _arrow_frog_crop_cut_rows()
    for row in rows:
        if row["band"] == "lower":
            row.update(text=partial_text, confidence=confidence)
        else:
            row["text"] = full_text
    worker = _worker(tmp_path, monkeypatch, rows, bands=_current_subtitle_bands())

    evidence = worker._load_evidence()

    # Geometry proves that two crops share glyphs, not which high-confidence
    # transcription is right. Removing even identical votes can change the
    # representative selected for a changing line later in the occurrence.
    assert sorted((item.id, item.text, item.confidence) for item in evidence) == sorted(
        (row["id"], row["text"], row["confidence"]) for row in rows
    )
    assert not worker._suppressed_readings


def test_reciprocal_crop_cut_readings_survive_without_a_retained_complete_winner(
    tmp_path, monkeypatch
):
    rows = _reciprocal_overlap_crop_rows()
    bands = [
        {"name": "A", "x1": 0.0, "y1": 0.0, "x2": 0.70, "y2": 1.0, "scale": 0.9},
        {"name": "B", "x1": 0.30, "y1": 0.0, "x2": 1.0, "y2": 1.0, "scale": 0.9},
    ]
    worker = _worker(tmp_path, monkeypatch, rows, bands=bands)
    raw_before = worker.evidence_path.read_text(encoding="utf-8")

    evidence = worker._load_evidence()

    assert len(worker._suppressed_readings) == 0
    assert [item.id for item in evidence] == [row["id"] for row in rows]
    assert worker.evidence_path.read_text(encoding="utf-8") == raw_before


def test_every_reconciliation_suppression_winner_is_retained(tmp_path, monkeypatch):
    rows = _reciprocal_overlap_crop_rows()
    bands = [
        {"name": "A", "x1": 0.0, "y1": 0.0, "x2": 0.70, "y2": 1.0, "scale": 0.9},
        {"name": "B", "x1": 0.30, "y1": 0.0, "x2": 1.0, "y2": 1.0, "scale": 0.9},
    ]
    worker = _worker(tmp_path, monkeypatch, rows, bands=bands)

    evidence = worker._load_evidence()

    retained_ids = {item.id for item in evidence}
    assert all(item.winner_evidence_id in retained_ids for item in worker._suppressed_readings)


@pytest.mark.parametrize(
    "boundary",
    [
        "uncut",
        "separate-line",
        "different-frame",
        "source",
        "segment",
        "provider",
        "model",
        "scene",
        "single-unconfirmed",
        "full-one-character",
        "shorter-full",
    ],
)
def test_equal_length_crop_cut_reconciliation_keeps_existing_safety_boundaries(
    tmp_path, monkeypatch, boundary
):
    rows = _arrow_frog_crop_cut_rows()
    first_timestamp = rows[0]["timestamp_us"]
    for row in rows:
        if boundary == "full-one-character":
            row["text"] = "爹"
        elif boundary == "single-unconfirmed" and row["band"] == "upper":
            if row["timestamp_us"] != first_timestamp:
                row["confidence"] = 0.89
        elif row["band"] == "lower":
            if boundary == "uncut":
                row["box"] = [0.44290502793296094, 0.90, 0.11418994413407824, 0.04279411764705876]
            elif boundary == "separate-line":
                row["box"] = [0.70, 0.885, 0.08, 0.04279411764705876]
            elif boundary == "different-frame":
                row["frame_id"] += "-other"
            elif boundary == "source":
                row["media_id"] = "other-source"
            elif boundary == "segment":
                row["scan_segment_index"] = 1
            elif boundary == "provider":
                row["provider_id"] = "other-provider"
            elif boundary == "model":
                row["model_id"] = "other-model"
            elif boundary == "scene":
                row["evidence_kind"] = "scene"
            elif boundary == "shorter-full":
                row["text"] = "前毒蛙"
        elif boundary == "shorter-full":
            row["text"] = "箭毒"

    worker = _worker(tmp_path, monkeypatch, rows, bands=_current_subtitle_bands())

    evidence = worker._load_evidence()

    assert len(evidence) == 12
    assert not worker._suppressed_readings


@pytest.mark.parametrize(
    "short,full,short_box,full_box",
    [
        (
            "会的路H",
            "在去太南小会的路上",
            [0.50978, 0.90054, 0.08865, 0.03446],
            [0.39128, 0.90212, 0.21587, 0.05135],
        ),
        (
            "这里住的",
            "这里是散修们聚集的一处秘境",
            [0.34681, 0.90514, 0.20859, 0.02757],
            [0.34436, 0.90212, 0.30972, 0.05135],
        ),
        (
            "田物",
            "交换了不少有用之物",
            [0.53846, 0.90743, 0.06388, 0.02297],
            [0.38972, 0.90212, 0.21743, 0.05421],
        ),
    ],
)
def test_worker_reconciles_actual_cut_readings_before_grouping(
    tmp_path,
    monkeypatch,
    short,
    full,
    short_box,
    full_box,
):
    worker = _worker(tmp_path, monkeypatch, _rows(short, full, short_box, full_box))
    evidence = worker._load_evidence()
    assert [item.text for item in evidence] == [full, full]
    # The original readings stay in the immutable/checkpoint input, not rewritten.
    assert short in worker.evidence_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["爹", "娘", "不过"])
def test_short_dialogue_without_same_frame_complete_counterpart_survives(
    tmp_path,
    monkeypatch,
    text,
):
    rows = _rows(text, "下一句不同的对白", [0.48, 0.90, 0.04, 0.03], [0.35, 0.90, 0.3, 0.055])
    for row in rows:
        if row["band"] == "lower":
            row["timestamp_us"] += 600_000
            row["frame_id"] += "-next"
    evidence = _worker(tmp_path, monkeypatch, rows)._load_evidence()
    assert sum(item.text == text for item in evidence) == 2


@pytest.mark.parametrize(
    "boundary", ["source", "segment", "model", "region", "scene", "uncut", "unconfirmed"]
)
def test_reconciliation_does_not_guess_from_time_or_longer_text(
    tmp_path,
    monkeypatch,
    boundary,
):
    rows = _rows(
        "田物",
        "交换了不少有用之物",
        [0.53846, 0.90743, 0.06388, 0.02297],
        [0.38972, 0.90212, 0.21743, 0.05421],
    )
    for row in rows:
        if row["band"] == "lower":
            if boundary == "source":
                row["media_id"] = "media-other"
            elif boundary == "segment":
                row["scan_segment_index"] = 1
            elif boundary == "model":
                row["model_id"] = "different-recognizer"
            elif boundary == "region":
                row["box"] = [0.1, 0.2, 0.3, 0.055]
            elif boundary == "unconfirmed":
                row["confidence"] = 0.65
        elif boundary == "scene":
            row["band"] = "scene"
            row["evidence_kind"] = "scene"
        elif boundary == "uncut":
            row["box"] = [0.53846, 0.87, 0.06388, 0.02297]
    evidence = _worker(tmp_path, monkeypatch, rows)._load_evidence()
    assert len(evidence) == 4


def test_all_same_frame_crop_fragments_are_reconciled_not_only_the_first(tmp_path, monkeypatch):
    rows = _rows(
        "会的路H", "在去太南小会的路上", [0.51, 0.90, 0.089, 0.034], [0.39, 0.902, 0.218, 0.052]
    )
    rows += [
        dict(row, id=row["id"] + "-second", text="太南小", box=[0.43, 0.907, 0.08, 0.023])
        for row in rows
        if row["band"] == "upper"
    ]
    worker = _worker(tmp_path, monkeypatch, rows)
    assert len(worker._load_evidence()) == 2
    assert len(worker._suppressed_readings) == 4


def test_same_full_text_in_a_different_model_does_not_corroborate_single_frame(
    tmp_path,
    monkeypatch,
):
    rows = _rows(
        "田物", "交换了不少有用之物", [0.538, 0.907, 0.064, 0.023], [0.389, 0.902, 0.219, 0.055]
    )
    rows[-1]["model_id"] = "other-model"
    assert len(_worker(tmp_path, monkeypatch, rows)._load_evidence()) == 4


@pytest.mark.parametrize("boundary", ["different-line", "region", "source", "frame"])
def test_merge_requires_same_visual_occurrence_when_evidence_is_available(boundary):
    first = SubtitleCandidate(
        "first", TimeRange(0, 1_000_000), "他们就在这里", 0.99, ("upper-1",), "paddle-fast:upper"
    )
    other = replace(first, id="other", evidence_ids=("lower-1",), source="paddle-fast:lower")
    rows = [
        SubtitleEvidence(
            "upper-1",
            "frame",
            500_000,
            "upper",
            first.text,
            0.99,
            "paddle-fast",
            "model",
            NormalizedBox(0.3, 0.9, 0.4, 0.05),
            source_scope="source:0",
        )
    ]
    if boundary == "different-line":
        other = replace(other, text="我们立刻离开")
    row = replace(rows[0], id="lower-1", band="lower", text=other.text)
    if boundary == "region":
        row = replace(row, box=NormalizedBox(0.3, 0.6, 0.4, 0.05))
    elif boundary == "source":
        row = replace(row, source_scope="source:1")
    elif boundary == "frame":
        row = replace(row, frame_id="next-frame", timestamp_us=600_000)
    rows.append(row)
    assert (
        len(merge_band_candidates((first,), (other,), id_factory=lambda: "merged", evidence=rows))
        == 2
    )


def test_independent_repeated_short_occurrences_remain_separate():
    cues = tuple(
        SubtitleCue(
            str(index),
            index,
            TimeRange(start, start + 500_000),
            "爹",
            "zh",
            source="ocr_review",
            visual_box=(0.48, 0.9, 0.04, 0.05),
            visual_box_source="ocr",
        )
        for index, start in enumerate((0, 1_000_000, 4_000_000), 1)
    )
    assert len(collapse_partial_ocr_cues(cues)) == 3


def test_unresolved_overlapping_lines_remain_reviewable_not_silently_dropped():
    first = SubtitleCandidate(
        "first", TimeRange(0, 3_000_000), "我们立刻離开", 0.99, ("one",), "upper"
    )
    second = replace(
        first,
        id="second",
        text="他们就在这里",
        evidence_ids=("two",),
        range=TimeRange(1_000_000, 4_000_000),
    )
    result = finalize_ocr_candidates((first, second), video_duration_us=5_000_000)
    assert len(result.cues) == 2
    assert all(cue.review_state == "review" for cue in result.cues)


def test_similar_complete_consecutive_lines_are_not_fuzzy_deduplicated():
    first = SubtitleCandidate(
        "first", TimeRange(0, 1_100_000), "今日见你出嫁十分高兴", 0.99, ("one",), "upper"
    )
    second = replace(
        first,
        id="second",
        text="明日见你出嫁十分高兴",
        evidence_ids=("two",),
        range=TimeRange(900_000, 2_000_000),
    )
    evidence = tuple(
        SubtitleEvidence(
            key,
            key,
            timestamp,
            "upper",
            candidate.text,
            0.99,
            "paddle-fast",
            "model",
            NormalizedBox(0.3, 0.9, 0.4, 0.05),
        )
        for key, timestamp, candidate in (("one", 500_000, first), ("two", 1_500_000, second))
    )
    result = finalize_ocr_candidates(
        (first, second), video_duration_us=2_000_000, evidence=evidence
    )
    assert [cue.text for cue in result.cues] == [first.text, second.text]
    assert result.cues[0].range.end_us == result.cues[1].range.start_us


def test_new_aggregation_does_not_resume_old_result_checkpoint(tmp_path, monkeypatch):
    from ducai_studio.application.ocr_pipeline import OCR_AGGREGATION_VERSION

    worker = _worker(tmp_path, monkeypatch, [])
    worker.config["ocr_aggregation_version"] = OCR_AGGREGATION_VERSION
    worker.spec = {"settings_fingerprint": "new-policy"}
    artifact = tmp_path / "old.ndjson"
    artifact.write_text("old evidence", encoding="utf-8")
    worker._restore_checkpoint(
        {"payload": {"stage": "quality", "cursor_us": 900_000}, "artifact": {"uri": str(artifact)}}
    )
    assert worker.cursor_us == 0 and not worker.evidence_path.exists()
    assert artifact.read_text(encoding="utf-8") == "old evidence"


@pytest.mark.parametrize(
    "metadata",
    [
        {"confidence": 0.98, "evidence_ids": ["first", "second", "first"]},
        {},
        {"confidence": "bad", "evidence_ids": [123]},
        {"confidence": True, "evidence_ids": None},
    ],
)
def test_qa_provenance_is_optional_validated_and_timestamp_bound(metadata):
    cue = SubtitleCue("cue", 1, TimeRange(0, 1_000_000), "保留", "zh")
    snapshot = ProjectSnapshot("fixture", "Fixture", 1, source_cues=(cue,))
    region = {"index": 1, "start_us": 0, "end_us": 1_000_000, **metadata}
    updated = _apply_ocr_cue_regions(snapshot, {"cue_regions": [region]}).source_cues[0]
    if metadata.get("confidence") == 0.98:
        assert updated.confidence == 0.98 and updated.evidence_ids == ("first", "second")
    else:
        assert updated.confidence is None and updated.evidence_ids == ()
    region["start_us"] = 1
    updated = _apply_ocr_cue_regions(snapshot, {"cue_regions": [region]}).source_cues[0]
    assert updated.confidence is None and updated.evidence_ids == ()


def test_multiband_merge_consumes_all_matching_fragments_of_one_occurrence():
    full = SubtitleCandidate(
        "full",
        TimeRange(3_500_000, 5_200_000),
        "在去太南小会的路上",
        0.999,
        ("full-frame",),
        "paddle-fast:lower",
    )
    fragments = tuple(
        replace(
            full,
            id=f"partial-{index}",
            text=text,
            range=TimeRange(start, end),
            confidence=0.92,
            evidence_ids=(f"partial-frame-{index}",),
            source="paddle-fast:upper",
        )
        for index, (text, start, end) in enumerate(
            [
                ("在去太南", 3_500_000, 4_300_000),
                ("会的路上", 4_000_000, 4_600_000),
                ("的路上", 4_600_000, 5_200_000),
            ]
        )
    )
    ids = count()
    merged = merge_band_candidates(fragments, (full,), id_factory=lambda: str(next(ids)))
    assert len(merged) == 1
    assert merged[0].text == full.text
    assert set(merged[0].evidence_ids) == {
        *full.evidence_ids,
        *(key for fragment in fragments for key in fragment.evidence_ids),
    }


def test_review_import_reuses_proofs_without_deleting_manual_or_unproven_cues():
    full = SubtitleCue(
        "full",
        1,
        TimeRange(26_150_000, 28_000_000),
        "交换了不少有用之物",
        "zh",
        source="ocr_review",
        evidence_ids=("full-1", "full-2"),
    )
    short = replace(
        full,
        id="short",
        index=2,
        text="田物",
        range=TimeRange(26_050_000, 26_150_000),
        evidence_ids=("short-1", "short-2"),
    )
    proofs = {"short-1": "full-1", "short-2": "full-2"}
    result = collapse_partial_ocr_cues((short, full), suppressed_evidence=proofs)
    assert len(result) == 1 and result[0].id == "full"
    assert result[0].range == TimeRange(26_050_000, 28_000_000)
    assert len(collapse_partial_ocr_cues((short, full))) == 2
    assert (
        len(
            collapse_partial_ocr_cues(
                (replace(short, source="manual"), full), suppressed_evidence=proofs
            )
        )
        == 2
    )
    assert (
        len(
            collapse_partial_ocr_cues(
                (short, replace(full, evidence_ids=())), suppressed_evidence=proofs
            )
        )
        == 2
    )


@pytest.mark.parametrize("boundary", ["partial-proof", "missing-winner", "two-winners"])
def test_final_candidate_proof_collapse_keeps_unproven_occurrences(boundary):
    winner = SubtitleCandidate(
        "full", TimeRange(0, 1_000_000), "箭毒蛙", 0.999, ("full-1", "full-2"), "paddle:upper"
    )
    partial = replace(
        winner, id="partial", text="前毒蛙", confidence=0.88,
        evidence_ids=("partial-1", "partial-2"), source="paddle:lower",
    )
    proofs = {"partial-1": "full-1", "partial-2": "full-2"}
    candidates = (winner, partial)
    if boundary == "partial-proof":
        proofs.pop("partial-2")
    elif boundary == "missing-winner":
        proofs["partial-2"] = "missing"
    else:
        other = replace(winner, id="other", text="另一句", evidence_ids=("other-1", "other-2"))
        candidates = (*candidates, other)
        proofs["partial-2"] = "other-2"

    result = finalize_ocr_candidates(
        candidates, video_duration_us=1_000_000, evidence=_candidate_evidence(candidates),
        suppressed_evidence=proofs,
    )

    assert sorted(cue.text for cue in result.cues) == sorted(item.text for item in candidates)
    assert result.blockers


def test_review_proof_collapse_does_not_guess_an_ambiguously_owned_winner():
    winner = SubtitleCue(
        "winner", 1, TimeRange(0, 1_000_000), "箭毒蛙", "zh",
        source="ocr_review", evidence_ids=("shared-winner",),
    )
    other = replace(winner, id="other", index=2, text="独立的对白")
    partial = replace(
        winner, id="partial", index=3, text="前毒蛙", evidence_ids=("partial",),
    )
    result = collapse_proven_ocr_cues(
        (winner, other, partial), suppressed_evidence={"partial": "shared-winner"}
    )
    assert {cue.id for cue in result} == {"winner", "other", "partial"}


@pytest.mark.parametrize("boundary", ["missing", "different", "mixed-winner", "mixed-loser"])
def test_final_proof_collapse_requires_complete_matching_source_scopes(boundary):
    winner = SubtitleCandidate(
        "full", TimeRange(0, 1_000_000), "箭毒蛙", 0.999,
        ("full-1", "full-2"), "paddle:upper",
    )
    partial = replace(
        winner, id="partial", text="前毒蛙", confidence=0.88,
        evidence_ids=("partial-1", "partial-2"), source="paddle:lower",
    )
    candidates = (winner, partial)
    evidence = _candidate_evidence(candidates)
    if boundary == "missing":
        evidence = tuple(row for row in evidence if row.id != "partial-2")
    else:
        changed_ids = {
            "different": {"partial-1", "partial-2"},
            "mixed-winner": {"full-2"},
            "mixed-loser": {"partial-2"},
        }[boundary]
        evidence = tuple(
            replace(row, source_scope="source:1") if row.id in changed_ids else row
            for row in evidence
        )

    assert collapse_proven_ocr_candidates(
        candidates, evidence=evidence,
        suppressed_evidence={"partial-1": "full-1", "partial-2": "full-2"},
    ) == candidates


def test_proof_collapse_preserves_occurrences_sharing_a_loser_reading():
    winner = SubtitleCandidate(
        "full", TimeRange(0, 1_000_000), "箭毒蛙", 0.999, ("full",), "paddle:upper"
    )
    partial = replace(winner, id="partial", text="前毒蛙", evidence_ids=("shared-partial",))
    other = replace(partial, id="other", text="独立的对白")
    candidates = (winner, partial, other)

    assert collapse_proven_ocr_candidates(
        candidates, evidence=_candidate_evidence(candidates),
        suppressed_evidence={"shared-partial": "full"},
    ) == candidates


def test_whole_occurrence_collapse_only_extends_the_retained_winners_range():
    winner = SubtitleCandidate(
        "full", TimeRange(100_000, 900_000), "箭毒蛙", 0.999,
        ("full-1", "full-2"), "paddle:upper",
    )
    partial = replace(
        winner, id="partial", range=TimeRange(0, 1_000_000), text="前毒蛙",
        confidence=0.88, evidence_ids=("partial-1", "partial-2"), source="paddle:lower",
    )
    candidates = (partial, winner)

    assert collapse_proven_ocr_candidates(
        candidates, evidence=_candidate_evidence(candidates),
        suppressed_evidence={"partial-1": "full-1", "partial-2": "full-2"},
    ) == (replace(winner, range=TimeRange(0, 1_000_000)),)


def test_strict_review_collapse_keeps_legacy_containment_without_a_proof():
    winner = SubtitleCue(
        "full", 1, TimeRange(0, 1_000_000), "这里是散修们聚集的一处秘境", "zh",
        source="ocr_review", evidence_ids=("full",), visual_box=(0.3, 0.9, 0.4, 0.05),
        visual_box_source="ocr",
    )
    partial = replace(
        winner, id="partial", index=2, text="这里是散修", evidence_ids=("partial",),
        visual_box=(0.4, 0.9, 0.2, 0.05),
    )
    cues = (winner, partial)
    assert len(collapse_partial_ocr_cues(cues)) == 1
    assert collapse_proven_ocr_cues(cues, suppressed_evidence={}) == cues


def test_targeted_rescan_collapses_proven_partial_before_overlap_ranking(tmp_path, monkeypatch):
    worker = _worker(
        tmp_path, monkeypatch, _arrow_frog_crop_cut_rows(), bands=_current_subtitle_bands()
    )
    candidates, _ = worker._fast_candidates(550_000_000, 200_000)
    candidates = tuple(
        replace(item, range=TimeRange(540_500_000, 541_900_000))
        if item.text == "前毒蛙" else item for item in candidates
    )
    worker.config.update(rescan_cue_start_us=540_500_000, rescan_cue_end_us=542_000_000)
    events = []
    monkeypatch.setattr(worker, "send", lambda kind, payload: events.append((kind, payload)))

    assert worker._finish_targeted_rescan(candidates) == 0
    assert events[-1][0] == "succeeded"
    result = events[-1][1]["rescan"]
    assert result["text"] == "箭毒蛙"
    assert result["evidence_ids"] == list(next(
        item.evidence_ids for item in candidates if item.text == "箭毒蛙"
    ))


def test_qa_distinguishes_deferred_proofs_from_votes_retained_in_cues(tmp_path, monkeypatch):
    worker = _worker(
        tmp_path, monkeypatch, _arrow_frog_crop_cut_rows(), bands=_current_subtitle_bands()
    )
    evidence = worker._load_evidence()
    deferred_ids = {item.evidence_id for item in worker._deferred_readings}
    worker.config["duration_us"] = 550_000_000
    assert len(deferred_ids) == 6
    upper_ids = tuple(row.id for row in evidence if row.band == "upper")
    retained_partial = min(deferred_ids)
    cue = SubtitleCue(
        "full", 1, TimeRange(540_600_000, 541_500_000), "箭毒蛙", "zh",
        source="ocr", evidence_ids=(*upper_ids, retained_partial),
    )
    cues = (cue,)
    path = tmp_path / "review.srt"
    path.write_text(render_srt(cues), encoding="utf-8-sig")
    coverage = measure_hard_sub_coverage(evidence, cues, video_duration_us=550_000_000)

    qa = worker._qa_payload(
        status="REVIEW", output_path=path, cue_count=1, coverage=coverage.as_json(),
        blockers=["needs_review"], warnings=[], cues=cues,
    )

    assert qa["proof_only_collapse"] is True
    assert set(qa["deferred_reading_ids"]) == deferred_ids
    assert qa["retained_deferred_reading_ids"] == [retained_partial]
    assert set(qa["cue_regions"][0]["evidence_ids"]) == {*upper_ids, retained_partial}
    assert len(qa["suppressed_readings"]) == 6
