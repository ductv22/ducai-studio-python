from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure.licensing_state import LicensingState
from ducai_studio.infrastructure.offline_license import (
    COMPACT_KEY_SLOTS,
    COMPACT_TOKEN_LENGTH,
    COMPACT_TOKEN_PREFIX,
    EXPIRING_TOKEN_LENGTH,
    EXPIRING_TOKEN_PREFIX,
    LICENSE_KEY_ID,
    MAX_COMPACT_TIMESTAMP,
    PRODUCT,
    LicenseError,
    canonical_payload,
    encode_compact_license,
    encode_expiring_license,
    verify_license,
)

L1_PREFIX = "DUCAI-L1."
L2_PREFIX = COMPACT_TOKEN_PREFIX
COMPACT_BODY_BYTES = 1 + 16 + 8 + 64
COMPACT_TOKEN_CHARS = COMPACT_TOKEN_LENGTH
ISSUED_EPOCH = 1_759_449_600  # 2025-10-03T00:00:00Z
ISSUED_AT = "2025-10-03T00:00:00Z"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _schema_two_payload(
    device_id: str,
    license_id: bytes,
    issued_epoch: int,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 2,
        "product": PRODUCT,
        "key_id": LICENSE_KEY_ID,
        "license_id": license_id.hex(),
        "device_schema": "windows-v1",
        "device_id": device_id,
        "issued_at": datetime.fromtimestamp(issued_epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "edition": "full",
    }
    payload.update(overrides)
    return payload


def _compact_token(
    key: Ed25519PrivateKey,
    *,
    device_id: str,
    license_id: bytes = bytes.fromhex("00112233445566778899aabbccddeeff"),
    issued_epoch: int = ISSUED_EPOCH,
    slot: int = 1,
    signed_payload_overrides: dict[str, object] | None = None,
) -> str:
    payload = _schema_two_payload(device_id, license_id, issued_epoch)
    payload.update(signed_payload_overrides or {})
    signature = key.sign(canonical_payload(payload))
    body = bytes([slot]) + license_id + issued_epoch.to_bytes(8, "big") + signature
    assert len(body) == COMPACT_BODY_BYTES
    return L2_PREFIX + _base64url(body)


def _expiring_payload(
    device_id: str,
    license_id: bytes,
    issued_epoch: int,
    expires_epoch: int,
) -> dict[str, object]:
    payload = _schema_two_payload(device_id, license_id, issued_epoch)
    payload.update(
        {
            "schema": 3,
            "expires_at": datetime.fromtimestamp(expires_epoch, UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
    )
    return payload


def _expiring_token(
    key: Ed25519PrivateKey,
    *,
    device_id: str,
    issued_epoch: int = ISSUED_EPOCH,
    expires_epoch: int = ISSUED_EPOCH + 3600,
    license_id: bytes = bytes.fromhex("00112233445566778899aabbccddeeff"),
) -> str:
    payload = _expiring_payload(device_id, license_id, issued_epoch, expires_epoch)
    return encode_expiring_license(payload, key.sign(canonical_payload(payload)))


def _public_keys(license_fixture) -> dict[str, bytes]:
    return {LICENSE_KEY_ID: license_fixture.public_key}


def test_genuine_compact_token_round_trips_all_metadata_in_exactly_128_characters(
    license_fixture,
) -> None:
    token = _compact_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )

    payload = verify_license(
        token,
        device_id=license_fixture.device_id,
        public_keys=_public_keys(license_fixture),
    )

    assert token.isascii()
    assert len(token) == COMPACT_TOKEN_CHARS
    assert COMPACT_TOKEN_CHARS == 128
    assert COMPACT_KEY_SLOTS == {1: LICENSE_KEY_ID}
    expected_payload = _schema_two_payload(
        license_fixture.device_id,
        bytes.fromhex("00112233445566778899aabbccddeeff"),
        ISSUED_EPOCH,
    )
    assert encode_compact_license(
        expected_payload,
        license_fixture.private_key.sign(canonical_payload(expected_payload)),
    ) == token
    assert payload == _schema_two_payload(
        license_fixture.device_id,
        bytes.fromhex("00112233445566778899aabbccddeeff"),
        ISSUED_EPOCH,
    )
    assert payload["issued_at"] == ISSUED_AT


def test_genuine_expiring_token_round_trips_and_exposes_expiry(license_fixture) -> None:
    token = _expiring_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )

    payload = verify_license(
        token,
        device_id=license_fixture.device_id,
        public_keys=_public_keys(license_fixture),
        now=datetime.fromtimestamp(ISSUED_EPOCH + 3599, UTC),
    )

    assert token.startswith(EXPIRING_TOKEN_PREFIX)
    assert len(token) == EXPIRING_TOKEN_LENGTH == 139
    assert payload["schema"] == 3
    assert payload["issued_at"] == ISSUED_AT
    assert payload["expires_at"] == "2025-10-03T01:00:00Z"


def test_expiring_token_rejects_exact_expiry_and_accepts_one_second_before(
    license_fixture,
) -> None:
    token = _expiring_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )

    verify_license(
        token,
        device_id=license_fixture.device_id,
        public_keys=_public_keys(license_fixture),
        now=datetime.fromtimestamp(ISSUED_EPOCH + 3599, UTC),
    )
    with pytest.raises(LicenseError, match="hết hạn"):
        verify_license(
            token,
            device_id=license_fixture.device_id,
            public_keys=_public_keys(license_fixture),
            now=datetime.fromtimestamp(ISSUED_EPOCH + 3600, UTC),
        )


