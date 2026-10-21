from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure import licensing_state as state_module
from ducai_studio.infrastructure.licensing_state import LicensingState
from ducai_studio.infrastructure.machine_identity import (
    identity_digest,
    parse_request_code,
    request_code,
)
from ducai_studio.infrastructure.offline_license import (
    LICENSE_KEY_ID,
    MAX_TOKEN_BYTES,
    TOKEN_PREFIX,
    LicenseError,
    canonical_payload,
    encode_base64,
    encode_expiring_license,
    verify_license,
)


def test_real_ed25519_token_verifies_only_for_its_machine(license_fixture) -> None:
    token = license_fixture.token()

    payload = verify_license(
        token,
        device_id=license_fixture.device_id,
        public_keys=license_fixture.public_keys,
    )

    assert payload["edition"] == "full"
    assert payload["license_id"]
    with pytest.raises(LicenseError, match="máy khác"):
        verify_license(
            token,
            device_id="f" * 64,
            public_keys=license_fixture.public_keys,
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"schema": 2}, "Phiên bản"),
        ({"product": "other-product"}, "sản phẩm"),
        ({"device_schema": "windows-v2"}, "sản phẩm"),
        ({"edition": "trial"}, "sản phẩm"),
        ({"license_id": "not-hex"}, "Mã giấy phép"),
        ({"issued_at": "03/10/2026"}, "Ngày cấp"),
    ],
)
def test_signed_but_invalid_payload_is_rejected(
    license_fixture, override: dict[str, object], message: str
) -> None:
    token = license_fixture.token(payload=license_fixture.payload(**override))
    with pytest.raises(LicenseError, match=message):
        verify_license(
            token,
            device_id=license_fixture.device_id,
            public_keys=license_fixture.public_keys,
        )


def test_wrong_key_tamper_unknown_and_duplicate_fields_fail_closed(license_fixture) -> None:
    wrong_key = Ed25519PrivateKey.generate()
    wrong_signature = license_fixture.token(signer=wrong_key)
    with pytest.raises(LicenseError, match="chữ ký"):
        verify_license(
            wrong_signature,
            device_id=license_fixture.device_id,
            public_keys=license_fixture.public_keys,
        )

    unknown = license_fixture.payload(unexpected="value")
    with pytest.raises(LicenseError, match="Cấu trúc"):
        verify_license(
            license_fixture.token(payload=unknown),
            device_id=license_fixture.device_id,
            public_keys=license_fixture.public_keys,
        )

    payload_json = canonical_payload(license_fixture.payload()).decode("utf-8")
    duplicate_payload = payload_json.replace(
        '"edition":"full"',
        '"edition":"full","edition":"full"',
    )
    envelope = (
        '{"payload":'
        + duplicate_payload
        + ',"signature":"'
        + encode_base64(b"x" * 64)
        + '"}'
    ).encode("utf-8")
    duplicate = TOKEN_PREFIX + encode_base64(envelope)
    with pytest.raises(LicenseError, match="trùng lặp"):
        verify_license(
            duplicate,
            device_id=license_fixture.device_id,
            public_keys=license_fixture.public_keys,
        )


@pytest.mark.parametrize(
    "token",
    ["", "not-a-license", TOKEN_PREFIX + "*=", "x" * (MAX_TOKEN_BYTES + 1)],
)
def test_malformed_or_oversized_tokens_are_bounded(license_fixture, token: str) -> None:
    with pytest.raises(LicenseError):
        verify_license(
            token,
            device_id=license_fixture.device_id,
            public_keys=license_fixture.public_keys,
        )


def test_machine_identity_is_normalized_versioned_and_checksummed() -> None:
    install = "{12345678-1234-5678-9ABC-1234567890AB}"
    hardware = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"

    first = identity_digest(install, hardware)
    second = identity_digest(install.lower().strip("{}"), hardware.lower())
    code = request_code(first)

    assert first == second
    assert len(first) == 64
    assert parse_request_code(code) == first
    with pytest.raises(LicenseError, match="checksum"):
        parse_request_code(code[:-1] + ("0" if code[-1] != "0" else "1"))
    with pytest.raises(LicenseError, match="định danh"):
        identity_digest(install, "00000000-0000-0000-0000-000000000000")


def test_machine_identity_uses_normalized_disk_fallback_when_smbios_is_absent() -> None:
    install = "12345678-1234-5678-9abc-1234567890ab"
    absent = "00000000-0000-0000-0000-000000000000"

    first = identity_digest(install, absent, disk_serial="  nvme-sn-12  34 ")
    second = identity_digest(install, "not-a-uuid", disk_serial="NVME-SN-12 34")

    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    "serial",
    ["", "unknown", "Default String", "To Be Filled By O.E.M.", "000000"],
)
def test_machine_identity_rejects_generic_disk_fallback(serial: str) -> None:
    with pytest.raises(LicenseError, match="định danh"):
        identity_digest(
            "12345678-1234-5678-9abc-1234567890ab",
            "00000000-0000-0000-0000-000000000000",
            disk_serial=serial,
        )


