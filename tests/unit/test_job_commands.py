import pytest

from ducai_studio.application.job_commands import control_intent, decode_job_spec, retry_target
from ducai_studio.domain.jobs import JobState


def test_control_intents_define_application_state_boundaries() -> None:
    assert control_intent("start", JobState.QUEUED).target is JobState.START_REQUESTED
    assert control_intent("pause", JobState.RUNNING).target is JobState.PAUSE_REQUESTED
    assert control_intent("resume", JobState.PAUSED).target is JobState.RESUME_REQUESTED
    assert control_intent("cancel", JobState.INTERRUPTED).target is JobState.CANCEL_REQUESTED


def test_invalid_control_and_retry_are_rejected_before_adapter_side_effects() -> None:
    with pytest.raises(ValueError, match="Cannot start"):
        control_intent("start", JobState.RUNNING)
    with pytest.raises(ValueError, match="Cannot pause"):
        control_intent("pause", JobState.PAUSED)
    with pytest.raises(ValueError, match="Cannot retry"):
        retry_target(JobState.RUNNING)

    assert retry_target(JobState.INTERRUPTED) is JobState.QUEUED
    assert retry_target(JobState.FAILED) is JobState.QUEUED


def test_job_command_decoder_rejects_silent_type_coercion() -> None:
    raw = {
        "kind": "ocr",
        "project_id": "project",
        "input_revision": 1,
        "provider_id": "paddle-fast",
        "provider_version": "3.2.0",
        "model_id": "PP-OCRv5-server",
        "input_fingerprint": "fixture",
        "provider_config": {},
    }
    assert decode_job_spec(raw).input_revision == 1
    with pytest.raises(ValueError, match="input_revision"):
        decode_job_spec({**raw, "input_revision": 1.9})
    with pytest.raises(ValueError, match="project_id"):
        decode_job_spec({**raw, "project_id": None})
