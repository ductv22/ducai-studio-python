from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

import pytest

from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.subtitles import NormalizedBox, RescueWindow, SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.job_manager.service import _apply_ocr_cue_regions
from ducai_studio.workers import ocr_worker as ocr_worker_module
from ducai_studio.workers.ocr_worker import EngineHostClient, OcrWorker
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def finalization_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = OcrWorker("finalization")
    worker.stage = "finalize"
    worker.cursor_us = 1_000_000
    worker.config = {"duration_us": 1_000_000, "scene_text_enabled": False,
                     "media_path": str(tmp_path / "source.mp4"),
                     "output_path": str(tmp_path / "result_ZH_FINAL.srt")}
    worker._append_evidence([
        {"id": f"e-{timestamp}", "frame_id": f"f-{timestamp}", "timestamp_us": timestamp,
         "band": "upper", "text": "你好", "confidence": .99, "provider_id": "paddle-fast",
         "model_id": "same-model", "box": [.3, .9, .3, .05]}
        for timestamp in (0, 400_000, 800_000)
    ])
    messages = []

    def capture(kind, payload, *, event_id=None):
        message = WorkerMessage(PROTOCOL_VERSION, worker.job_id, event_id or str(len(messages)),
                                kind, payload)
        messages.append(message)
        return message

    monkeypatch.setattr(worker, "send", capture)
    return worker, messages


def test_finalization_progress_pause_resume_preserves_safe_evidence(tmp_path, monkeypatch):
    worker, messages = finalization_worker(tmp_path, monkeypatch)
    original = worker.evidence_path.read_bytes()
    for action in ("pause", "resume"):
        worker.commands.put(WorkerMessage(PROTOCOL_VERSION, worker.job_id, action, "control", {
            "action": action, "command_id": action,
        }))
    assert worker._finalization_step("Đối chiếu bằng chứng", lambda: "done") == "done"
    assert [message.kind for message in messages] == ["progress", "checkpoint", "paused", "resumed"]
    progress = messages[0].payload
    assert progress["phase"] == "finalize" and progress["eta_ms"] is None
    assert progress["phase_detail"] == "Đối chiếu bằng chứng"
    checkpoint = messages[1].payload
    assert checkpoint["payload"]["stage"] == "finalize"
    assert Path(checkpoint["artifact_path"]).read_bytes() == original
    assert worker._finalization_detail == ""


def test_finalization_cancel_checkpoint_restores_without_rescanning(tmp_path, monkeypatch):
    worker, messages = finalization_worker(tmp_path, monkeypatch)
    worker.commands.put(WorkerMessage(PROTOCOL_VERSION, worker.job_id, "cancel", "control", {
        "action": "cancel", "command_id": "cancel",
    }))
    with pytest.raises(ocr_worker_module.WorkerStopped):
        worker._finalization_step("Đang ghép cue", lambda: pytest.fail("cancel must stop work"))
    checkpoint = next(message.payload for message in messages if message.kind == "checkpoint")
    worker._restore_checkpoint({"payload": checkpoint["payload"], "artifact": {
        "uri": checkpoint["artifact_path"],
    }})
    assert worker.stage == "finalize"
    for name in ("_scan_fast", "_scan_quality", "_scan_scene_quality", "_scan_scene_text"):
        monkeypatch.setattr(worker, name, lambda *_a, **_k: pytest.fail("must not rescan frames"))
    outcome = worker._execute()
    assert outcome == 0
    assert messages[-1].kind == "succeeded"
    assert "你好" in (tmp_path / "result_ZH_FINAL.srt").read_text(encoding="utf-8-sig")
    assert any(message.kind == "progress" and message.payload.get("phase") == "finalize"
               for message in messages)


class _RecordingScanPlanEngine:
    def __init__(self) -> None:
        self.mode = "fast"
        self.requests: list[dict] = []
        self.opened_media_paths: list[str] = []

    def request(self, action: str, payload: dict) -> dict:
        if action == "open":
            self.mode = str(payload["mode"])
            self.opened_media_paths.append(str(payload["media_path"]))
            return {}
        assert action == "recognize"
        self.requests.append(json.loads(json.dumps(payload)))
        rows = []
        band_timestamps = payload.get("band_timestamps_us", {})
        evidence_kinds = payload.get("evidence_kinds_by_band", {})
        provider_id = (
            "paddle-quality" if self.mode == "quality" else "paddle-fast"
        )
        for timestamp_us in payload["timestamps_us"]:
            for band in payload["bands"]:
                name = band["name"]
                scene_band = band.get("reading_mode", name) == "scene"
                if name == "lower" or (
                    band_timestamps and timestamp_us not in band_timestamps[name]
                ):
                    continue
                text = "\u5220\u9664\u533a\u6587\u5b57" if timestamp_us == 15_000_000 else (
                    "\u573a\u666f\u6587\u5b57" if scene_band else "\u4ed9"
                )
                frame_id = f"frame-{timestamp_us:012d}"
                digest = sha256(
                    f"{provider_id}|{frame_id}|{name}|{text}".encode()
                ).hexdigest()[:24]
                row = {
                    "id": f"ocr-{digest}",
                    "frame_id": frame_id,
                    "timestamp_us": timestamp_us,
                    "band": "scene" if scene_band else name,
                    "text": text,
                    "confidence": 0.99,
                    "provider_id": provider_id,
                    "model_id": f"recording-{self.mode}",
                    "box": [0.1, 0.2, 0.2, 0.5],
                    "evidence_kind": evidence_kinds.get(
                        name, payload.get("evidence_kind", "global")
                    ),
                }
                if scene_band:
                    row.update(
                        {
                            "orientation": "vertical",
                            "style_hint": "calligraphy",
                            "panel_box": [0.08, 0.18, 0.24, 0.58],
                            "background_color": "#111111",
                            "scene_confidence": 0.99,
                        }
                    )
                rows.append(row)
        changes = payload.get("change_timestamps_us", [])
        events = changes[:1] if payload.get("detect_changes") and changes else []
        return {
            "evidence": rows,
            "watermark_detections": [],
            "event_timestamps_us": events,
            "scene_event_timestamps_us": events,
            "processed_until_us": payload["timestamps_us"][-1],
            "frame_count": len(payload["timestamps_us"]),
        }

    def close(self) -> None:
        pass


