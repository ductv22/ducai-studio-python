from __future__ import annotations

import sys
from uuid import uuid4

from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage

JOB_ID = sys.argv[1]
pause_attempts: dict[str, int] = {}


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
    command_id = str(message.payload.get("command_id", ""))
    if action == "start":
        write("started", {"status": "running", "command_id": command_id})
    elif action == "pause":
        pause_attempts[command_id] = pause_attempts.get(command_id, 0) + 1
        if pause_attempts[command_id] < 2:
            continue
        write("paused", {"status": "paused", "command_id": command_id})
    elif action == "cancel":
        write("stopped", {"status": "cancel", "command_id": command_id})
        break
    elif action == "stop":
        write("stopped", {"status": "stop"})
        break
