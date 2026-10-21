from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure import licensing_state
from ducai_studio.infrastructure.offline_license import (
    PRODUCT,
    TOKEN_PREFIX,
    canonical_payload,
    encode_base64,
)

TEST_DEVICE_ID = hashlib.sha256(b"ducai-test-device-v1").hexdigest()
TEST_KEY_ID = "test-license-key"
TEST_LICENSE_ID = hashlib.md5(b"ducai-test-license", usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class SignedLicenseFixture:
    private_key: Ed25519PrivateKey
    public_key: bytes
    device_id: str = TEST_DEVICE_ID
    key_id: str = TEST_KEY_ID

    @property
    def public_keys(self) -> dict[str, bytes]:
        return {self.key_id: self.public_key}

    def payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": 1,
            "product": PRODUCT,
            "key_id": self.key_id,
            "license_id": TEST_LICENSE_ID,
            "device_schema": "windows-v1",
            "device_id": self.device_id,
            "issued_at": "2026-10-03T00:00:00Z",
            "edition": "full",
        }
        payload.update(overrides)
        return payload

    def token(
        self,
        *,
        payload: dict[str, object] | None = None,
        signer: Ed25519PrivateKey | None = None,
    ) -> str:
        value = self.payload() if payload is None else payload
        signature = (signer or self.private_key).sign(canonical_payload(value))
        envelope = json.dumps(
            {"payload": value, "signature": encode_base64(signature)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return TOKEN_PREFIX + encode_base64(envelope)


@pytest.fixture(scope="session")
def license_fixture() -> SignedLicenseFixture:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return SignedLicenseFixture(private_key=private_key, public_key=public_key)


@pytest.fixture(autouse=True)
def licensed_test_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    license_fixture: SignedLicenseFixture,
) -> Path:
    """Keep legacy tests licensed without a production bypass or real machine ID."""

    path = tmp_path / "test-license-v1.txt"
    path.write_text(license_fixture.token(), encoding="utf-8")
    baseline_path = path
    original_init = licensing_state.LicensingState.__init__

    def test_init(
        self,
        *,
        path: Path | None = None,
        identity=None,
        public_keys: dict[str, bytes] | None = None,
    ) -> None:
        if path is None and identity is None and public_keys is None:
            original_init(
                self,
                path=baseline_path,
                identity=lambda: license_fixture.device_id,
                public_keys=license_fixture.public_keys,
            )
            return
        original_init(self, path=path, identity=identity, public_keys=public_keys)

    monkeypatch.setattr(licensing_state.LicensingState, "__init__", test_init)

    # Legacy tests exercise product behavior, not activation. Preserve that
    # baseline only after a real signed token has passed production verification.
    from ducai_studio.ui.viewmodels.application_view_model import JobRuntime
    from ducai_studio.ui.viewmodels.license_view_model import LicenseViewModel

    original_runtime_init = JobRuntime.__init__
    original_view_model_init = LicenseViewModel.__init__

    def licensed_runtime_init(self, *args, **kwargs) -> None:
        original_runtime_init(self, *args, **kwargs)
        self._licensed = licensing_state.LicensingState().status()["active"] is True

    def licensed_view_model_init(self, *args, **kwargs) -> None:
        original_view_model_init(self, *args, **kwargs)
        self._status = licensing_state.LicensingState().status()

    monkeypatch.setattr(JobRuntime, "__init__", licensed_runtime_init)
    monkeypatch.setattr(LicenseViewModel, "__init__", licensed_view_model_init)

    bootstrap = Path(__file__).parent / "fixtures" / "license-bootstrap"
    current_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = str(bootstrap)
    if current_pythonpath:
        pythonpath += os.pathsep + current_pythonpath
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.setenv("DUCAI_TEST_LICENSE_BOOTSTRAP", "1")
    monkeypatch.setenv("DUCAI_TEST_LICENSE_PATH", str(path))
    monkeypatch.setenv("DUCAI_TEST_LICENSE_DEVICE_ID", license_fixture.device_id)
    monkeypatch.setenv("DUCAI_TEST_LICENSE_KEY_ID", license_fixture.key_id)
    monkeypatch.setenv(
        "DUCAI_TEST_LICENSE_PUBLIC_KEY",
        base64.urlsafe_b64encode(license_fixture.public_key).decode("ascii"),
    )
    return path
