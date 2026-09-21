from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ducai_studio.domain.artifacts import ArtifactState
from ducai_studio.domain.jobs import JsonValue, freeze_json


def _optional_string(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Checkpoint {key} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class CommitCheckpointCommand:
    stage_key: str = "runtime"
    checkpoint_key: str = "latest"
    schema_version: int = 1
    cursor_us: int | None = None
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    artifact_path: str | None = None
    artifact_kind: str = "checkpoint"
    artifact_state: ArtifactState = ArtifactState.TECHNICAL
    artifact_id: str | None = None
    expected_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.stage_key.strip() or not self.checkpoint_key.strip():
            raise ValueError("Checkpoint stage and key are required")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("Checkpoint schema version must be a positive integer")
        if self.cursor_us is not None and (
            type(self.cursor_us) is not int or self.cursor_us < 0
        ):
            raise ValueError("Checkpoint cursor must be a non-negative integer")
        if not self.artifact_kind.strip():
            raise ValueError("Checkpoint artifact kind is required")
        object.__setattr__(self, "payload", freeze_json(dict(self.payload)))

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> CommitCheckpointCommand:
        stage_key = raw.get("stage_key", "runtime")
        checkpoint_key = raw.get("checkpoint_key", "latest")
        schema_version = raw.get("schema_version", 1)
        cursor_us = raw.get("cursor_us")
        body = raw.get("payload", {})
        artifact_kind = raw.get("artifact_kind", "checkpoint")
        artifact_state = raw.get("artifact_state", ArtifactState.TECHNICAL.value)
        if not isinstance(stage_key, str) or not isinstance(checkpoint_key, str):
            raise ValueError("Checkpoint stage and key must be strings")
        if type(schema_version) is not int:
            raise ValueError("Checkpoint schema version must be an integer")
        if cursor_us is not None and type(cursor_us) is not int:
            raise ValueError("Checkpoint cursor must be an integer")
        if not isinstance(body, dict):
            raise ValueError("Checkpoint payload must be an object")
        if not isinstance(artifact_kind, str):
            raise ValueError("Checkpoint artifact kind must be a string")
        if not isinstance(artifact_state, str):
            raise ValueError("Checkpoint artifact state must be a string")
        return cls(
            stage_key=stage_key,
            checkpoint_key=checkpoint_key,
            schema_version=schema_version,
            cursor_us=cursor_us,
            payload=body,
            artifact_path=_optional_string(raw, "artifact_path"),
            artifact_kind=artifact_kind,
            artifact_state=ArtifactState(artifact_state),
            artifact_id=_optional_string(raw, "artifact_id"),
            expected_sha256=_optional_string(raw, "expected_sha256"),
        )
