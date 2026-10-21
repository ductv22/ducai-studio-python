from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
FONT_ROOT = PROJECT_ROOT / "src" / "ducai_studio" / "ui" / "assets" / "fonts"


@pytest.mark.skipif(sys.platform != "win32", reason="The product targets Windows")
def test_bundled_vietnamese_and_cjk_fonts_cover_editor_text() -> None:
    script = r"""
import sys
from pathlib import Path
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QRawFont, QTextLayout

app = QGuiApplication([])
font_root = Path(sys.argv[1])
for path in (*font_root.glob("*.ttf"), *font_root.glob("*.otf")):
    if QFontDatabase.addApplicationFont(str(path)) < 0:
        raise SystemExit(f"Cannot register {path.name}")
vietnamese = QRawFont.fromFont(QFont("Be Vietnam Pro"))
if not all(vietnamese.supportsCharacter(ord(character)) for character in "Đạo Việt ngữ"):
    raise SystemExit("Bundled Vietnamese coverage is incomplete")
for family in (
    "Playwrite VN",
    "Dancing Script",
    "Lobster",
    "Agbalumo",
    "Comforter Brush",
    "Water Brush",
    "Splash",
    "Hurricane",
):
    calligraphy = QRawFont.fromFont(QFont(family))
    if calligraphy.familyName() != family:
        raise SystemExit(f"Bundled calligraphy family is unavailable: {family}")
    if not all(
        calligraphy.supportsCharacter(ord(character))
        for character in "Hồng Ly tỷ tỷ"
    ):
        raise SystemExit(f"Bundled calligraphy coverage is incomplete: {family}")
cjk = QRawFont.fromFont(QFont("Noto Sans CJK SC"))
if not all(cjk.supportsCharacter(ord(character)) for character in "中文字幕"):
    raise SystemExit("Bundled CJK coverage is incomplete")
QFont.insertSubstitution("Be Vietnam Pro", "Noto Sans CJK SC")
layout = QTextLayout("中", QFont("Be Vietnam Pro"))
layout.beginLayout()
layout.createLine()
layout.endLayout()
families = {run.rawFont().familyName() for run in layout.glyphRuns()}
if "Noto Sans CJK SC" not in families:
    raise SystemExit(f"CJK substitution was not used: {sorted(families)}")
"""
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script, str(FONT_ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("consumer", ["ui", "subtitle"])
@pytest.mark.skipif(sys.platform != "win32", reason="The product targets Windows")
def test_bundled_fonts_load_from_relocated_unicode_directory(tmp_path, consumer):
    font_root = tmp_path / "Giải nén thử" / "ui" / "assets" / "fonts"
    font_root.mkdir(parents=True)
    shutil.copy2(FONT_ROOT / "Agbalumo-Regular.ttf", font_root)
    script = r"""
import sys
from pathlib import Path
from PySide6.QtGui import QFont, QFontInfo, QGuiApplication

app = QGuiApplication([])
root = Path(sys.argv[1])
if sys.argv[2] == "ui":
    from ducai_studio.ui import app as ui
    ui._ui_resource_root = lambda: root.parent.parent
    ui._register_bundled_fonts()
else:
    from ducai_studio.providers import subtitle_artwork
    from ducai_studio.providers.export import ffmpeg
    ffmpeg._bundled_font_directory = lambda: root
    subtitle_artwork._ensure_fonts()
assert QFontInfo(QFont("Agbalumo")).family() == "Agbalumo"
"""
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run(
        [sys.executable, "-c", script, str(font_root), consumer],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=environment, timeout=20, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
