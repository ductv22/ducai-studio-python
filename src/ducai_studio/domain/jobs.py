from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from .fingerprint import stable_fingerprint

type JsonScalar = str | int | float | bool | None
type JsonValue = (
    JsonScalar
    | list[JsonValue]
    | tuple[JsonValue, ...]
    | dict[str, JsonValue]
    | Mapping[str, JsonValue]
)

_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer_token",
        "oauth_token",
        "token",
        "password",
        "secret",
        "client_secret",
        "private_key",
        "credential",
    }
)

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[ _-]?key|access[ _-]?token|refresh[ _-]?token|auth[ _-]?token|"
    r"bearer[ _-]?token|oauth[ _-]?token|token|password|secret|credential|"
    r"client[ _-]?secret|private[ _-]?key)"
    r"\b(\s*[:=]\s*)['\"]?([^\s,;'\"]+)"
)
_SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN_VALUE = re.compile(
    r"\b(?:AIza[0-9A-Za-z_-]{30,}|sk-[0-9A-Za-z_-]{16,}|"
    r"eyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,})\b"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)


def _normalized_config_key(raw_key: object) -> str:
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(raw_key).strip())
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def _is_secret_field(raw_key: object) -> bool:
    key = _normalized_config_key(raw_key)
    is_reference = key.endswith(
        ("credential_id", "credential_ref", "credential_reference")
    )
    return not is_reference and (
        key in _SECRET_FIELD_NAMES
        or any(
            key.endswith(suffix)
            for suffix in (
                "_api_key",
                "_password",
                "_secret",
                "_private_key",
                "_token",
            )
        )
    )


def redact_sensitive_text(value: str) -> str:
    value = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", value)
    value = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value
    )
    value = _BEARER_VALUE.sub("Bearer [REDACTED]", value)
    return _KNOWN_TOKEN_VALUE.sub("[REDACTED TOKEN]", value)


def assert_safe_error_code(value: str) -> None:
    if (
        not isinstance(value, str)
        or not _SAFE_ERROR_CODE.fullmatch(value)
        or redact_sensitive_text(value) != value
    ):
        raise ValueError("Job error code must be a bounded identifier")


def assert_no_secret_fields(value: JsonValue, *, path: str = "config") -> None:
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            assert_no_secret_fields(item, path=f"{path}[{index}]")
        return
    else:
        if isinstance(value, str) and redact_sensitive_text(value) != value:
            raise ValueError(f"Provider configuration cannot store a secret value: {path}")
        return

    for raw_key, item in items:
        if _is_secret_field(raw_key):
            raise ValueError(f"Provider configuration cannot store secret field: {path}.{raw_key}")
        assert_no_secret_fields(item, path=f"{path}.{raw_key}")


