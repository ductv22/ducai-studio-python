from __future__ import annotations

import sys

from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def emit(kind: str, event_id: str, payload: dict) -> None:
    sys.stdout.buffer.write(
        WorkerMessage(
            PROTOCOL_VERSION,
            "terminal-garbage-test",
            event_id,
            kind,
            payload,
        ).to_json_line()
    )
    sys.stdout.buffer.flush()


emit("ready", "ready", {"status": "ready"})
emit("failed", "terminal", {"code": "expected_review"})
sys.stdout.buffer.write(b"native shutdown notice\n")
sys.stdout.buffer.flush()
