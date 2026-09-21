from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("ffmpeg") is None,
    reason="The Windows Qt Multimedia smoke test requires FFmpeg",
)
def test_preview_load_play_navigation_seek_and_pause(tmp_path: Path) -> None:
    media = tmp_path / "preview fixture.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(media),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "DUCAI_PREVIEW_FIXTURE": str(media),
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (str(PROJECT_ROOT / "src"), environment.get("PYTHONPATH"))
                if part
            ),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "QT_FFMPEG_DEBUG": "0",
            "QT_LOGGING_RULES": (
                "qt.multimedia.ffmpeg=false;qt.multimedia.ffmpeg.*=false"
            ),
        }
    )
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tests" / "fixtures" / "preview_smoke.py")],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, (
        f"preview smoke exited with {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
