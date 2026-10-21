from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest


def _fixture():
    path = Path(__file__).parents[1] / "fixtures" / "ocr-speed-benchmark.py"
    spec = importlib.util.spec_from_file_location("ocr_speed_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_evidence():
    return [
        {
            "id": "ocr-timeline-evidence-1",
            "frame_id": "timeline-000000400000-segment-0",
            "timestamp_us": 400_000,
            "band": "upper",
            "text": "保留识别文本",
            "confidence": 0.98,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6_small_rec",
            "box": [0.1, 0.1, 0.2, 0.1],
            "segment_count": 1,
            "evidence_kind": "global",
            "source_timestamp_us": 10_400_000,
            "base_timestamp_us": 10_400_000,
            "source_evidence_id": "ocr-source-1",
            "source_frame_id": "frame-000010400000",
            "scan_segment_index": 0,
            "media_id": "media-1",
            "media_path": "E:/frozen-input.mp4",
        },
        {
            "id": "ocr-watermark-evidence-1",
            "frame_id": "timeline-000000800000-segment-0",
            "timestamp_us": 800_000,
            "band": "lower",
            "text": "bilibili",
            "confidence": 0.96,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6_small_rec",
            "box": [0.85, 0.05, 0.1, 0.05],
            "ignored_overlay": True,
            "evidence_kind": "global",
            "source_timestamp_us": 10_800_000,
            "base_timestamp_us": 10_800_000,
            "source_evidence_id": "ocr-source-2",
            "source_frame_id": "frame-000010800000",
            "scan_segment_index": 0,
            "media_id": "media-1",
            "media_path": "E:/frozen-input.mp4",
        },
        {
            "id": "scene-probe-1",
            "record_kind": "scene_probe",
            "frame_id": "timeline-000001200000-segment-0",
            "timestamp_us": 1_200_000,
            "region": [0.0, 0.0, 1.0, 1.0],
            "provider_id": "paddle-fast",
            "source_timestamp_us": 11_200_000,
            "base_timestamp_us": 11_200_000,
            "source_evidence_id": "scene-source-1",
            "source_frame_id": "frame-000011200000",
            "scan_segment_index": 0,
            "media_id": "media-1",
            "media_path": "E:/frozen-input.mp4",
        },
    ]


def _write_ndjson(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _runs(tmp_path, change, *, evidence_change=None, evidence_state="present"):
    qa = {"status": "PASS", "blockers": [], "warnings": [], "cue_count": 1,
          "coverage_ratio": 1.0, "covered_timestamps": 2,
          "subtitle_positive_timestamps": 2, "timeline_continuity_passes": True,
          "suppressed_readings": [], "unmatched_samples": [],
          "cue_regions": [{"id": "cue-1", "start_us": 0, "end_us": 1_000_000,
                           "box": [.1, .1, .2, .1], "confidence": .98,
                           "evidence_ids": ["evidence-1"], "review_state": "accepted"}],
          "watermark_regions": [{"id": "mark-1", "start_us": 0, "end_us": 2_000_000,
                                 "box": [.1, .1, .2, .1], "sample_count": 3,
                                 "evidence_ids": ["watermark-evidence-1"]}]}
    for name, seconds in (("baseline", 100.0), ("candidate", 80.0)):
        root = tmp_path / name
        root.mkdir()
        current = deepcopy(qa)
        if name == "candidate":
            change(current)
        evidence = _raw_evidence()
        if name == "candidate" and evidence_change is not None:
            evidence_change(evidence)
        evidence_path = root / "ocr-evidence-working.ndjson"
        if name != "candidate" or evidence_state == "present":
            _write_ndjson(evidence_path, evidence)
        elif evidence_state == "corrupt":
            evidence_path.write_text("{not-json}\n", encoding="utf-8")
        qa_path = root / "qa.json"
        qa_path.write_text(json.dumps(current), encoding="utf-8")
        (root / "report.json").write_text(json.dumps({"seconds": seconds,
            "qa_path": str(qa_path), "configuration_fingerprint": "frozen-input",
            "srt_sha256": "same-output", "frame_sizes": [[1000, 500]],
            "raw_evidence_path": str(evidence_path)}), encoding="utf-8")
    return _fixture().compare_runs(tmp_path / "candidate", tmp_path / "baseline")


@pytest.mark.parametrize("key, value", [
    ("warnings", ["cue_review_required"]),
    ("timeline_continuity_passes", False),
    ("unmatched_samples", [{"timestamp_us": 100_000, "texts": ["娘"]}]),
    ("suppressed_readings", [{"reason": "same_frame_crop_cut"}]),
])
def test_benchmark_checks_all_qa_decisions_not_only_status_and_coverage(tmp_path, key, value):
    result = _runs(tmp_path, lambda qa: qa.update({key: value}))
    assert not result["parity"]
    assert key in result["qa_decision_changes"]


def test_benchmark_audits_numeric_region_deltas_within_tolerance(tmp_path):
    def change(qa):
        qa["cue_regions"][0].update(confidence=.97, box=[.101, .1, .2, .1])

    result = _runs(tmp_path, change)
    assert result["parity"]
    assert result["numeric_region_audit"]["confidence_changed_regions"] == 1
    assert result["numeric_region_audit"]["maximum_confidence_delta"] == pytest.approx(.01)
    assert result["maximum_region_edge_delta_pixels"] == pytest.approx(1.0)


def _exact_comparison(tmp_path):
    for name in ("baseline", "candidate"):
        root = tmp_path / name
        path = root / "report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        srt = root / "subtitles.srt"
        srt.write_bytes("1\n00:00:00,000 --> 00:00:01,000\n娘\n".encode())
        report.update(srt_path=str(srt), srt_sha256=sha256(srt.read_bytes()).hexdigest())
        path.write_text(json.dumps(report), encoding="utf-8")
    return _fixture().compare_exact_runs(tmp_path / "candidate", tmp_path / "baseline")


def test_exact_gate_accepts_identical_data_and_ignores_only_run_metadata(tmp_path):
    _runs(tmp_path, lambda qa: qa.update(job_id="different-run", output_path="different-path",
                                      output_mtime_ns=123, output_size=456,
                                      output_sha256="run-metadata"))
    result = _exact_comparison(tmp_path)
    assert result["parity"] and result["exact_normalized_qa"]
    assert result["exact_raw_evidence"] and result["exact_srt_bytes"]


@pytest.mark.parametrize("field, value", [("confidence", .97), ("box", [.101, .1, .2, .1])])
def test_exact_gate_rejects_even_tolerated_qa_numeric_changes(tmp_path, field, value):
    _runs(tmp_path, lambda qa: qa["cue_regions"][0].update({field: value}))
    result = _exact_comparison(tmp_path)
    assert result["tolerant_parity"] and not result["parity"]
    assert not result["exact_normalized_qa"]


@pytest.mark.parametrize("field, value", [("confidence", .97), ("box", [.101, .1, .2, .1])])
def test_exact_gate_rejects_even_tolerated_raw_numeric_changes(tmp_path, field, value):
    _runs(tmp_path, lambda _qa: None, evidence_change=lambda rows: rows[0].update({field: value}))
    result = _exact_comparison(tmp_path)
    assert result["tolerant_parity"] and not result["parity"]
    assert not result["exact_raw_evidence"]


def test_exact_gate_does_not_trust_missing_or_changed_srt_file(tmp_path):
    _runs(tmp_path, lambda _qa: None)
    result = _exact_comparison(tmp_path)
    candidate = tmp_path / "candidate"
    report = json.loads((candidate / "report.json").read_text(encoding="utf-8"))
    Path(report["srt_path"]).write_bytes(b"changed output")
    result = _fixture().compare_exact_runs(candidate, tmp_path / "baseline")
    assert result["tolerant_parity"] and not result["parity"]
    assert not result["srt_report_hashes_valid"] and not result["exact_srt_bytes"]
    Path(report["srt_path"]).unlink()
    assert not _fixture().compare_exact_runs(candidate, tmp_path / "baseline")["parity"]


@pytest.mark.parametrize("group", ["cue_regions", "watermark_regions"])
def test_benchmark_rejects_changed_region_evidence_identity(tmp_path, group):
    result = _runs(
        tmp_path,
        lambda qa: qa[group][0].update(evidence_ids=["different-evidence"]),
    )
    assert not result["parity"]
    assert f"{group}.evidence_ids" in result["semantic_region_changes"]
    assert "evidence_ids_changed_regions" not in result["numeric_region_audit"]


@pytest.mark.parametrize(
    ("row_index", "field", "value"),
    [
        (0, "text", "改变识别文本"),
        (0, "id", "ocr-timeline-different"),
        (0, "frame_id", "timeline-000000600000-segment-0"),
        (0, "timestamp_us", 600_000),
        (0, "band", "lower"),
        (0, "segment_count", 2),
        (0, "model_id", "different-model"),
        (0, "source_timestamp_us", 10_600_000),
        (0, "source_evidence_id", "different-source"),
        (0, "evidence_kind", "burst"),
        (2, "record_kind", "different_probe_kind"),
    ],
)
def test_benchmark_rejects_raw_evidence_semantic_or_scope_changes(
    tmp_path, row_index, field, value
):
    result = _runs(
        tmp_path,
        lambda _qa: None,
        evidence_change=lambda rows: rows[row_index].update({field: value}),
    )
    assert not result["parity"]
    assert field in result["semantic_evidence_changes"]


def test_benchmark_rejects_raw_evidence_order_changes(tmp_path):
    result = _runs(
        tmp_path,
        lambda _qa: None,
        evidence_change=lambda rows: rows.reverse(),
    )
    assert not result["parity"]
    assert result["raw_evidence_count_equal"]
    assert result["semantic_evidence_changes"]


@pytest.mark.parametrize(
    ("change", "expected_count"),
    [
        (lambda rows: rows.pop(), 2),
        (lambda rows: rows.append(deepcopy(rows[-1])), 4),
    ],
)
def test_benchmark_rejects_raw_evidence_count_changes(tmp_path, change, expected_count):
    result = _runs(
        tmp_path,
        lambda _qa: None,
        evidence_change=change,
    )
    assert not result["parity"]
    assert not result["raw_evidence_count_equal"]
    assert result["raw_evidence_rows"] == {
        "baseline": 3,
        "candidate": expected_count,
    }


@pytest.mark.parametrize("evidence_state", ["missing", "corrupt"])
def test_benchmark_rejects_unavailable_raw_evidence_artifact(tmp_path, evidence_state):
    result = _runs(
        tmp_path,
        lambda _qa: None,
        evidence_state=evidence_state,
    )
    assert not result["parity"]
    assert not result["raw_evidence_available"]


def test_benchmark_audits_raw_numeric_deltas_within_tolerance(tmp_path):
    def change(rows):
        rows[0].update(confidence=.97, box=[.101, .1, .2, .1])

    result = _runs(tmp_path, lambda _qa: None, evidence_change=change)
    assert result["parity"]
    assert result["numeric_evidence_audit"]["confidence_changed_rows"] == 1
    assert result["numeric_evidence_audit"]["box_changed_rows"] == 1
    assert result["numeric_evidence_audit"]["maximum_confidence_delta"] == pytest.approx(.01)
    assert result["maximum_evidence_edge_delta_pixels"] == pytest.approx(1.0)
    assert result["raw_evidence_geometry_within_two_pixels"]


def test_benchmark_checks_watermark_semantics_and_actual_box_edges(tmp_path):
    def change(qa):
        qa["watermark_regions"][0]["box"] = [.102, .1, .202, .1]

    result = _runs(tmp_path, change)
    assert not result["parity"]
    assert result["maximum_region_edge_delta_pixels"] == pytest.approx(4.0)


def test_benchmark_rejects_watermark_timing_or_region_count_changes(tmp_path):
    result = _runs(tmp_path, lambda qa: qa["watermark_regions"][0].update(start_us=1))
    assert not result["parity"]
    assert "watermark_regions.start_us" in result["semantic_region_changes"]


def test_benchmark_rejects_extra_regions_even_if_existing_cues_match(tmp_path):
    result = _runs(tmp_path, lambda qa: qa["cue_regions"].append(deepcopy(qa["cue_regions"][0])))
    assert not result["parity"]
    assert not result["region_count_equal"]


def test_benchmark_ignores_only_artifact_location_and_time(tmp_path):
    result = _runs(tmp_path, lambda qa: qa.update(job_id="new-job", output_path="new-output",
        output_mtime_ns=100, output_sha256="same-output", output_size=100))
    assert result["parity"]
    assert not result["qa_decision_changes"]
