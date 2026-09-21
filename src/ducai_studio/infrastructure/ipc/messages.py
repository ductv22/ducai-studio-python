from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field

from ducai_studio.domain.jobs import JsonValue

IPC_PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
_HEADER = struct.Struct(">I")


@dataclass(frozen=True, slots=True)
class IpcMessage:
    protocol_version: int
    message_id: str
    kind: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int:
            raise TypeError("IPC protocol version must be an integer")
        if not isinstance(self.message_id, str) or not isinstance(self.kind, str):
            raise TypeError("IPC message id and kind must be strings")
        if not isinstance(self.payload, Mapping):
            raise TypeError("IPC message payload must be an object")
        if self.protocol_version != IPC_PROTOCOL_VERSION:
            raise ValueError(f"Unsupported IPC protocol: {self.protocol_version}")
        if not self.message_id.strip() or not self.kind.strip():
            raise ValueError("IPC message id and kind are required")

    def to_payload(self) -> bytes:
        return json.dumps(
            {
                "protocol_version": self.protocol_version,
                "message_id": self.message_id,
                "kind": self.kind,
                "payload": dict(self.payload),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_payload(cls, payload: bytes) -> IpcMessage:
        raw = json.loads(payload.decode("utf-8", "strict"))
        if not isinstance(raw, dict):
            raise ValueError("IPC payload must be an object")
        expected = {"protocol_version", "message_id", "kind", "payload"}
        missing = expected - set(raw)
        if missing:
            raise ValueError(f"Missing IPC fields: {sorted(missing)}")
        unknown = set(raw) - expected
        if unknown:
            raise ValueError(f"Unknown IPC fields: {sorted(unknown)}")
        body = raw.get("payload", {})
        if not isinstance(body, dict):
            raise ValueError("IPC message payload must be an object")
        if type(raw["protocol_version"]) is not int:
            raise ValueError("IPC protocol version must be an integer")
        if not isinstance(raw["message_id"], str) or not isinstance(raw["kind"], str):
            raise ValueError("IPC message id and kind must be strings")
        return cls(raw["protocol_version"], raw["message_id"], raw["kind"], body)


def encode_frame(message: IpcMessage) -> bytes:
    payload = message.to_payload()
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("IPC message exceeds the frame limit")
    return _HEADER.pack(len(payload)) + payload


class FrameDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[IpcMessage, ...]:
        self._buffer.extend(chunk)
        messages: list[IpcMessage] = []
        while len(self._buffer) >= _HEADER.size:
            (length,) = _HEADER.unpack(self._buffer[: _HEADER.size])
            if length <= 0 or length > MAX_FRAME_BYTES:
                self._buffer.clear()
                raise ValueError(f"Invalid IPC frame length: {length}")
            frame_end = _HEADER.size + length
            if len(self._buffer) < frame_end:
                break
            payload = bytes(self._buffer[_HEADER.size:frame_end])
            del self._buffer[:frame_end]
            messages.append(IpcMessage.from_payload(payload))
        return tuple(messages)
