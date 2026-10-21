"""Strict, offline verification. Only the external owner tool can sign licenses."""
from __future__ import annotations

import base64
import binascii
import json
import re
import struct
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PRODUCT = "ducai-studio-2"
LICENSE_KEY_ID = "license-2026-01"
# Dedicated licensing trust root; never reuse the application-update signing key.
LICENSE_PUBLIC_KEYS: dict[str, bytes] = {
    LICENSE_KEY_ID: bytes.fromhex(
        "2b7b0ebe216dd81d95649d8df072a0f2deb66ddde9e613ddae45b92feb18adcf"),
}
TOKEN_PREFIX = "DUCAI-L1."
COMPACT_TOKEN_PREFIX = "DUCAI-L2."
COMPACT_TOKEN_LENGTH = 128
EXPIRING_TOKEN_PREFIX = "DUCAI-L3."
# 1-byte slot + 16-byte license id + two 8-byte UTC timestamps + 64-byte signature.
EXPIRING_TOKEN_LENGTH = 139
# Append new slots rather than renumbering a trust root already used by licenses.
COMPACT_KEY_SLOTS = MappingProxyType({1: LICENSE_KEY_ID})
MAX_COMPACT_TIMESTAMP = 253402300799  # 9999-12-31T23:59:59Z, independent of client clock.
_COMPACT_HEADER = struct.Struct(">B16sQ")
_EXPIRING_HEADER = struct.Struct(">B16sQQ")
_EPOCH = datetime(1970, 1, 1)
MAX_TOKEN_BYTES = 4096
PAYLOAD_FIELDS = frozenset({
    "schema", "product", "key_id", "license_id", "device_schema", "device_id",
    "issued_at", "edition",
})
EXPIRING_PAYLOAD_FIELDS = PAYLOAD_FIELDS | {"expires_at"}


class LicenseError(ValueError):
    """Safe public activation error; never includes the submitted token."""


