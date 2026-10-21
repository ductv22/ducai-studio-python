from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_named_styles_qml_save_select_and_database_reload(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "translation-style-presets.py"
    directory = tmp_path / "styles-ui"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(fixture), str(directory)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
             "QT_SCALE_FACTOR": "1.25", "QT_LOGGING_RULES": "qt.multimedia.*=false",
             "DUCAI_APP_DATA_ROOT": str(directory / "isolated-data")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for error in ("Traceback (most recent call last)", "TypeError:", "ReferenceError:"):
        assert error not in result.stderr, result.stderr
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["same_rules_distinct_names"] and report["database_reload"]