@pytest.mark.parametrize("expires_epoch", [ISSUED_EPOCH, ISSUED_EPOCH - 1])
def test_expiring_encoder_rejects_non_positive_duration(
    license_fixture, expires_epoch: int
) -> None:
    payload = _expiring_payload(
        license_fixture.device_id,
        bytes.fromhex("00112233445566778899aabbccddeeff"),
        ISSUED_EPOCH,
        expires_epoch,
    )
    with pytest.raises(LicenseError, match="hết hạn"):
        encode_expiring_license(
            payload, license_fixture.private_key.sign(canonical_payload(payload))
        )


def test_expiring_token_rejects_tampered_expiry_header(license_fixture) -> None:
    token = _expiring_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )
    encoded = token[len(EXPIRING_TOKEN_PREFIX):]
    body = bytearray(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    body[25] ^= 1  # First byte of the encoded expiration timestamp.
    tampered = EXPIRING_TOKEN_PREFIX + _base64url(bytes(body))

    with pytest.raises(LicenseError):
        verify_license(
            tampered,
            device_id=license_fixture.device_id,
            public_keys=_public_keys(license_fixture),
            now=datetime.fromtimestamp(ISSUED_EPOCH + 1, UTC),
        )


def test_compact_token_rejects_wrong_machine_and_wrong_signing_key(license_fixture) -> None:
    token = _compact_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )
    with pytest.raises(LicenseError):
        verify_license(token, device_id="f" * 64, public_keys=_public_keys(license_fixture))

    wrong_key = Ed25519PrivateKey.generate()
    wrong = _compact_token(wrong_key, device_id=license_fixture.device_id)
    with pytest.raises(LicenseError):
        verify_license(
            wrong,
            device_id=license_fixture.device_id,
            public_keys=_public_keys(license_fixture),
        )


@pytest.mark.parametrize("body_index", [0, 1, 16, 17, 24, 25, 56, 88])
def test_compact_token_rejects_tampering_in_every_wire_field(
    license_fixture,
    body_index: int,
) -> None:
    token = _compact_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )
    body = bytearray(base64.urlsafe_b64decode(token[len(L2_PREFIX):] + "="))
    body[body_index] ^= 1
    tampered = L2_PREFIX + _base64url(bytes(body))

    with pytest.raises(LicenseError):
        verify_license(
            tampered,
            device_id=license_fixture.device_id,
            public_keys=_public_keys(license_fixture),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", 1),
        ("product", "other-product"),
        ("key_id", "other-key"),
        ("device_schema", "windows-v2"),
        ("edition", "trial"),
        ("device_id", "f" * 64),
        ("license_id", "f" * 32),
        ("issued_at", "2025-10-04T00:00:00Z"),
    ],
)
def test_compact_signature_binds_every_reconstructed_payload_field(
    license_fixture,
    field: str,
    value: object,
) -> None:
    token = _compact_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
        signed_payload_overrides={field: value},
    )

    with pytest.raises(LicenseError):
        verify_license(
            token,
            device_id=license_fixture.device_id,
            public_keys=_public_keys(license_fixture),
        )


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda valid: valid[:-1],
        lambda valid: valid + "A",
        lambda valid: "DUCAI-L3." + valid[len(L2_PREFIX):],
        lambda valid: L2_PREFIX + "*" + valid[len(L2_PREFIX) + 1:],
    ],
)
def test_compact_token_rejects_wrong_length_unknown_header_and_invalid_alphabet(
    license_fixture,
    token_factory,
) -> None:
    valid = _compact_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )
    with pytest.raises(LicenseError):
        verify_license(
            token_factory(valid),
            device_id=license_fixture.device_id,
            public_keys=_public_keys(license_fixture),
        )


