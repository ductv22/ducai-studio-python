"""Measure isolated manager telemetry with a production-shaped cue array.

No production SQLite, provider, credentials, media or GPU are used.
"""
import gc
import json
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from ducai_studio.job_manager.server import JobManagerServer
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.workers.protocol import WorkerMessage


def main():
    app = QCoreApplication.instance() or QCoreApplication([])
    with tempfile.TemporaryDirectory(prefix="ducai-progress-") as directory:
        root = Path(directory)
        service = JobManagerService(
            database_path=root / "state.db",
            artifact_root=root / "artifacts",
        )
        job = service.create_job({
            "kind": "tts", "project_id": "benchmark", "input_revision": 1,
            "provider_id": "vieneu-3.8", "provider_version": "3.8.1",
            "model_id": "v3turbo", "input_fingerprint": "benchmark",
            "settings_fingerprint": "benchmark", "provider_config": {"cues": [
                {"id": f"cue-{i}", "index": i, "start_us": i * 2_000_000,
                 "end_us": (i + 1) * 2_000_000, "text": "Đây là câu đo hiệu năng tiến độ."}
                for i in range(8587)
            ]},
        })
        job_id = job["id"]
        timings = {}
        for name, reader in (("full_job_read", service.jobs.get),
                             ("compact_owner_read", service.jobs.project_id_for)):
            started = time.perf_counter()
            for _ in range(20):
                reader(job_id)
            timings[name + "_ms_per_event"] = round((time.perf_counter() - started) * 50, 3)
        server = JobManagerServer(name="benchmark-unused", service=service)
        try:
            started = time.perf_counter()
            for i in range(538):
                server.workers._deliver_event(job_id, WorkerMessage(
                    1, job_id, f"event-{i}", "progress",
                    {"phase": "cache_restore", "current": 0, "total": 8587,
                     "phase_current": min(8587, i * 16), "phase_total": 8587},
                ))
            server.workers._flush_progress()
            timings["burst_538_delivery_ms"] = round((time.perf_counter() - started) * 1000, 3)
            timings["progress_events_persisted"] = sum(
                event.kind == "progress" for event in service.jobs.list_events(job_id)
            )
            assert timings["progress_events_persisted"] == 1
            app.processEvents()
        finally:
            server.close()
        gc.collect()
        print(json.dumps(timings))


if __name__ == "__main__":
    main()