class ActivationRequired(LicenseError):
    def __init__(self) -> None:
        super().__init__("Chưa kích hoạt. Hãy mở Cài đặt → Kích hoạt và nhập key cho máy này.")


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_base64(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise LicenseError("Định dạng key không hợp lệ. Hãy dán đầy đủ key đã được cấp.")
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise LicenseError("Định dạng key không hợp lệ.") from exc
    if encode_base64(result) != value:
        raise LicenseError("Định dạng key không hợp lệ.")
    return result


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise LicenseError("Key có trường trùng lặp; hãy yêu cầu cấp lại.")
        result[key] = value
    return result


def _validate_payload(payload: dict, *, schema: int) -> None:
    fields = EXPIRING_PAYLOAD_FIELDS if schema == 3 else PAYLOAD_FIELDS
    if type(payload) is not dict or set(payload) != fields:
        raise LicenseError("Cấu trúc giấy phép không hợp lệ.")
    if (type(payload["schema"]) is not int or payload["schema"] != schema
            or any(type(payload[key]) is not str for key in fields - {"schema"})):
        raise LicenseError("Phiên bản hoặc dữ liệu giấy phép không hợp lệ.")
    if (payload["product"] != PRODUCT or payload["device_schema"] != "windows-v1"
            or payload["edition"] != "full"):
        raise LicenseError("Key không dành cho sản phẩm hoặc phiên bản này.")
    if (not re.fullmatch(r"[0-9a-f]{32}", payload["license_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", payload["device_id"])):
        raise LicenseError("Mã giấy phép hoặc mã máy không hợp lệ.")
    issued = _parse_timestamp(payload["issued_at"], "Ngày cấp giấy phép")
    if schema == 3:
        expires = _parse_timestamp(payload["expires_at"], "Ngày hết hạn")
        if expires <= issued:
            raise LicenseError("Ngày hết hạn phải sau ngày cấp giấy phép.")


def _parse_timestamp(value: str, label: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise LicenseError(f"{label} không hợp lệ.")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise LicenseError(f"{label} không hợp lệ.") from exc


def _timestamp_seconds(value: str, label: str) -> int:
    timestamp = _parse_timestamp(value, label)
    seconds = int((timestamp - _EPOCH.replace(tzinfo=UTC)).total_seconds())
    if not 0 <= seconds <= MAX_COMPACT_TIMESTAMP:
        raise LicenseError(f"{label} không hợp lệ.")
    return seconds


def encode_compact_license(payload: dict, signature: bytes) -> str:
    """Pack signed metadata; never truncate the signature or expose a signing key."""
    _validate_payload(payload, schema=2)
    slot = next((slot for slot, key_id in COMPACT_KEY_SLOTS.items()
                 if key_id == payload["key_id"]), None)
    if slot is None:
        raise LicenseError("Key không do bộ cấp key tin cậy phát hành.")
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise LicenseError("Chữ ký key không hợp lệ.")
    seconds = _timestamp_seconds(payload["issued_at"], "Ngày cấp giấy phép")
    header = _COMPACT_HEADER.pack(slot, bytes.fromhex(payload["license_id"]), seconds)
    return COMPACT_TOKEN_PREFIX + encode_base64(header + signature)


def encode_expiring_license(payload: dict, signature: bytes) -> str:
    """Pack a signed schema-3 license with an absolute UTC expiration."""
    _validate_payload(payload, schema=3)
    slot = next((slot for slot, key_id in COMPACT_KEY_SLOTS.items()
                 if key_id == payload["key_id"]), None)
    if slot is None:
        raise LicenseError("Key không do bộ cấp key tin cậy phát hành.")
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise LicenseError("Chữ ký key không hợp lệ.")
    issued = _timestamp_seconds(payload["issued_at"], "Ngày cấp giấy phép")
    expires = _timestamp_seconds(payload["expires_at"], "Ngày hết hạn")
    if expires <= issued:
        raise LicenseError("Ngày hết hạn phải sau ngày cấp giấy phép.")
    header = _EXPIRING_HEADER.pack(
        slot, bytes.fromhex(payload["license_id"]), issued, expires
    )
    return EXPIRING_TOKEN_PREFIX + encode_base64(header + signature)


def _decode_compact_license(token: str, *, device_id: str) -> tuple[dict, bytes]:
    if len(token) != COMPACT_TOKEN_LENGTH:
        raise LicenseError("Key ngắn phải có đủ 128 ký tự. Hãy sao chép lại key được cấp.")
    raw = _decode_base64(token[len(COMPACT_TOKEN_PREFIX):])
    if len(raw) != _COMPACT_HEADER.size + 64:
        raise LicenseError("Cấu trúc key ngắn không hợp lệ.")
    slot, license_id, seconds = _COMPACT_HEADER.unpack_from(raw)
    key_id = COMPACT_KEY_SLOTS.get(slot)
    if key_id is None:
        raise LicenseError("Key không do bộ cấp key tin cậy phát hành.")
    if seconds > MAX_COMPACT_TIMESTAMP:
        raise LicenseError("Ngày cấp giấy phép không hợp lệ.")
    payload = {
        "schema": 2, "product": PRODUCT, "key_id": key_id,
        "license_id": license_id.hex(), "device_schema": "windows-v1",
        "device_id": device_id, "edition": "full",
        "issued_at": (_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return payload, raw[_COMPACT_HEADER.size:]


def _decode_expiring_license(token: str, *, device_id: str) -> tuple[dict, bytes]:
    if len(token) != EXPIRING_TOKEN_LENGTH:
        raise LicenseError("Key có thời hạn không hợp lệ hoặc bị thiếu ký tự.")
    raw = _decode_base64(token[len(EXPIRING_TOKEN_PREFIX):])
    if len(raw) != _EXPIRING_HEADER.size + 64:
        raise LicenseError("Cấu trúc key có thời hạn không hợp lệ.")
    slot, license_id, issued, expires = _EXPIRING_HEADER.unpack_from(raw)
    key_id = COMPACT_KEY_SLOTS.get(slot)
    if key_id is None:
        raise LicenseError("Key không do bộ cấp key tin cậy phát hành.")
    if (issued > MAX_COMPACT_TIMESTAMP or expires > MAX_COMPACT_TIMESTAMP
            or expires <= issued):
        raise LicenseError("Khoảng thời gian key không hợp lệ.")
    payload = {
        "schema": 3, "product": PRODUCT, "key_id": key_id,
        "license_id": license_id.hex(), "device_schema": "windows-v1",
        "device_id": device_id, "edition": "full",
        "issued_at": (_EPOCH + timedelta(seconds=issued)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (_EPOCH + timedelta(seconds=expires)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return payload, raw[_EXPIRING_HEADER.size:]


def verify_license(token: str, *, device_id: str,
                   public_keys: dict[str, bytes] | None = None,
                   now: datetime | None = None) -> dict:
    """Verify the entire signed payload before exposing any entitlement."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES:
        raise LicenseError("Key quá dài hoặc không đúng định dạng.")
    token = token.strip()
    try:
        if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{64}", device_id):
            raise LicenseError("Mã giấy phép hoặc mã máy không hợp lệ.")
        if token.startswith(TOKEN_PREFIX):
            raw = _decode_base64(token[len(TOKEN_PREFIX):])
            envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
            if type(envelope) is not dict or set(envelope) != {"payload", "signature"}:
                raise LicenseError("Cấu trúc key không hợp lệ.")
            payload = envelope["payload"]
            _validate_payload(payload, schema=1)
            signature = _decode_base64(envelope["signature"])
        elif token.startswith(COMPACT_TOKEN_PREFIX):
            payload, signature = _decode_compact_license(token, device_id=device_id)
            _validate_payload(payload, schema=2)
        elif token.startswith(EXPIRING_TOKEN_PREFIX):
            payload, signature = _decode_expiring_license(token, device_id=device_id)
            _validate_payload(payload, schema=3)
        else:
            raise LicenseError("Key phải bắt đầu bằng DUCAI-L1., DUCAI-L2. hoặc DUCAI-L3. "
                               "Hãy dán đầy đủ key.")
        keys = LICENSE_PUBLIC_KEYS if public_keys is None else public_keys
        public = keys.get(payload["key_id"])
        if public is None:
            raise LicenseError("Key không do bộ cấp key tin cậy phát hành.")
        if len(signature) != 64:
            raise LicenseError("Chữ ký key không hợp lệ.")
        Ed25519PublicKey.from_public_bytes(public).verify(signature, canonical_payload(payload))
        if payload["device_id"] != device_id:
            raise LicenseError("Key thuộc máy khác. Hãy gửi mã máy hiện tại để được cấp key.")
        if payload["schema"] == 3:
            current = datetime.now(UTC) if now is None else now
            if not isinstance(current, datetime):
                raise LicenseError("Thời điểm kiểm tra giấy phép không hợp lệ.")
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            else:
                current = current.astimezone(UTC)
            if current >= _parse_timestamp(payload["expires_at"], "Ngày hết hạn"):
                raise LicenseError("Key đã hết hạn. Hãy yêu cầu cấp key thử nghiệm mới.")
    except LicenseError:
        raise
    except InvalidSignature as exc:
        raise LicenseError("Key sai chữ ký, đã bị sửa hoặc thuộc máy khác. "
                           "Hãy kiểm tra key được cấp.") from exc
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise LicenseError(
            "Không đọc được key. Hãy dán key nguyên vẹn hoặc yêu cầu cấp lại."
        ) from exc
    return payload
