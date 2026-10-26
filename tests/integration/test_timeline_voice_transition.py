from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_timeline_d1_starts_at_runtime_boundaries(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    environment = os.environ.copy()
    environment.update(
        {
            "DUCAI_STUDIO_DATA_ROOT": str(tmp_path / "data"),
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (str(project_root / "src"), environment.get("PYTHONPATH"))
                if part
            ),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "QT_LOGGING_RULES": "qt.multimedia.*=false",
            "PYTHONUTF8": "1",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "tests/fixtures/timeline-voice-transition-regression.py"),
            str(tmp_path),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=35,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(
        (tmp_path / "timeline-voice-transition-report.json").read_text(encoding="utf-8")
    )
    assert report == {
        "status": "PASS",
        "normal_entry_started": True,
        "relative_seek_resynced": True,
        "absolute_seek_then_entry_started": True,
        "edited_timeline_entry_started": True,
    }
