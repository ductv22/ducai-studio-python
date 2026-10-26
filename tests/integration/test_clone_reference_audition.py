from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Real MP3 fixture needs FFmpeg")
def test_clone_reference_audition_uses_real_qml_controls_and_decoders(tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "fixtures/clone-reference-audition.py"),
         str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
             "QT_LOGGING_RULES": "qt.multimedia.*=false", "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=35,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TypeError" not in result.stderr and "ReferenceError" not in result.stderr
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "PASS"
