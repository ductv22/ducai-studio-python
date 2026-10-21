from __future__ import annotations

import os
import runpy
from pathlib import Path

from PySide6.QtCore import QStandardPaths

_original_writable_location = QStandardPaths.writableLocation


def _isolated_writable_location(location):
    if location == QStandardPaths.StandardLocation.AppLocalDataLocation:
        return os.environ["DUCAI_TEST_DATA_ROOT"]
    return _original_writable_location(location)


QStandardPaths.writableLocation = staticmethod(_isolated_writable_location)

if os.environ.get("DUCAI_TEST_LICENSE_BOOTSTRAP") == "1":
    runpy.run_path(
        Path(__file__).parents[1] / "license-bootstrap" / "sitecustomize.py"
    )
