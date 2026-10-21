"""Keep real Qt event-loop policy checks isolated from other Qt test lifetimes."""
import os
import subprocess
import sys
from pathlib import Path


def test_project_save_policy_in_isolated_qt_event_loop():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest",
         "tests/fixtures/project-save-policy.py", "-q", "--tb=short"],
        cwd=root, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
