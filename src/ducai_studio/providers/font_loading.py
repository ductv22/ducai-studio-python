"""Load the same bundled font when a Qt font backend cannot open its path."""

from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFontDatabase


def load_application_font(path: str | Path) -> int:
    path = Path(path)
    font_id = QFontDatabase.addApplicationFont(str(path))
    if font_id >= 0:
        return font_id
    # Some Windows font backends fail on non-ASCII installation paths even
    # though Python can open the exact file. Keep its bytes/family unchanged.
    try:
        data = path.read_bytes()
    except OSError:
        return -1
    return QFontDatabase.addApplicationFontFromData(QByteArray(data))
