"""Native IPC stays responsive while slow hardware diagnostics run separately."""

import time
from threading import Event
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtTest import QTest

from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
    encode_frame,
)
from ducai_studio.job_manager import server as server_module
from ducai_studio.job_manager.server import JobManagerServer
from ducai_studio.job_manager.service import JobManagerService


@pytest.mark.parametrize("finish", ["complete", "disconnect", "shutdown", "error"])
def test_diagnostics_leave_ping_responsive_and_stop_cleanly(tmp_path, monkeypatch, finish):
    app = QCoreApplication.instance() or QCoreApplication([])
    started, stopped, release = Event(), Event(), Event()

    def diagnostic(*, data_root, cancelled):
        assert data_root == str(tmp_path)
        started.set()
        try:
            while not release.wait(0.005):
                if cancelled():
                    return None
            if finish == "error":
                raise OSError("private diagnostic stderr must not be published")
            return {"schema": "ducai-runtime-diagnostics-v1", "checks": []}
        finally:
            stopped.set()

    monkeypatch.setattr(server_module, "isolated_runtime_diagnostics", diagnostic)
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts")
    server = JobManagerServer(name=f"diagnostics-{uuid4()}", service=service)
    socket, decoder, replies = QLocalSocket(), FrameDecoder(), {}

    def until(condition):
        deadline = time.monotonic() + 3
        while not condition():
            assert time.monotonic() < deadline
            app.processEvents()
            if socket.bytesAvailable():
                for response in decoder.feed(bytes(socket.readAll())):
                    replies[response.payload["request_id"]] = response.payload
            QTest.qWait(5)

    def send(action, request_id, params=None):
        socket.write(encode_frame(IpcMessage(IPC_PROTOCOL_VERSION, request_id, "request",
                     {"action": action, "params": params or {}})))
        socket.flush()

    try:
        server.listen()
        socket.connectToServer(server.name)
        assert socket.waitForConnected(1000)
        until(lambda: bool(server._decoders))
        send("runtime_diagnostics", "diagnostic", {"data_root": str(tmp_path)})
        until(started.is_set)
        send("ping", "ping")
        until(lambda: "ping" in replies)
        assert replies["ping"]["ok"] and replies["ping"]["result"]["status"] == "ready"
        assert "diagnostic" not in replies and not stopped.is_set()
        send("runtime_diagnostics", "duplicate")
        until(lambda: "duplicate" in replies)
        assert replies["duplicate"]["ok"] is False
        if finish == "disconnect":
            socket.abort()
            until(lambda: not server._decoders)
            until(stopped.is_set)
        elif finish == "shutdown":
            quit_calls = []
            monkeypatch.setattr(server, "_quit_application", lambda: quit_calls.append(True))
            send("shutdown", "shutdown")
            until(lambda: "shutdown" in replies)
            until(stopped.is_set)
            until(lambda: bool(quit_calls))
        else:
            release.set()
            until(lambda: "diagnostic" in replies)
            if finish == "error":
                assert replies["diagnostic"]["ok"] is False
                assert "private" not in str(replies["diagnostic"])
            else:
                assert replies["diagnostic"]["result"]["schema"] == "ducai-runtime-diagnostics-v1"
        server.close()
        assert stopped.wait(1)
        until(lambda: not server._diagnostic_threads)
    finally:
        release.set()
        server.close()
        socket.abort()
