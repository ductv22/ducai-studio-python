from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_HOST_PYTHON_KEYS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONEXECUTABLE",
    "VIRTUAL_ENV",
)


def isolated_python_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an environment for a separately managed Python runtime.

    DUCAI can itself be frozen or launched from another virtual environment.
    Host-specific Python variables must not leak into Paddle, Piper or VieNeu.
    """

    environment = os.environ.copy()
    for key in _HOST_PYTHON_KEYS:
        environment.pop(key, None)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    if overrides is not None:
        environment.update(overrides)
    return environment


def isolated_python_working_directory(python_executable: str | Path) -> Path:
    """Return a DLL-safe cwd for an external Python interpreter on Windows.

    A standalone DUCAI release contains ``python312.dll``. If its release
    directory is inherited as cwd, a Python 3.11 OCR/TTS runtime can load that
    DLL before its own and terminate with a version-conflict ImportError.
    """

    return Path(python_executable).expanduser().resolve().parent
