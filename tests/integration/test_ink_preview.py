from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
INK_COMPONENT = (
    PROJECT_ROOT
    / "src"
    / "ducai_studio"
    / "ui"
    / "qml"
    / "controls"
    / "InkSplashBackground.qml"
)


@pytest.mark.skipif(sys.platform != "win32", reason="The product targets Windows")
@pytest.mark.parametrize(
    ("variant", "vertical", "width", "height"),
    tuple(
        (variant, vertical, 180 if vertical else 520, 520 if vertical else 180)
        for variant in ("ink_splash", "ink_wash", "ink_cloud", "brush_stroke")
        for vertical in (True, False)
    ),
)
def test_ink_splash_qml_paints_an_opaque_irregular_shape(
    variant: str,
    vertical: bool,
    width: int,
    height: int,
) -> None:
    script = r"""
import sys
from pathlib import Path
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQuick import QQuickView

app = QGuiApplication([])
view = QQuickView()
view.setColor(QColor("white"))
view.setResizeMode(QQuickView.SizeRootObjectToView)
view.resize(int(sys.argv[4]), int(sys.argv[5]))
view.setSource(QUrl.fromLocalFile(str(Path(sys.argv[1]).resolve())))
if view.status() != QQuickView.Ready:
    raise SystemExit("InkSplashBackground QML could not be loaded")
root = view.rootObject()
root.setProperty("variant", sys.argv[2])
root.setProperty("vertical", sys.argv[3] == "true")
view.show()

def verify():
    image = view.grabWindow()
    if image.isNull():
        raise SystemExit("InkSplashBackground could not be captured")
    ink = 0
    clear = 0
    for y in range(0, image.height(), 4):
        for x in range(0, image.width(), 4):
            color = image.pixelColor(x, y)
            if color.red() < 64 and color.green() < 64 and color.blue() < 64:
                ink += 1
            else:
                clear += 1
    if ink < 100 or clear < 100:
        raise SystemExit(f"Ink silhouette is not irregular: {ink=} {clear=}")
    app.quit()

QTimer.singleShot(250, verify)
QTimer.singleShot(2000, lambda: app.exit(9))
raise SystemExit(app.exec())
"""
    environment = os.environ.copy()
    environment.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(INK_COMPONENT),
            variant,
            str(vertical).lower(),
            str(width),
            str(height),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