class _FiniteVideoEngine(_RecordingScanPlanEngine):
    def __init__(self, durations: dict[str, int]) -> None:
        super().__init__()
        self.durations = durations
        self.duration_us = 0

    def request(self, action: str, payload: dict) -> dict:
        if action == "open":
            super().request(action, payload)
            self.duration_us = self.durations[payload["media_path"]]
            return {"duration_us": self.duration_us, "fps": 25.0,
                    "frame_count": self.duration_us * 25 // 1_000_000}
        assert payload["timestamps_us"], "decoder cannot receive an empty batch"
        requested = [*payload["timestamps_us"], *payload.get("change_timestamps_us", [])]
        assert all(t <= self.duration_us for t in requested), "video_timestamp_out_of_range"
        return super().request(action, payload)


@pytest.mark.parametrize("scan_plan", [False, True])
@pytest.mark.parametrize("mode", ["fast", "quality"])
def test_ocr_bounds_all_read_schedules_to_decoded_video_end(
    tmp_path: Path, monkeypatch, scan_plan: bool, mode: str,
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    media = str(tmp_path / "portrait.mp4")
    engine = _FiniteVideoEngine({media: 122_000_000})
    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", lambda *_args: engine)
    worker = OcrWorker("portrait-tail")
    crop = {"name": "upper", "reading_mode": "dialogue", "scale": 1.0,
            "x1": .028, "x2": .962, "y1": .623, "y2": .704}
    worker.config = {"media_path": media, "duration_us": 122_136_961}
    if scan_plan:
        worker.config["scan_plan"] = {"schema_version": 1, "duration_us": 122_136_961,
            "segments": [{"media_id": "portrait", "media_path": media,
                          "source_start_us": 0, "source_end_us": 122_136_961,
                          "timeline_start_us": 0, "timeline_end_us": 122_136_961,
                          "base_start_us": 0, "base_end_us": 122_136_961}]}
        worker._configure_scan_plan()
    worker._open_engine(mode)
    payload = {"timestamps_us": [121_900_000, 122_000_000, 122_100_000],
               "bands": [crop], "band_timestamps_us": {
                   "upper": [121_900_000, 122_000_000, 122_100_000]},
               "detect_changes": True,
               "change_timestamps_us": [121_960_000, 122_100_000]}

    result = worker._recognize(payload, mode=mode)

    request = engine.requests[-1]
    assert request["timestamps_us"] == [121_900_000, 122_000_000]
    assert request["change_timestamps_us"] == [121_960_000]
    assert request["band_timestamps_us"] == {"upper": [121_900_000, 122_000_000]}
    assert request["bands"] == [crop]
    assert result["frame_count"] == 2
    assert [row["timestamp_us"] for row in result["evidence"]] == [121_900_000, 122_000_000]
    assert payload["timestamps_us"][-1] == 122_100_000


@pytest.mark.parametrize("scan_plan", [False, True])
def test_ocr_all_tail_batch_never_decodes_or_fabricates_evidence(
    tmp_path: Path, monkeypatch, scan_plan: bool,
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    media = str(tmp_path / "portrait.mp4")
    engine = _FiniteVideoEngine({media: 122_000_000})
    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", lambda *_args: engine)
    worker = OcrWorker("empty-tail")
    worker.config = {"media_path": media, "duration_us": 122_136_961}
    if scan_plan:
        worker.config["scan_plan"] = {"schema_version": 1, "duration_us": 122_136_961,
            "segments": [{"media_id": "portrait", "media_path": media,
                          "source_start_us": 0, "source_end_us": 122_136_961,
                          "timeline_start_us": 0, "timeline_end_us": 122_136_961,
                          "base_start_us": 0, "base_end_us": 122_136_961}]}
        worker._configure_scan_plan()
    worker._open_engine("fast")

    result = worker._recognize({"timestamps_us": [122_100_000], "bands": []}, mode="fast")

    assert not engine.requests
    assert result["evidence"] == [] and result["frame_count"] == 0
    assert result["processed_until_us"] == 122_100_000


def test_ocr_decoder_bound_resets_when_switching_media_and_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    short, long = str(tmp_path / "short.mp4"), str(tmp_path / "long.mp4")
    engine = _FiniteVideoEngine({short: 1_000_000, long: 3_000_000})
    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", lambda *_args: engine)
    worker = OcrWorker("switch-bounds")
    worker.config = {"media_path": short}
    payload = {"timestamps_us": [2_000_000], "bands": [{"name": "upper"}]}
    worker._open_engine("fast")
    assert worker._recognize(payload, mode="fast")["frame_count"] == 0

    worker._switch_engine_media("fast", long)
    assert worker._recognize(payload, mode="fast")["frame_count"] == 1
    worker._switch_engine_media("quality", short)
    assert worker._recognize(payload, mode="quality")["frame_count"] == 0
    assert engine.opened_media_paths == [short, long, short]
    assert worker._decoder_video_durations == {short: 1_000_000, long: 3_000_000}


@pytest.mark.parametrize("timeline_duration", [22_136_961, 44_273_922])
def test_ocr_after_trim_bounds_source_not_timeline_time(
    tmp_path, monkeypatch, timeline_duration,
):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    media = str(tmp_path / "trimmed.mp4")
    engine = _FiniteVideoEngine({media: 122_000_000})
    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", lambda *_args: engine)
    worker = OcrWorker("trimmed-tail")
    worker.config = {"media_path": media, "scan_plan": {
        "schema_version": 1, "duration_us": timeline_duration,
        "segments": [{"media_id": "trimmed", "media_path": media,
                      "source_start_us": 100_000_000, "source_end_us": 122_136_961,
                      "timeline_start_us": 0, "timeline_end_us": timeline_duration,
                      "base_start_us": 0, "base_end_us": 22_136_961}],
    }}
    worker._configure_scan_plan()
    factor = timeline_duration // 22_136_961
    kept, tail = 21_900_000 * factor, 22_100_000 * factor

    result = worker._recognize({"timestamps_us": [kept, tail],
                                "bands": [{"name": "upper"}]}, mode="fast")

    assert engine.requests[-1]["timestamps_us"] == [121_900_000]
    assert result["frame_count"] == 1
    assert result["processed_until_us"] == tail
    assert result["evidence"][0]["timestamp_us"] == kept
    assert result["evidence"][0]["source_timestamp_us"] == 121_900_000
    assert worker._scan_plan_duration_us == timeline_duration


@pytest.mark.parametrize("metadata", [{}, {"frame_count": 0}, {"frame_count": None}])
def test_ocr_legacy_host_keeps_unbounded_reads_and_clears_previous_bound(
    tmp_path, monkeypatch, metadata,
):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    engine = _RecordingScanPlanEngine()
    worker = OcrWorker("legacy-bounds")
    worker.engine = engine
    worker._engine_media_path = "legacy.mp4"
    worker._remember_video_duration({"frame_count": 25, "duration_us": 1_000_000, "fps": 25.0})
    worker._remember_video_duration(metadata)
    payload = {"timestamps_us": [2_000_000], "bands": [{"name": "upper"}]}

    result = worker._recognize(payload, mode="fast")

    assert result["frame_count"] == 1
    assert engine.requests[-1] == payload
    assert worker._engine_video_duration_us is None
    assert worker._decoder_video_durations == {}


@pytest.mark.parametrize("overrides", [
    {"duration_us": 1_000_001}, {"duration_us": 0}, {"duration_us": True},
    {"fps": 0}, {"fps": float("nan")}, {"fps": float("inf")}, {"fps": True},
])
def test_ocr_invalid_finite_metadata_is_not_used_as_a_bound(tmp_path, monkeypatch, overrides):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = OcrWorker("invalid-bound")
    with pytest.raises(ocr_worker_module.EngineError, match="ocr_engine_invalid_video_metadata"):
        worker._remember_video_duration({
            "frame_count": 25, "duration_us": 1_000_000, "fps": 25.0, **overrides,
        })
    assert worker._engine_video_duration_us is None


def test_ocr_valid_change_probes_do_not_become_ocr_when_primary_frames_are_tail(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    media = str(tmp_path / "portrait.mp4")
    engine = _FiniteVideoEngine({media: 122_000_000})
    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", lambda *_args: engine)
    worker = OcrWorker("probe-only")
    worker.config = {"media_path": media}
    worker._open_engine("fast")
    payload = {"timestamps_us": [122_100_000], "bands": [{"name": "upper"}],
               "detect_changes": True, "change_timestamps_us": [121_960_000],
               "evidence_kind": "burst"}

    result = worker._recognize(payload, mode="fast")

    request = engine.requests[-1]
    assert request["timestamps_us"] == [121_960_000]
    assert request["band_timestamps_us"] == {"upper": []}
    assert request["evidence_kinds_by_band"] == {"upper": "burst"}
    assert result["evidence"] == []
    assert result["event_timestamps_us"] == [121_960_000]
    assert result["processed_until_us"] == 122_100_000
    assert "band_timestamps_us" not in payload


def test_ocr_valid_primary_frames_survive_an_all_tail_change_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    media = str(tmp_path / "portrait.mp4")
    engine = _FiniteVideoEngine({media: 122_000_000})
    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", lambda *_args: engine)
    worker = OcrWorker("primary-only")
    worker.config = {"media_path": media}
    worker._open_engine("fast")
    payload = {"timestamps_us": [121_960_000], "bands": [{"name": "upper"}],
               "detect_changes": True, "change_timestamps_us": [122_100_000]}

    result = worker._recognize(payload, mode="fast")

    request = engine.requests[-1]
    assert request["timestamps_us"] == [121_960_000]
    assert request.get("change_timestamps_us", request["timestamps_us"])
    assert request["detect_changes"] is False
    assert len(result["evidence"]) == 1
    assert result["event_timestamps_us"] == []
    assert payload["detect_changes"] is True


def test_ocr_real_decoder_failure_is_never_silently_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))

    class CorruptFrameEngine(_FiniteVideoEngine):
        def request(self, action, payload):
            if action == "recognize":
                raise ocr_worker_module.EngineError("video_frame_unreadable")
            return super().request(action, payload)

    media = str(tmp_path / "corrupt.mp4")
    engine = CorruptFrameEngine({media: 122_000_000})
    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", lambda *_args: engine)
    worker = OcrWorker("corrupt-frame")
    worker.config = {"media_path": media}
    worker._open_engine("fast")
    with pytest.raises(ocr_worker_module.EngineError, match="video_frame_unreadable"):
        worker._recognize({"timestamps_us": [121_960_000, 122_100_000], "bands": []}, mode="fast")


def test_ocr_scene_cues_are_projected_to_a_separate_s2_track() -> None:
    first = TimeRange(0, 800_000)
    second = TimeRange(900_000, 1_700_000)
    media = MediaAsset("subtitle-media", "fixture.srt", MediaKind.SUBTITLE, 1_700_000)
    snapshot = ProjectSnapshot(
        "project",
        "Fixture",
        1,
        media=(media,),
        tracks=(
            TimelineTrack(
                "S1",
                TrackKind.SUBTITLE,
                "Phụ đề nguồn",
                (
                    TimelineClip("clip-1", "S1", first, first, media.id),
                    TimelineClip("clip-2", "S1", second, second, media.id),
                ),
            ),
        ),
        source_cues=(
            SubtitleCue("cue-1", 1, first, "走", "zh"),
            SubtitleCue("cue-2", 2, second, "海王兽", "zh"),
        ),
    )

    updated = _apply_ocr_cue_regions(
        snapshot,
        {
            "cue_regions": [
                {
                    "index": 2,
                    "start_us": second.start_us,
                    "end_us": second.end_us,
                    "box": [0.72, 0.22, 0.08, 0.31],
                    "panel_box": [0.68, 0.18, 0.16, 0.39],
                    "role": "scene_text",
                    "orientation": "vertical",
                    "style_hint": "calligraphy",
                    "review_state": "accepted",
                }
            ]
        },
    )

    assert [(track.id, track.name, len(track.clips)) for track in updated.tracks] == [
        ("S1", "Phụ đề nguồn", 1),
        ("S2", "Chữ trong cảnh", 1),
    ]
    assert updated.tracks[0].clips[0].id == "clip-1"
    assert updated.tracks[1].clips[0].id == "clip-2"
    assert updated.source_cues[1].role == "scene_text"
    assert updated.source_cues[1].text_font_size == 40
    assert updated.source_cues[0].text_font_size == 12
    assert updated.source_cues[1].style_hint == "calligraphy"
    assert updated.source_cues[1].text_color == "#FFFFFF"
    assert updated.source_cues[1].background_color == "#000000"
    assert updated.source_cues[1].background_style == "ink_splash"
    assert updated.source_cues[1].background_opacity_percent == 100
    assert updated.source_cues[1].fade_enabled is True
    assert updated.source_cues[1].fade_duration_ms == 300


@pytest.mark.parametrize("font_size", [40, 73, 240])
def test_ocr_qa_round_trip_preserves_scene_font_size(
    tmp_path: Path, monkeypatch, font_size: int,
) -> None:
    from ducai_studio.application.subtitle_finalizer import render_srt
    from ducai_studio.infrastructure.project_repository import decode_project_snapshot
    from ducai_studio.job_manager.service import _translation_cue_payload
    from ducai_studio.workers.translation_worker import _cue_from_config

    cue_range = TimeRange(0, 1_000_000)
    scene = SubtitleCue(
        "ocr-scene", 1, cue_range, "海王兽", "zh", role="scene_text",
        text_font_size=font_size, visual_box=(0.4, 0.2, 0.2, 0.1),
        visual_box_source="ocr",
    )
    output = tmp_path / "scene.srt"
    output.write_text(render_srt((scene,)), encoding="utf-8")
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = OcrWorker("scene-size-job")
    worker.spec = {}
    worker.config = {"duration_us": 1_000_000}
    qa = worker._qa_payload(
        status="PASS", output_path=output, cue_count=1, blockers=[], warnings=[],
        cues=(scene,), coverage={
            "minimum_coverage": 0.95, "coverage_ratio": 1.0,
            "subtitle_positive_timestamps": 2, "covered_timestamps": 2,
            "continuity_gap_limit_us": 1_000_000, "maximum_positive_gap_us": 0,
            "timeline_continuity_passes": True, "unmatched_samples": [],
        },
    )
    assert qa["cue_regions"][0]["text_font_size"] == font_size
    snapshot = ProjectSnapshot(
        "scene-size-project", "Scene size", 1,
        source_cues=(SubtitleCue("imported", 1, cue_range, scene.text, "zh"),),
    )
    updated = _apply_ocr_cue_regions(snapshot, qa)
    cue = updated.source_cues[0]
    assert cue.role == "scene_text" and cue.text_font_size == font_size
    assert _cue_from_config(_translation_cue_payload(cue)).text_font_size == font_size
    reopened = decode_project_snapshot(json.loads(json.dumps(asdict(updated))))
    assert reopened.source_cues[0].text_font_size == font_size


@pytest.mark.parametrize("font_size", [None, True, "40", 0, -1, 40.0])
def test_ocr_scene_size_invalid_metadata_uses_default(font_size: object) -> None:
    cue_range = TimeRange(0, 1_000_000)
    snapshot = ProjectSnapshot(
        "project", "Fixture", 1,
        source_cues=(SubtitleCue("cue-1", 1, cue_range, "海王兽", "zh"),),
    )
    region = {
        "index": 1, "start_us": 0, "end_us": 1_000_000,
        "role": "scene_text", "text_font_size": font_size,
    }
    updated = _apply_ocr_cue_regions(snapshot, {"cue_regions": [region]})
    assert updated.source_cues[0].text_font_size == 40
    region["start_us"] = 100_000
    unchanged = _apply_ocr_cue_regions(snapshot, {"cue_regions": [region]})
    assert unchanged.source_cues[0].role == "dialogue"
    assert unchanged.source_cues[0].text_font_size == 12


@pytest.mark.parametrize("role", ["dialogue", "scene_text"])
def test_ocr_legacy_regions_preserve_existing_custom_size(role: str) -> None:
    cue_range = TimeRange(0, 1_000_000)
    cue = SubtitleCue(
        "existing", 1, cue_range, "海王兽", "zh", role=role, text_font_size=27,
    )
    snapshot = ProjectSnapshot("project", "Fixture", 1, source_cues=(cue,))
    updated = _apply_ocr_cue_regions(snapshot, {"cue_regions": [{
        "index": 1, "start_us": 0, "end_us": 1_000_000, "role": role,
    }]})
    assert updated.source_cues[0].text_font_size == 27


def test_ocr_region_publish_ignores_malformed_optional_scene_metadata() -> None:
    cue_range = TimeRange(0, 800_000)
    snapshot = ProjectSnapshot(
        "project",
        "Fixture",
        1,
        source_cues=(SubtitleCue("cue-1", 1, cue_range, "第一句", "zh"),),
    )

    updated = _apply_ocr_cue_regions(
        snapshot,
        {
            "cue_regions": [
                {
                    "index": 1,
                    "start_us": cue_range.start_us,
                    "end_us": cue_range.end_us,
                    "box": [0.9, 0.9, 0.5, 0.5],
                    "panel_box": ["bad", 0.2, 0.3, 0.1],
                    "role": "unsupported",
                    "orientation": "diagonal",
                    "style_hint": "missing-font",
                    "background_style": "missing-background",
                    "background_color": "black",
                    "text_color": 123,
                    "background_opacity_percent": "100",
                    "fade_enabled": "false",
                    "fade_duration_ms": "300",
                    "review_state": "unknown",
                }
            ]
        },
    )

    cue = updated.source_cues[0]
    assert cue.visual_box is None
    assert cue.panel_box is None
    assert cue.role == "dialogue"
    assert cue.orientation == "horizontal"
    assert cue.style_hint == "standard"
    assert cue.background_style == "solid"
    assert cue.background_color == ""
    assert cue.text_color == ""
    assert cue.background_opacity_percent == 90
    assert cue.fade_enabled is False
    assert cue.fade_duration_ms == 300
    assert cue.review_state == "accepted"


def test_adaptive_rescue_is_bounded_without_a_second_video_decode_pass() -> None:
    events = list(range(200_000, 60_000_000, 200_000))

    burst, scene = OcrWorker._adaptive_timestamps_from_events(events, 60_000_000)

    # 48 visual transitions per 30 s, six local 100 ms samples each.
    assert len(burst) <= 2 * 48 * 6
    # 24 scene transitions/30 s, three samples, plus static 3 s anchors.
    assert len(scene) <= 2 * 24 * 3 + 20
    assert tuple(range(0, 60_000_000, 3_000_000)) == tuple(
        timestamp for timestamp in scene if timestamp % 3_000_000 == 0
    )


def test_scene_quality_checkpoint_rewinds_only_scene_rows(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("scene-resume")
    worker.config = {"duration_us": 5_000_000}
    def row(identity, timestamp, *, scene=False):
        return {"id": identity, "frame_id": f"frame-{timestamp}", "timestamp_us": timestamp,
                "band": "scene" if scene else "upper", "text": "天南",
                "confidence": .96, "provider_id": "paddle-quality", "model_id": "medium",
                "box": [.1, .2, .2, .1], "evidence_kind": "scene" if scene else "global"}
    rows = [row("dialogue", 2_000_000), row("old-scene", 400_000, scene=True),
            row("rewind-scene", 2_000_000, scene=True),
            {"id": "probe", "record_kind": "scene_probe", "timestamp_us": 2_000_000,
             "region": [.1, .2, .2, .1], "provider_id": "paddle-quality"}]
    checkpoint = tmp_path / "checkpoint.ndjson"
    checkpoint.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    worker._restore_checkpoint({"payload": {"stage": "scene_quality", "cursor_us": 2_000_000},
                                "artifact": {"uri": str(checkpoint)}})
    kept = [json.loads(line) for line in
            worker.evidence_path.read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in kept] == ["dialogue", "old-scene"]
    assert worker.cursor_us == 1_200_000


def test_scene_probes_survive_evidence_checkpoint_without_becoming_dialogue(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("scene-probe")
    worker.config = {"duration_us": 5_000_000}
    worker._append_evidence([{"id": "probe", "record_kind": "scene_probe",
                             "timestamp_us": 100_000, "region": [.1, .2, .2, .1],
                             "media_id": "media-one", "scan_segment_index": 2}])
    assert worker._load_evidence() == ()
    assert worker._load_scene_probes()[0].source_scope == "media-one:2"


def test_persistent_platform_anchor_filters_scene_fragments_only_in_its_source_scope(
    tmp_path, monkeypatch,
):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("scene-platform")
    anchors = tuple(SubtitleEvidence(
        f"anchor-{t}", f"frame-{t}", t, "scene", "bilibili", .95,
        "paddle-fast", "small", NormalizedBox(.86, .03, .1, .07),
        source_scope="media-one:0",
    ) for t in (0, 1_000_000, 2_000_000))
    monkeypatch.setattr(worker, "_load_watermark_evidence", lambda: anchors)
    fragment = SubtitleEvidence(
        "fragment", "frame-1", 1_000_000, "scene", "独", .95,
        "paddle-quality", "medium", NormalizedBox(.94, .04, .02, .04),
        "scene", source_scope="media-one:0",
    )
    from dataclasses import replace
    elsewhere = replace(fragment, id="other-scope", source_scope="media-two:1")
    title = replace(fragment, id="name-card", text="天星锤",
                    box=NormalizedBox(.2, .3, .07, .25))
    assert worker._scene_evidence((fragment, elsewhere, title)) == (elsewhere, title)


def test_adaptive_event_bounding_is_stable_while_the_window_grows() -> None:
    early = list(range(200_000, 10_000_000, 200_000))
    complete = list(range(200_000, 30_000_000, 200_000))

    early_burst, _ = OcrWorker._adaptive_timestamps_from_events(
        early, 30_000_000
    )
    complete_burst, _ = OcrWorker._adaptive_timestamps_from_events(
        complete, 30_000_000
    )

    assert set(early_burst).issubset(complete_burst)
    assert len(complete_burst) <= 48 * 6


def test_short_dialogue_transitions_inside_old_slot_get_independent_bursts():
    events = [1_000_000, 1_800_000, 2_600_000, 3_400_000]
    burst, _ = OcrWorker._adaptive_timestamps_from_events(events, 30_000_000)
    for start in events:
        assert start in burst
        assert start + 100_000 in burst
        assert start + 400_000 in burst


def test_full_frame_events_extend_scene_scan_without_expanding_dialogue_bursts() -> None:
    dialogue_events = [1_000_000]
    full_frame_events = [1_000_000, 5_000_000]

    burst, scene = OcrWorker._adaptive_timestamps_from_events(
        dialogue_events,
        12_000_000,
        scene_events=full_frame_events,
    )

    assert 5_000_000 not in burst
    assert 5_000_000 in scene
    assert 5_240_000 in scene
    assert {
        900_000,
        1_000_000,
        1_100_000,
        1_200_000,
        1_300_000,
        1_400_000,
    }.issubset(burst)


@pytest.mark.parametrize("scene_enabled", [True, False])
def test_scan_plan_maps_every_visual_pass_and_isolates_deleted_joins(
    tmp_path: Path, monkeypatch, scene_enabled: bool
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    media = tmp_path / "source.mp4"
    media.write_bytes(b"fixture")
    second_media = tmp_path / "second-source.mp4"
    second_media.write_bytes(b"fixture-two")
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("scan-plan")
    worker.config = {
        "media_path": str(media),
        "scene_text_enabled": scene_enabled,
        "duration_us": 30_000_000,
        "sample_interval_us": 800_000,
        "scan_plan": {
            "schema_version": 1,
            "duration_us": 4_000_000,
            "segments": [
                {
                    "media_id": "media-one",
                    "media_path": str(media),
                    "source_start_us": 10_000_000,
                    "source_end_us": 12_000_000,
                    "timeline_start_us": 0,
                    "timeline_end_us": 2_000_000,
                    "base_start_us": 5_000_000,
                    "base_end_us": 7_000_000,
                },
                {
                    "media_id": "media-two",
                    "media_path": str(second_media),
                    "source_start_us": 20_000_000,
                    "source_end_us": 22_000_000,
                    "timeline_start_us": 2_000_000,
                    "timeline_end_us": 4_000_000,
                    "base_start_us": 9_000_000,
                    "base_end_us": 11_000_000,
                },
            ],
        },
    }
    worker._configure_scan_plan()
    engine = _RecordingScanPlanEngine()
    worker.engine = engine

    def open_recording_engine(mode: str, *, media_path: str | None = None) -> None:
        engine.mode = mode
        worker._engine_mode = mode
        worker._engine_media_path = media_path or str(media)
        worker._change_tracking_segment_index = None

    monkeypatch.setattr(worker, "_open_engine", open_recording_engine)
    bands = worker._bands_payload()
    worker._scan_fast(4_000_000, 800_000, 3, bands, ())
    for request in engine.requests:
        names = {band["name"] for band in request["bands"]}
        assert set(request.get("exclusion_regions_by_band", {})).issubset(names)
        if "band_timestamps_us" in request:
            assert set(request["band_timestamps_us"]) == names
            assert set(request["evidence_kinds_by_band"]) == names
    worker.stage = "quality"
    worker.cursor_us = 0
    worker._scan_quality(
        4_000_000,
        400_000,
        3,
        bands,
        (RescueWindow(TimeRange(0, 4_000_000), ("coverage_gap",)),),
    )
    worker._scan_scene_text((0, 100_000, 2_000_000, 2_100_000), 2)
    worker.config["dialogue_region"] = [.05, .80, .90, .15]
    worker.config["exclusion_regions"] = [[.85, 0, .15, .12]]
    worker.stage = "scene_quality"
    worker.cursor_us = 0
    worker._scan_scene_quality(4_000_000, 3)
    roi_requests = [request for request in engine.requests
                    if any(band["name"].startswith("scene-roi-") for band in request["bands"])]
    assert bool(roi_requests) is scene_enabled
    if not scene_enabled:
        assert all(not band["name"].startswith("scene")
                   for request in engine.requests for band in request["bands"])
        assert worker._scene_cues((), duration_us=4_000_000,
                                  first_index=1, dialogue_cues=()) == ()
        burst, scenes = worker._adaptive_timestamps([500_000], 4_000_000)
        assert burst and scenes == ()
    for request in roi_requests:
        assert request["exclusion_regions"] == [[.85, 0, .15, .12]]
        assert all(band["scale"] == 2 for band in request["bands"])
        assert all(mask == [[.05, .80, .90, .15]] for mask in
                   request["exclusion_regions_by_band"].values())

    requested = [
        timestamp
        for request in engine.requests
        for key in ("timestamps_us", "change_timestamps_us")
        for timestamp in request.get(key, [])
    ]
    assert requested
    assert all(
        10_000_000 <= value < 12_000_000
        or 20_000_000 <= value < 22_000_000
        for value in requested
    )
    assert 20_000_000 in {
        timestamp
        for request in engine.requests
        for timestamp in request.get("change_timestamps_us", [])
    }
    assert 15_000_000 not in requested
    reset_requests = [
        request
        for request in engine.requests
        if request.get("detect_changes") is True
        and request.get("reset_change_tracking") is True
    ]
    assert len(reset_requests) >= 2
    assert str(second_media) in engine.opened_media_paths
    assert set(engine.opened_media_paths).issubset({str(media), str(second_media)})

    raw_rows = [
        json.loads(line)
        for line in worker.evidence_path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(row["text"] != "\u5220\u9664\u533a\u6587\u5b57" for row in raw_rows)
    first = next(row for row in raw_rows if row["source_timestamp_us"] == 10_000_000)
    assert first["timestamp_us"] == 0
    assert first["base_timestamp_us"] == 5_000_000
    assert first["scan_segment_index"] == 0

    fast_candidates, _rescue = worker._fast_candidates(4_000_000, 800_000)
    assert fast_candidates
    assert all(
        candidate.range.end_us <= 2_000_000
        or candidate.range.start_us >= 2_000_000
        for candidate in fast_candidates
    )
    scene_cues = worker._scene_cues(
        worker._load_evidence(),
        duration_us=4_000_000,
        first_index=1,
        dialogue_cues=(),
    )
    assert bool(scene_cues) is scene_enabled
    assert all(
        cue.range.end_us <= 2_000_000 or cue.range.start_us >= 2_000_000
        for cue in scene_cues
    )


def test_scan_plan_checkpoint_rejects_stale_frozen_settings(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    media = tmp_path / "source.mp4"
    media.write_bytes(b"fixture")
    artifact = tmp_path / "checkpoint.ndjson"
    artifact.write_text('{"id":"retained"}\n', encoding="utf-8")
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("scan-plan-checkpoint")
    worker.spec = {
        "input_fingerprint": "current-input",
        "settings_fingerprint": "current-settings",
    }
    worker.config = {
        "media_path": str(media),
        "duration_us": 30_000_000,
        "sample_interval_us": 800_000,
        "scan_plan": {
            "schema_version": 1,
            "duration_us": 2_000_000,
            "segments": [
                {
                    "media_id": "media",
                    "media_path": str(media),
                    "source_start_us": 10_000_000,
                    "source_end_us": 12_000_000,
                    "timeline_start_us": 0,
                    "timeline_end_us": 2_000_000,
                    "base_start_us": 5_000_000,
                    "base_end_us": 7_000_000,
                }
            ],
        },
    }
    worker._configure_scan_plan()

    worker._restore_checkpoint(
        {
            "payload": {
                "stage": "quality",
                "cursor_us": 0,
                "scan_plan_fingerprint": worker._scan_plan_fingerprint,
                "input_fingerprint": "current-input",
                "settings_fingerprint": "stale-settings",
            },
            "artifact": {"uri": str(artifact)},
        }
    )
    assert not worker.evidence_path.exists()

    worker._restore_checkpoint(
        {
            "payload": {
                "stage": "quality",
                "cursor_us": 0,
                "scan_plan_fingerprint": worker._scan_plan_fingerprint,
                "input_fingerprint": "current-input",
                "settings_fingerprint": "current-settings",
            },
            "artifact": {"uri": str(artifact)},
        }
    )
    assert worker.evidence_path.read_text(encoding="utf-8") == '{"id":"retained"}\n'


def test_worker_final_candidates_bridges_one_fast_and_one_quality_observation() -> None:
    evidence = (
        SubtitleEvidence(
            "fast-dialogue",
            "frame-232200000",
            232_200_000,
            "upper",
            "在这呢",
            0.999950,
            "paddle-fast",
            "PP-OCRv6-small",
            NormalizedBox(0.46, 0.89, 0.09, 0.06),
        ),
        SubtitleEvidence(
            "quality-dialogue",
            "frame-232400000",
            232_400_000,
            "lower",
            "在这呢",
            0.999866,
            "paddle-quality",
            "PP-OCRv6-medium",
            NormalizedBox(0.461, 0.895, 0.088, 0.058),
        ),
    )
    worker = object.__new__(OcrWorker)
    worker._load_evidence = lambda: evidence

    candidates = worker._final_candidates(
        (),
        (RescueWindow(TimeRange(230_000_000, 235_000_000), ("coverage_gap",)),),
        300_000_000,
        800_000,
    )

    assert len(candidates) == 1
    assert candidates[0].text == "在这呢"
    assert candidates[0].source == "paddle-cross-pass-consensus"


def test_ocr_engine_process_receives_portable_paddlex_cache(
    tmp_path: Path, monkeypatch
) -> None:
    python = tmp_path / "DUCAI_STUDIO_DATA" / "paddle_runtime" / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    cache = tmp_path / "DUCAI_STUDIO_DATA" / "paddle_model_cache"
    cache.mkdir()
    host = tmp_path / "ocr-engine-host.py"
    host.touch()
    captured: dict[str, object] = {}

    class FakeProcess:
        stderr: list[str] = []
        pid = 1234

        class stdin:
            @staticmethod
            def fileno():
                return 321

    class FakeGuard:
        def assign_pid(self, pid):
            captured["guard_pid"] = pid

        def close(self):
            pass

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(ocr_worker_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ocr_worker_module, "ProcessTreeGuard", FakeGuard)
    monkeypatch.setattr(ocr_worker_module, "isolated_python_environment", lambda: {})
    def write_gate(fd, data):
        captured["launch_gate"] = (fd, data)
        return len(data)

    monkeypatch.setattr(ocr_worker_module.os, "write", write_gate)

    EngineHostClient(python, host).start()

    assert captured["env"]["PADDLE_PDX_CACHE_HOME"] == str(cache.resolve())
    assert captured["env"]["DUCAI_WORKER_PROGRAM"] == str(python)
    assert json.loads(captured["env"]["DUCAI_WORKER_ARGUMENTS_JSON"]) == ["-u", str(host)]
    assert captured["launch_gate"] == (321, b"DUCAI_LAUNCH\n")
    assert captured["guard_pid"] == 1234


def test_ocr_worker_uses_checkpoint_compatible_runtime_engine_override(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    configured_host = tmp_path / "old-engine-host.py"
    configured_host.write_text("old", encoding="utf-8")
    runtime_host = tmp_path / "fixed-engine-host.py"
    runtime_host.write_text("fixed", encoding="utf-8")
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"fixture")
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    monkeypatch.setenv("DUCAI_OCR_ENGINE_HOST_PATH", str(runtime_host))
    monkeypatch.setenv(
        "DUCAI_OCR_ENGINE_HOST_SHA256", sha256(runtime_host.read_bytes()).hexdigest()
    )
    captured: dict[str, object] = {}

    class FakeEngineHostClient:
        def __init__(self, python_path: Path, host_path: Path) -> None:
            captured["python_path"] = python_path
            captured["host_path"] = host_path

        def request(self, action: str, payload: dict) -> dict:
            captured["action"] = action
            captured["payload"] = payload
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(ocr_worker_module, "EngineHostClient", FakeEngineHostClient)
    worker = OcrWorker("ocr-runtime-host")
    worker.config = {
        "engine_python": sys.executable,
        "engine_host_path": str(configured_host),
        "media_path": str(media),
    }

    worker._open_engine("fast")

    assert captured["host_path"] == runtime_host.resolve()
    assert captured["action"] == "open"


def test_ocr_worker_runs_external_host_and_publishes_only_final_srt(tmp_path: Path) -> None:
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"video fixture")
    output = tmp_path / "fixture_ZH_FINAL.srt"
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_ocr_engine_host.py"
    environment = os.environ.copy()
    environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio.workers.ocr_worker", "ocr-fixture"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    ready = WorkerMessage.from_json_line(process.stdout.readline())
    assert ready.kind == "ready"
    start = WorkerMessage(
        PROTOCOL_VERSION,
        "ocr-fixture",
        "start-request",
        "control",
        {
            "action": "start",
            "command_id": "start-command",
            "job": {
                "kind": "ocr",
                "provider_config": {
                    "media_path": str(media),
                    "duration_us": 2_400_000,
                    "sample_interval_us": 800_000,
                    "window_frame_count": 3,
                    "engine_python": sys.executable,
                    "engine_host_path": str(fake_host),
                    "output_path": str(output),
                },
            },
        },
    )
    process.stdin.write(start.to_json_line())
    process.stdin.flush()
    kinds = []
    while True:
        message = WorkerMessage.from_json_line(process.stdout.readline())
        kinds.append(message.kind)
        if message.kind in {"succeeded", "failed"}:
            terminal = message
            break
    assert terminal.kind == "succeeded", terminal.payload
    assert terminal.payload["status"] == "PASS"
    assert terminal.payload["cue_count"] == 1
    assert process.wait(timeout=10) == 0
    assert output.is_file()
    assert output.read_text(encoding="utf-8-sig").count("-->") == 1
    assert "checkpoint" in kinds
    assert not list(tmp_path.glob("*partial*.srt"))


def test_ocr_worker_pause_resume_keeps_checkpoint_and_finishes(tmp_path: Path) -> None:
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"video fixture")
    output = tmp_path / "fixture_ZH_FINAL.srt"
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_ocr_engine_host.py"
    environment = os.environ.copy()
    environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)
    environment["DUCAI_FAKE_OCR_DELAY_MS"] = "60"
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio.workers.ocr_worker", "ocr-control"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    assert WorkerMessage.from_json_line(process.stdout.readline()).kind == "ready"
    start = WorkerMessage(
        PROTOCOL_VERSION,
        "ocr-control",
        "start-request",
        "control",
        {
            "action": "start",
            "command_id": "start-command",
            "job": {
                "kind": "ocr",
                "provider_config": {
                    "media_path": str(media),
                    "duration_us": 8_000_000,
                    "scan_plan": {
                        "schema_version": 1,
                        "duration_us": 8_000_000,
                        "segments": [
                            {
                                "media_id": "media",
                                "media_path": str(media),
                                "source_start_us": 10_000_000,
                                "source_end_us": 18_000_000,
                                "timeline_start_us": 0,
                                "timeline_end_us": 8_000_000,
                                "base_start_us": 0,
                                "base_end_us": 8_000_000,
                            }
                        ],
                    },
                    "sample_interval_us": 800_000,
                    "window_frame_count": 2,
                    "engine_python": sys.executable,
                    "engine_host_path": str(fake_host),
                    "output_path": str(output),
                },
            },
        },
    )
    process.stdin.write(start.to_json_line())
    process.stdin.flush()
    while WorkerMessage.from_json_line(process.stdout.readline()).kind != "progress":
        pass
    pause = WorkerMessage(
        PROTOCOL_VERSION,
        "ocr-control",
        "pause-request",
        "control",
        {"action": "pause", "command_id": "pause-command"},
    )
    process.stdin.write(pause.to_json_line())
    process.stdin.flush()
    seen = []
    while "paused" not in seen:
        seen.append(WorkerMessage.from_json_line(process.stdout.readline()).kind)
    assert "checkpoint" in seen
    resume = WorkerMessage(
        PROTOCOL_VERSION,
        "ocr-control",
        "resume-request",
        "control",
        {"action": "resume", "command_id": "resume-command"},
    )
    process.stdin.write(resume.to_json_line())
    process.stdin.flush()
    terminal = None
    while terminal is None:
        message = WorkerMessage.from_json_line(process.stdout.readline())
        if message.kind in {"succeeded", "failed"}:
            terminal = message
    assert terminal.kind == "succeeded", terminal.payload
    assert process.wait(timeout=10) == 0
    assert output.is_file()


def test_targeted_rescan_returns_evidence_without_publishing_partial_final(
    tmp_path: Path,
) -> None:
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"video fixture")
    output = tmp_path / "fixture_ZH_FINAL.srt"
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_ocr_engine_host.py"
    environment = os.environ.copy()
    environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio.workers.ocr_worker", "ocr-rescan"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    assert WorkerMessage.from_json_line(process.stdout.readline()).kind == "ready"
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "ocr-rescan",
            "start-request",
            "control",
            {
                "action": "start",
                "command_id": "start-command",
                "job": {
                    "kind": "ocr",
                    "provider_config": {
                        "media_path": str(media),
                        "duration_us": 5_000_000,
                        "scan_plan": {
                            "schema_version": 1,
                            "duration_us": 5_000_000,
                            "segments": [
                                {
                                    "media_id": "media",
                                    "media_path": str(media),
                                    "source_start_us": 10_000_000,
                                    "source_end_us": 15_000_000,
                                    "timeline_start_us": 0,
                                    "timeline_end_us": 5_000_000,
                                    "base_start_us": 0,
                                    "base_end_us": 5_000_000,
                                }
                            ],
                        },
                        "sample_interval_us": 400_000,
                        "window_frame_count": 4,
                        "engine_python": sys.executable,
                        "engine_host_path": str(fake_host),
                        "output_path": str(output),
                        "rescan_cue_id": "cue-one",
                        "rescan_cue_start_us": 1_000_000,
                        "rescan_cue_end_us": 3_000_000,
                        "scan_windows": [
                            {
                                "start_us": 0,
                                "end_us": 4_200_000,
                                "reasons": ["user_targeted_rescan"],
                            }
                        ],
                    },
                },
            },
        ).to_json_line()
    )
    process.stdin.flush()
    terminal = None
    while terminal is None:
        message = WorkerMessage.from_json_line(process.stdout.readline())
        if message.kind in {"succeeded", "failed"}:
            terminal = message

    assert terminal.kind == "succeeded", terminal.payload
    assert terminal.payload["rescan"]["text"] == "我要修炼成仙"
    assert len(terminal.payload["rescan"]["evidence_ids"]) >= 2
    assert process.wait(timeout=10) == 0
    assert not output.exists()


def test_quality_checkpoint_resume_rewinds_overlap_without_dropping_fast_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    artifact = tmp_path / "immutable.ndjson"
    rows = [
        {"id": "fast", "provider_id": "paddle-fast", "timestamp_us": 1_600_000},
        {"id": "quality-0", "provider_id": "paddle-quality", "timestamp_us": 0},
        {
            "id": "quality-800",
            "provider_id": "paddle-quality",
            "timestamp_us": 800_000,
        },
        {
            "id": "quality-1600",
            "provider_id": "paddle-quality",
            "timestamp_us": 1_600_000,
        },
    ]
    artifact.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    worker = OcrWorker("resume-quality")
    worker.config = {"sample_interval_us": 800_000}

    worker._restore_checkpoint(
        {
            "payload": {"stage": "quality", "cursor_us": 1_600_000},
            "artifact": {"uri": str(artifact)},
        }
    )

    retained = [
        json.loads(line)["id"]
        for line in worker.evidence_path.read_text(encoding="utf-8").splitlines()
    ]
    assert worker.cursor_us == 800_000
    assert retained == ["fast", "quality-0"]


def test_fast_checkpoint_resume_restores_transition_history_before_rewind(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    artifact = tmp_path / "immutable.ndjson"
    artifact.write_text("", encoding="utf-8")
    worker = OcrWorker("resume-fast-events")
    worker.config = {"sample_interval_us": 800_000}

    worker._restore_checkpoint(
        {
            "payload": {
                "stage": "fast",
                "cursor_us": 5_000_000,
                "change_events_us": [1_000_000, 3_000_000, 4_000_000],
                "scene_change_events_us": [2_000_000, 3_200_000, 4_400_000],
            },
            "artifact": {"uri": str(artifact)},
        }
    )

    assert worker.cursor_us == 3_400_000
    assert worker._change_events == [1_000_000, 3_000_000]
    assert worker._scene_change_events == [2_000_000, 3_200_000]


def test_watermark_evidence_stays_out_of_cues_and_requires_persistent_edge_samples(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("watermark-regions")
    worker.config = {"duration_us": 8_000_000}
    rows = [
        {
            "id": "dialogue",
            "frame_id": "frame-000000000000",
            "timestamp_us": 0,
            "band": "lower",
            "text": "真正的台词",
            "confidence": 0.99,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.2, 0.7, 0.6, 0.1],
        },
        {
            "id": "watermark-1",
            "frame_id": "frame-000000000000",
            "timestamp_us": 0,
            "band": "lower",
            "text": "小李看漫剧bilibili",
            "confidence": 0.98,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.78, 0.62, 0.2, 0.08],
            "ignored_overlay": True,
        },
        {
            "id": "watermark-2",
            "frame_id": "frame-000000000800",
            "timestamp_us": 800_000,
            "band": "lower",
            "text": "小李看漫剧bilibili",
            "confidence": 0.97,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.79, 0.621, 0.19, 0.079],
            "ignored_overlay": True,
        },
        {
            "id": "watermark-3",
            "frame_id": "frame-000000002000",
            "timestamp_us": 2_000_000,
            "band": "lower",
            "text": "小李看漫剧bilibili",
            "confidence": 0.97,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.785, 0.62, 0.195, 0.08],
            "ignored_overlay": True,
        },
        {
            "id": "single-observation",
            "frame_id": "frame-000000001600",
            "timestamp_us": 1_600_000,
            "band": "upper",
            "text": "bilibili",
            "confidence": 0.96,
            "provider_id": "paddle-fast",
            "model_id": "PP-OCRv6-mobile",
            "box": [0.02, 0.2, 0.12, 0.05],
            "ignored_overlay": True,
        },
    ]
    worker.evidence_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    assert [item.id for item in worker._load_evidence()] == ["dialogue"]
    regions = worker._watermark_regions()

    assert len(regions) == 1
    assert regions[0]["start_us"] == 0
    assert regions[0]["end_us"] == 8_000_000
    assert regions[0]["sample_count"] == 3
    assert regions[0]["box"] == pytest.approx([0.772, 0.614, 0.216, 0.092])


