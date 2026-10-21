from __future__ import annotations

import os
from pathlib import Path

_DLL_DIRECTORY_HANDLES: list[object] = []


def configure_qt_plugin_dll_search() -> None:
    """Keep the PySide runtime visible while Qt loads plugins on Windows.

    Qt plugins live below the PySide package while their dependent Qt DLLs
    live in the package root.  Python's safe DLL search does not reliably use
    PATH changes made after process start, so register that root explicitly
    and retain the handle for the process lifetime.
    """

    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    if _DLL_DIRECTORY_HANDLES:
        return
    try:
        import PySide6

        package_root = Path(PySide6.__file__).resolve().parent
        if not (package_root / "Qt6Core.dll").is_file():
            return
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(package_root)))
    except (ImportError, OSError):
        # Frozen distributions resolve Qt beside the executable.  If that
        # layout does not expose a normal PySide package root, let Qt use its
        # native deployment search path.
        return
