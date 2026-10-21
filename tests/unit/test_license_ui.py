from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QUrl, Signal

from ducai_studio.infrastructure.ipc.messages import IPC_PROTOCOL_VERSION, IpcMessage
from ducai_studio.ui.viewmodels import license_view_model as module
from ducai_studio.ui.viewmodels.application_view_model import JobRuntime


class Runtime(QObject):
    actionSucceeded = Signal(str, object, object)
    actionFailed = Signal(str, object, object)
    readyChanged = Signal(bool)

    def __init__(self, *, ready: bool = False) -> None:
        super().__init__()
        self.ready = ready
        self.licensing = LocalStatus()
        self.requests: list[tuple[str, object, object]] = []

    def request(self, action: str, params=None, *, context=None) -> str:
        self.requests.append((action, params, context))
        return str(len(self.requests))


class LocalStatus:
    def status(self) -> dict[str, object]:
        return {
            "active": False,
            "request_code": "DUCAI-M1-test",
            "license_id": "",
            "issued_at": "",
            "message": "Chưa kích hoạt",
        }


def _view_model(*, ready: bool = False):
    QCoreApplication.instance() or QCoreApplication([])
    runtime = Runtime(ready=ready)
    view_model = module.LicenseViewModel(runtime)
    view_model._status = runtime.licensing.status()
    if hasattr(runtime, "_licensed"):
        runtime._licensed = False
    return view_model, runtime


def test_refresh_waits_for_runtime_and_uses_manager_status() -> None:
    view_model, runtime = _view_model()

    view_model.refresh()
    assert runtime.requests == []
    runtime.ready = True
    runtime.readyChanged.emit(True)

    assert view_model.busy is True
    assert runtime.requests == [
        ("get_license_status", None, {"licensing": True})
    ]
    runtime.actionSucceeded.emit(
        "get_license_status",
        {
            "active": False,
            "request_code": "DUCAI-M1-manager",
            "license_id": "",
            "message": "Cần kích hoạt",
        },
        {"licensing": True},
    )
    assert view_model.busy is False
    assert view_model.active is False
    assert view_model.requestCode == "DUCAI-M1-manager"
    assert view_model.message == "Cần kích hoạt"


def test_activation_unlocks_only_after_manager_confirmation() -> None:
    view_model, runtime = _view_model(ready=True)

    view_model.activate("DUCAI-L1.fixture")

    assert view_model.busy is True
    assert view_model.active is False
    assert runtime.requests[-1] == (
        "activate_license",
        {"token": "DUCAI-L1.fixture"},
        {"licensing": True},
    )
    runtime.actionSucceeded.emit(
        "activate_license",
        {
            "active": True,
            "request_code": "DUCAI-M1-manager",
            "license_id": "a" * 32,
            "expires_at": "2026-10-10T00:00:00Z",
            "message": "Đã kích hoạt",
        },
        {"licensing": True},
    )
    assert view_model.active is True
    assert view_model.licenseId == "a" * 32
    assert view_model.expiresAt == "2026-10-10T00:00:00Z"
    assert view_model.message == "Đã kích hoạt"


def test_activation_validation_and_failure_never_echo_token() -> None:
    view_model, runtime = _view_model(ready=True)
    secret_token = "DUCAI-L1.sensitive-fixture"

    view_model.activate("   ")
    assert runtime.requests == []
    assert "dán đầy đủ" in view_model.message
    view_model.activate(secret_token)
    runtime.actionFailed.emit(
        "activate_license",
        {"message": "Key sai chữ ký hoặc đã bị sửa."},
        {"licensing": True},
    )

    assert view_model.busy is False
    assert view_model.active is False
    assert secret_token not in view_model.message
    assert "sai chữ ký" in view_model.message


def test_utf8_file_import_uses_same_manager_activation_path(tmp_path: Path) -> None:
    view_model, runtime = _view_model(ready=True)
    token_path = tmp_path / "license.ducai-key"
    token_path.write_bytes(b"\xef\xbb\xbfDUCAI-L1.from-file\n")

    view_model.importLicense(QUrl.fromLocalFile(str(token_path)))

    assert runtime.requests[-1] == (
        "activate_license",
        {"token": "DUCAI-L1.from-file\n"},
        {"licensing": True},
    )


def test_nonlocal_or_invalid_license_file_is_rejected_without_request(
    tmp_path: Path,
) -> None:
    view_model, runtime = _view_model(ready=True)
    view_model.importLicense(QUrl("https://example.invalid/license"))
    assert runtime.requests == []
    assert "UTF-8" in view_model.message

    invalid = tmp_path / "invalid.key"
    invalid.write_bytes(b"\xff\xfe\x00")
    view_model.importLicense(QUrl.fromLocalFile(str(invalid)))
    assert runtime.requests == []
    assert "UTF-8" in view_model.message


@pytest.mark.parametrize(
    ("action", "code", "message"),
    [
        ("activate_license", "LicenseError", "Key sai chữ ký hoặc đã bị sửa."),
        (
            "create_job",
            "ActivationRequired",
            "Chưa kích hoạt. Hãy mở Cài đặt → Kích hoạt và nhập key cho máy này.",
        ),
    ],
)
def test_runtime_rejected_license_action_clears_pending_and_emits_failure(
    action: str, code: str, message: str
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = JobRuntime()
    runtime._pending_requests["license-error"] = (action, {"licensing": True})
    failures: list[tuple[str, object, object]] = []
    statuses: list[str] = []
    runtime.actionFailed.connect(
        lambda current_action, error, context: failures.append(
            (current_action, error, context)
        )
    )
    runtime.statusChanged.connect(statuses.append)

    runtime._response(
        IpcMessage(
            IPC_PROTOCOL_VERSION,
            "response-id",
            "response",
            {
                "request_id": "license-error",
                "ok": False,
                "error": {"code": code, "message": message},
            },
        )
    )

    assert "license-error" not in runtime._pending_requests
    assert failures == [
        (action, {"code": code, "message": message}, {"licensing": True})
    ]
    assert len(statuses) == 1
    assert action in statuses[0]