def _start_fake_whisper_worker(
    tmp_path: Path,
    job_id: str,
    *,
    environment_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], Path]:
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"video fixture")
    output = tmp_path / "fixture_ZH_FINAL.srt"
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    job_staging = staging / job_id
    job_staging.mkdir()
    (job_staging / "dialogue-16k-mono.wav").write_bytes(b"W" * 8_192)
    model = tmp_path / "large-v3"
    model.mkdir(exist_ok=True)
    fake_host = Path(__file__).parents[1] / "fixtures" / "fake_whisper_engine_host.py"
    environment = os.environ.copy()
    environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)
    environment.update(environment_overrides or {})
    process = subprocess.Popen(
        [sys.executable, "-m", "ducai_studio.workers.ocr_worker", job_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    assert WorkerMessage.from_json_line(process.stdout.readline()).kind == "ready"
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            job_id,
            "start-request",
            "control",
            {
                "action": "start",
                "command_id": "start-command",
                "job": {
                    "kind": "ocr",
                    "provider_id": "faster-whisper-audio",
                    "model_id": "large-v3",
                    "input_fingerprint": "input",
                    "settings_fingerprint": "settings",
                    "provider_config": {
                        "extraction_method": "whisper_audio",
                        "media_path": str(media),
                        "duration_us": 9_000_000,
                        "sample_interval_us": 800_000,
                        "engine_python": sys.executable,
                        "engine_host_path": str(fake_host),
                        "model_path": str(model),
                        "model_id": "large-v3",
                        "language": "zh",
                        "device": "auto",
                        "output_path": str(output),
                    },
                },
            },
        ).to_json_line()
    )
    process.stdin.flush()
    return process, output


