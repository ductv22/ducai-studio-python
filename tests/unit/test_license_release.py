from __future__ import annotations

import base64
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure.machine_identity import request_code
from ducai_studio.infrastructure.offline_license import (
    COMPACT_TOKEN_LENGTH,
    COMPACT_TOKEN_PREFIX,
    EXPIRING_TOKEN_LENGTH,
    EXPIRING_TOKEN_PREFIX,
    LICENSE_KEY_ID,
    LICENSE_PUBLIC_KEYS,
    verify_license,
)
from ducai_studio.infrastructure.update_manifest import PUBLIC_KEY_BYTES


def _load_script(name: str):
    path = Path(__file__).parents[2] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _MemoryCredentialStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = {} if values is None else values

    def read(self, reference: str) -> str:
        try:
            return self.values[reference]
        except KeyError as exc:
            raise LookupError(reference) from exc

    def write(self, reference: str, secret: str, *, username: str = "api-key") -> None:
        del username
        self.values[reference] = secret


def _encoded_private_key(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.private_bytes_raw()).decode("ascii")


def test_customer_release_requires_distinct_pinned_license_key(tmp_path: Path) -> None:
    checker = _load_script("check-license-release.py")
    license_public = LICENSE_PUBLIC_KEYS[LICENSE_KEY_ID]
    assert len(license_public) == 32
    assert license_public != PUBLIC_KEY_BYTES

    release = tmp_path / "customer-release"
    release.mkdir()
    (release / "DucaiStudio2.exe").write_bytes(b"customer executable fixture")

    checker.check_release(release)


@pytest.mark.parametrize(
    ("relative_path", "contents"),
    [
        ("license-issuer.py", b"owner tool"),
        ("owner-backup.pem", b"owner key"),
        ("license-v1.txt", b"activation"),
        ("sitecustomize.py", b"test bootstrap"),
        ("private.key", b"private"),
        ("source.whl", b"wheel"),
    ],
)
def test_customer_release_rejects_owner_activation_and_test_material(
    tmp_path: Path, relative_path: str, contents: bytes
) -> None:
    checker = _load_script("check-license-release.py")
    release = tmp_path / "release"
    release.mkdir()
    (release / "DucaiStudio2.exe").write_bytes(b"exe")
    target = release / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)

    with pytest.raises(ValueError, match="Owner/test/activation material"):
        checker.check_release(release)


def test_customer_release_rejects_embedded_private_pem_and_license_token(
    tmp_path: Path, license_fixture
) -> None:
    checker = _load_script("check-license-release.py")
    release = tmp_path / "release"
    release.mkdir()
    executable = release / "DucaiStudio2.exe"

    executable.write_bytes(
        b"prefix\n-----BEGIN ENCRYPTED PRIVATE KEY-----\n" + b"A" * 80 + b"\n"
    )
    with pytest.raises(ValueError, match="Private key"):
        checker.check_release(release)

    executable.write_bytes(b"prefix\n" + license_fixture.token().encode("ascii"))
    with pytest.raises(ValueError, match="personal activation token"):
        checker.check_release(release)

    compact_lookalike = COMPACT_TOKEN_PREFIX + "A" * (
        COMPACT_TOKEN_LENGTH - len(COMPACT_TOKEN_PREFIX)
    )
    executable.write_bytes(b"prefix\n" + compact_lookalike.encode("ascii"))
    with pytest.raises(ValueError, match="personal activation token"):
        checker.check_release(release)

    expiring_lookalike = EXPIRING_TOKEN_PREFIX + "A" * (
        EXPIRING_TOKEN_LENGTH - len(EXPIRING_TOKEN_PREFIX)
    )
    executable.write_bytes(b"prefix\n" + expiring_lookalike.encode("ascii"))
    with pytest.raises(ValueError, match="personal activation token"):
        checker.check_release(release)


