import pytest

from ducai_studio.domain.jobs import JobKind, JobSpec, JobState, assert_job_transition


def make_spec(**overrides) -> JobSpec:
    values = {
        "kind": JobKind.OCR,
        "project_id": "project",
        "input_revision": 1,
        "provider_id": "paddle-fast",
        "provider_version": "3.2.0",
        "model_id": "PP-OCRv5-server",
        "input_fingerprint": "input-v1",
        "settings_fingerprint": "settings-v1",
        "provider_config": {"mode": "quality"},
    }
    values.update(overrides)
    return JobSpec(**values)


def test_valid_pause_resume_and_cancel_transitions() -> None:
    assert_job_transition(JobState.QUEUED, JobState.START_REQUESTED)
    assert_job_transition(JobState.START_REQUESTED, JobState.RUNNING)
    assert_job_transition(JobState.RUNNING, JobState.PAUSE_REQUESTED)
    assert_job_transition(JobState.PAUSE_REQUESTED, JobState.PAUSED)
    assert_job_transition(JobState.PAUSED, JobState.RESUME_REQUESTED)
    assert_job_transition(JobState.RESUME_REQUESTED, JobState.RUNNING)
    assert_job_transition(JobState.CANCEL_REQUESTED, JobState.CANCELLED)


def test_terminal_success_cannot_restart() -> None:
    with pytest.raises(ValueError, match="Invalid job transition"):
        assert_job_transition(JobState.SUCCEEDED, JobState.QUEUED)


def test_job_spec_rejects_provider_secrets_but_allows_credential_references() -> None:
    with pytest.raises(ValueError, match="secret"):
        make_spec(provider_config={"api_key": "do-not-store"})
    with pytest.raises(ValueError, match="secret"):
        make_spec(provider_config={"profiles": [{"oauthToken": "do-not-store"}]})
    with pytest.raises(ValueError, match="secret"):
        make_spec(provider_config={"nested": {"token": "do-not-store"}})
    with pytest.raises(ValueError, match="secret value"):
        make_spec(provider_config={"endpoint": "https://example.test?api_key=do-not-store"})

    spec = make_spec(
        provider_config={"profiles": [{"credentialRef": "gemini-account-1"}]}
    )
    assert spec.provider_config["profiles"][0]["credentialRef"] == "gemini-account-1"


def test_provider_configuration_is_deeply_immutable_after_validation() -> None:
    source = {"profiles": [{"credentialRef": "gemini-account-1"}]}
    spec = make_spec(provider_config=source)
    source["profiles"][0]["api_key"] = "late-secret"

    assert "api_key" not in spec.provider_config["profiles"][0]
    with pytest.raises(TypeError, match="immutable"):
        spec.provider_config["token"] = "late-secret"
    with pytest.raises(TypeError, match="immutable"):
        spec.provider_config["profiles"][0]["token"] = "late-secret"
    with pytest.raises(TypeError):
        dict.__setitem__(spec.provider_config, "api_key", "late-secret")


def test_job_spec_rejects_boolean_revision_at_domain_boundary() -> None:
    with pytest.raises(TypeError, match="integer"):
        make_spec(input_revision=True)


def test_resume_fingerprint_covers_provider_model_and_settings() -> None:
    baseline = make_spec()
    assert make_spec(provider_version="3.2.1").resume_fingerprint != baseline.resume_fingerprint
    assert make_spec(model_id="PP-OCRv5-mobile").resume_fingerprint != baseline.resume_fingerprint
    assert make_spec(settings_fingerprint="settings-v2").resume_fingerprint != (
        baseline.resume_fingerprint
    )
