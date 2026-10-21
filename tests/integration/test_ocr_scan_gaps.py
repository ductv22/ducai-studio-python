from ducai_studio.application.ocr_pipeline import measure_hard_sub_coverage
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.subtitles import SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.workers.ocr_worker import OcrWorker


def test_legacy_moved_clips_scan_only_present_frames_and_do_not_count_blank_gaps(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "artifacts"))
    worker = OcrWorker("legacy-gaps")
    starts = (5_000_000, 500_000_000)
    worker.config = {"scan_plan": {"schema_version": 1, "duration_us": 502_000_000,
                                  "segments": [
        {"media_id": "media", "media_path": str(tmp_path / "source.mp4"),
         "source_start_us": index*10_000_000, "source_end_us": index*10_000_000+2_000_000,
         "timeline_start_us": start, "timeline_end_us": start+2_000_000,
         "base_start_us": start, "base_end_us": start+2_000_000}
        for index, start in enumerate(starts)
    ]}}
    worker._configure_scan_plan()
    timestamps = worker._timeline_timestamps(0, 502_000_000, 500_000)
    assert timestamps == [start+offset for start in starts
                          for offset in range(0, 2_000_000, 500_000)]
    evidence = tuple(SubtitleEvidence(str(value), str(value), value, "upper", "保留", .99,
                                      "paddle-fast", "model") for value in timestamps)
    cues = tuple(SubtitleCue(str(index), index, TimeRange(start, start+2_000_000), "保留", "zh")
                 for index, start in enumerate(starts, 1))
    sampled, visible, duration = worker._coverage_timeline(evidence, cues, 502_000_000)
    assert duration == 4_000_000
    assert [cue.range for cue in visible] == [TimeRange(0, 2_000_000),
                                             TimeRange(2_000_000, 4_000_000)]
    assert measure_hard_sub_coverage(sampled, visible, video_duration_us=duration).passes
    # Published evidence and SRT positions remain the actual legacy Timeline positions.
    assert evidence[0].timestamp_us == starts[0]
    assert cues[1].range.start_us == starts[1]
