from __future__ import annotations

import json

from PySide6.QtCore import QCoreApplication

from ducai_studio.application.timeline_commands import TimelineEditor
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.infrastructure.project_repository import (
    decode_project_snapshot,
    encode_project_snapshot,
)
from ducai_studio.ui.viewmodels.project_job_models import TimelineClipListModel


def _snapshot() -> ProjectSnapshot:
    asset = MediaAsset("m", "C:/media/movie.mp4", MediaKind.VIDEO, 60_000_000)
    video = TimelineClip("v", "V1", TimeRange(0, 60_000_000), TimeRange(0, 60_000_000), "m")
    return ProjectSnapshot(
        "p",
        "Movie",
        1,
        media=(asset,),
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),),
    )


def test_markers_are_sorted_undoable_and_round_trip_without_legacy_field_noise() -> None:
    original = _snapshot()
    editor = TimelineEditor(original)
    updated = editor.set_markers([25_000_000, 1_000_000, 25_000_000])

    assert updated.timeline_markers == (1_000_000, 25_000_000)
    assert editor.undo().timeline_markers == ()
    assert editor.redo().timeline_markers == (1_000_000, 25_000_000)

    encoded, _digest = encode_project_snapshot(updated)
    decoded = decode_project_snapshot(json.loads(encoded))
    assert decoded.timeline_markers == updated.timeline_markers

    legacy_payload = json.loads(encode_project_snapshot(original)[0])
    legacy_payload.pop("timeline_markers", None)
    assert decode_project_snapshot(legacy_payload).timeline_markers == ()


def test_subtitle_overlap_warning_only_marks_heavy_same_lane_collisions() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    model = TimelineClipListModel()
    model.setViewport(0, 20_000_000, 0)
    model.replace(
        [
            {
                "id": "S1",
                "kind": "subtitle",
                "name": "Thoại",
                "clips": [
                    {
                        "id": "heavy-a",
                        "timeline_range": {"start_us": 0, "end_us": 10_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 1.0,
                    },
                    {
                        "id": "heavy-b",
                        "timeline_range": {"start_us": 2_000_000, "end_us": 9_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 1.0,
                    },
                    {
                        "id": "light",
                        "timeline_range": {"start_us": 8_000_000, "end_us": 12_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 1.0,
                    },
                ],
            },
            {
                "id": "S2",
                "kind": "subtitle",
                "name": "Cảnh",
                "clips": [
                    {
                        "id": "scene-a",
                        "timeline_range": {"start_us": 2_000_000, "end_us": 8_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 1.0,
                    },
                    {
                        "id": "scene-b",
                        "timeline_range": {"start_us": 3_000_000, "end_us": 4_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 1.0,
                    },
                ],
            },
            {
                "id": "V1",
                "kind": "video",
                "name": "Video",
                "clips": [
                    {
                        "id": "video",
                        "timeline_range": {"start_us": 0, "end_us": 20_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 1.0,
                    }
                ],
            },
        ]
    )

    warnings = {
        model.data(model.index(row, 0), TimelineClipListModel.IdRole): model.data(
            model.index(row, 0), TimelineClipListModel.OverlapWarningRole
        )
        for row in range(model.count)
    }
    assert warnings["heavy-a"] is True
    assert warnings["heavy-b"] is True
    assert warnings["light"] is False
    assert warnings["scene-a"] is True
    assert warnings["scene-b"] is True
    assert warnings["video"] is False
