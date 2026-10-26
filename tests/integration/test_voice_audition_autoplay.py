from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_restored_voice_preview_requires_explicit_audition(tmp_path: Path) -> None:
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
            str(project_root / "tests/fixtures/voice-audition-autoplay-regression.py"),
            str(tmp_path),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(
        (tmp_path / "voice-audition-report.json").read_text(encoding="utf-8")
    )
    assert report == {
        "status": "PASS",
        "restored_preview_silent": True,
        "explicit_same_source_replayed": True,
    }