def test_owner_issuer_signs_full_machine_bound_payload_without_pii(license_fixture) -> None:
    issuer = _load_script("license-issuer.py")
    token, payload = issuer.issue_token(
        request_code(license_fixture.device_id),
        license_fixture.private_key,
    )

    verified = verify_license(
        token,
        device_id=license_fixture.device_id,
        public_keys={LICENSE_KEY_ID: license_fixture.public_key},
    )
    assert verified == payload
    assert token.startswith(COMPACT_TOKEN_PREFIX)
    assert len(token) == COMPACT_TOKEN_LENGTH == 128
    assert payload["schema"] == 2
    assert payload["edition"] == "full"
    assert set(payload) == {
        "schema",
        "product",
        "key_id",
        "license_id",
        "device_schema",
        "device_id",
        "issued_at",
        "edition",
    }
    assert not ({"name", "email", "customer", "expires_at", "features"} & set(payload))


def test_owner_issuer_can_explicitly_issue_legacy_l1_for_pre_a140_customers(
    license_fixture,
) -> None:
    issuer = _load_script("license-issuer.py")

    token, payload = issuer.issue_token(
        request_code(license_fixture.device_id),
        license_fixture.private_key,
        legacy=True,
    )

    assert token.startswith("DUCAI-L1.")
    assert payload["schema"] == 1
    assert verify_license(
        token,
        device_id=license_fixture.device_id,
        public_keys={LICENSE_KEY_ID: license_fixture.public_key},
    ) == payload


def test_owner_issuer_can_issue_time_limited_l3_and_keep_default_perpetual(
    license_fixture,
) -> None:
    issuer = _load_script("license-issuer.py")
    now = datetime(2026, 10, 3, tzinfo=UTC)
    token, payload = issuer.issue_token(
        request_code(license_fixture.device_id),
        license_fixture.private_key,
        duration_hours=24,
        now=now,
    )

    assert token.startswith(EXPIRING_TOKEN_PREFIX)
    assert len(token) == EXPIRING_TOKEN_LENGTH
    assert payload["schema"] == 3
    assert payload["expires_at"] == "2026-10-04T00:00:00Z"
    assert verify_license(
        token,
        device_id=license_fixture.device_id,
        public_keys={LICENSE_KEY_ID: license_fixture.public_key},
        now=datetime(2026, 10, 3, 23, 59, 59, tzinfo=UTC),
    ) == payload

    perpetual, perpetual_payload = issuer.issue_token(
        request_code(license_fixture.device_id),
        license_fixture.private_key,
        now=now,
    )
    assert perpetual.startswith(COMPACT_TOKEN_PREFIX)
    assert perpetual_payload["schema"] == 2
    assert "expires_at" not in perpetual_payload

    with pytest.raises(ValueError, match="không thể kết hợp"):
        issuer.issue_token(
            request_code(license_fixture.device_id),
            license_fixture.private_key,
            legacy=True,
            duration_hours=24,
            now=now,
        )


def test_owner_cli_forwards_explicit_legacy_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = _load_script("license-issuer.py")
    captured: dict[str, object] = {}

    def fake_issue(root: Path, request: str, destination: Path, *, legacy: bool = False) -> str:
        captured.update(root=root, request=request, destination=destination, legacy=legacy)
        return "fixture-token"

    monkeypatch.setattr(issuer, "issue", fake_issue)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "license-issuer.py",
            "--state-root",
            str(tmp_path / "state"),
            "issue",
            "--request-code",
            "fixture-request",
            "--output",
            str(tmp_path / "legacy.ducai-license"),
            "--legacy",
        ],
    )

    assert issuer.main() == 0
    assert captured["legacy"] is True


def test_owner_cli_forwards_duration_hours(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = _load_script("license-issuer.py")
    captured: dict[str, object] = {}

    def fake_issue(
        root: Path,
        request: str,
        destination: Path,
        *,
        legacy: bool = False,
        duration_hours: int | None = None,
    ) -> str:
        captured.update(
            root=root,
            request=request,
            destination=destination,
            legacy=legacy,
            duration_hours=duration_hours,
        )
        return "fixture-token"

    monkeypatch.setattr(issuer, "issue", fake_issue)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "license-issuer.py",
            "--state-root",
            str(tmp_path / "state"),
            "issue",
            "--request-code",
            "fixture-request",
            "--output",
            str(tmp_path / "timed.ducai-license"),
            "--duration-hours",
            "72",
        ],
    )

    assert issuer.main() == 0
    assert captured["legacy"] is False
    assert captured["duration_hours"] == 72


