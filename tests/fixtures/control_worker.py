from __future__ import annotations

import sys
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
    command_id = message.payload.get("command_id")
    if action == "start":
        write("started", {"status": "running", "command_id": command_id})
    elif action == "pause":
        write("paused", {"status": "paused", "command_id": command_id})
    elif action == "resume":
        write("resumed", {"status": "running", "command_id": command_id})
    elif action in {"cancel", "stop"}:
        payload = {"status": str(action)}
        if command_id is not None:
            payload["command_id"] = command_id
        write("stopped", payload)
        break
