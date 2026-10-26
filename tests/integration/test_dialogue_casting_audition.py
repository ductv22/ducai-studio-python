from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_dialogue_casting_audition_and_viewport_use_real_qml(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "fixtures/dialogue-casting-audition.py"),
            str(tmp_path),
        ],
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "QT_LOGGING_RULES": "qt.multimedia.*=false",
            "PYTHONUTF8": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "TypeError" not in result.stderr
    assert "ReferenceError" not in result.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report == {
        "status": "PASS",
        "distinct_audio_outputs": True,
        "source_sequence_played_twice": True,
        "same_file_parts_played": True,
        "source_bound_enforced": True,
        "source_error_visible": True,
        "same_tts_preview_replayed": True,
        "bulk_label_scroll_preserved": True,
        "filtered_label_scroll_preserved": True,
    }
