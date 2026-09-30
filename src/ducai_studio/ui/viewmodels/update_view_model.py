"""Presentation state for manually initiated, signed application updates."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from packaging.version import Version
from PySide6.QtCore import (
    Property,
    QObject,
    QRunnable,
    QSettings,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)

from ducai_studio import __version__
from ducai_studio.infrastructure.github_updates import GitHubUpdateClient, VerifiedRelease
from ducai_studio.infrastructure.update_download import (
    UpdateCancelled,
    download_verified_archive,
    extract_verified_archive,
)
from ducai_studio.infrastructure.update_install import (
    current_install_root,
    launch_update_helper,
    prepare_update_handoff,
)


def _update_work_root(runtime: QObject) -> Path:
    """Keep portable staging outside the application tree being replaced."""

    data_root = Path(runtime.database_path).expanduser().resolve().parent
    candidate = data_root / "updates"
    try:
        install_root = current_install_root()
    except RuntimeError:
        return candidate
    if candidate != install_root and install_root not in candidate.parents:
        return candidate
    local_root = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_root).expanduser() if local_root else Path(tempfile.gettempdir())
    return (base / "DUCAI STUDIO" / "updates").resolve()


class _UpdateSignals(QObject):
    checked = Signal(object, str)
    progress = Signal(int, int)
    downloaded = Signal(object, str)
    launched = Signal(str)


class _CheckTask(QRunnable):
    def __init__(self, signals: _UpdateSignals, version: str, channel: str) -> None:
        super().__init__()
        self.signals = signals
        self.version = version
        self.channel = channel

    def run(self) -> None:
        try:
            self.signals.checked.emit(GitHubUpdateClient().check(self.version, self.channel), "")
        except Exception as exc:
            self.signals.checked.emit(None, str(exc))


class _DownloadTask(QRunnable):
    def __init__(
        self,
        signals: _UpdateSignals,
        release: VerifiedRelease,
        update_root: Path,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.signals = signals
        self.release = release
        self.update_root = update_root
        self.cancel_event = cancel_event

    def run(self) -> None:
        work = self.update_root / f"candidate-{uuid4().hex}"
        try:
            work.mkdir(parents=True, exist_ok=False)
            archive = download_verified_archive(
                self.release.release.archive.url,
                work / self.release.manifest.archive.name,
                self.release.manifest,
                progress=lambda received, total: self.signals.progress.emit(received, total),
                cancelled=self.cancel_event.is_set,
            )
            stage = extract_verified_archive(
                archive,
                work / "payload",
                self.release.manifest,
                cancelled=self.cancel_event.is_set,
            )
            manifest_path = work / "update-manifest.json"
            signature_path = work / "update-manifest.sig"
            manifest_path.write_bytes(self.release.manifest_bytes)
            signature_path.write_bytes(self.release.signature_bytes)
            if self.cancel_event.is_set():
                raise UpdateCancelled("Đã hủy tải bản cập nhật")
            self.signals.downloaded.emit((stage, manifest_path, signature_path), "")
        except UpdateCancelled:
            shutil.rmtree(work, ignore_errors=True)
            self.signals.downloaded.emit(None, "Đã hủy tải bản cập nhật")
        except Exception as exc:
            shutil.rmtree(work, ignore_errors=True)
            self.signals.downloaded.emit(None, str(exc))


class _InstallTask(QRunnable):
    def __init__(
        self,
        signals: _UpdateSignals,
        stage: Path,
        manifest_path: Path,
        signature_path: Path,
        update_root: Path,
        version: str,
        manifest_sha256: str,
    ) -> None:
        super().__init__()
        self.signals = signals
        self.stage = stage
        self.manifest_path = manifest_path
        self.signature_path = signature_path
        self.update_root = update_root
        self.version = version
        self.manifest_sha256 = manifest_sha256

    def run(self) -> None:
        try:
            handoff = prepare_update_handoff(
                install_root=current_install_root(),
                payload_root=self.stage,
                manifest_path=self.manifest_path,
                signature_path=self.signature_path,
                update_root=self.update_root,
                version=self.version,
                expected_manifest_sha256=self.manifest_sha256,
            )
            launch_update_helper(handoff, parent_pid=os.getpid())
            self.signals.launched.emit("")
        except Exception as exc:
            self.signals.launched.emit(str(exc))


class UpdateViewModel(QObject):
    changed = Signal()
    installConfirmationRequested = Signal()

    def __init__(
        self, runtime: QObject, settings: QSettings, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._settings = settings
        self._pool = QThreadPool(self)
        self._signals = _UpdateSignals(self)
        self._signals.checked.connect(self._checked)
        self._signals.progress.connect(self._progress)
        self._signals.downloaded.connect(self._downloaded)
        self._signals.launched.connect(self._launched)
        self._state = "idle"
        pre = Version(__version__).pre
        default_channel = "testing" if pre and pre[0] == "a" else "stable"
        saved = str(settings.value("updates/channel", default_channel, type=str))
        self._channel = saved if saved in {"stable", "testing"} else default_channel
        self._message = "Chỉ kiểm tra khi bạn yêu cầu."
        self._release: VerifiedRelease | None = None
        self._stage: Path | None = None
        self._manifest_path: Path | None = None
        self._signature_path: Path | None = None
        self._received = 0
        self._total = 0
        self._cancel_event = threading.Event()
        self._reservation = ""
        self._install_after_jobs = False
        self._update_root = _update_work_root(runtime)
        self._wait_timer = QTimer(self)
        self._wait_timer.setInterval(2000)
        self._wait_timer.timeout.connect(self._check_eligibility)

    @Property(str, notify=changed)
    def state(self) -> str:
        return self._state

    @Property(str, notify=changed)
    def channel(self) -> str:
        return self._channel

    @Property(str, notify=changed)
    def message(self) -> str:
        return self._message

    @Property(str, notify=changed)
    def version(self) -> str:
        return self._release.manifest.version if self._release else ""

    @Property(str, notify=changed)
    def releaseNotes(self) -> str:
        # Remote release text is plain text; QML must not enable rich-text rendering.
        if self._release is None:
            return ""
        return str(getattr(self._release.release, "notes", "") or "")[:4096]

    @Property(int, notify=changed)
    def receivedBytes(self) -> int:
        return self._received

    @Property(int, notify=changed)
    def totalBytes(self) -> int:
        return self._total

    @Property(int, notify=changed)
    def percent(self) -> int:
        return int(self._received * 100 / self._total) if self._total else 0

    @Property(bool, notify=changed)
    def installAfterJobs(self) -> bool:
        return self._install_after_jobs

    def _set_state(self, state: str, message: str) -> None:
        self._state = state
        self._message = message[:500]
        self.changed.emit()

    @Slot(str)
    def setChannel(self, value: str) -> None:
        if value not in {"stable", "testing"} or value == self._channel:
            return
        if self._state in {"checking", "downloading", "installing"}:
            return
        self._channel = value
        self._settings.setValue("updates/channel", value)
        self._release = None
        self._stage = None
        self._manifest_path = None
        self._signature_path = None
        self._set_state("idle", "Kênh đã đổi. Hãy kiểm tra bản cập nhật.")

    @Slot()
    def check(self) -> None:
        if self._state in {"checking", "downloading", "installing"}:
            return
        self._release = None
        self._stage = None
        self._manifest_path = None
        self._signature_path = None
        self._received = 0
        self._total = 0
        self._set_state("checking", "Đang kiểm tra bản phát hành đã ký…")
        self._pool.start(_CheckTask(self._signals, __version__, self._channel))

    @Slot(object, str)
    def _checked(self, release: object, error: str) -> None:
        if self._state != "checking":
            return
        if error:
            self._set_state("error", "Không thể kiểm tra bản cập nhật: " + error)
        elif release is None:
            self._set_state("up_to_date", "Đây là bản mới nhất trên kênh đã chọn.")
        else:
            self._release = release
            self._total = release.manifest.archive.size
            self._set_state("available", "Đã xác minh chữ ký bản phát hành.")

    @Slot()
    def download(self) -> None:
        if self._state != "available" or self._release is None:
            return
        self._received = 0
        self._cancel_event = threading.Event()
        self._set_state("downloading", "Đang tải và kiểm tra dữ liệu…")
        self._pool.start(
            _DownloadTask(self._signals, self._release, self._update_root, self._cancel_event)
        )

    @Slot()
    def cancelDownload(self) -> None:
        if self._state == "downloading":
            self._cancel_event.set()
            self._message = "Đang hủy tải…"
            self.changed.emit()

    @Slot(int, int)
    def _progress(self, received: int, total: int) -> None:
        if self._state == "downloading":
            self._received = received
            self._total = total
            self.changed.emit()

    @Slot(object, str)
    def _downloaded(self, result: object, error: str) -> None:
        if self._state != "downloading":
            return
        if error:
            self._set_state("available" if "Đã hủy" in error else "error", error)
            return
        self._stage, self._manifest_path, self._signature_path = result
        self._set_state("ready", "Gói đã được xác minh. Cài đặt sẽ khởi động lại ứng dụng.")

    @Slot(bool)
    def setInstallAfterJobs(self, enabled: bool) -> None:
        self._install_after_jobs = enabled
        if self._state == "waiting_jobs":
            self._wait_timer.start() if enabled else self._wait_timer.stop()
        self.changed.emit()

    @Slot()
    def requestInstallConfirmation(self) -> None:
        if self._state in {"ready", "waiting_jobs"}:
            self.installConfirmationRequested.emit()

    @Slot()
    def installConfirmed(self) -> None:
        if self._state not in {"ready", "waiting_jobs"} or not self._stage:
            return
        self._wait_timer.stop()
        self._set_state("installing", "Đang xác nhận không còn tác vụ hoạt động…")
        try:
            self._runtime.request("reserve_update_install", context={"updater": True})
        except Exception as exc:
            self._set_state("error", "Không thể xác nhận tác vụ: " + str(exc))

    @Slot()
    def _check_eligibility(self) -> None:
        if self._state != "waiting_jobs":
            self._wait_timer.stop()
            return
        try:
            self._runtime.request("get_update_install_eligibility", context={"updater": True})
        except Exception:
            self._wait_timer.stop()

    def action_succeeded(self, action: str, result: object) -> bool:
        if action == "get_update_install_eligibility":
            if (
                self._state == "waiting_jobs"
                and isinstance(result, dict)
                and result.get("eligible")
            ):
                self._wait_timer.stop()
                self._set_state(
                    "ready", "Tác vụ đã hoàn tất. Bấm Cài đặt để xác nhận khởi động lại."
                )
            return True
        if action != "reserve_update_install":
            return False
        if self._state != "installing":
            return True
        if not isinstance(result, dict) or not result.get("eligible") or not result.get("token"):
            count = result.get("active_job_count", 0) if isinstance(result, dict) else 0
            multi = result.get("multi_state", "") if isinstance(result, dict) else ""
            reason = f"Còn {count} tác vụ chưa kết thúc"
            if multi in {"running", "pause_requested"}:
                reason += "; Multi video đang hoạt động"
            self._set_state("waiting_jobs", reason + ". Dữ liệu tải đã được giữ lại.")
            if self._install_after_jobs:
                self._wait_timer.start()
            return True
        self._reservation = str(result["token"])
        self._set_state("installing", "Đang chuẩn bị khởi động lại an toàn…")
        assert (
            self._stage is not None
            and self._manifest_path is not None
            and self._signature_path is not None
            and self._release
        )
        self._pool.start(
            _InstallTask(
                self._signals,
                self._stage,
                self._manifest_path,
                self._signature_path,
                self._update_root,
                self._release.manifest.version,
                self._release.manifest.digest,
            )
        )
        return True

    def action_failed(self, action: str, error: object) -> bool:
        if action == "get_update_install_eligibility":
            return True
        if action != "reserve_update_install":
            return False
        message = (
            error.get("message", "không rõ nguyên nhân")
            if isinstance(error, dict)
            else str(error)
        )
        self._set_state("waiting_jobs", "Chưa thể cài đặt: " + str(message))
        if self._install_after_jobs:
            self._wait_timer.start()
        return True

    @Slot(str)
    def _launched(self, error: str) -> None:
        if error:
            if self._reservation:
                with suppress(Exception):
                    self._runtime.request(
                        "release_update_install", {"token": self._reservation},
                        context={"updater": True},
                    )
                self._reservation = ""
            self._set_state("ready", "Không thể chuẩn bị cài đặt: " + error)
            return
        self._set_state("installing", "Trình cài đặt đã chạy. Đang đóng ứng dụng…")
        self.parent().requestQuit()
