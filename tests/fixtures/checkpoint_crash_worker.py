from __future__ import annotations

import argparse
import time
from pathlib import Path

from ducai_studio.infrastructure.checkpoint_repository import CheckpointRepository
from ducai_studio.infrastructure.database import Database


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--event-id", required=True)
    arguments = parser.parse_args()

    def observe(phase: str) -> None:
        if phase != arguments.phase:
            return
        arguments.marker.write_text(phase, encoding="utf-8")
        while True:
            time.sleep(1)

    CheckpointRepository(
        Database(arguments.database),
        arguments.artifacts,
        phase_observer=observe,
    ).commit(
        job_id=arguments.job_id,
        stage_key="ocr",
        checkpoint_key="primary",
        schema_version=1,
        resume_fingerprint=arguments.fingerprint,
        cursor_us=42_000_000,
        payload={"rows": 12},
        artifact_path=arguments.source,
        event_id=arguments.event_id,
        event_payload={"cursor_us": 42_000_000},
    )


if __name__ == "__main__":
    main()