def _read_until_terminal(process: subprocess.Popen[bytes]) -> tuple[list[str], WorkerMessage]:
    assert process.stdout is not None
    kinds: list[str] = []
    while True:
        message = WorkerMessage.from_json_line(process.stdout.readline())
        kinds.append(message.kind)
        if message.kind in {"succeeded", "failed", "stopped"}:
            return kinds, message


def test_whisper_worker_streams_segments_and_publishes_guarded_srt(tmp_path: Path) -> None:
    process, output = _start_fake_whisper_worker(tmp_path, "whisper-success")

    kinds, terminal = _read_until_terminal(process)

    assert terminal.kind == "succeeded", terminal.payload
    assert terminal.payload["cue_count"] == 3
    assert process.wait(timeout=10) == 0
    assert output.read_text(encoding="utf-8-sig").count("-->") == 3
    qa = json.loads(output.with_name(output.stem + "_QA.json").read_text(encoding="utf-8"))
    assert qa["kind"] == "whisper_asr"
    assert qa["output_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert "progress" in kinds


def test_whisper_worker_pause_resume_rewinds_without_duplicate_cues(tmp_path: Path) -> None:
    process, output = _start_fake_whisper_worker(
        tmp_path,
        "whisper-resume",
        environment_overrides={"DUCAI_FAKE_WHISPER_DELAY_MS": "120"},
    )
    assert process.stdin is not None and process.stdout is not None
    while WorkerMessage.from_json_line(process.stdout.readline()).kind != "progress":
        pass
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "whisper-resume",
            "pause-request",
            "control",
            {"action": "pause", "command_id": "pause-command"},
        ).to_json_line()
    )
    process.stdin.flush()
    seen: list[str] = []
    while "paused" not in seen:
        seen.append(WorkerMessage.from_json_line(process.stdout.readline()).kind)
    assert "checkpoint" in seen
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "whisper-resume",
            "resume-request",
            "control",
            {"action": "resume", "command_id": "resume-command"},
        ).to_json_line()
    )
    process.stdin.flush()

    _kinds, terminal = _read_until_terminal(process)

    assert terminal.kind == "succeeded", terminal.payload
    assert process.wait(timeout=10) == 0
    assert output.read_text(encoding="utf-8-sig").count("-->") == 3


