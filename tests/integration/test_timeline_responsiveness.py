from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_hidden_preview_stops_time_queries_and_refreshes_when_shown(tmp_path):
    script = Path(__file__).parents[1] / "fixtures" / "hidden-preview-work.py"
    result = subprocess.run([sys.executable, str(script), str(tmp_path / "settings.ini")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ReferenceError" not in result.stderr and "TypeError" not in result.stderr


def test_translation_models_keys_and_scene_toggle_keep_native_editing_contract(tmp_path):
    script = Path(__file__).parents[1] / "fixtures" / "translation-controls.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "controls")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
             "QT_SCALE_FACTOR": "1.25", "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ReferenceError" not in result.stderr and "TypeError" not in result.stderr
    report = json.loads((tmp_path / "controls/report.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASS"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Real media fixture requires FFmpeg")
def test_dialogue_preview_group_gestures_preserve_font_selection_and_persistence(tmp_path):
    script = Path(__file__).parents[1] / "fixtures" / "subtitle-geometry-gestures.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "geometry")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
             "QT_LOGGING_RULES": "qt.multimedia.*=false"},
        capture_output=True, text=True, encoding="utf-8", timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ReferenceError" not in result.stderr and "TypeError" not in result.stderr
    assert json.loads((tmp_path / "geometry/result.json").read_text())["status"] == "PASS"


def test_bulk_dialogue_geometry_measures_in_background_and_flushes_before_close(tmp_path):
    script = Path(__file__).parents[1] / "fixtures" / "bulk-subtitle-geometry.py"
    result = subprocess.run([sys.executable, str(script), str(tmp_path / "bulk")],
             env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True,
             text=True, encoding="utf-8", timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "bulk/result.json").read_text())["status"] == "PASS"


def test_cue_timing_gestures_save_fine_drag_without_snapback(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures" / "cue-timing-gestures.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "settings.ini")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ReferenceError" not in result.stderr and "TypeError" not in result.stderr


def test_new_video_import_initializes_viewport_once_without_retention_controls(
    tmp_path: Path,
) -> None:
    script = Path(__file__).parents[1] / "fixtures" / "timeline-cut-markers.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "settings.ini")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ReferenceError" not in result.stderr and "TypeError" not in result.stderr


def test_text_inspector_shortcuts_delete_scissors_and_stable_text_identity(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures" / "timeline-delete-scissors.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "inspector")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
             "QT_SCALE_FACTOR": "1.25", "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Windows Qt/FFmpeg can announce the installed Media Foundation encoders
    # at startup. Keep rejecting every actual QML/media/teardown diagnostic.
    diagnostics = [line for line in result.stderr.splitlines() if not re.fullmatch(
        r"\[(?:h264_mf|hevc_mf) @ [0-9a-fA-F]+\] MFT name: '[^']+'", line)]
    assert not diagnostics, result.stderr


def test_dense_timeline_marquee_bulk_style_and_zoom_remain_responsive(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures" / "timeline-stress.py"
    report_path = tmp_path / "timeline.json"
    result = subprocess.run(
        [sys.executable, str(script), str(report_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr and "TypeError" not in result.stderr
    assert "ReferenceError" not in result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["selected_count"] == 12000
    assert report["selection_ms"] < 500 and report["bulk_style_ms"] < 500
    assert report["delegate_count"] <= 480
