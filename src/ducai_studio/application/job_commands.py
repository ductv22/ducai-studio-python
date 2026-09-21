from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.domain.jobs import JobKind, JobSpec, JobState, assert_job_transition

type JobControlAction = Literal["start", "pause", "resume", "cancel"]


@dataclass(frozen=True, slots=True)
class JobControlIntent:
    action: JobControlAction
    target: JobState


_CONTROL_TARGETS = MappingProxyType(
    {
        "start": JobState.START_REQUESTED,
        "pause": JobState.PAUSE_REQUESTED,
        "resume": JobState.RESUME_REQUESTED,
        "cancel": JobState.CANCEL_REQUESTED,
    }
)


def control_intent(action: JobControlAction, current: JobState) -> JobControlIntent:
    target = _CONTROL_TARGETS[action]
    try:
        assert_job_transition(current, target)
    except ValueError:
        raise ValueError(f"Cannot {action} job from {current.value}") from None
    return JobControlIntent(action, target)


def retry_target(current: JobState) -> JobState:
    try:
        assert_job_transition(current, JobState.QUEUED)
    except ValueError:
        raise ValueError(f"Cannot retry job from {current.value}") from None
    return JobState.QUEUED


def decode_job_spec(raw: dict[str, Any]) -> JobSpec:
    required_strings = (
        "kind",
        "project_id",
        "provider_id",
        "provider_version",
        "model_id",
        "input_fingerprint",
    )
    for key in required_strings:
        if not isinstance(raw.get(key), str):
            raise ValueError(f"Job {key} must be a string")
    revision = raw.get("input_revision")
    if type(revision) is not int:
        raise ValueError("Job input_revision must be an integer")
    provider_config = raw.get("provider_config", {})
    if not isinstance(provider_config, dict):
        raise ValueError("Job provider_config must be an object")
    settings = raw.get("settings_fingerprint")
    if settings is None:
        settings = stable_fingerprint(provider_config)
    elif not isinstance(settings, str):
        raise ValueError("Job settings_fingerprint must be a string")
    return JobSpec(
        kind=JobKind(raw["kind"]),
        project_id=raw["project_id"],
        input_revision=revision,
        provider_id=raw["provider_id"],
        provider_version=raw["provider_version"],
        model_id=raw["model_id"],
        input_fingerprint=raw["input_fingerprint"],
        settings_fingerprint=settings,
        provider_config=provider_config,
    )
