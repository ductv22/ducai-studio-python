"""Owned CPU-only WAV host, also explicitly dispatched by the frozen EXE."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ducai_studio.providers.tts.wav_preparation import prepare_task


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    from ducai_studio.infrastructure.licensing_state import worker_activation_exit_code

    denied = worker_activation_exit_code()
    if denied:
        return denied
    root = Path(sys.argv[1]).expanduser().resolve()
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("protocol_version") != 1:
                return 2
            if request.get("action") == "shutdown":
                return 0
            if request.get("action") != "prepare":
                return 2
            result = prepare_task(request["payload"], root)
            response = {"id": request["id"], "ok": True, "result": result}
        except Exception as exc:
            # Never emit raw payloads, paths or provider error messages.
            trace = exc.__traceback__
            while trace is not None and trace.tb_next is not None:
                trace = trace.tb_next
            site = (
                f"{Path(trace.tb_frame.f_code.co_filename).name}:{trace.tb_lineno}"
                if trace is not None else "unknown:0"
            )
            response = {"id": request.get("id"), "ok": False,
                        "error": type(exc).__name__, "site": site}
        sys.stdout.write(json.dumps({"protocol_version": 1, **response}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
