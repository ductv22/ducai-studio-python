from __future__ import annotations

import json
import os
import subprocess
import sys

_LAUNCH_GATE = b"DUCAI_LAUNCH\n"


def main() -> int:
    program = os.environ.pop("DUCAI_WORKER_PROGRAM", "")
    encoded_arguments = os.environ.pop("DUCAI_WORKER_ARGUMENTS_JSON", "[]")
    if not program:
        return 64
    arguments = json.loads(encoded_arguments)
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        return 64
    # Do not read ahead: the child inherits the pipe, not this interpreter's
    # BufferedReader. A command arriving with the gate must remain in the pipe.
    gate = b"".join(os.read(sys.stdin.fileno(), 1) for _ in range(len(_LAUNCH_GATE)))
    if gate != _LAUNCH_GATE:
        return 65

    child = subprocess.Popen(
        [program, *arguments],
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
        env=os.environ.copy(),
        close_fds=False,
    )
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
