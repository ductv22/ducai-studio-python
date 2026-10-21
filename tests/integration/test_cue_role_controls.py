from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Native media fixture needs FFmpeg")
def test_native_cue_role_controls_preserve_text_identity_and_atomicity(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures" / "cue-role-controls.py"
    output = tmp_path / "cue-role-controls"
    result = subprocess.run(
        [sys.executable, str(script), str(output)],
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
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TypeError" not in result.stderr
    assert "ReferenceError" not in result.stderr
    assert "Traceback (most recent call last)" not in result.stderr, result.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
