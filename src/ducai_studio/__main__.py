from __future__ import annotations

import sys


def _dispatch_update_stage_verifier(argv: list[str]) -> int | None:
    """Authenticate a staged update when invoked by the external helper."""

    if not argv or argv[0] != "--verify-update-stage":
        return None
    if (
        len(argv) != 8
        or argv[2] != "--signature"
        or argv[4] != "--payload"
        or argv[6] != "--manifest-sha256"
        or not argv[7]
    ):
        return 2
    try:
        from ducai_studio.infrastructure.update_install import verify_staged_update

        verify_staged_update(
            manifest_path=argv[1],
            signature_path=argv[3],
            payload_root=argv[5],
            expected_manifest_sha256=argv[7],
        )
    except Exception:
        return 1
    return 0


def _consume_update_health_args(argv: list[str]) -> list[str]:
    """Remove updater-only arguments before Qt parses the command line."""

    values: dict[str, str] = {}
    remaining: list[str] = []
    index = 0
    names = {
        "--update-health-path": "DUCAI_UPDATE_HEALTH_PATH",
        "--update-health-nonce": "DUCAI_UPDATE_HEALTH_NONCE",
        "--update-health-version": "DUCAI_UPDATE_HEALTH_VERSION",
    }
    while index < len(argv):
        key = argv[index]
        environment_key = names.get(key)
        if environment_key is None:
            remaining.append(key)
            index += 1
            continue
        if index + 1 >= len(argv) or not argv[index + 1]:
            raise ValueError(f"Missing value for {key}")
        values[environment_key] = argv[index + 1]
        index += 2
    if values and set(values) != set(names.values()):
        raise ValueError("Incomplete update health handshake arguments")
    if values:
        import os

        os.environ.update(values)
    return remaining


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
    verified = _dispatch_update_stage_verifier(sys.argv[1:])
    if verified is not None:
        return verified

    from ducai_studio.infrastructure.portable_runtime import configure_portable_environment

    configure_portable_environment()
    sys.argv[1:] = _consume_update_health_args(sys.argv[1:])
    dispatched = _dispatch_frozen_module(sys.argv[1:])
    if dispatched is not None:
        return dispatched
    from ducai_studio.ui.app import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
