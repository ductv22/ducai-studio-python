import struct

import pytest

from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
    encode_frame,
)


def test_frame_decoder_handles_fragmented_and_combined_frames() -> None:
    first = IpcMessage(IPC_PROTOCOL_VERSION, "one", "request", {"action": "ping"})
    second = IpcMessage(IPC_PROTOCOL_VERSION, "two", "request", {"action": "ping"})
    encoded = encode_frame(first) + encode_frame(second)
    decoder = FrameDecoder()
    assert decoder.feed(encoded[:3]) == ()
    assert decoder.feed(encoded[3:11]) == ()
    assert decoder.feed(encoded[11:]) == (first, second)


def test_frame_decoder_rejects_oversized_frame() -> None:
    decoder = FrameDecoder()
    with pytest.raises(ValueError, match="Invalid IPC frame length"):
        decoder.feed(struct.pack(">I", 20 * 1024 * 1024))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"protocol_version":1.9,"message_id":"m","kind":"request","payload":{}}',
        b'{"protocol_version":1,"message_id":null,"kind":"request","payload":{}}',
        b'{"protocol_version":true,"message_id":"m","kind":"request","payload":{}}',
    ],
)
def test_ipc_message_rejects_coercible_but_invalid_json_types(payload: bytes) -> None:
    with pytest.raises((TypeError, ValueError)):
        IpcMessage.from_payload(payload)