def test_machine_identity_prefers_smbios_uuid_over_disk_serial() -> None:
    install = "12345678-1234-5678-9abc-1234567890ab"
    hardware = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    assert identity_digest(install, hardware, disk_serial="DISK-ONE") == identity_digest(
        install,
        hardware,
        disk_serial="DISK-TWO",
    )


def test_activation_persists_and_invalid_replacement_preserves_valid_bytes(
    tmp_path: Path, license_fixture
) -> None:
    path = tmp_path / "license.txt"
    state = LicensingState(
        path=path,
        identity=lambda: license_fixture.device_id,
        public_keys=license_fixture.public_keys,
    )
    token = license_fixture.token()

    activated = state.activate(token)
    original = path.read_bytes()
    restarted = LicensingState(
        path=path,
        identity=lambda: license_fixture.device_id,
        public_keys=license_fixture.public_keys,
    )

    assert activated["active"] is True
    assert restarted.status()["active"] is True
    assert restarted.status()["license_id"] == license_fixture.payload()["license_id"]
    with pytest.raises(LicenseError):
        restarted.activate(token[:-1] + ("A" if token[-1] != "A" else "B"))
    assert path.read_bytes() == original
    assert restarted.status(refresh=True)["active"] is True


def test_status_cache_binds_token_content_and_current_device(
    tmp_path: Path, license_fixture
) -> None:
    path = tmp_path / "license-cache.txt"
    current_device = license_fixture.device_id
    state = LicensingState(
        path=path,
        identity=lambda: current_device,
        public_keys=license_fixture.public_keys,
    )
    first = license_fixture.token()
    state.activate(first)
    original_stat = path.stat()
    second = license_fixture.token(
        payload=license_fixture.payload(license_id="b" * 32)
    )
    assert len(second) == len(first)
    path.write_text(second, encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    status = state.status()
    assert status["active"] is True
    assert status["license_id"] == "b" * 32

    current_device = "f" * 64
    changed_device = state.status()
    assert changed_device["active"] is False
    assert "máy khác" in changed_device["message"] or "sai chữ ký" in changed_device["message"]


def test_timed_activation_status_exposes_expiry_and_expired_key_fails_closed(
    tmp_path: Path, license_fixture
) -> None:
    issued = datetime.now(UTC).replace(microsecond=0)
    payload = license_fixture.payload(
        schema=3,
        key_id=LICENSE_KEY_ID,
        expires_at=(issued + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        issued_at=issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    token = encode_expiring_license(
        payload, license_fixture.private_key.sign(canonical_payload(payload))
    )
    path = tmp_path / "timed-license.txt"
    state = LicensingState(
        path=path,
        identity=lambda: license_fixture.device_id,
        public_keys={LICENSE_KEY_ID: license_fixture.public_key},
    )

    status = state.activate(token)
    assert status["active"] is True
    assert status["expires_at"] == payload["expires_at"]
    assert "Hết hạn" in status["message"]

    expired_payload = {
        **payload,
        "issued_at": (issued - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (issued - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    expired = encode_expiring_license(
        expired_payload,
        license_fixture.private_key.sign(canonical_payload(expired_payload)),
    )
    path.write_text(expired, encoding="utf-8")
    assert state.status(refresh=True)["active"] is False
    assert "hết hạn" in state.status(refresh=True)["message"]


def test_timed_status_rechecks_clock_even_without_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, license_fixture
) -> None:
    issued = datetime.now(UTC).replace(microsecond=0)
    payload = license_fixture.payload(
        schema=3,
        key_id=LICENSE_KEY_ID,
        issued_at=issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(issued + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    token = encode_expiring_license(
        payload, license_fixture.private_key.sign(canonical_payload(payload))
    )
    path = tmp_path / "timed-status.txt"
    state = LicensingState(
        path=path,
        identity=lambda: license_fixture.device_id,
        public_keys={LICENSE_KEY_ID: license_fixture.public_key},
    )
    state.activate(token)
    real_verify = state_module.verify_license

    def expired_verify(*args, **kwargs):
        return real_verify(*args, **kwargs, now=issued + timedelta(hours=1))

    monkeypatch.setattr(state_module, "verify_license", expired_verify)
    assert state.status()["active"] is False
    assert "hết hạn" in state.status()["message"]


def test_atomic_replace_failure_keeps_previous_license(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, license_fixture
) -> None:
    path = tmp_path / "license.txt"
    state = LicensingState(
        path=path,
        identity=lambda: license_fixture.device_id,
        public_keys=license_fixture.public_keys,
    )
    original_token = license_fixture.token()
    state.activate(original_token)
    original = path.read_bytes()
    replacement = license_fixture.token(
        payload=license_fixture.payload(license_id="b" * 32)
    )
    monkeypatch.setattr(
        state_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(LicenseError, match="Key cũ vẫn được giữ"):
        state.activate(replacement)

    assert path.read_bytes() == original
    assert json.loads(canonical_payload(license_fixture.payload()))["edition"] == "full"
