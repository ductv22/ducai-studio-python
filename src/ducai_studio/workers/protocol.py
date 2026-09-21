from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from ducai_studio.domain.jobs import JsonValue

PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class WorkerMessage:
    protocol_version: int
    job_id: str
    event_id: str
    kind: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int:
            raise TypeError("Worker protocol version must be an integer")
        if not all(isinstance(value, str) for value in (self.job_id, self.event_id, self.kind)):
            raise TypeError("Worker message identifiers and kind must be strings")
        if not isinstance(self.payload, Mapping):
            raise TypeError("Worker message payload must be an object")
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"Unsupported protocol version: {self.protocol_version}")
        if not self.job_id.strip() or not self.event_id.strip() or not self.kind.strip():
            raise ValueError("Worker message identifiers and kind are required")

    def to_json_line(self) -> bytes:
        body = {
            "protocol_version": self.protocol_version,
            "job_id": self.job_id,
            "event_id": self.event_id,
            "kind": self.kind,
            "payload": dict(self.payload),
        }
        return (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    @classmethod
    def from_json_line(cls, line: bytes | str) -> WorkerMessage:
        text = line.decode("utf-8", "strict") if isinstance(line, bytes) else line
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("Worker message must be a JSON object")
        expected = {"protocol_version", "job_id", "event_id", "kind", "payload"}
        missing = expected - set(raw)
        if missing:
            raise ValueError(f"Missing worker message fields: {sorted(missing)}")
        unknown = set(raw) - expected
        if unknown:
            raise ValueError(f"Unknown worker message fields: {sorted(unknown)}")
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Worker message payload must be an object")
        if type(raw["protocol_version"]) is not int:
            raise ValueError("Worker protocol version must be an integer")
        if not all(
            isinstance(raw[field], str) for field in ("job_id", "event_id", "kind")
        ):
            raise ValueError("Worker message identifiers and kind must be strings")
        return cls(
            raw["protocol_version"],
            raw["job_id"],
            raw["event_id"],
            raw["kind"],
            payload,
        )
