from __future__ import annotations

import sys
import time
from uuid import uuid4

from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage

JOB_ID = sys.argv[1]


def write(kind: str, payload: dict) -> None:
    message = WorkerMessage(PROTOCOL_VERSION, JOB_ID, str(uuid4()), kind, payload)
    sys.stdout.buffer.write(message.to_json_line())
    sys.stdout.buffer.flush()


write("ready", {"status": "ready"})
for line in sys.stdin.buffer:
    message = WorkerMessage.from_json_line(line)
    if message.kind != "control":
        continue
    action = message.payload.get("action")
    if action == "start":
        checkpoint = message.payload.get("checkpoint")
        write(
            "started",
            {
                "status": "running",
                "command_id": message.payload.get("command_id"),
                "resumed_cursor_us": (
                    checkpoint.get("cursor_us") if isinstance(checkpoint, dict) else None
                ),
            },
        )
    elif action == "stop":
        time.sleep(0.25)
        write("checkpoint", {"cursor_us": 42_000_000})
        write("stopped", {"status": "stopped"})
        break
