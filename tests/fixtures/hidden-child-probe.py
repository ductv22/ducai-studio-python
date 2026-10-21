"""Report real console attachment and exercise inherited worker streams."""

import ctypes
import json
import sys

ctypes.windll.kernel32.GetConsoleWindow.restype = ctypes.c_void_p
print(json.dumps({
    "console": ctypes.windll.kernel32.GetConsoleWindow() or 0,
    "input": sys.stdin.readline().rstrip(),
}))
print("child diagnostic", file=sys.stderr)
raise SystemExit(17)
