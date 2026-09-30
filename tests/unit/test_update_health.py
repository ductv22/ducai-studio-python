from __future__ import annotations

import os

import pytest

from ducai_studio.__main__ import _consume_update_health_args, _dispatch_update_stage_verifier


def test_update_health_arguments_are_removed_before_qt(monkeypatch) -> None:
    for name in (
        "DUCAI_UPDATE_HEALTH_PATH",
        "DUCAI_UPDATE_HEALTH_NONCE",
        "DUCAI_UPDATE_HEALTH_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)

    remaining = _consume_update_health_args(
        [
            "--some-qt-arg",
            "--update-health-path",
            "C:/staging/health.json",
            "--update-health-nonce",
            "nonce-value",
            "--update-health-version",
            "2.0.0a118",
        ]
    )

    assert remaining == ["--some-qt-arg"]
    assert os.environ["DUCAI_UPDATE_HEALTH_PATH"] == "C:/staging/health.json"
    assert os.environ["DUCAI_UPDATE_HEALTH_NONCE"] == "nonce-value"
    assert os.environ["DUCAI_UPDATE_HEALTH_VERSION"] == "2.0.0a118"


def test_update_health_arguments_must_be_complete(monkeypatch) -> None:
    monkeypatch.delenv("DUCAI_UPDATE_HEALTH_PATH", raising=False)
    with pytest.raises(ValueError, match="Incomplete"):
        _consume_update_health_args(["--update-health-path", "C:/health.json"])


def test_update_stage_verifier_dispatches_before_qt(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        "ducai_studio.infrastructure.update_install.verify_staged_update",
        lambda **values: captured.update(values),
    )

    result = _dispatch_update_stage_verifier(
        [
            "--verify-update-stage",
            "C:/stage/update-manifest.json",
            "--signature",
            "C:/stage/update-manifest.sig",
            "--payload",
            "C:/stage/payload",
            "--manifest-sha256",
            "a" * 64,
        ]
    )

    assert result == 0
    assert captured["expected_manifest_sha256"] == "a" * 64
    assert _dispatch_update_stage_verifier(["--verify-update-stage"]) == 2
