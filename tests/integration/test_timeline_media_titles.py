import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Native media fixture needs FFmpeg")
def test_native_titles_and_media_are_independent_editable_and_durable(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "fixtures" / "timeline-media-titles.py"),
            str(tmp_path),
        ],
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "QT_SCALE_FACTOR": "1.25",
            "QT_LOGGING_RULES": "qt.multimedia.*=false",
            "PYTHONUTF8": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TypeError" not in result.stderr and "ReferenceError" not in result.stderr
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "PASS"


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("ffmpeg") is None,
    reason="Native Windows title/export comparison",
)
def test_independent_title_preview_matches_real_export_when_subtitles_disabled(tmp_path):
    script = Path(__file__).parents[1] / "fixtures" / "subtitle-project-parity.py"
    env = {key: value for key, value in os.environ.items() if key != "QT_QPA_PLATFORM"}
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), shutil.which("ffmpeg")],
        env={
            **env,
            "QT_QUICK_BACKEND": "software",
            "DUCAI_TEST_TITLE_MODE": "1",
            "QT_SCALE_FACTOR": "1.25",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "comparison.json").read_text())
    assert report["input_parity"] and report["independent_title"] and report["iou"] > 0.9