def test_compact_token_rejects_noncanonical_base64url(license_fixture) -> None:
    token = _compact_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last_index = alphabet.index(token[-1])
    noncanonical_last = alphabet[(last_index & ~0b11) | ((last_index + 1) & 0b11)]
    assert noncanonical_last != token[-1]
    noncanonical = token[:-1] + noncanonical_last
    assert base64.urlsafe_b64decode(noncanonical[len(L2_PREFIX):] + "=") == (
        base64.urlsafe_b64decode(token[len(L2_PREFIX):] + "=")
    )

    with pytest.raises(LicenseError):
        verify_license(
            noncanonical,
            device_id=license_fixture.device_id,
            public_keys=_public_keys(license_fixture),
        )


def test_compact_token_rejects_issue_dates_outside_supported_range(license_fixture) -> None:
    issued_epoch = MAX_COMPACT_TIMESTAMP + 1
    license_id = bytes.fromhex("00112233445566778899aabbccddeeff")
    # Sign arbitrary bytes for the extreme timestamp. Date validation must happen
    # safely even when the platform datetime cannot represent the value.
    signature = license_fixture.private_key.sign(b"out-of-range-date")
    body = bytes([1]) + license_id + issued_epoch.to_bytes(8, "big") + signature
    token = L2_PREFIX + _base64url(body)

    with pytest.raises(LicenseError):
        verify_license(
            token,
            device_id=license_fixture.device_id,
            public_keys=_public_keys(license_fixture),
        )


@pytest.mark.parametrize(
    "override",
    [
        {"schema": 1},
        {"key_id": "unknown-key"},
        {"license_id": "not-a-uuid"},
        {"issued_at": "10000-01-01T00:00:00Z"},
        {"device_id": "not-a-device"},
        {"edition": "trial"},
    ],
)
def test_compact_encoder_rejects_noncanonical_schema_two_payloads(
    license_fixture,
    override: dict[str, object],
) -> None:
    payload = _schema_two_payload(
        license_fixture.device_id,
        bytes.fromhex("00112233445566778899aabbccddeeff"),
        ISSUED_EPOCH,
    )
    payload.update(override)
    signature = license_fixture.private_key.sign(canonical_payload(payload))

    with pytest.raises((LicenseError, ValueError)):
        encode_compact_license(payload, signature)


def test_l1_and_l2_protocol_bodies_cannot_be_retagged(license_fixture) -> None:
    l1 = license_fixture.token()
    l2 = _compact_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )

    for token in (
        L2_PREFIX + l1[len(L1_PREFIX):],
        L1_PREFIX + l2[len(L2_PREFIX):],
    ):
        with pytest.raises(LicenseError):
            verify_license(
                token,
                device_id=license_fixture.device_id,
                public_keys={
                    license_fixture.key_id: license_fixture.public_key,
                    LICENSE_KEY_ID: license_fixture.public_key,
                },
            )


def test_l1_remains_valid_and_failed_l1_l2_replacements_preserve_exact_bytes(
    tmp_path,
    license_fixture,
) -> None:
    path = tmp_path / "license-v1.txt"
    keys = {
        license_fixture.key_id: license_fixture.public_key,
        LICENSE_KEY_ID: license_fixture.public_key,
    }
    state = LicensingState(
        path=path,
        identity=lambda: license_fixture.device_id,
        public_keys=keys,
    )
    l1 = license_fixture.token()
    l2 = _compact_token(
        license_fixture.private_key,
        device_id=license_fixture.device_id,
    )

    assert verify_license(
        l1,
        device_id=license_fixture.device_id,
        public_keys=keys,
    )["schema"] == 1
    assert state.activate(l1)["active"]
    l1_bytes = path.read_bytes()
    with pytest.raises(LicenseError):
        state.activate(l2[:-1] + ("A" if l2[-1] != "A" else "B"))
    assert path.read_bytes() == l1_bytes

    assert state.activate(l2)["active"]
    l2_bytes = path.read_bytes()
    with pytest.raises(LicenseError):
        state.activate(l1[:-1] + ("A" if l1[-1] != "A" else "B"))
    assert path.read_bytes() == l2_bytes
    assert state.activate(l1)["active"]
    assert path.read_bytes() == l1_bytes
    assert LicensingState(
        path=path,
        identity=lambda: license_fixture.device_id,
        public_keys=keys,
    ).status()["active"]
