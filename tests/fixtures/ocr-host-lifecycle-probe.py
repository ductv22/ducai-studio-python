"""Probe real hidden process-tree lifetime; this fixture supplies no OCR."""

import ctypes
import json
import os
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
)
console = 0
if os.name == "nt":
    ctypes.windll.kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    console = ctypes.windll.kernel32.GetConsoleWindow() or 0
for line in sys.stdin:
    request = json.loads(line)
    if request["action"] == "shutdown":
        if os.environ.get("DUCAI_LIFECYCLE_STALL") == "1":
            time.sleep(60)
        break
    print(json.dumps({"id": request["id"], "ok": True, "result": {
        "pid": os.getpid(), "child_pid": child.pid, "console": console,
    }}), flush=True)
