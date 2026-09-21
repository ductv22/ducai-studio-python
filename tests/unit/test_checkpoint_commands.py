import pytest

from ducai_studio.application.checkpoint_commands import CommitCheckpointCommand


def test_checkpoint_command_preserves_integer_microseconds() -> None:
    command = CommitCheckpointCommand.from_payload({"cursor_us": 42_000_001})
    assert command.cursor_us == 42_000_001
    assert command.stage_key == "runtime"


@pytest.mark.parametrize(
    "payload",
    [
        {"cursor_us": 1.9},
        {"cursor_us": True},
        {"schema_version": 1.9},
        {"schema_version": True},
        {"stage_key": None},
        {"payload": []},
        {"artifact_state": None},
    ],
)
def test_checkpoint_command_rejects_coercible_invalid_types(payload: dict) -> None:
    with pytest.raises((TypeError, ValueError)):
        CommitCheckpointCommand.from_payload(payload)
