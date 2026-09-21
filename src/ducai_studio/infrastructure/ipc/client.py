from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalSocket

from .messages import IPC_PROTOCOL_VERSION, FrameDecoder, IpcMessage, encode_frame


class JobManagerClient(QObject):
    connectedChanged = Signal(bool)
    responseReceived = Signal(object)
    errorOccurred = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.socket = QLocalSocket(self)
        self._decoder = FrameDecoder()
        self.socket.connected.connect(lambda: self.connectedChanged.emit(True))
        self.socket.disconnected.connect(lambda: self.connectedChanged.emit(False))
        self.socket.errorOccurred.connect(
            lambda _error: self.errorOccurred.emit(self.socket.errorString())
        )
        self.socket.readyRead.connect(self._read)

    def connect_to(self, server_name: str) -> None:
        if self.socket.state() is not QLocalSocket.LocalSocketState.UnconnectedState:
            return
        self.socket.connectToServer(server_name)

    def disconnect(self) -> None:
        self.socket.abort()

    def request(self, action: str, params: dict | None = None) -> str:
        if self.socket.state() is not QLocalSocket.LocalSocketState.ConnectedState:
            raise RuntimeError("Job Manager is not connected")
        request_id = str(uuid4())
        message = IpcMessage(
            protocol_version=IPC_PROTOCOL_VERSION,
            message_id=request_id,
            kind="request",
            payload={"action": action, "params": params or {}},
        )
        self.socket.write(encode_frame(message))
        return request_id

    def _read(self) -> None:
        try:
            messages = self._decoder.feed(bytes(self.socket.readAll()))
        except Exception as exc:
            self.errorOccurred.emit(f"Invalid Job Manager response: {exc}")
            self.socket.abort()
            return
        for message in messages:
            self.responseReceived.emit(message)
