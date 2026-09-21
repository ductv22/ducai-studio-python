from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QCoreApplication

from ducai_studio.domain.jobs import TERMINAL_JOB_STATES
from ducai_studio.job_manager.server import JobManagerServer
from ducai_studio.job_manager.service import JobManagerService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--engine-python", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    arguments = parser.parse_args()
    application = QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=arguments.data_root / "state.db",
        artifact_root=arguments.data_root / "artifacts",
    )
    server = JobManagerServer(name=f"ocr-smoke-{uuid4()}", service=service)
    project = service.create_project(
        project_path=str(arguments.data_root / "fixture.ducai"),
        name=arguments.media.stem,
    )
    service.import_media(project_id=project["id"], media_path=str(arguments.media))
    job = service.create_ocr_job(
        project_id=project["id"],
        sample_interval_us=800_000,
        engine_python=str(arguments.engine_python),
    )
    server._dispatch(
        "start_job",
        {
            "job_id": job["id"],
            "program": sys.executable,
            "arguments": ["-m", "ducai_studio.workers.ocr_worker", job["id"]],
            "resume_stage_key": "ocr",
        },
    )
    deadline = time.monotonic() + arguments.timeout
    try:
        while time.monotonic() < deadline:
            application.processEvents()
            record = service.jobs.get(job["id"])
            if record.state in TERMINAL_JOB_STATES:
                snapshot = service.get_project(project["id"])
                print(
                    json.dumps(
                        {
                            "state": record.state.value,
                            "error_code": record.error_code,
                            "cue_count": len(snapshot["source_cues"]),
                            "output_path": job["spec"]["provider_config"]["output_path"],
                            "progress": service.list_jobs(1)[0].get("progress", {}),
                        },
                        ensure_ascii=False,
                    )
                )
                return 0 if record.state.value == "succeeded" else 2
            time.sleep(0.01)
        return 3
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