def test_owner_gui_exposes_legacy_issuance_choice() -> None:
    source = (
        Path(__file__).parents[2] / "scripts" / "license-issuer.py"
    ).read_text(encoding="utf-8")
    assert "Checkbutton" in source
    assert "legacy" in source
    assert "Có thời hạn (L3)" in source
    assert "duration_hours" in source


def test_owner_encrypted_backup_restores_fixture_key_without_real_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, license_fixture
) -> None:
    issuer = _load_script("license-issuer.py")
    vault: dict[str, str] = {
        LICENSE_KEY_ID: _encoded_private_key(license_fixture.private_key),
    }
    monkeypatch.setattr(issuer, "owner_store", lambda: _MemoryCredentialStore(vault))
    monkeypatch.setattr(
        issuer,
        "LICENSE_PUBLIC_KEYS",
        {LICENSE_KEY_ID: license_fixture.public_key},
    )
    backup_path = tmp_path / "owner-backup.pem"
    password = "fixture-only-password"

    issuer.backup(backup_path, password)

    encrypted = backup_path.read_bytes()
    assert encrypted.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
    assert license_fixture.private_key.private_bytes_raw() not in encrypted

    vault.clear()
    public = issuer.restore(tmp_path / "issuer-state", backup_path, password)
    restored = base64.b64decode(vault[LICENSE_KEY_ID], validate=True)
    assert restored == license_fixture.private_key.private_bytes_raw()
    assert public == license_fixture.public_key.hex()

    token, _payload = issuer.issue_token(
        request_code(license_fixture.device_id), issuer.private_key()
    )
    verify_license(
        token,
        device_id=license_fixture.device_id,
        public_keys={LICENSE_KEY_ID: license_fixture.public_key},
    )


def test_owner_restore_refuses_mismatched_existing_key_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, license_fixture
) -> None:
    issuer = _load_script("license-issuer.py")
    existing = _encoded_private_key(license_fixture.private_key)
    vault = {LICENSE_KEY_ID: existing}
    replacement = Ed25519PrivateKey.generate()
    backup_path = tmp_path / "different-owner-backup.pem"
    backup_path.write_bytes(
        replacement.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"fixture-only-password"),
        )
    )
    monkeypatch.setattr(issuer, "owner_store", lambda: _MemoryCredentialStore(vault))
    monkeypatch.setattr(
        issuer,
        "LICENSE_PUBLIC_KEYS",
        {LICENSE_KEY_ID: replacement.public_key().public_bytes_raw()},
    )

    with pytest.raises(ValueError, match="Không ghi đè khóa cấp key đang có"):
        issuer.restore(
            tmp_path / "issuer-state",
            backup_path,
            "fixture-only-password",
        )

    assert vault == {LICENSE_KEY_ID: existing}


def test_owner_outputs_use_exclusive_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, license_fixture
) -> None:
    issuer = _load_script("license-issuer.py")
    vault = {LICENSE_KEY_ID: _encoded_private_key(license_fixture.private_key)}
    monkeypatch.setattr(issuer, "owner_store", lambda: _MemoryCredentialStore(vault))
    monkeypatch.setattr(
        issuer,
        "LICENSE_PUBLIC_KEYS",
        {LICENSE_KEY_ID: license_fixture.public_key},
    )
    destination = tmp_path / "existing.ducai-license"
    destination.write_text("keep-existing", encoding="utf-8")
    backup_path = tmp_path / "existing.pem"
    backup_path.write_text("keep-backup", encoding="utf-8")

    with pytest.raises(FileExistsError):
        issuer.issue(
            tmp_path / "issuer-state",
            request_code(license_fixture.device_id),
            destination,
        )
    with pytest.raises(FileExistsError):
        issuer.backup(backup_path, "fixture-only-password")

    assert destination.read_text(encoding="utf-8") == "keep-existing"
    assert backup_path.read_text(encoding="utf-8") == "keep-backup"


def test_owner_cli_has_no_password_or_private_key_argument() -> None:
    source = (
        Path(__file__).parents[2] / "scripts" / "license-issuer.py"
    ).read_text(encoding="utf-8")
    assert 'add_argument("--password"' not in source
    assert 'add_argument("--private-key"' not in source
    assert "getpass.getpass" in source