def redact_secret_fields(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_secret_field(key) else redact_secret_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_secret_fields(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


class FrozenJsonMapping(Mapping[str, JsonValue]):
    __slots__ = ("_values",)

    def __init__(self, values=()) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getitem__(self, key: str) -> JsonValue:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return repr(dict(self._values))

    @staticmethod
    def _immutable(*_args, **_kwargs) -> None:
        raise TypeError("Provider configuration is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __setattr__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __deepcopy__(self, _memo):
        return self


def freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return FrozenJsonMapping((str(key), freeze_json(item)) for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [thaw_json(item) for item in value]
    return value


class JobKind(StrEnum):
    OCR = "ocr"
    TRANSLATE = "translate"
    TTS = "tts"
    EXPORT = "export"


class JobState(StrEnum):
    QUEUED = "queued"
    START_REQUESTED = "start_requested"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    RESUME_REQUESTED = "resume_requested"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


TERMINAL_JOB_STATES = frozenset({JobState.CANCELLED, JobState.SUCCEEDED, JobState.FAILED})

ALLOWED_JOB_TRANSITIONS: Mapping[JobState, frozenset[JobState]] = MappingProxyType(
    {
        JobState.QUEUED: frozenset({JobState.START_REQUESTED, JobState.CANCEL_REQUESTED}),
        JobState.START_REQUESTED: frozenset(
            {
                JobState.RUNNING,
                JobState.CANCEL_REQUESTED,
                JobState.FAILED,
                JobState.INTERRUPTED,
            }
        ),
        JobState.RUNNING: frozenset(
            {
                JobState.PAUSE_REQUESTED,
                JobState.CANCEL_REQUESTED,
                JobState.SUCCEEDED,
                JobState.FAILED,
                JobState.INTERRUPTED,
            }
        ),
        JobState.PAUSE_REQUESTED: frozenset(
            {JobState.PAUSED, JobState.CANCEL_REQUESTED, JobState.FAILED, JobState.INTERRUPTED}
        ),
        JobState.PAUSED: frozenset(
            {
                JobState.RESUME_REQUESTED,
                JobState.CANCEL_REQUESTED,
                JobState.FAILED,
                JobState.INTERRUPTED,
            }
        ),
        JobState.RESUME_REQUESTED: frozenset(
            {
                JobState.RUNNING,
                JobState.CANCEL_REQUESTED,
                JobState.FAILED,
                JobState.INTERRUPTED,
            }
        ),
        JobState.CANCEL_REQUESTED: frozenset(
            {JobState.CANCELLED, JobState.FAILED, JobState.INTERRUPTED}
        ),
        JobState.INTERRUPTED: frozenset({JobState.QUEUED, JobState.CANCEL_REQUESTED}),
        # A cancelled translation keeps its immutable ledger/checkpoint and
        # can be explicitly retried from that cache. The server limits this
        # transition to translation jobs and enforces the active-job guard.
        JobState.CANCELLED: frozenset({JobState.QUEUED}),
        JobState.SUCCEEDED: frozenset(),
        JobState.FAILED: frozenset({JobState.QUEUED}),
    }
)


def assert_job_transition(current: JobState, target: JobState) -> None:
    if target not in ALLOWED_JOB_TRANSITIONS[current]:
        raise ValueError(f"Invalid job transition: {current.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class JobSpec:
    kind: JobKind
    project_id: str
    input_revision: int
    provider_id: str
    provider_version: str
    model_id: str
    input_fingerprint: str
    settings_fingerprint: str
    provider_config: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, JobKind):
            raise TypeError("Job kind must be a JobKind")
        if type(self.input_revision) is not int:
            raise TypeError("Job revision must be an integer")
        for name, value in (
            ("project_id", self.project_id),
            ("provider_id", self.provider_id),
            ("provider_version", self.provider_version),
            ("model_id", self.model_id),
            ("input_fingerprint", self.input_fingerprint),
            ("settings_fingerprint", self.settings_fingerprint),
        ):
            if not isinstance(value, str):
                raise TypeError(f"Job {name} must be a string")
        if not isinstance(self.provider_config, Mapping):
            raise TypeError("Job provider configuration must be an object")
        if not self.project_id.strip() or not self.provider_id.strip():
            raise ValueError("Job project and provider are required")
        if (
            self.input_revision <= 0
            or not self.input_fingerprint.strip()
            or not self.provider_version.strip()
            or not self.model_id.strip()
            or not self.settings_fingerprint.strip()
        ):
            raise ValueError("Job revision and fingerprint are required")
        config = dict(self.provider_config)
        assert_no_secret_fields(config)
        object.__setattr__(self, "provider_config", freeze_json(config))

    @property
    def resume_fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "input": self.input_fingerprint,
                "provider": self.provider_id,
                "provider_version": self.provider_version,
                "model": self.model_id,
                "settings": self.settings_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    spec: JobSpec
    state: JobState
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    error_payload: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "error_payload", freeze_json(dict(self.error_payload)))


@dataclass(frozen=True, slots=True)
class JobEvent:
    protocol_version: int
    event_id: str
    job_id: str
    sequence: int
    kind: str
    occurred_at: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.protocol_version <= 0 or self.sequence < 0:
            raise ValueError("Protocol version must be positive and sequence non-negative")
        if not self.event_id.strip() or not self.job_id.strip() or not self.kind.strip():
            raise ValueError("Job event identifiers and kind are required")
        object.__setattr__(self, "payload", freeze_json(dict(self.payload)))
