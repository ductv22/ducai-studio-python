from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_timeline_routes_text_headers_and_selected_scene_acceptance() -> None:
    qml_root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    timeline = (qml_root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    toolbar = (qml_root / "controls" / "SubtitleTextToolbar.qml").read_text(
        encoding="utf-8"
    )

    assert "selectTimelineTrackCues(" in timeline
    assert 'root.selectInspectorTab("text")' in timeline
    assert 'text: "Chấp nhận chữ cảnh đã chọn"' in timeline
    assert "acceptSelectedSceneTextCues()" in timeline
    assert "selectedSceneTextCueCount" in timeline
    assert "!clipItem.model.trackLocked" in timeline
    assert "selectedTextCueEditingLocked" in toolbar


def test_real_qml_track_header_bulk_style_and_scene_acceptance_persist(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures" / "timeline-bulk-text-controls.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "bulk-text")],
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "PYTHONUTF8": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ReferenceError" not in result.stderr and "TypeError" not in result.stderr
    report = json.loads((tmp_path / "bulk-text" / "report.json").read_text(encoding="utf-8"))
    assert report == {
        "status": "PASS",
        "header_selected": ["scene-1", "scene-2", "dialogue-on-s2"],
        "styled": ["scene-1", "scene-2", "dialogue-on-s2"],
        "accepted": ["scene-1", "scene-2"],
        "media_tracks_preserved": ["A1", "V1"],
        "title_header_selected": ["title-1", "title-2"],
    }
