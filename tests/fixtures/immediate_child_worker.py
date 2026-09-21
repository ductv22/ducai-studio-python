from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage

JOB_ID = sys.argv[1]
PID_FILE = Path(sys.argv[2])


def write(kind: str, payload: dict) -> None:
    message = WorkerMessage(PROTOCOL_VERSION, JOB_ID, str(uuid4()), kind, payload)
    sys.stdout.buffer.write(message.to_json_line())
    sys.stdout.buffer.flush()


child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
PID_FILE.write_text(str(child.pid), encoding="ascii")
write("ready", {"status": "ready", "child_pid": child.pid})
for line in sys.stdin.buffer:
    message = WorkerMessage.from_json_line(line)
    if message.kind == "control" and message.payload.get("action") == "stop":
        write("stopped", {"status": "stopped"})
        break
