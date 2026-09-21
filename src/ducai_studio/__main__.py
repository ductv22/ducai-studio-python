from __future__ import annotations

import sys


def _dispatch_frozen_module(argv: list[str]) -> int | None:
    """Route ``python -m`` style child commands when sys.executable is frozen.

    In source installs the real Python interpreter handles ``-m`` before this
    module runs. A Nuitka executable receives those arguments itself, so the GUI
    binary must remain capable of launching its owned manager/workers.
    """
    if len(argv) < 2 or argv[0] != "-m":
        return None
    module, arguments = argv[1], argv[2:]
    if module == "ducai_studio.job_manager":
        from ducai_studio.job_manager.__main__ import main as target

        return target(arguments)
    if module == "ducai_studio.workers.ocr_worker":
        from ducai_studio.workers.ocr_worker import main as target

        return target(arguments)
    if module == "ducai_studio.workers.preowned_launcher":
        from ducai_studio.workers.preowned_launcher import main as target

        sys.argv = [sys.argv[0], *arguments]
        return target()
    worker_modules = {
        "ducai_studio.workers.translation_worker": "translation",
        "ducai_studio.workers.tts_worker": "tts",
        "ducai_studio.workers.export_worker": "export",
    }
    worker = worker_modules.get(module)
    if worker is None:
        return None
    sys.argv = [sys.argv[0], *arguments]
    if worker == "translation":
        from ducai_studio.workers.translation_worker import main as target
    elif worker == "tts":
        from ducai_studio.workers.tts_worker import main as target
    else:
        from ducai_studio.workers.export_worker import main as target
    return target()


def main() -> int:
    """Start the desktop application when the UI phase is available."""
    from ducai_studio.infrastructure.portable_runtime import configure_portable_environment

    configure_portable_environment()
    dispatched = _dispatch_frozen_module(sys.argv[1:])
    if dispatched is not None:
        return dispatched
    from ducai_studio.ui.app import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