def test_whisper_worker_cancel_never_publishes_partial_s1(tmp_path: Path) -> None:
    process, output = _start_fake_whisper_worker(
        tmp_path,
        "whisper-cancel",
        environment_overrides={"DUCAI_FAKE_WHISPER_DELAY_MS": "120"},
    )
    assert process.stdin is not None and process.stdout is not None
    while WorkerMessage.from_json_line(process.stdout.readline()).kind != "progress":
        pass
    process.stdin.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "whisper-cancel",
            "cancel-request",
            "control",
            {"action": "cancel", "command_id": "cancel-command"},
        ).to_json_line()
    )
    process.stdin.flush()

    _kinds, terminal = _read_until_terminal(process)

    assert terminal.kind == "stopped"
    assert process.wait(timeout=10) == 0
    assert not output.exists()
    assert not output.with_name(output.stem + "_QA.json").exists()


def test_whisper_worker_falls_back_from_cuda_to_cpu_once(tmp_path: Path) -> None:
    process, output = _start_fake_whisper_worker(
        tmp_path,
        "whisper-fallback",
        environment_overrides={"DUCAI_FAKE_WHISPER_CUDA_FAIL": "1"},
    )

    _kinds, terminal = _read_until_terminal(process)

    assert terminal.kind == "succeeded", terminal.payload
    assert terminal.payload["device"] == "cpu"
    assert terminal.payload["compute_type"] == "int8"
    assert process.wait(timeout=10) == 0
    assert output.is_file()


def test_whisper_checkpoint_rewind_uses_a_complete_segment_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    staging = tmp_path / "artifacts" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(staging))
    worker = OcrWorker("whisper-boundary")
    worker.whisper_segments_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in (
                {"start_us": 0, "end_us": 3_000_000, "text": "第一句"},
                {"start_us": 3_000_000, "end_us": 6_000_000, "text": "第二句"},
                {"start_us": 6_000_000, "end_us": 9_000_000, "text": "第三句"},
            )
        ),
        encoding="utf-8",
    )
    worker.cursor_us = 9_000_000

    worker._rewind_whisper_segments()

    assert worker.cursor_us == 3_000_000
    assert [row["text"] for row in worker._load_whisper_segments()] == ["第一句"]
