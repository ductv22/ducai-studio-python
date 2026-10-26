from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Native GPU QML regression is Windows-only")
@pytest.mark.parametrize(
    ("preview_height", "blur_text"),
    (
        (480, "LÀM MỜ MẠNH"),
        (360, "LÀM MỜ MẠNH"),
        (480, "MỜ NỀN NHIỀU MÀU"),
        (360, "MỜ NỀN NHIỀU MÀU"),
    ),
    ids=("default-h480", "default-h360", "wide-color-h480", "wide-color-h360"),
)
def test_native_timeline_preview_matches_real_mp4_cue_blur(
    tmp_path: Path,
    preview_height: int,
    blur_text: str,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required for the real MP4 regression")
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
        "QSG_RHI_BACKEND": "d3d11",
        "QT_LOGGING_RULES": "qt.multimedia.ffmpeg=false;qt.multimedia.ffmpeg.*=false",
    }
    environment.pop("QT_QPA_PLATFORM", None)
    environment.pop("QT_QUICK_BACKEND", None)
    environment.pop("DUCAI_TEST_BLUR_KERNEL_FACTOR", None)
    environment["DUCAI_TEST_PREVIEW_HEIGHT"] = str(preview_height)
    environment["DUCAI_TEST_BLUR_TEXT"] = blur_text
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests/fixtures/cue-background-preview.py"),
            str(tmp_path),
            ffmpeg,
            ffprobe,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=150,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, (
        f"native cue-background fixture exited with {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    report = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))
    assert "Software" not in report["graphics_api"]
    assert report["boxblur_passes"] >= 2
    assert report["preview_blur_changed_fraction"] > 0.45
    assert report["export_blur_changed_fraction"] > 0.45
