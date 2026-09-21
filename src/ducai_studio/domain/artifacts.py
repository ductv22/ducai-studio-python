from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .jobs import JsonValue, freeze_json
from .timebase import TimeUs


class ArtifactState(StrEnum):
    TECHNICAL = "technical"
    VALIDATED = "validated"
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    job_id: str
    kind: str
    uri: str
    sha256: str
    size_bytes: int
    state: ArtifactState
    created_at: str


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    job_id: str
    stage_key: str
    checkpoint_key: str
    schema_version: int
    resume_fingerprint: str
    cursor_us: TimeUs | None
    artifact_id: str | None
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json(dict(self.payload)))
