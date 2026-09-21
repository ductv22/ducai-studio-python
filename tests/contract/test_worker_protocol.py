import pytest

from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def test_worker_message_round_trip_preserves_unicode() -> None:
    message = WorkerMessage(
        protocol_version=PROTOCOL_VERSION,
        job_id="job-1",
        event_id="event-1",
        kind="progress",
        payload={"message": "Đang nhận dạng phụ đề 中文", "completed": 12},
    )
    assert WorkerMessage.from_json_line(message.to_json_line()) == message


def test_worker_message_rejects_unknown_fields_and_versions() -> None:
    with pytest.raises(ValueError, match="Unsupported protocol"):
        WorkerMessage(2, "job", "event", "progress")
    with pytest.raises(ValueError, match="Unknown worker message fields"):
        WorkerMessage.from_json_line(
            '{"protocol_version":1,"job_id":"j","event_id":"e","kind":"x","payload":{},"extra":1}'
        )


@pytest.mark.parametrize(
    "line",
    [
        '{"protocol_version":1.9,"job_id":"j","event_id":"e","kind":"x","payload":{}}',
        '{"protocol_version":1,"job_id":null,"event_id":"e","kind":"x","payload":{}}',
        '{"protocol_version":true,"job_id":"j","event_id":"e","kind":"x","payload":{}}',
    ],
)
def test_worker_message_rejects_coercible_but_invalid_json_types(line: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        WorkerMessage.from_json_line(line)
