from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLockFile

from .server import JobManagerServer
from .service import JobManagerService


def _canonical_runtime_path(path: Path) -> Path:
    return path.expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DUCAI STUDIO Job Manager")
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    database_path = _canonical_runtime_path(arguments.database)
    artifact_root = _canonical_runtime_path(arguments.artifacts)
    application = QCoreApplication(sys.argv[:1])
    manager_lock = QLockFile(str(database_path.with_suffix(".manager.lock")))
    manager_lock.setStaleLockTime(0)
    if not manager_lock.tryLock(0) and (
        not manager_lock.removeStaleLockFile() or not manager_lock.tryLock(0)
    ):
        return 4
    service = JobManagerService(
        database_path=database_path,
        artifact_root=artifact_root,
    )
    service.recover_startup()
    server = JobManagerServer(name=arguments.server_name, service=service)
    server.listen()
    application.aboutToQuit.connect(server.close)
    exit_code = application.exec()
    manager_lock.unlock()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
