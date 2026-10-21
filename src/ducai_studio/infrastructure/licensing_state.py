"""Per-user durable activation with machine binding and fail-closed admission."""
from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

from .machine_identity import current_device_id, request_code
from .offline_license import MAX_TOKEN_BYTES, ActivationRequired, LicenseError, verify_license

# Recovery/read-only operations do not constitute new processing or editing.
UNLICENSED_ACTIONS = frozenset({
    "ping", "get_license_status", "activate_license", "runtime_diagnostics", "shutdown",
    "get_update_install_eligibility", "reserve_update_install", "release_update_install",
    "pause_job", "cancel_job", "multi_pause",
    "cancel_database_optimization",
})


def license_path() -> Path:
    # Independent of portable data and install root: a verified update keeps activation.
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise LicenseError("Không tìm thấy thư mục LocalAppData để lưu kích hoạt.")
    return Path(root) / "DUCAI STUDIO" / "Licensing" / "license-v1.txt"


class LicensingState:
    def __init__(self, *, path: Path | None = None,
                 identity: Callable[[], str] | None = None,
                 public_keys: dict[str, bytes] | None = None) -> None:
        self.path = path if path is not None else license_path()
        self.identity = identity if identity is not None else current_device_id
        self.public_keys = public_keys
        self._stamp: tuple[str, str] | None = None
        self._cached: dict | None = None

    def status(self, *, refresh: bool = False) -> dict:
        stamp: tuple[str, str] | None = None
        status = {"active": False, "request_code": "", "license_id": "", "issued_at": "",
                  "expires_at": "",
                  "message": "Chưa kích hoạt. Sao chép mã máy và gửi người cấp key."}
        try:
            device = self.identity()
            status["request_code"] = request_code(device)
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_TOKEN_BYTES + 1)
            if len(raw) > MAX_TOKEN_BYTES:
                raise LicenseError("File kích hoạt không hợp lệ; hãy nhập lại key.")
            stamp = (device, sha256(raw).hexdigest())
            # Cache only perpetual verification, bound to actual bytes and
            # identity. Timed keys always recheck the clock, even without refresh.
            if (not refresh and self._cached is not None and self._cached["active"]
                    and self._stamp == stamp and not self._cached.get("expires_at")):
                return dict(self._cached)
            token = raw.decode("utf-8").strip()
            payload = verify_license(token, device_id=device, public_keys=self.public_keys)
            expires_at = payload.get("expires_at", "")
            if expires_at:
                message = (f"Đã kích hoạt · Hết hạn {expires_at} UTC · "
                           "Chỉ dùng trên máy này")
            else:
                message = "Đã kích hoạt · Vĩnh viễn · Chỉ dùng trên máy này"
            status.update(active=True, license_id=payload["license_id"],
                          issued_at=payload["issued_at"], expires_at=expires_at,
                          message=message)
        except FileNotFoundError:
            pass
        except (LicenseError, OSError, UnicodeError) as exc:
            status["message"] = str(exc) if isinstance(exc, LicenseError) else (
                "Không đọc được file kích hoạt; dữ liệu dự án vẫn nguyên vẹn. Hãy nhập lại key.")
        self._stamp, self._cached = stamp, status
        return dict(status)

    def require_active(self) -> None:
        if not self.status()["active"]:
            raise ActivationRequired()

    def require_action(self, action: str) -> None:
        if action not in UNLICENSED_ACTIONS:
            self.require_active()

    def activate(self, token: str) -> dict:
        # Never overwrite a good token with a failed verification or failed write.
        verify_license(token, device_id=self.identity(), public_keys=self.public_keys)
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(token.strip())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise LicenseError(
                "Không lưu được kích hoạt. Key cũ vẫn được giữ; hãy thử lại."
            ) from exc
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
        self._cached = None
        return self.status(refresh=True)


def require_worker_activation() -> None:
    """No environment flag or frozen CLI route can waive activation."""
    LicensingState().require_active()


def worker_activation_exit_code() -> int:
    try:
        require_worker_activation()
    except LicenseError:
        sys.stderr.write("activation_required\n")
        return 3
    return 0
