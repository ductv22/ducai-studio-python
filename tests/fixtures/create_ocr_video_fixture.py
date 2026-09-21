from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QImage, QPainter


def main() -> int:
    application = QGuiApplication.instance() or QGuiApplication([])
    assert application is not None
    output = Path(sys.argv[1]).resolve()
    font_path = Path(sys.argv[2]).resolve()
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        return 2
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(QColor("#25313c"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = QFont(families[0], 36, QFont.Weight.DemiBold)
    painter.setFont(font)
    target = QRect(40, 545, 1200, 90)
    painter.setPen(QColor("#000000"))
    painter.drawText(
        target.translated(3, 3),
        Qt.AlignmentFlag.AlignCenter,
        "我要开始修炼，然后成仙。",
    )
    painter.setPen(QColor("#ffffff"))
    painter.drawText(target, Qt.AlignmentFlag.AlignCenter, "我要开始修炼，然后成仙。")
    painter.end()
    output.parent.mkdir(parents=True, exist_ok=True)
    return 0 if image.save(str(output)) else 3


if __name__ == "__main__":
    raise SystemExit(main())
