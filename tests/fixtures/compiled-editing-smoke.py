"""Exercise the real frozen manager on disposable 12k-cue data, without license bypasses."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

from ducai_studio.domain.project import ProjectSnapshot, SubtitleCue
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.infrastructure.database import Database
from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
    encode_frame,
)
from ducai_studio.infrastructure.project_repository import ProjectRepository


def unpack(result):
    envelope = result.get("__project_transport__")
    if envelope is None:
        return result
    assert envelope["encoding"] == "zlib+base64+json"
    raw = zlib.decompress(base64.b64decode(envelope["data"]))
    assert hashlib.sha256(raw).hexdigest() == envelope["sha256"]
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    release, directory = args.release.resolve(), args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    database = directory / "state.db"
    store = Database(database)
    store.initialize(applied_at=datetime.now(UTC).isoformat())
    repository = ProjectRepository(store)
    snapshot = ProjectSnapshot("compiled-12k", "Isolated compiled editing", 1,
        source_cues=tuple(SubtitleCue(f"c{i}", i + 1, TimeRange(i * 2000, i * 2000 + 1500),
            f"原文{i}", "zh") for i in range(12000)))
    repository.save(project_path=directory / "fixture.ducai", snapshot=snapshot)
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    report = {"status": "RUNNING", "initial_cues": 12000, "processes": [], "steps": []}
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith("DUCAI_TEST_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    environment["DUCAI_STUDIO_DATA_ROOT"] = str(directory / "empty-runtime-data")

    for run in range(2):
        server_name = "compiled-smoke-" + uuid4().hex
        stdout = (directory / f"manager-{run}-stdout.txt").open("wb")
        stderr = (directory / f"manager-{run}-stderr.txt").open("wb")
        process = subprocess.Popen([str(release / "DucaiStudio2.exe"), "-m",
            "ducai_studio.job_manager", "--server-name", server_name, "--database", str(database),
            "--artifacts", str(directory / "artifacts")], cwd=release, env=environment,
            stdout=stdout, stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        report["processes"].append(process.pid)
        socket, decoder, responses = QLocalSocket(), FrameDecoder(), {}

        def send(action, params=None, *, socket=socket):
            request_id = uuid4().hex
            socket.write(encode_frame(IpcMessage(IPC_PROTOCOL_VERSION, request_id, "request",
                {"action": action, "params": params or {}})))
            socket.flush()
            return request_id

        def receive(request_id, *, socket=socket, decoder=decoder, responses=responses):
            deadline = time.monotonic() + 30
            while request_id not in responses:
                assert time.monotonic() < deadline, "Compiled IPC response timeout"
                if socket.bytesAvailable() or socket.waitForReadyRead(200):
                    for reply in decoder.feed(bytes(socket.readAll())):
                        responses[reply.payload["request_id"]] = reply.payload
            reply = responses.pop(request_id)
            assert reply["ok"], f"Compiled IPC request failed: {reply.get('error')}"
            return unpack(reply["result"]) if isinstance(reply["result"], dict) else reply["result"]

        def request(action, params=None):
            return receive(send(action, params))

        try:
            deadline = time.monotonic() + 20
            while socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
                assert process.poll() is None and time.monotonic() < deadline
                socket.abort()
                socket.connectToServer(server_name)
                socket.waitForConnected(200)
            assert request("ping")["license"]["active"], "Machine activation is required"
            project = request("get_project", {"project_id": snapshot.id})
            if run == 0:
                assert len(project["source_cues"]) == 12000
                patch = request("subtitle_command", {"project_id": snapshot.id,
                    "command": "update_text", "cue_id": "c6000", "values": {"text": "已修改"},
                    "compact": True, "deferred": True})
                assert patch["_subtitle_patch"] and patch["_project_dirty"]
                assert "source_cues" not in patch and len(json.dumps(patch)) < 12000
                request("save_project_edits", {"project_id": snapshot.id})
                request("subtitle_command", {"project_id": snapshot.id,
                    "command": "delete_cues", "values": {"cue_ids": ["c7000"]},
                    "compact": True, "deferred": True})
                for command, count in (("undo", 12000), ("redo", 11999)):
                    request("timeline_command", {"project_id": snapshot.id,
                        "command": command, "deferred": True})
                    current = request("get_project", {"project_id": snapshot.id})
                    assert len(current["source_cues"]) == count
                request("timeline_command", {"project_id": snapshot.id, "command": "set_markers",
                    "value": {"markers": [2_000_000, 4_000_000]}, "deferred": True})
                request("save_project_edits", {"project_id": snapshot.id})
                diagnostic = send("runtime_diagnostics",
                    {"data_root": str(directory / "empty-runtime-data")})
                ping = send("ping")
                assert receive(ping)["status"] == "ready"
                assert receive(diagnostic)["schema"] == "ducai-runtime-diagnostics-v1"
                report["steps"].extend(["load-12k", "compact-edit", "deferred-save", "delete",
                    "undo-redo", "markers", "diagnostics-with-ping"])
            else:
                assert len(project["source_cues"]) == 11999
                cues = {cue["id"]: cue for cue in project["source_cues"]}
                assert cues["c6000"]["text"] == "已修改" and "c7000" not in cues
                assert project["timeline_markers"] == [2_000_000, 4_000_000]
                assert cues["c6000"]["range"] == {"start_us": 12_000_000, "end_us": 12_001_500}
                report["steps"].append("restart-and-reopen-exact-saved-state")
            request("shutdown")
            assert process.wait(timeout=15) == 0
        finally:
            socket.abort()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            stdout.close()
            stderr.close()
    saved = ProjectRepository(Database(database)).load(snapshot.id)
    assert len(saved.source_cues) == 11999
    report["status"] = "PASS"
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("PASS: actual frozen manager edit/save/Undo/Redo/markers/diagnostics/restart on 12k cues")


if __name__ == "__main__":
    main()
