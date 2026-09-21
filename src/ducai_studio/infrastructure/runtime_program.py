from __future__ import annotations

import sys
from pathlib import Path


def runtime_program_path() -> Path:
    """Return the executable that can relaunch this DUCAI runtime.

    Source installs should use the Python interpreter. Some Nuitka standalone
    builds expose a synthetic ``python.exe`` through ``sys.executable`` even
    though the actual launchable program is ``sys.argv[0]``. Prefer the normal
    interpreter when it exists, then fall back to the real application binary.
    """

    executable = Path(sys.executable).expanduser()
    if executable.is_file():
        return executable.resolve()
    argv0 = Path(sys.argv[0]).expanduser() if sys.argv else executable
    if argv0.is_file():
        return argv0.resolve()
    return executable.resolve()
