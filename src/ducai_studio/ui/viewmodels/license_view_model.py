from __future__ import annotations

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication

from ducai_studio.infrastructure.offline_license import MAX_TOKEN_BYTES


class LicenseViewModel(QObject):
    changed = Signal()

    def __init__(self, runtime: QObject, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self._status = {"active": False, "message": "Đang kiểm tra kích hoạt…"}
        self._busy = False
        self._message_error = False
        runtime.actionSucceeded.connect(self._succeeded)
        runtime.actionFailed.connect(self._failed)
        runtime.readyChanged.connect(self._ready)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(20_000)
        self._deadline.timeout.connect(self._timed_out)
        self._timer = QTimer(self)
        self._timer.setInterval(15_000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    @Property(bool, notify=changed)
    def active(self) -> bool:
        return self._status.get("active") is True

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(bool, notify=changed)
    def hasError(self) -> bool:
        return self._message_error

    @Property(str, notify=changed)
    def requestCode(self) -> str:
        return str(self._status.get("request_code", ""))

    @Property(str, notify=changed)
    def licenseId(self) -> str:
        return str(self._status.get("license_id", ""))

    @Property(str, notify=changed)
    def expiresAt(self) -> str:
        return str(self._status.get("expires_at", ""))

    @Property(str, notify=changed)
    def message(self) -> str:
        return str(self._status.get("message", ""))

    @Slot(bool)
    def _ready(self, ready: bool) -> None:
        if ready:
            self.refresh()
        elif self._busy:
            self._timed_out()

    def _begin_request(self) -> None:
        self._busy = True
        self._deadline.start()
        self.changed.emit()

    def _timed_out(self) -> None:
        self._busy = False
        self._deadline.stop()
        self._message_error = True
        self._status["message"] = "Chưa nhận được xác nhận kích hoạt. Bấm Kiểm tra lại để thử lại."
        self.changed.emit()

    @Slot()
    def refresh(self) -> None:
        if self._busy or not self.runtime.ready:
            return
        self._begin_request()
        self.runtime.request("get_license_status", context={"licensing": True})

    @Slot()
    def copyRequestCode(self) -> None:
        if self.requestCode:
            clipboard = QGuiApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(self.requestCode)

    @Slot(str)
    def activate(self, token: str) -> None:
        if self._busy:
            return
        if not token.strip() or len(token) > MAX_TOKEN_BYTES:
            self._message_error = True
            self._status["message"] = "Hãy dán đầy đủ key được cấp (tối đa 4.096 ký tự)."
            self.changed.emit()
            return
        if not self.runtime.ready:
            self._message_error = True
            self._status["message"] = "Job Manager chưa sẵn sàng. Hãy chờ rồi thử lại."
            self.changed.emit()
            return
        self._begin_request()
        self.runtime.request("activate_license", {"token": token}, context={"licensing": True})

    @Slot(QUrl)
    def importLicense(self, url: QUrl) -> None:
        try:
            if not url.isLocalFile():
                raise ValueError("Giấy phép phải là file trên máy.")
            with open(url.toLocalFile(), "rb") as stream:
                data = stream.read(MAX_TOKEN_BYTES + 1)
            if len(data) > MAX_TOKEN_BYTES:
                raise ValueError("File key quá lớn. Hãy chọn đúng file key được cấp.")
            self.activate(data.decode("utf-8-sig"))
        except (ValueError, OSError, UnicodeError):
            self._message_error = True
            self._status["message"] = "Không đọc được file key; hãy chọn file UTF-8 được cấp."
            self.changed.emit()

    @Slot(str, object, object)
    def _succeeded(self, action: str, result: object, _context: object) -> None:
        if action not in {"get_license_status", "activate_license"}:
            return
        self._busy = False
        self._deadline.stop()
        if isinstance(result, dict) and type(result.get("active")) is bool:
            self._status = dict(result)
            self._message_error = False
        else:
            self._message_error = True
            self._status = {"active": False, "message": "Phản hồi kích hoạt không hợp lệ."}
        self.changed.emit()

    @Slot(str, object, object)
    def _failed(self, action: str, error: object, _context: object) -> None:
        if action not in {"get_license_status", "activate_license"}:
            return
        self._busy = False
        self._deadline.stop()
        self._message_error = True
        self._status["message"] = (str(error.get("message", "Không kích hoạt được."))
                                   if isinstance(error, dict) else "Không kích hoạt được.")
        self.changed.emit()
