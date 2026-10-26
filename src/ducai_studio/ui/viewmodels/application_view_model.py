from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import zlib
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    QProcess,
    QRunnable,
    QSettings,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QImage
from PySide6.QtNetwork import QLocalSocket

from ducai_studio import __version__
from ducai_studio.application.translation_pipeline import contains_cjk
from ducai_studio.domain.jobs import redact_sensitive_text
from ducai_studio.domain.project import (
    CUE_BACKGROUND_STYLES,
    SCENE_TEXT_DEFAULT_BACKGROUND_COLOR,
    SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT,
    SCENE_TEXT_DEFAULT_BACKGROUND_STYLE,
    SCENE_TEXT_DEFAULT_FADE_DURATION_MS,
    SCENE_TEXT_DEFAULT_FADE_ENABLED,
    SCENE_TEXT_DEFAULT_STYLE_HINT,
    SCENE_TEXT_DEFAULT_TEXT_COLOR,
)
from ducai_studio.infrastructure.ipc.client import JobManagerClient
from ducai_studio.infrastructure.licensing_state import UNLICENSED_ACTIONS
from ducai_studio.infrastructure.offline_license import ActivationRequired
from ducai_studio.infrastructure.process_tree import ProcessTreeGuard
from ducai_studio.infrastructure.runtime_program import runtime_program_path
from ducai_studio.infrastructure.voice_installer import PiperVoiceInstaller
from ducai_studio.infrastructure.whisper_runtime import (
    WHISPER_MODEL_ID,
    whisper_model_path,
    whisper_runtime_is_ready,
    whisper_runtime_python,
)
from ducai_studio.providers.preview import QtMultimediaPreviewProvider
from ducai_studio.providers.translation.openai_compatible import (
    OPENAI_COMPATIBLE_PROVIDER_IDS,
)
from ducai_studio.providers.tts import VoiceCatalog, discover_data_root

from .license_view_model import LicenseViewModel
from .project_job_models import (
    JobListModel,
    MultiVideoListModel,
    ProjectListModel,
    ProjectStateModel,
)
from .update_view_model import UpdateViewModel
from .voice_models import VoiceCatalogModel


@dataclass(frozen=True, slots=True)
class NavigationItem:
    workspace_id: str
    title: str
    short_code: str
    icon_name: str


NAVIGATION = (
    NavigationItem("dashboard", "Tổng quan", "TG", "house"),
    NavigationItem("projects", "Dự án", "DA", "folder"),
    NavigationItem("timeline", "Timeline", "TL", "panels-top-left"),
    NavigationItem("transcribe", "Trích xuất", "TX", "scan-text"),
    NavigationItem("translate", "Dịch thuật", "DT", "languages"),
    NavigationItem("subtitles", "Biên tập phụ đề", "PD", "captions"),
    NavigationItem("voice", "Phòng thu giọng", "GV", "mic-vocal"),
    NavigationItem("multi", "Multi video", "MV", "list-todo"),
    NavigationItem("export", "Xuất video", "XV", "upload"),
    NavigationItem("queue", "Hàng đợi", "HD", "list-todo"),
    NavigationItem("settings", "Cài đặt", "CD", "settings"),
)

_INVALID_MODEL_INDEX = QModelIndex()


class _ProjectSnapshotDecodeTask(QRunnable):
    """Decode a large project-snapshot transport envelope off the UI thread."""

    def __init__(
        self,
        runtime: JobRuntime,
        result: object,
        context: object,
        *,
        action: str = "get_project",
        artifact_root: Path,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.result = result
        self.context = context
        self.action = action
        self.artifact_root = artifact_root

    def run(self) -> None:  # pragma: no cover - exercised through Qt signal tests
        try:
            snapshot = self._decode(self.result)
        except Exception as exc:
            self.runtime.projectPayloadDecoded.emit(
                self.action,
                False,
                {"message": str(exc)},
                self.context,
            )
            return
        self.runtime.projectPayloadDecoded.emit(
            self.action,
            True,
            snapshot,
            self.context,
        )

    def _decode(self, result: object) -> dict:
        if not isinstance(result, dict) or "__project_transport__" not in result:
            if not isinstance(result, dict):
                raise ValueError("Project snapshot response is not an object")
            return result
        envelope = result.get("__project_transport__")
        if not isinstance(envelope, dict):
            raise ValueError("Project snapshot transport envelope is invalid")
        encoding = str(envelope.get("encoding", ""))
        expected_size = envelope.get("uncompressed_bytes")
        if (
            type(expected_size) is not int
            or expected_size <= 0
            or expected_size > 256 * 1024 * 1024
        ):
            raise ValueError("Project snapshot size is invalid")
        expected_sha = str(envelope.get("sha256", "")).strip().casefold()
        if len(expected_sha) != 64:
            raise ValueError("Project snapshot checksum is invalid")
        if encoding == "zlib+base64+json":
            encoded = envelope.get("data")
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("Project snapshot data is missing")
            compressed = base64.b64decode(encoded, validate=True)
        elif encoding == "file+zlib+json":
            path = Path(str(envelope.get("path", ""))).expanduser().resolve(strict=True)
            if not path.is_relative_to(self.artifact_root):
                raise ValueError("Project snapshot file is outside the application cache")
            compressed = path.read_bytes()
            with suppress(OSError):
                path.unlink(missing_ok=True)
        else:
            raise ValueError("Project snapshot encoding is unsupported")
        decoded = zlib.decompress(compressed)
        if len(decoded) != expected_size:
            raise ValueError("Project snapshot size changed during transport")
        if hashlib.sha256(decoded).hexdigest().casefold() != expected_sha:
            raise ValueError("Project snapshot checksum mismatch")
        snapshot = json.loads(decoded.decode("utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("Project snapshot JSON is invalid")
        project_id = str(envelope.get("project_id", ""))
        revision = envelope.get("revision")
        if project_id and snapshot.get("id") != project_id:
            raise ValueError("Project snapshot belongs to another project")
        if type(revision) is int and snapshot.get("revision") != revision:
            raise ValueError("Project snapshot revision changed during transport")
        return snapshot


class _TimelinePatchProjectionTask(QRunnable):
    """Prepare a potentially large compact timeline patch off the UI thread."""

    def __init__(
        self,
        runtime: JobRuntime,
        snapshot: dict,
        patch: dict,
        context: object,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.snapshot = snapshot
        self.patch = patch
        self.context = context

    def run(self) -> None:  # pragma: no cover - exercised through Qt signal tests
        try:
            prepare = (ProjectStateModel.prepare_subtitle_patch
                       if self.patch.get("_subtitle_patch") is True
                       else ProjectStateModel.prepare_timeline_patch)
            canonical, projected = prepare(
                self.snapshot,
                self.patch,
            )
        except Exception as exc:
            self.runtime.timelinePatchProjected.emit(
                False,
                {"message": str(exc)},
                self.context,
            )
            return
        self.runtime.timelinePatchProjected.emit(
            True,
            {
                "canonical": canonical,
                "projected": projected,
                "patch": self.patch,
            },
            self.context,
        )


class _VoiceInstallTask(QRunnable):
    """Run network and digest work outside the GUI thread."""

    def __init__(
        self,
        voice_id: str,
        installer: PiperVoiceInstaller,
        emit_event: Callable[[str, str, int, str], None],
    ) -> None:
        super().__init__()
        self.voice_id = voice_id
        self.installer = installer
        self.emit_event = emit_event

    def run(self) -> None:  # pragma: no cover - covered through installer and signal tests
        try:
            self.installer.install(
                self.voice_id,
                lambda value: self.emit_event(self.voice_id, "downloading", value, ""),
            )
        except Exception as exc:
            self.emit_event(self.voice_id, "error", 0, str(exc))
            return
        self.emit_event(self.voice_id, "installed", 100, "")


class _SubtitleGeometryMeasureTask(QRunnable):
    """Measure a large selection without creating glyph paths or blocking QML."""

    def __init__(self, cues: list[dict], style, width: int, height: int,
                 context: dict, emit_result: Callable) -> None:
        super().__init__()
        self.cues, self.style = cues, style
        self.width, self.height = width, height
        self.context, self.emit_result = context, emit_result

    def run(self) -> None:
        from ducai_studio.providers.subtitle_artwork import editor_payload, layout_json

        try:
            seeds = {}
            for cue in self.cues:
                payload = editor_payload(cue, self.width, self.height, self.style)
                layout = layout_json(json.dumps(payload, sort_keys=True), include_paths=False)
                x, y, width, height = layout["placement_rect"]
                seeds[cue["cue_id"]] = [x / self.width, y / self.height,
                                        width / self.width, height / self.height]
        except Exception as exc:
            self.emit_result(False, {"message": str(exc)}, self.context)
            return
        self.emit_result(True, seeds, self.context)


def _configured_ocr_engine_python() -> str | None:
    configured = os.environ.get("DUCAI_PADDLE_PYTHON", "").strip()
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser().resolve())
    return None


def _default_ocr_engine_python() -> str:
    configured = _configured_ocr_engine_python()
    if configured is not None:
        return configured
    shared_data = discover_data_root()
    local_data = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    candidates = [
        shared_data / "paddle_runtime" / ".venv" / "Scripts" / "python.exe",
        shared_data.parent / ".bench-paddle-rapid" / "venv" / "Scripts" / "python.exe",
        local_data / "engines" / "paddle" / "venv" / "Scripts" / "python.exe",
    ]
    for ancestor in Path(__file__).resolve().parents:
        candidates.append(
            ancestor / "outputs" / ".bench-paddle-rapid" / "venv" / "Scripts" / "python.exe"
        )
    runtime = next((candidate for candidate in candidates if candidate.is_file()), None)
    return str(runtime.resolve()) if runtime is not None else str(runtime_program_path())


def _local_path_from_ui(value: str) -> Path:
    """Normalize either a native Windows path or a QML file URL."""
    raw = value.strip()
    if raw.casefold().startswith("file:"):
        raw = QUrl(raw).toLocalFile()
    return Path(raw).expanduser().resolve()


def _stable_credential_references(values: object) -> list[str]:
    raw_values = [values] if isinstance(values, str) else values
    if not isinstance(raw_values, (tuple, list)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        value = str(raw).strip()
        folded = value.casefold()
        if not value or folded in seen:
            continue
        seen.add(folded)
        result.append(value)
    return result


def _normalized_hex_color(value: str) -> str | None:
    candidate = value.strip().upper()
    if len(candidate) != 7 or not candidate.startswith("#"):
        return None
    try:
        int(candidate[1:], 16)
    except ValueError:
        return None
    return candidate


def _credential_setting_key(provider_id: str) -> str:
    normalized = provider_id.strip().casefold()
    if normalized == "gemini-api":
        return "translation/geminiApiCredentialReferences"
    return f"translation/{normalized}/credentialReferences"


def _credential_prefix(provider_id: str) -> str:
    return {
        "gemini-api": "gemini",
        "deepseek-api": "deepseek",
        "tokenhub-api": "hy3",
        "qwen-api": "qwen",
        "glm-api": "glm",
        "openai-compatible": "openai",
    }.get(provider_id.strip().casefold(), "api")


def _parse_translation_api_credentials(
    raw: str,
    *,
    provider_id: str,
    existing_references: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    lines = [line.strip() for line in raw.replace(";", "\n").splitlines() if line.strip()]
    if not lines:
        return []
    if len(lines) > 32:
        raise ValueError("Chỉ có thể lưu tối đa 32 API key trong một lần")
    used = {value.casefold() for value in existing_references if value.strip()}
    batch_names: set[str] = set()
    generated_index = 1
    credentials: list[dict[str, str]] = []
    for line in lines:
        if "=" in line:
            reference, secret = (value.strip() for value in line.split("=", 1))
            if not reference or not secret:
                raise ValueError("Mỗi dòng có tên phải dùng định dạng ten=API_KEY")
        else:
            secret = line
            prefix = _credential_prefix(provider_id)
            while f"{prefix}-{generated_index}".casefold() in used | batch_names:
                generated_index += 1
            reference = f"{prefix}-{generated_index}"
            generated_index += 1
        if any(character in reference for character in "\r\n\0="):
            raise ValueError("Tên API key chứa ký tự không hợp lệ")
        if len(reference) > 80:
            raise ValueError("Tên API key không được dài quá 80 ký tự")
        if redact_sensitive_text(reference) != reference:
            raise ValueError("Tên API key phải là nhãn mô tả, không được chứa khóa bí mật")
        folded = reference.casefold()
        if folded in batch_names:
            raise ValueError(f"Tên API key bị lặp trong danh sách: {reference}")
        batch_names.add(folded)
        credentials.append({"reference": reference, "secret": secret})
    return credentials


def _parse_gemini_api_credentials(
    raw: str,
    *,
    existing_references: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Backward-compatible parser for the existing Gemini settings/tests."""

    return _parse_translation_api_credentials(
        raw,
        provider_id="gemini-api",
        existing_references=existing_references,
    )


def _legacy_qsettings_values(settings: dict[str, object], project_id: str) -> dict[str, object]:
    migrated = {
        f"migration/legacy/{key}": value
        for key, value in settings.items()
        if isinstance(value, (bool, int, float, str, list))
    }
    font = settings.get("font_file")
    if isinstance(font, str) and font.strip():
        candidate = Path(font.strip())
        migrated["export/fontFamily"] = candidate.stem if candidate.suffix else font.strip()
    preset = settings.get("export_preset")
    if isinstance(preset, str) and preset in {"source-quality", "1080p", "720p", "480p"}:
        migrated["export/presetId"] = preset
    output = settings.get("export_output")
    if isinstance(output, str) and output.strip() and project_id:
        migrated[f"export/outputPath/{project_id}"] = output.strip()
    font_size = settings.get("sub_font_size")
    if type(font_size) is int:
        migrated["export/fontSize"] = max(12, min(120, font_size))
    margin = settings.get("sub_margin_bottom")
    if type(margin) is int:
        migrated["export/subtitleMarginBottom"] = max(0, min(500, margin))
    return migrated


class NavigationModel(QAbstractListModel):
    WorkspaceIdRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = Qt.ItemDataRole.UserRole + 2
    ShortCodeRole = Qt.ItemDataRole.UserRole + 3
    IconNameRole = Qt.ItemDataRole.UserRole + 4

    def rowCount(self, parent: QModelIndex = _INVALID_MODEL_INDEX) -> int:
        return 0 if parent.isValid() else len(NAVIGATION)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(NAVIGATION):
            return None
        item = NAVIGATION[index.row()]
        if role == self.WorkspaceIdRole:
            return item.workspace_id
        if role in {self.TitleRole, Qt.ItemDataRole.DisplayRole}:
            return item.title
        if role == self.ShortCodeRole:
            return item.short_code
        if role == self.IconNameRole:
            return item.icon_name
        return None

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.WorkspaceIdRole: b"workspaceId",
            self.TitleRole: b"title",
            self.ShortCodeRole: b"shortCode",
            self.IconNameRole: b"iconName",
        }


class JobRuntime(QObject):
    statusChanged = Signal(str)
    stateChanged = Signal(str)
    readyChanged = Signal(bool)
    shutdownComplete = Signal()
    projectsReceived = Signal(object)
    jobsReceived = Signal(object)
    multiVideosReceived = Signal(object)
    actionSucceeded = Signal(str, object, object)
    actionFailed = Signal(str, object, object)
    projectPayloadDecoded = Signal(str, bool, object, object)
    timelinePatchProjected = Signal(bool, object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.server_name = f"ducai-studio-{os.getpid()}-{uuid4().hex[:8]}"
        configured_root = os.environ.get("DUCAI_APP_DATA_ROOT")
        root = (
            Path(
                configured_root
                or QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppLocalDataLocation
                )
            )
            .expanduser()
            .resolve()
        )
        self.database_path = root / "state.db"
        self.artifact_root = root / "artifacts"
        self.process = QProcess(self)
        self.client = JobManagerClient(self)
        self.process_tree = ProcessTreeGuard()
        self._ready = False
        self._licensed = False
        self._state = "stopped"
        self._attempts = 0
        self._startup_deadline = 0.0
        self._last_client_error = ""
        self._shutting_down = False
        self._pending_requests: dict[str, tuple[str, object]] = {}
        self._decode_pool = QThreadPool(self)
        self._decode_pool.setMaxThreadCount(1)
        self._job_poll_timer = QTimer(self)
        self._job_poll_timer.setInterval(750)
        self._job_poll_timer.timeout.connect(self._poll_jobs)
        self.process.started.connect(self._process_started)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._process_finished)
        self.client.connectedChanged.connect(self._connected)
        self.client.responseReceived.connect(self._response)
        self.client.errorOccurred.connect(self._client_error)
        self.projectPayloadDecoded.connect(self._project_payload_decoded)

    def start(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._attempts = 0
        self._startup_deadline = monotonic() + 60.0
        self._last_client_error = ""
        self._set_state("starting")
        self.statusChanged.emit("Đang khởi động Job Manager")
        self.process.setProgram(str(runtime_program_path()))
        self.process.setArguments(
            [
                "-m",
                "ducai_studio.job_manager",
                "--server-name",
                self.server_name,
                "--database",
                str(self.database_path),
                "--artifacts",
                str(self.artifact_root),
            ]
        )
        self.process.start()

    def request_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._job_poll_timer.stop()
        self._set_state("shutting_down")
        self.statusChanged.emit("Đang lưu checkpoint và dừng worker")
        if self.process.state() is QProcess.ProcessState.NotRunning:
            self._complete_shutdown()
            return
        if self.client.socket.state() is QLocalSocket.LocalSocketState.ConnectedState:
            try:
                self.client.request("shutdown")
            except Exception:
                self.process.terminate()
        else:
            self.process.terminate()
        QTimer.singleShot(15_000, self.force_shutdown)

    def force_shutdown(self) -> None:
        if self.process.state() is not QProcess.ProcessState.NotRunning:
            self.process_tree.close()
            self.process.kill()
        self.client.disconnect()

    @Slot()
    def _process_started(self) -> None:
        try:
            self.process_tree.assign_pid(int(self.process.processId()))
        except OSError as exc:
            self.statusChanged.emit(f"Không thể sở hữu Job Manager: {exc}")
            self.process.kill()
            return
        self._connect()

    @Slot()
    def _connect(self) -> None:
        if self._ready or self._shutting_down or self._state != "starting":
            return
        if self._startup_deadline and monotonic() >= self._startup_deadline:
            self._set_state("error")
            self.statusChanged.emit(
                "Job Manager khởi động quá lâu. Dữ liệu dự án vẫn được giữ nguyên; "
                "hãy đóng ứng dụng rồi mở lại."
            )
            return
        self._attempts += 1
        self.client.connect_to(self.server_name)
        if not self._ready:
            QTimer.singleShot(100, self._connect)

    @Slot(bool)
    def _connected(self, connected: bool) -> None:
        if connected:
            self._request("ping")
            return
        # A socket can disappear while the Job Manager process is still
        # alive (for example after an oversized/invalid frame).  Publish the
        # loss immediately so ApplicationViewModel releases transient busy
        # gates such as Multi detail loading instead of waiting for the
        # process-finished signal or a 30-second timeout.
        if self._ready:
            self._ready = False
            self.readyChanged.emit(False)
            self._fail_pending_requests("Kết nối Job Manager bị gián đoạn")
            self._set_state("error")
            self.statusChanged.emit(
                "Kết nối Job Manager bị gián đoạn; dữ liệu hiện tại vẫn được giữ nguyên."
            )

    @Slot(object)
    def _response(self, message) -> None:
        if message.kind == "event":
            if message.payload.get("event") == "multi_status":
                result = message.payload.get("result", {})
                if isinstance(result, dict):
                    result = {
                        **result,
                        "partial": message.payload.get("partial") is not False,
                    }
                self.multiVideosReceived.emit(result)
            return
        if message.kind != "response":
            return
        request_id = str(message.payload.get("request_id", ""))
        action, context = self._pending_requests.pop(request_id, ("", {}))
        if not message.payload.get("ok"):
            raw_error = message.payload.get("error", {})
            error = (
                dict(raw_error)
                if isinstance(raw_error, dict)
                else {"message": str(raw_error or "không thành công")}
            )
            if action != "list_events":
                action_label = action or "Yêu cầu"
                raw_message = str(error.get("message") or error.get("reason") or "không thành công")
                code = str(error.get("code", error.get("error_code", ""))).strip()
                message_text = ApplicationViewModel._localized_error_message(code, raw_message)
                self.statusChanged.emit(f"{action_label}: {message_text}")
            self.actionFailed.emit(action, error, context)
            return
        result = message.payload.get("result", {})
        if isinstance(result, dict) and "__project_transport__" in result:
            self._decode_pool.start(
                _ProjectSnapshotDecodeTask(
                    self,
                    result,
                    context,
                    action=action,
                    artifact_root=self.artifact_root,
                )
            )
            return
        if action == "ping" and isinstance(result, dict) and result.get("status") == "ready":
            self._ready = True
            self._set_state("ready")
            self.readyChanged.emit(True)
            self.statusChanged.emit("Sẵn sàng")
            self._update_license_admission(result.get("license", {}))
        elif action in {"get_license_status", "activate_license"} and isinstance(result, dict):
            self._update_license_admission(result)
        elif action == "list_projects":
            self.projectsReceived.emit(result)
        elif action == "list_jobs":
            self.jobsReceived.emit(result)
        elif action in {
            "multi_list",
            "multi_status",
            "multi_add",
            "multi_remove",
            "multi_reorder",
            "multi_select",
            "multi_select_all",
            "multi_start",
            "multi_pause",
            "multi_resume",
            "multi_restore_hidden",
        }:
            if action in {"multi_select", "multi_select_all"}:
                # The view model already applied this selection locally. An
                # older response must not overwrite a newer rapid click.
                pass
            elif action != "multi_list" and isinstance(result, dict):
                # Mutations and control actions usually retain card identity.
                # Merge them in place; the model falls back to a reset only
                # when membership or order truly changed.
                result = {**result, "partial": True}
                self.multiVideosReceived.emit(result)
            else:
                self.multiVideosReceived.emit(result)
        self.actionSucceeded.emit(action, result, context)

    @Slot(str, bool, object, object)
    def _project_payload_decoded(
        self,
        action: str,
        succeeded: bool,
        result: object,
        context: object,
    ) -> None:
        if succeeded:
            self.actionSucceeded.emit(action, result, context)
            return
        error = dict(result) if isinstance(result, dict) else {"message": str(result)}
        self.statusChanged.emit(
            "Không thể đọc dữ liệu Timeline: "
            + str(error.get("message", "dữ liệu project không hợp lệ"))
        )
        self.actionFailed.emit(action, error, context)

    def project_timeline_patch(
        self,
        snapshot: dict,
        patch: dict,
        context: object,
    ) -> None:
        self._decode_pool.start(
            _TimelinePatchProjectionTask(self, snapshot, patch, context)
        )

    @Slot(str)
    def _client_error(self, error: str) -> None:
        self._last_client_error = error
        startup_failure = not self._ready and self._state == "starting"
        if startup_failure and (not self._startup_deadline or monotonic() < self._startup_deadline):
            return
        if not startup_failure and self._state == "error":
            return
        self._set_state("error")
        if startup_failure:
            self.statusChanged.emit(
                "Không thể khởi động Job Manager. Dữ liệu dự án vẫn được giữ nguyên; "
                "hãy đóng ứng dụng rồi mở lại."
            )
            return
        self.statusChanged.emit(f"Job Manager: {error}")

    @Slot(QProcess.ProcessError)
    def _process_error(self, _error: QProcess.ProcessError) -> None:
        self._set_state("error")
        self.statusChanged.emit(self.process.errorString())

    @Slot(int, QProcess.ExitStatus)
    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._job_poll_timer.stop()
        self.process_tree.close()
        was_ready = self._ready
        self._ready = False
        if was_ready:
            self.readyChanged.emit(False)
        self._fail_pending_requests("Job Manager đã dừng trước khi xác nhận thao tác")
        if self._shutting_down:
            self._complete_shutdown()
        else:
            # Set the error state before publishing the human-readable reason
            # so the view model promotes the unexpected stop to the same
            # persistent top-center error toast as other backend failures.
            self._set_state("error")
            if exit_code != 0:
                self.statusChanged.emit(
                    "Bộ quản lý tác vụ đã dừng bất ngờ; hãy mở lại ứng dụng và tiếp tục từ cache."
                )
            else:
                self.statusChanged.emit("Job Manager đã dừng ngoài dự kiến")

    def _complete_shutdown(self) -> None:
        self.client.disconnect()
        self._set_state("stopped")
        self.shutdownComplete.emit()

    def _fail_pending_requests(self, message: str) -> None:
        pending, self._pending_requests = self._pending_requests, {}
        for action, context in pending.values():
            self.actionFailed.emit(action, {
                "code": "transport_disconnected", "message": message,
            }, context)

    def _set_state(self, value: str) -> None:
        if value == self._state:
            return
        self._state = value
        self.stateChanged.emit(value)

    @Slot()
    def _poll_jobs(self) -> None:
        if not self._ready or self._shutting_down:
            return
        if any(action == "list_jobs" for action, _context in self._pending_requests.values()):
            return
        self._request("list_jobs")

    def request(
        self,
        action: str,
        params: dict | None = None,
        *,
        context: object = None,
    ) -> str:
        if not self._ready:
            raise RuntimeError("Job Manager is not ready")
        if not self._licensed and action not in UNLICENSED_ACTIONS:
            exc = ActivationRequired()
            error = {"code": type(exc).__name__, "message": str(exc)}
            QTimer.singleShot(0, lambda: self.actionFailed.emit(action, error, context or {}))
            self.statusChanged.emit(str(exc))
            return str(uuid4())
        if action in {"subtitle_command", "timeline_command", "effect_command"}:
            params = {**(params or {}), "deferred": True}
        return self._request(action, params, context=context)

    @property
    def ready(self) -> bool:
        return self._ready

    def _update_license_admission(self, status: dict) -> None:
        licensed = status.get("active") is True
        if licensed == self._licensed:
            return
        self._licensed = licensed
        if not licensed:
            self._job_poll_timer.stop()
            return
        self._job_poll_timer.start()
        for action in ("list_projects", "list_jobs", "multi_list", "list_credentials"):
            self._request(action)

    def _request(
        self,
        action: str,
        params: dict | None = None,
        *,
        context: object = None,
    ) -> str:
        if action == "list_jobs":
            for request_id, (pending_action, _pending_context) in self._pending_requests.items():
                if pending_action == "list_jobs":
                    return request_id
        request_id = self.client.request(action, params)
        self._pending_requests[request_id] = (action, context if context is not None else {})
        return request_id


class ApplicationViewModel(QObject):
    currentWorkspaceChanged = Signal()
    currentIndexChanged = Signal()
    timelineSettingsRequested = Signal()
    statusMessageChanged = Signal()
    statusNotificationRequested = Signal(str, str, int)
    statusNotificationDismissRequested = Signal()
    backendReadyChanged = Signal()
    runtimeStateChanged = Signal()
    projectNameChanged = Signal()
    saveStateChanged = Signal()
    projectAutosaveRequested = Signal()
    shuttingDownChanged = Signal()
    quitReady = Signal()
    importDialogRequested = Signal()
    multiImportDialogRequested = Signal()
    newProjectDialogRequested = Signal()
    legacyImportDialogRequested = Signal()
    duplicateMediaDialogRequested = Signal()
    duplicateMediaChanged = Signal()
    importBusyChanged = Signal()
    jobHistoryBusyChanged = Signal()
    projectDeleteChanged = Signal()
    selectedClipChanged = Signal()
    videoClipboardChanged = Signal()
    selectedTrackChanged = Signal()
    playheadChanged = Signal()
    previewMasterVolumeChanged = Signal()
    timelineBusyChanged = Signal()
    timelineCutSettled = Signal(bool, str, float)
    timelineHistoryChanged = Signal()
    timelineNavigationChanged = Signal()
    ocrBusyChanged = Signal()
    ocrSettingsChanged = Signal()
    selectedCueChanged = Signal()
    subtitleBusyChanged = Signal()
    subtitleGeometrySettled = Signal(int, bool)
    subtitleExportBusyChanged = Signal()
    subtitleHistoryChanged = Signal()
    translationBusyChanged = Signal()
    translationPreferencesBusyChanged = Signal()
    translationSettingsChanged = Signal()
    translationModelCatalogChanged = Signal()
    translationCachesChanged = Signal()
    translationQaChanged = Signal()
    translationQaNavigationRequested = Signal(int)
    subtitleNavigationRequested = Signal(str)
    voiceTimingQaChanged = Signal()
    voiceConformChanged = Signal()
    geminiApiCredentialsStored = Signal()
    voiceSettingsChanged = Signal()
    dialogueCastingChanged = Signal()
    ttsBusyChanged = Signal()
    voicePreviewChanged = Signal()
    voiceCatalogPlaybackChanged = Signal()
    voiceInstallEvent = Signal(str, str, int, str)
    voicePlaybackEnabledChanged = Signal()
    exportSettingsChanged = Signal()
    exportBusyChanged = Signal()
    exportPreviewChanged = Signal()
    diagnosticsChanged = Signal()
    databaseMaintenanceChanged = Signal()
    legacyMigrationChanged = Signal()
    overlaySelectionRequested = Signal(str)
    overlayGeometrySettled = Signal(str, bool)
    sceneTimingSettled = Signal(int, bool)
    subtitleGeometryMeasured = Signal(bool, object, object)
    multiVideoChanged = Signal()
    multiPreviewReady = Signal(object, bool)
    parallelJobWarningRequested = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.navigation = NavigationModel(self)
        self.projects = ProjectListModel(self)
        self.multi_videos = MultiVideoListModel(self)
        self.jobs = JobListModel(self)
        self.subtitle_jobs = JobListModel(self, accepted_kinds=frozenset({"ocr", "translate"}))
        self.voice_jobs = JobListModel(
            self, accepted_kinds=frozenset({"tts", "dialogue_casting"})
        )
        self.export_jobs = JobListModel(self, accepted_kinds=frozenset({"export"}))
        self.project_state = ProjectStateModel(self)
        self.voice_catalog = VoiceCatalogModel(self)
        self._voice_installer = PiperVoiceInstaller(VoiceCatalog())
        self._voice_installing: set[str] = set()
        self.preview_provider = QtMultimediaPreviewProvider()
        self.settings = QSettings(self)
        saved_workspace = self.settings.value("ui/currentWorkspace", "dashboard", type=str)
        valid_workspaces = {item.workspace_id for item in NAVIGATION}
        self._current_workspace = (
            saved_workspace if saved_workspace in valid_workspaces else "dashboard"
        )
        self._current_index = self._screen_index(self._current_workspace)
        self._activation_return_workspace = self._current_workspace
        self._status_message = "Đang chuẩn bị nền tảng"
        self._backend_ready = False
        self._runtime_state = "stopped"
        self._shutting_down = False
        self._project_name = "Chưa mở dự án"
        self._save_state = "Không có thay đổi"
        self._import_busy = False
        self._job_history_busy = False
        self._deleting_job_id = ""
        self._pending_history_deletion_ids: set[str] = set()
        self._project_delete_busy = False
        self._deleting_project_id = ""
        self._deleting_project_name = ""
        self._duplicate_media_info: dict[str, object] = {}
        self._multi_batch: dict[str, object] = {"state": "idle"}
        self._multi_items: list[dict[str, object]] = []
        self._multi_hidden_count = 0
        self._multi_selection_generation = 0
        self._multi_import_paths: list[Path] = []
        self._multi_import_project_ids: list[str] = []
        self._multi_detail_busy = False
        self._multi_detail_generation = 0
        self._pending_multi_detail_project_id = ""
        self._multi_control_snapshot: dict[str, object] | None = None
        self._pending_parallel_action: tuple[str, tuple[object, ...]] | None = None
        self._parallel_warning_approved_once = False
        self._pending_media_path = ""
        self._pending_media_intent = ""
        self._selected_clip_id = ""
        self._video_clipboard_project_id = ""
        self._selected_track_id = ""
        self._selected_track_ids: set[str] = set()
        self._playhead_us = 0
        self._timeline_seek_step_ms = max(
            1,
            min(1_000, self.settings.value("timeline/seekStepMs", 100, type=int)),
        )
        self._preview_master_volume_percent = max(
            0,
            min(100, self.settings.value("preview/masterVolumePercent", 90, type=int)),
        )
        self._timeline_busy = False
        self._active_timeline_cut_request_id = ""
        self._timeline_cut_timeout_timer = QTimer(self)
        self._timeline_cut_timeout_timer.setSingleShot(True)
        self._timeline_cut_timeout_timer.setInterval(30_000)
        self._timeline_cut_timeout_timer.timeout.connect(self._timeline_cut_timed_out)
        self._timeline_cut_context: dict[str, object] = {}
        self._timeline_reconcile_pending = False
        self._timeline_reconcile_generation = 0
        self._timeline_reconcile_project_id = ""
        self._pending_effect_commands: list[dict[str, object]] = []
        self._effect_generations: dict[str, int] = {}
        self._can_undo_timeline = False
        self._can_redo_timeline = False
        self._ocr_busy = False
        self._ocr_job_id = ""
        self._ocr_job_state = ""
        self._ocr_job_method = "frame_ocr"
        saved_extraction_method = (
            self.settings.value("ocr/extractionMethod", "frame_ocr", type=str).strip().casefold()
        )
        self._ocr_extraction_method = (
            saved_extraction_method
            if saved_extraction_method in {"frame_ocr", "whisper_audio"}
            else "frame_ocr"
        )
        try:
            saved_ocr_fps = float(self.settings.value("ocr/sampleFps", 1.25))
        except (TypeError, ValueError):
            saved_ocr_fps = 1.25
        self._ocr_sample_fps = round(max(0.67, min(2.5, saved_ocr_fps)), 2)
        self._ocr_scene_text_enabled = self.settings.value(
            "ocr/sceneTextEnabled", True, type=bool
        )
        self._reloaded_ocr_jobs: set[str] = set()
        self._casting_after_ocr_jobs: set[str] = set()
        self._translation_busy = False
        self._translation_catalog_generation = 0
        self._translation_catalog_models: list[str] = []
        self._translation_catalog_provider = ""
        self._translation_catalog_busy = False
        self._translation_catalog_status = "Gợi ý model. Có thể chọn hoặc nhập tên thủ công."

        self._translation_preferences_busy = False
        self._translation_job_id = ""
        self._translation_job_state = ""
        self._translation_provider_id = ""
        self._translation_model_id = ""
        self._translation_base_url = ""
        self._translation_job_credential_references: list[str] = []
        self._translation_job_style_preset = "auto"
        self._translation_job_custom_rules = ""
        self._translation_style_control_pending: dict[str, object] | None = None
        self._translation_switch_pending: dict[str, object] | None = None
        self._translation_caches: list[dict[str, object]] = []
        self._selected_translation_cache_id = ""
        self._translation_cache_selection_explicit = False
        self._translation_cache_refresh_pending = False
        self._translation_cache_refresh_marker: tuple[str, str, str, str] = (
            "",
            "",
            "",
            "",
        )
        self._reloaded_translation_jobs: set[str] = set()
        self._pending_translation_start: dict[str, object] | None = None
        self._translation_event_sequences: dict[str, int] = {}
        self._translation_event_requests: set[str] = set()
        self._translation_qa_event_sequences: dict[str, int] = {}
        self._translation_qa_event_requests: set[str] = set()
        self._translation_terminal_event_jobs: set[str] = set()
        # Terminal job records are polled repeatedly. Keep the last observed
        # marker so a failure/completion notice is emitted once per transition
        # instead of either spamming every poll or silently disappearing.
        self._terminal_job_notification_markers: dict[str, tuple[object, ...]] = {}
        self._superseded_job_ids: set[str] = set()
        self._jobs_snapshot_initialized = False
        self._translation_qa: dict[str, object] = {
            "status": "NONE",
            "completed_cues": 0,
            "checked_cues": 0,
            "cue_total": 0,
            "blocking_count": 0,
            "blocking_issue_count": 0,
            "warning_count": 0,
            "issues": [],
            "content_warnings": [],
        }
        self._tts_busy = False
        self._tts_job_id = ""
        self._tts_job_state = ""
        self._tts_job_purpose = ""
        self._pending_tts_start: dict[str, object] | None = None
        self._dialogue_casting: dict[str, object] = {
            "project_id": "",
            "config": {
                "enabled": False,
                "male_voice": None,
                "female_voice": None,
                "fallback_voice": None,
            },
            "rows": [],
            "experimental": True,
        }
        self._dialogue_casting_loading = False
        self._dialogue_casting_saving = False
        self._dialogue_casting_job_id = ""
        self._dialogue_casting_job_state = ""
        self._dialogue_casting_auto_tts = False
        self._dialogue_casting_auto_tts_job_id = ""
        self._dialogue_casting_terminal_markers: set[tuple[str, str]] = set()
        self._observed_active_ocr_jobs: set[str] = set()
        self._pending_casting_after_ocr: dict[str, str] | None = None
        self._voice_playback_enabled = True
        self._voice_timing_auto_repair_busy = False
        self._reloaded_tts_jobs: set[str] = set()
        try:
            saved_read_speed = float(self.settings.value("voice/readSpeed", 1.3))
        except (TypeError, ValueError):
            saved_read_speed = 1.3
        self._voice_read_speed = round(max(1.1, min(3.0, saved_read_speed)), 1)
        try:
            saved_cue_speed = float(
                self.settings.value("voice/cueMaxTotalSpeed", max(self._voice_read_speed, 1.5))
            )
        except (TypeError, ValueError):
            saved_cue_speed = max(self._voice_read_speed, 1.5)
        self._voice_cue_max_speed = round(max(self._voice_read_speed, min(3.0, saved_cue_speed)), 1)
        self._voice_timing_review_requests: set[str] = set()
        self._voice_timing_repair_requests: set[str] = set()
        self._voice_timing_retry_requests: set[str] = set()
        self._voice_timing_override_requests: set[str] = set()
        self._voice_timing_qa: dict[str, object] = {
            "status": "NONE",
            "job_id": "",
            "issue_count": 0,
            "issues": [],
            "overflow_us": 0,
            "maximum_delay_us": 0,
            "repair_cycle": 0,
            "max_repair_cycles": 3,
            "repairable": False,
            "message": "",
        }
        self._voice_conform_mode = (
            str(self.settings.value("voice/conformMode", "hybrid", type=str)).strip().casefold()
        )
        if self._voice_conform_mode not in {"stretch_video", "speed_audio", "hybrid"}:
            self._voice_conform_mode = "hybrid"
        try:
            saved_conform_speed = float(self.settings.value("voice/conformMaxAudioSpeed", 1.25))
        except (TypeError, ValueError):
            saved_conform_speed = 1.25
        self._voice_conform_max_audio_speed = max(1.0, min(2.0, saved_conform_speed))
        self._voice_conform_estimate: dict[str, object] = {}
        # Keep the immutable production TTS parent separate from the cache-only
        # conform job. Reusing one identifier for both makes the next estimate
        # ask the service to treat a ``voice_conform`` job as a production D1.
        self._voice_conform_parent_tts_job_id = ""
        self._voice_conform_job_id = ""
        self._voice_conform_job_state = ""
        self._voice_conform_busy = False
        self._voice_conform_applied = False
        self._voice_conform_estimate_request_pending = False
        self._voice_conform_estimate_generation = 0
        self._voice_conform_estimate_refresh_queued = False
        self._voice_preview_source = ""
        self._voice_preview_job_id = ""
        self._voice_preview_target_job_id: str | None = None
        self._casting_preview_request_serial = 0
        self._casting_preview_role = ""
        self._voice_catalog_preview_source = ""
        self._voice_catalog_preview_id = ""
        self._export_busy = False
        self._export_job_id = ""
        self._export_job_state = ""
        self._last_export_source = ""
        self._last_export_qa_path = ""
        self._runtime_diagnostics: dict[str, object] = {}
        self._database_maintenance: dict[str, object] = {"state": "idle"}
        self._legacy_migration_report: dict[str, object] = {}
        self._legacy_source_path = ""
        saved_voice = self.settings.value("voice/selectedId", "", type=str)
        selected_voice = self.voice_catalog.find(saved_voice)
        if not selected_voice.get("installed"):
            preferred = self.voice_catalog.find("preset:Minh Quân Pro")
            selected_voice = (
                preferred
                if preferred.get("installed")
                else self.voice_catalog.first_installed("piper")
            )
        if not selected_voice:
            selected_voice = self.voice_catalog.first_installed()
        self._selected_voice_id = str(selected_voice.get("voice_id", ""))
        self._selected_cue_id = ""
        self._selected_cue_ids: list[str] = []
        self._subtitle_busy = False
        self._subtitle_command_generation = 0
        self._subtitle_command_context: dict[str, object] = {}
        self._subtitle_command_watchdog = QTimer(self)
        self._subtitle_command_watchdog.setSingleShot(True)
        self._subtitle_command_watchdog.setInterval(120_000)
        self._subtitle_command_watchdog.timeout.connect(self._subtitle_command_timed_out)
        self._scene_timing_generation = 0
        self._scene_timing_context: dict[str, object] = {}
        self._subtitle_geometry_generation = 0
        self._subtitle_geometry_context: dict = {}
        self._subtitle_save_in_flight = False
        self._subtitle_save_generation = 0
        self._subtitle_save_context: dict[str, object] = {}
        self._subtitle_save_queue: list[dict[str, object]] = []
        self._subtitle_flush_callbacks: dict[str, Callable[[], None]] = {}
        self._subtitle_save_timer = QTimer(self)
        self._subtitle_save_timer.setSingleShot(True)
        self._subtitle_save_timer.setInterval(150)
        self._subtitle_save_timer.timeout.connect(self._dispatch_background_subtitle_save)
        self._subtitle_save_watchdog = QTimer(self)
        self._subtitle_save_watchdog.setSingleShot(True)
        self._subtitle_save_watchdog.setInterval(120_000)
        self._subtitle_save_watchdog.timeout.connect(self._background_subtitle_save_timed_out)
        self._project_dirty = False
        self._draft_project_id = ""
        self._last_saved_revision = 0
        self._project_save_generation = 0
        self._project_save_context: dict[str, object] = {}
        self._project_after_save_callbacks: dict[str, Callable[[], None]] = {}
        self._project_autosave_timer = QTimer(self)
        self._project_autosave_timer.setSingleShot(True)
        self._project_autosave_timer.setInterval(300_000)
        self._project_autosave_timer.timeout.connect(self._request_project_autosave)
        self._project_save_watchdog = QTimer(self)
        self._project_save_watchdog.setSingleShot(True)
        self._project_save_watchdog.setInterval(30_000)
        self._project_save_watchdog.timeout.connect(self._project_save_timed_out)
        self._database_maintenance_timer = QTimer(self)
        self._database_maintenance_timer.setInterval(500)
        self._database_maintenance_timer.timeout.connect(self._poll_database_optimization)
        self._pending_manual_scene_text_commands: list[dict[str, object]] = []
        self._subtitle_export_busy = False
        self._can_undo_subtitle = False
        self._can_redo_subtitle = False
        self.runtime = JobRuntime(self)
        self.license_model = LicenseViewModel(self.runtime, self)
        self.license_model.changed.connect(self._license_changed)
        if not self.license_model.active:
            self._current_workspace = "settings"
            self._current_index = self._screen_index("settings")
        self.updater_model = UpdateViewModel(self.runtime, self.settings, self)
        self.runtime.statusChanged.connect(self._set_status)
        self.runtime.stateChanged.connect(self._set_runtime_state)
        self.runtime.readyChanged.connect(self._set_backend_ready)
        self.runtime.shutdownComplete.connect(self.quitReady)
        self.runtime.projectsReceived.connect(self.projects.replace)
        self.runtime.jobsReceived.connect(self._replace_jobs)
        self.runtime.multiVideosReceived.connect(self._replace_multi_videos)
        self.runtime.actionSucceeded.connect(self._action_succeeded)
        self.runtime.actionFailed.connect(self._action_failed)
        self.runtime.timelinePatchProjected.connect(self._timeline_patch_projected)
        self.voiceInstallEvent.connect(self._handle_voice_install_event)
        self.subtitleGeometryMeasured.connect(self._subtitle_geometry_measured,
                                               Qt.ConnectionType.QueuedConnection)

    @Property(QObject, constant=True)
    def navigationModel(self) -> QObject:
        return self.navigation

    @Property(QObject, constant=True)
    def updater(self) -> QObject:
        return self.updater_model

    @Property(QObject, constant=True)
    def activation(self) -> QObject:
        return self.license_model

    def _license_changed(self) -> None:
        if not self.license_model.active:
            self.navigate("settings")
        elif self._activation_return_workspace:
            target, self._activation_return_workspace = self._activation_return_workspace, ""
            self.navigate(target)

    def _features_available(self) -> bool:
        if self.license_model.active:
            return True
        self.navigate("settings")
        self._set_status("Chưa kích hoạt. Hãy nhập key tại Cài đặt → Kích hoạt.")
        return False

    @Property(QObject, constant=True)
    def projectModel(self) -> QObject:
        return self.projects

    @Property(QObject, constant=True)
    def multiVideoModel(self) -> QObject:
        return self.multi_videos

    @Property("QVariantMap", notify=multiVideoChanged)
    def multiBatch(self) -> dict[str, object]:
        return dict(self._multi_batch)

    @Property(str, notify=multiVideoChanged)
    def multiBatchState(self) -> str:
        return str(self._multi_batch.get("state", "idle"))

    @Property(int, notify=multiVideoChanged)
    def multiSelectedCount(self) -> int:
        return sum(1 for item in self._multi_items if item.get("selected") is True)

    @Property(int, notify=multiVideoChanged)
    def multiHiddenCount(self) -> int:
        return self._multi_hidden_count

    @Property(bool, notify=multiVideoChanged)
    def multiListLocked(self) -> bool:
        return self.multiBatchState in {"running", "pause_requested"}

    @Property(bool, notify=multiVideoChanged)
    def multiDetailBusy(self) -> bool:
        return self._multi_detail_busy

    @Property(QObject, constant=True)
    def jobModel(self) -> QObject:
        return self.jobs

    @Property(QObject, constant=True)
    def subtitleJobModel(self) -> QObject:
        return self.subtitle_jobs

    @Property(QObject, constant=True)
    def voiceJobModel(self) -> QObject:
        return self.voice_jobs

    @Property(QObject, constant=True)
    def voiceCatalogModel(self) -> QObject:
        return self.voice_catalog

    @Property(QObject, constant=True)
    def exportJobModel(self) -> QObject:
        return self.export_jobs

    @Property(QObject, constant=True)
    def currentProject(self) -> QObject:
        return self.project_state

    @Property(str, constant=True)
    def previewProviderId(self) -> str:
        return self.preview_provider.provider_id

    @Property(str, constant=True)
    def previewProviderSummary(self) -> str:
        return self.preview_provider.health().summary

    @Property(str, notify=currentWorkspaceChanged)
    def currentWorkspace(self) -> str:
        return self._current_workspace

    @Property(str, notify=currentWorkspaceChanged)
    def currentWorkspaceTitle(self) -> str:
        return next(
            item.title for item in NAVIGATION if item.workspace_id == self._current_workspace
        )

    @Property(int, notify=currentIndexChanged)
    def currentIndex(self) -> int:
        return self._current_index

    @Property(str, notify=projectNameChanged)
    def projectName(self) -> str:
        return self._project_name

    @Property(str, notify=saveStateChanged)
    def saveState(self) -> str:
        return self._save_state

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @Property(bool, notify=backendReadyChanged)
    def backendReady(self) -> bool:
        return self._backend_ready

    @Property(str, notify=runtimeStateChanged)
    def runtimeState(self) -> str:
        return self._runtime_state

    @Property(bool, notify=shuttingDownChanged)
    def shuttingDown(self) -> bool:
        return self._shutting_down

    @Property(bool, notify=importBusyChanged)
    def importBusy(self) -> bool:
        return self._import_busy

    @Property(bool, notify=jobHistoryBusyChanged)
    def jobHistoryBusy(self) -> bool:
        return self._job_history_busy

    @Property(str, notify=jobHistoryBusyChanged)
    def deletingJobId(self) -> str:
        return self._deleting_job_id

    @Property(bool, notify=projectDeleteChanged)
    def projectDeleteBusy(self) -> bool:
        return self._project_delete_busy

    @Property(str, notify=projectDeleteChanged)
    def deletingProjectId(self) -> str:
        return self._deleting_project_id

    @Property(str, notify=projectDeleteChanged)
    def deletingProjectName(self) -> str:
        return self._deleting_project_name

    @Property("QVariantMap", notify=duplicateMediaChanged)
    def duplicateMediaInfo(self) -> dict[str, object]:
        return dict(self._duplicate_media_info)

    @Property(str, notify=selectedClipChanged)
    def selectedClipId(self) -> str:
        return self._selected_clip_id

    @Property(bool, notify=selectedClipChanged)
    def hasSelectedClip(self) -> bool:
        return bool(self._selected_clip_id)

    @Property(str, notify=selectedTrackChanged)
    def selectedTrackId(self) -> str:
        return self._selected_track_id

    @Property(bool, notify=selectedTrackChanged)
    def hasSelectedTrack(self) -> bool:
        return bool(self._selected_track_id)

    @Property("QVariantList", notify=selectedTrackChanged)
    def selectedTrackIds(self) -> list[str]:
        return sorted(self._selected_track_ids)

    @Property(bool, notify=selectedTrackChanged)
    def hasMultipleSelectedTracks(self) -> bool:
        return len(self._selected_track_ids) > 1

    @Property(bool, notify=selectedTrackChanged)
    def selectedTrackMuted(self) -> bool:
        track = self.project_state.track_info(self._selected_track_id)
        return track.get("muted") is True if track else False

    @Property(bool, notify=selectedTrackChanged)
    def selectedTrackLocked(self) -> bool:
        track = self.project_state.track_info(self._selected_track_id)
        return track.get("locked") is True if track else False

    @Property(float, notify=selectedClipChanged)
    def selectedClipSpeedPercent(self) -> float:
        clip = self.project_state.clip_info(self._selected_clip_id)
        speed = clip.get("speed", {}) if clip else {}
        numerator = speed.get("numerator", 1)
        denominator = speed.get("denominator", 1)
        if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
            return 100.0
        return numerator * 100.0 / denominator

    @Property(float, notify=selectedClipChanged)
    def selectedClipVolumePercent(self) -> float:
        clip = self.project_state.clip_info(self._selected_clip_id)
        volume = clip.get("volume", 1.0) if clip else 1.0
        if isinstance(volume, bool) or not isinstance(volume, (int, float)):
            return 100.0
        return float(volume) * 100.0

    @Property(bool, notify=selectedClipChanged)
    def selectedClipMuted(self) -> bool:
        clip = self.project_state.clip_info(self._selected_clip_id)
        return bool(clip.get("muted", False)) if clip else False

    @Property(bool, notify=videoClipboardChanged)
    def canPasteVideo(self) -> bool:
        return bool(self._video_clipboard_project_id
                    and self._video_clipboard_project_id == self.project_state.projectId)

    @Property(str, notify=selectedClipChanged)
    def selectedClipCueText(self) -> str:
        clip = self.project_state.clip_info(self._selected_clip_id)
        return str(clip.get("cue_text", "")) if clip else ""

    @Property(str, notify=selectedClipChanged)
    def selectedClipCueId(self) -> str:
        clip = self.project_state.clip_info(self._selected_clip_id)
        cue_id = clip.get("cue_id") if clip else ""
        return cue_id if isinstance(cue_id, str) else ""

    @Property(str, notify=selectedClipChanged)
    def selectedClipTrackKind(self) -> str:
        clip = self.project_state.clip_info(self._selected_clip_id)
        if not clip:
            return ""
        track = self.project_state.track_info(str(clip.get("track_id", "")))
        return str(track.get("kind", "")) if track else ""

    @Property(bool, notify=selectedClipChanged)
    def selectedClipTimingUnresolved(self) -> bool:
        timing = self.project_state.clip_timing_info(self._selected_clip_id)
        return timing.get("state") == "overflow"

    @Property(str, notify=selectedClipChanged)
    def selectedClipTimingState(self) -> str:
        timing = self.project_state.clip_timing_info(self._selected_clip_id)
        return str(timing.get("state", "unknown"))

    @Property(bool, notify=selectedClipChanged)
    def selectedClipContentWarning(self) -> bool:
        clip = self.project_state.clip_info(self._selected_clip_id)
        return bool(clip.get("content_warning", False)) if clip else False

    @Property(str, notify=selectedClipChanged)
    def selectedClipIssueCode(self) -> str:
        clip = self.project_state.clip_info(self._selected_clip_id)
        return str(clip.get("issue_code", "")) if clip else ""

    @Property(str, notify=selectedClipChanged)
    def selectedClipTimingText(self) -> str:
        clip = self.project_state.clip_info(self._selected_clip_id)
        if not clip:
            return ""
        timeline = clip.get("timeline_range", {})
        slot = clip.get("timing_slot", {})
        if not isinstance(timeline, dict):
            return "Chưa có khoảng thời gian clip để kiểm tra"
        if not isinstance(slot, dict):
            return "Chưa có mốc SRT để đối chiếu; hãy tạo lại D1 từ cache nếu cần chỉnh cue"
        try:
            actual = (int(timeline["end_us"]) - int(timeline["start_us"])) / 1_000_000
            expected = (int(slot["end_us"]) - int(slot["start_us"])) / 1_000_000
        except (KeyError, TypeError, ValueError):
            return ""
        timing = self.project_state.clip_timing_info(self._selected_clip_id)
        state = str(timing.get("state", "unknown"))
        start_delta_ms = int(timing.get("start_delta_us", 0) or 0) / 1_000
        duration_delta_ms = int(timing.get("duration_delta_us", 0) or 0) / 1_000
        required_speed = float(timing.get("required_speed", 1.0) or 1.0)
        state_text = {
            "overflow": (
                f"Dài hơn SRT {duration_delta_ms:.0f} ms; cần khoảng {required_speed:.2f}× để khớp"
            ),
            "underflow": f"Ngắn hơn SRT {abs(duration_delta_ms):.0f} ms",
            "position_mismatch": (f"Thời lượng phù hợp nhưng lệch vị trí {start_delta_ms:+.0f} ms"),
            "matched": "Đã khớp SRT trong sai số ±50 ms",
            "unknown": "Chưa có mốc SRT để đối chiếu",
        }.get(state, "Chưa xác định trạng thái timing")
        return (
            f"{state_text}\nSRT {expected:.3f}s · hiện tại {actual:.3f}s · "
            f"độ lệch thời lượng {actual - expected:+.3f}s"
        )

    @Property(float, notify=playheadChanged)
    def playheadUs(self) -> float:
        return float(self._playhead_us)

    @Property(int, notify=timelineNavigationChanged)
    def timelineSeekStepMs(self) -> int:
        return self._timeline_seek_step_ms

    @Slot(int)
    def setTimelineSeekStepMs(self, value: int) -> None:
        normalized = max(1, min(1_000, int(value)))
        if normalized == self._timeline_seek_step_ms:
            return
        self._timeline_seek_step_ms = normalized
        self.settings.setValue("timeline/seekStepMs", normalized)
        self.timelineNavigationChanged.emit()

    @Property(bool, notify=timelineBusyChanged)
    def timelineBusy(self) -> bool:
        return self._timeline_busy

    @Property(bool, notify=timelineHistoryChanged)
    def canUndoTimeline(self) -> bool:
        return self._can_undo_timeline

    @Property(bool, notify=timelineHistoryChanged)
    def canRedoTimeline(self) -> bool:
        return self._can_redo_timeline

    @Property(bool, notify=ocrBusyChanged)
    def ocrBusy(self) -> bool:
        return self._ocr_busy

    @Property(str, notify=ocrSettingsChanged)
    def ocrEnginePython(self) -> str:
        # Explicit process-local overrides must match runtime diagnostics even
        # when a different installation's Python was saved in user settings.
        return _configured_ocr_engine_python() or self.settings.value(
            "engines/paddlePython", _default_ocr_engine_python(), type=str,
        )

    @Property(str, notify=ocrSettingsChanged)
    def ocrExtractionMethod(self) -> str:
        return self._ocr_extraction_method

    @Property(str, notify=ocrSettingsChanged)
    def whisperRuntimePython(self) -> str:
        return str(whisper_runtime_python())

    @Property(str, notify=ocrSettingsChanged)
    def whisperModelPath(self) -> str:
        model = whisper_model_path()
        return str(model) if model is not None else ""

    @Property(bool, notify=ocrSettingsChanged)
    def whisperReady(self) -> bool:
        return whisper_runtime_is_ready() and whisper_model_path() is not None

    @Property(str, notify=ocrSettingsChanged)
    def whisperHealthText(self) -> str:
        runtime_ready = whisper_runtime_is_ready()
        model_ready = whisper_model_path() is not None
        if runtime_ready and model_ready:
            return "Whisper large-v3 sẵn sàng · ưu tiên CUDA, tự dự phòng CPU"
        if not runtime_ready and not model_ready:
            return "Chưa cài runtime và model Whisper large-v3"
        if not runtime_ready:
            return "Đã có model nhưng thiếu runtime Whisper"
        return "Đã có runtime nhưng thiếu model Whisper large-v3"

    @Property(float, notify=ocrSettingsChanged)
    def ocrSampleFps(self) -> float:
        """The OCR sampling rate shared by single-video and Multi runs."""

        return self._ocr_sample_fps

    @Property(bool, notify=ocrSettingsChanged)
    def ocrSceneTextEnabled(self) -> bool:
        return self._ocr_scene_text_enabled

    @Slot(bool)
    def setOcrSceneTextEnabled(self, enabled: bool) -> None:
        if self._ocr_scene_text_enabled == enabled:
            return
        self._ocr_scene_text_enabled = enabled
        self.settings.setValue("ocr/sceneTextEnabled", enabled)
        self.ocrSettingsChanged.emit()

    @Property(str, notify=ocrBusyChanged)
    def ocrJobId(self) -> str:
        return self._ocr_job_id

    @Property(str, notify=ocrBusyChanged)
    def ocrJobState(self) -> str:
        return self._ocr_job_state

    @Property(bool, notify=translationBusyChanged)
    def translationBusy(self) -> bool:
        return self._translation_busy

    @Property(bool, notify=translationPreferencesBusyChanged)
    def translationPreferencesBusy(self) -> bool:
        return self._translation_preferences_busy

    @Property(str, notify=translationBusyChanged)
    def translationJobId(self) -> str:
        return self._translation_job_id

    @Property(str, notify=translationBusyChanged)
    def translationJobState(self) -> str:
        return self._translation_job_state

    @Property("QVariantList", notify=translationCachesChanged)
    def translationCaches(self) -> list[dict[str, object]]:
        return [dict(item) for item in self._translation_caches]

    @Property(str, notify=translationCachesChanged)
    def selectedTranslationCacheId(self) -> str:
        return self._selected_translation_cache_id

    @Property("QVariantMap", notify=translationQaChanged)
    def translationQa(self) -> dict[str, object]:
        snapshot = dict(self._translation_qa)
        snapshot["issues"] = [
            dict(issue) for issue in snapshot.get("issues", []) if isinstance(issue, dict)
        ]
        return snapshot

    @Property("QVariantMap", notify=voiceTimingQaChanged)
    def voiceTimingQa(self) -> dict[str, object]:
        snapshot = dict(self._voice_timing_qa)
        snapshot["issues"] = [
            dict(issue) for issue in snapshot.get("issues", []) if isinstance(issue, dict)
        ]
        return snapshot

    @Property(str, notify=voiceConformChanged)
    def voiceConformMode(self) -> str:
        return self._voice_conform_mode

    @Property(float, notify=voiceConformChanged)
    def voiceConformMaxAudioSpeed(self) -> float:
        return self._voice_conform_max_audio_speed

    @Property("QVariantMap", notify=voiceConformChanged)
    def voiceConformEstimate(self) -> dict[str, object]:
        snapshot = dict(self._voice_conform_estimate)
        raw_issues = snapshot.get("issues", [])
        snapshot["issues"] = [dict(item) for item in raw_issues if isinstance(item, dict)]
        return snapshot

    @Property(bool, notify=voiceConformChanged)
    def voiceConformCanApply(self) -> bool:
        return bool(self._voice_conform_estimate.get("can_apply") is True)

    @Property(bool, notify=voiceConformChanged)
    def voiceConformApplied(self) -> bool:
        return self._voice_conform_applied

    @Property(bool, notify=voiceConformChanged)
    def voiceConformBusy(self) -> bool:
        return self._voice_conform_busy

    @Property(str, notify=voiceConformChanged)
    def voiceConformJobState(self) -> str:
        return self._voice_conform_job_state

    @Property(bool, notify=ttsBusyChanged)
    def ttsBusy(self) -> bool:
        return self._tts_busy

    @Property(str, notify=ttsBusyChanged)
    def ttsJobId(self) -> str:
        return self._tts_job_id

    @Property(str, notify=ttsBusyChanged)
    def ttsJobState(self) -> str:
        return self._tts_job_state

    @Property(str, notify=ttsBusyChanged)
    def ttsJobPurpose(self) -> str:
        return self._tts_job_purpose

    @Property("QVariantMap", notify=dialogueCastingChanged)
    def dialogueCasting(self) -> dict:
        return deepcopy(self._dialogue_casting)

    @Property(bool, notify=dialogueCastingChanged)
    def dialogueCastingLoading(self) -> bool:
        return self._dialogue_casting_loading

    @Property(bool, notify=dialogueCastingChanged)
    def dialogueCastingSaving(self) -> bool:
        return self._dialogue_casting_saving

    @Property(str, notify=dialogueCastingChanged)
    def dialogueCastingJobState(self) -> str:
        return self._dialogue_casting_job_state

    @Property(bool, notify=dialogueCastingChanged)
    def dialogueCastingBusy(self) -> bool:
        return self._dialogue_casting_job_state in {
            "queued", "start_requested", "running", "pause_requested",
            "paused", "resume_requested", "cancel_requested",
        }

    @Property(str, notify=voiceSettingsChanged)
    def selectedVoiceId(self) -> str:
        return self._selected_voice_id

    @Property(str, notify=voiceSettingsChanged)
    def selectedVoiceProvider(self) -> str:
        return str(self.voice_catalog.find(self._selected_voice_id).get("provider_id", ""))

    @Property("QVariantMap", notify=voiceSettingsChanged)
    def selectedVoice(self) -> dict:
        return self.voice_catalog.find(self._selected_voice_id)

    @Property(str, notify=voicePreviewChanged)
    def voicePreviewSource(self) -> str:
        return self._voice_preview_source

    @Property(str, notify=ttsBusyChanged)
    def dialogueCastingPreviewRole(self) -> str:
        return (self._casting_preview_role
                if self._tts_busy and self._tts_job_purpose == "preview" else "")

    @Property(str, notify=voiceCatalogPlaybackChanged)
    def voiceCatalogPreviewSource(self) -> str:
        return self._voice_catalog_preview_source

    @Property(str, notify=voiceCatalogPlaybackChanged)
    def voiceCatalogPreviewId(self) -> str:
        return self._voice_catalog_preview_id

    @Property(bool, notify=voicePlaybackEnabledChanged)
    def voicePlaybackEnabled(self) -> bool:
        return self._voice_playback_enabled

    @Property(bool, notify=exportBusyChanged)
    def exportBusy(self) -> bool:
        return self._export_busy

    @Property(str, notify=exportBusyChanged)
    def exportJobId(self) -> str:
        return self._export_job_id

    @Property(str, notify=exportBusyChanged)
    def exportJobState(self) -> str:
        return self._export_job_state

    @Property(str, notify=exportSettingsChanged)
    def exportOutputPath(self) -> str:
        key = f"export/outputPath/{self.project_state.projectId or 'none'}"
        configured = self.settings.value(key, "", type=str).strip()
        if configured:
            return configured
        source = self.project_state.primaryVideoPath
        if not source:
            return ""
        path = Path(source)
        return str(path.with_name(path.stem + "_VI_FINAL.mp4"))

    @Property(str, notify=exportSettingsChanged)
    def exportPresetId(self) -> str:
        return self.settings.value("export/presetId", "source-quality", type=str)

    @Property(str, notify=exportSettingsChanged)
    def exportBackend(self) -> str:
        return self.settings.value("export/backend", "auto", type=str)

    @Property(int, notify=exportSettingsChanged)
    def exportChunkMinutes(self) -> int:
        return self.settings.value("export/chunkMinutes", 10, type=int)

    @Property(bool, notify=exportSettingsChanged)
    def exportOverwrite(self) -> bool:
        return self.settings.value("export/overwrite", False, type=bool)

    @Property(bool, notify=exportSettingsChanged)
    def exportSubtitleEnabled(self) -> bool:
        return self.settings.value("export/subtitleEnabled", True, type=bool)

    @Property(bool, notify=exportSettingsChanged)
    def exportSubtitleBackgroundEnabled(self) -> bool:
        return self.settings.value("export/subtitleBackgroundEnabled", True, type=bool)

    @Property(str, notify=exportSettingsChanged)
    def exportSubtitleBackgroundMode(self) -> str:
        value = self.settings.value("export/subtitleBackgroundMode", "black", type=str)
        return value if value in {"black", "blur"} else "black"

    @Slot(bool)
    def setExportSubtitleBackgroundEnabled(self, enabled: bool) -> None:
        self.settings.setValue("export/subtitleBackgroundEnabled", enabled)
        self.exportSettingsChanged.emit()

    @Slot(str)
    def setExportSubtitleBackgroundMode(self, mode: str) -> None:
        if mode not in {"black", "blur"}:
            return
        self.settings.setValue("export/subtitleBackgroundMode", mode)
        self.exportSettingsChanged.emit()

    @Property(str, notify=exportSettingsChanged)
    def exportFontFamily(self) -> str:
        return self.settings.value("export/fontFamily", "Arial", type=str)

    @Property(int, notify=exportSettingsChanged)
    def exportFontSize(self) -> int:
        return self.settings.value("export/fontSize", 42, type=int)

    @Property(int, notify=exportSettingsChanged)
    def exportSubtitleMarginBottom(self) -> int:
        return self.settings.value("export/subtitleMarginBottom", 28, type=int)

    @Property(str, notify=exportSettingsChanged)
    def exportSubtitlePrimaryColor(self) -> str:
        value = self.settings.value("export/subtitlePrimaryColor", "#FFFFFF", type=str)
        return _normalized_hex_color(value) or "#FFFFFF"

    @Property(str, notify=exportSettingsChanged)
    def exportSubtitleOutlineColor(self) -> str:
        value = self.settings.value("export/subtitleOutlineColor", "#000000", type=str)
        return _normalized_hex_color(value) or "#000000"

    @Property(int, notify=exportSettingsChanged)
    def exportSubtitleOutlineWidth(self) -> int:
        return max(
            0,
            min(12, self.settings.value("export/subtitleOutlineWidth", 3, type=int)),
        )

    @Property(int, notify=exportSettingsChanged)
    def exportSubtitleAlignment(self) -> int:
        value = self.settings.value("export/subtitleAlignment", 2, type=int)
        return value if value in {1, 2, 3} else 2

    @Property(int, notify=exportSettingsChanged)
    def exportSubtitleOpacityPercent(self) -> int:
        return max(
            0,
            min(100, self.settings.value("export/subtitleOpacityPercent", 100, type=int)),
        )

    @Property(str, notify=exportSettingsChanged)
    def exportVideoFilterPreset(self) -> str:
        value = self.settings.value("export/videoFilterPreset", "none", type=str)
        return value if value in {"none", "cinematic-warm", "cool", "grayscale"} else "none"

    @Property(int, notify=exportSettingsChanged)
    def exportVideoFilterIntensity(self) -> int:
        return max(
            0,
            min(100, self.settings.value("export/videoFilterIntensity", 100, type=int)),
        )

    @Property(int, notify=exportSettingsChanged)
    def exportVideoBrightness(self) -> int:
        return max(
            -50,
            min(50, self.settings.value("export/videoBrightness", 0, type=int)),
        )

    @Property(int, notify=exportSettingsChanged)
    def exportVideoContrast(self) -> int:
        return max(
            50,
            min(150, self.settings.value("export/videoContrast", 100, type=int)),
        )

    @Property(int, notify=exportSettingsChanged)
    def exportVideoSaturation(self) -> int:
        return max(
            0,
            min(200, self.settings.value("export/videoSaturation", 100, type=int)),
        )

    @Property(str, notify=exportPreviewChanged)
    def lastExportSource(self) -> str:
        return self._last_export_source

    @Property(str, notify=exportPreviewChanged)
    def lastExportQaPath(self) -> str:
        return self._last_export_qa_path

    @Property(bool, notify=exportPreviewChanged)
    def canOpenExportFolder(self) -> bool:
        path = QUrl(self._last_export_source).toLocalFile()
        return bool(path and Path(path).is_file())

    @Slot()
    def openExportFolder(self) -> None:
        path = QUrl(self._last_export_source).toLocalFile()
        if not path or not Path(path).is_file():
            self._set_status("Chưa có video xuất thành công hoặc file đã được di chuyển")
            return
        folder = Path(path).resolve().parent
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self._set_status("Không mở được thư mục xuất: " + str(folder))

    @Property("QVariantMap", notify=diagnosticsChanged)
    def runtimeDiagnostics(self) -> dict[str, object]:
        return self._runtime_diagnostics

    @Property("QVariantMap", notify=databaseMaintenanceChanged)
    def databaseMaintenance(self) -> dict[str, object]:
        return dict(self._database_maintenance)

    @Property(bool, notify=databaseMaintenanceChanged)
    def databaseMaintenanceBusy(self) -> bool:
        return self._database_maintenance.get("state") in {"running", "cancelling"}

    @Property("QVariantMap", notify=legacyMigrationChanged)
    def legacyMigrationReport(self) -> dict[str, object]:
        return self._legacy_migration_report

    @Property(str, notify=voiceSettingsChanged)
    def vieneuReferenceAudio(self) -> str:
        return self.settings.value("voice/vieneuReferenceAudio", "", type=str)

    @Property(str, notify=voiceSettingsChanged)
    def vieneuReferenceSource(self) -> str:
        path = self.vieneuReferenceAudio
        return QUrl.fromLocalFile(path).toString() if path else ""

    @Property(str, notify=voiceSettingsChanged)
    def vieneuReferenceText(self) -> str:
        return self.settings.value("voice/vieneuReferenceText", "", type=str)

    @Property(bool, notify=voiceSettingsChanged)
    def vieneuReferenceDenoise(self) -> bool:
        return self.settings.value("voice/vieneuReferenceDenoise", False, type=bool)

    @Property(bool, notify=voiceSettingsChanged)
    def voiceAdaptiveTimingEnabled(self) -> bool:
        return True

    @Property(bool, notify=voiceSettingsChanged)
    def voiceAutoCondenseLongCuesEnabled(self) -> bool:
        return True

    @Property(bool, notify=ttsBusyChanged)
    def voiceTimingAutoRepairBusy(self) -> bool:
        """Whether automatic timing-repair translation is currently in flight."""

        return self._voice_timing_auto_repair_busy

    @Property(float, notify=voiceSettingsChanged)
    def voiceReadSpeed(self) -> float:
        return self._voice_read_speed

    @Property(float, notify=voiceSettingsChanged)
    def voiceCueMaxSpeed(self) -> float:
        return self._voice_cue_max_speed

    @Property("QStringList", notify=translationSettingsChanged)
    def geminiApiCredentialReferences(self) -> list[str]:
        raw = self.settings.value("translation/geminiApiCredentialReferences", [])
        return _stable_credential_references(raw)

    def _translation_api_references(self, provider_id: str) -> list[str]:
        raw = self.settings.value(_credential_setting_key(provider_id), [])
        return _stable_credential_references(raw)

    @Property("QStringList", notify=translationSettingsChanged)
    def deepseekApiCredentialReferences(self) -> list[str]:
        return self._translation_api_references("deepseek-api")

    @Property("QStringList", notify=translationSettingsChanged)
    def tokenhubApiCredentialReferences(self) -> list[str]:
        return self._translation_api_references("tokenhub-api")

    @Property("QStringList", notify=translationSettingsChanged)
    def qwenApiCredentialReferences(self) -> list[str]:
        return self._translation_api_references("qwen-api")

    @Property("QStringList", notify=translationSettingsChanged)
    def glmApiCredentialReferences(self) -> list[str]:
        return self._translation_api_references("glm-api")

    @Property("QStringList", notify=translationSettingsChanged)
    def openaiCompatibleCredentialReferences(self) -> list[str]:
        return self._translation_api_references("openai-compatible")

    @Property(str, notify=selectedCueChanged)
    def selectedCueId(self) -> str:
        return self._selected_cue_id

    @Property("QVariantList", notify=selectedCueChanged)
    def selectedCueIds(self) -> list[str]:
        return list(self._selected_cue_ids)

    @Property(int, notify=selectedCueChanged)
    def selectedCueCount(self) -> int:
        return len(self._selected_cue_ids)

    @Property(int, notify=selectedCueChanged)
    def selectedSceneTextCueCount(self) -> int:
        return sum(self.project_state.subtitle_cues.find(cue_id).get("cue_role") == "scene_text"
                   for cue_id in self._selected_cue_ids)

    @Property(bool, notify=selectedCueChanged)
    def selectedTextCueEditingLocked(self) -> bool:
        return self._text_cue_ids_locked(self._selected_cue_ids)

    def _text_cue_ids_locked(self, cue_ids: list[str]) -> bool:
        for cue_id in cue_ids:
            cue = self.project_state.subtitle_cues.find(cue_id)
            track_id = str(cue.get("subtitle_track_id") or (
                "T1" if self.project_state.title_cues.find(cue_id)
                else "S2" if cue.get("cue_role") == "scene_text" else "S1"
            ))
            track = self.project_state.track_info(track_id) or {}
            if track.get("locked") is True:
                return True
        return False

    @Property("QVariantMap", notify=selectedCueChanged)
    def selectedCue(self) -> dict:
        return self.project_state.subtitle_cues.find(self._selected_cue_id)

    @Property("QVariantMap", notify=selectedCueChanged)
    def selectedCueClassification(self) -> dict:
        cue_ids = self._selected_cue_ids or (
            [self._selected_cue_id] if self._selected_cue_id else []
        )
        cues = [self.project_state.subtitle_cues.find(cue_id) for cue_id in cue_ids]
        roles = {cue.get("cue_role", "dialogue") for cue in cues if cue}
        lanes = {cue.get("subtitle_track_id", "") for cue in cues if cue}
        return {
            "count": len(cue_ids),
            "role": next(iter(roles)) if len(roles) == 1 else ("mixed" if roles else ""),
            "track": next(iter(lanes)) if len(lanes) == 1 else "",
            "can_convert": bool(cues) and all(cues) and roles <= {"dialogue", "scene_text"},
        }

    @Property(bool, notify=subtitleBusyChanged)
    def subtitleBusy(self) -> bool:
        return self._subtitle_busy

    @Property(bool, notify=subtitleExportBusyChanged)
    def subtitleExportBusy(self) -> bool:
        return self._subtitle_export_busy

    @Property(bool, notify=subtitleHistoryChanged)
    def canUndoSubtitle(self) -> bool:
        return self._can_undo_subtitle

    @Property(bool, notify=subtitleHistoryChanged)
    def canRedoSubtitle(self) -> bool:
        return self._can_redo_subtitle

    @Property(str, constant=True)
    def buildVersion(self) -> str:
        return __version__

    @Slot()
    def start(self) -> None:
        self.runtime.start()

    @Slot()
    def requestQuit(self) -> None:
        if self._shutting_down:
            return
        if self._defer_until_subtitle_saves_flushed(
            "quit", self.requestQuit, "Đang lưu thay đổi cuối trước khi đóng ứng dụng…",
            durable=True,
        ):
            return
        self._shutting_down = True
        self.shuttingDownChanged.emit()
        self.runtime.request_shutdown()

    @Slot()
    def forceShutdown(self) -> None:
        self.runtime.force_shutdown()

    @Slot(str)
    def setOcrEnginePython(self, value: str) -> None:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            self._set_status(
                "Bộ máy trích xuất phụ đề chưa sẵn sàng. Hãy mở Cài đặt → Chẩn đoán runtime."
            )
            return
        self.settings.setValue("engines/paddlePython", str(path))
        self.ocrSettingsChanged.emit()

    @Slot(str)
    def setOcrExtractionMethod(self, value: str) -> None:
        method = value.strip().casefold()
        if method not in {"frame_ocr", "whisper_audio"}:
            self._set_status("Phương thức trích xuất không hợp lệ")
            return
        if self._ocr_busy:
            self._set_status("Hãy tạm dừng hoặc hoàn tất tác vụ trích xuất trước khi đổi chế độ")
            return
        if method == self._ocr_extraction_method:
            return
        self._ocr_extraction_method = method
        self.settings.setValue("ocr/extractionMethod", method)
        self.ocrSettingsChanged.emit()
        self._set_status(
            "Đã chọn Whisper âm thanh" if method == "whisper_audio" else "Đã chọn OCR khung hình"
        )

    @Slot(float)
    def setOcrSampleFps(self, value: float) -> None:
        if not math.isfinite(value) or not 0.67 <= value <= 2.5:
            self._set_status("FPS OCR phải nằm trong khoảng 0,67–2,5")
            return
        normalized = round(float(value), 2)
        if abs(normalized - self._ocr_sample_fps) < 1e-9:
            return
        self._ocr_sample_fps = normalized
        self.settings.setValue("ocr/sampleFps", normalized)
        self.ocrSettingsChanged.emit()

    @Slot()
    def runRuntimeDiagnostics(self) -> None:
        if not self._backend_ready:
            self._set_status("Job Manager chưa sẵn sàng để chẩn đoán")
            return
        self.runtime.request("runtime_diagnostics")
        self._set_status("Đang kiểm tra runtime và engine ở tiến trình nền")

    @Slot()
    def optimizeProjectDatabase(self) -> None:
        if not self._backend_ready or self.databaseMaintenanceBusy:
            return
        if self._defer_until_subtitle_saves_flushed(
            "database-optimizer",
            self.optimizeProjectDatabase,
            "Đang lưu project trước khi tối ưu database…",
            durable=True,
        ):
            return
        self._database_maintenance = {"state": "running"}
        self.databaseMaintenanceChanged.emit()
        self.runtime.request("optimize_project_database")
        self._set_status("Đang tạo database tối ưu và kiểm tra toàn vẹn trước khi đổi file…")

    @Slot()
    def cancelProjectDatabaseOptimization(self) -> None:
        if not self._backend_ready or not self.databaseMaintenanceBusy:
            return
        if self._database_maintenance.get("state") == "cancelling":
            return
        self._database_maintenance = {
            **self._database_maintenance,
            "state": "cancelling",
        }
        self.databaseMaintenanceChanged.emit()
        self.runtime.request("cancel_database_optimization")
        self._set_status("Đang hủy tối ưu tại checkpoint an toàn trước khi đổi database…")

    @Slot()
    def _poll_database_optimization(self) -> None:
        if not self._backend_ready or not self.databaseMaintenanceBusy:
            self._database_maintenance_timer.stop()
            return
        self.runtime.request("get_database_optimization")

    @Slot()
    def showLegacyImportDialog(self) -> None:
        if not self._backend_ready:
            self._set_status("Job Manager chưa sẵn sàng để đọc dự án cũ")
            return
        self.legacyImportDialogRequested.emit()

    @Slot(str)
    def inspectLegacyProjectPath(self, raw_path: str) -> None:
        path = _local_path_from_ui(raw_path)
        if not path.is_file():
            self._set_status("File dự án 0.1.99 không tồn tại")
            return
        self._legacy_source_path = str(path)
        self.runtime.request("inspect_legacy_project", {"source_path": str(path)})
        self._set_status("Đang dry-run dự án cũ; file nguồn chỉ được đọc")

    @Slot()
    def importInspectedLegacyProject(self) -> None:
        if not self._legacy_source_path or not self._legacy_migration_report:
            self._set_status("Hãy dry-run một dự án 0.1.99 trước")
            return
        source = Path(self._legacy_source_path)
        project_path = source.with_name(source.stem + "_DUCAI2.ducai")
        self.runtime.request(
            "import_legacy_project",
            {
                "source_path": str(source),
                "project_path": str(project_path),
                "name": source.stem,
            },
        )
        self._set_status("Đang nhập một chiều; dữ liệu 0.1.99 không bị sửa")

    @Slot(float)
    def startOcr(self, sample_fps: float = 1.25) -> None:
        if not self._features_available():
            return
        if self._guard_manual_parallel("ocr", (sample_fps,)):
            return
        if not self._backend_ready or not self.project_state.hasProject:
            self._set_status("Hãy nhập video trước khi chạy OCR")
            return
        if self._ocr_busy:
            self._set_status("OCR đang chạy; có thể tạm dừng hoặc hủy tác vụ hiện tại")
            return
        if self._defer_until_subtitle_saves_flushed(
            "ocr",
            lambda: self.startOcr(sample_fps),
            "Đang lưu project trước khi tạo tác vụ OCR…",
            durable=True,
        ):
            return
        method = self._ocr_extraction_method
        if method == "frame_ocr" and (
            not math.isfinite(sample_fps) or not 0.67 <= sample_fps <= 2.5
        ):
            self._set_status("FPS OCR phải nằm trong khoảng 0,67–2,5")
            return
        if method == "frame_ocr":
            self.setOcrSampleFps(sample_fps)
        interval_us = int(round(1_000_000 / self._ocr_sample_fps))
        runtime = (
            whisper_runtime_python() if method == "whisper_audio" else Path(self.ocrEnginePython)
        )
        model = whisper_model_path() if method == "whisper_audio" else None
        if method == "whisper_audio" and not whisper_runtime_is_ready(runtime):
            self._set_status(
                "Chưa cài runtime Whisper. Hãy chạy scripts/setup-whisper-runtime.ps1."
            )
            return
        if method == "whisper_audio" and model is None:
            self._set_status(
                "Chưa có model Whisper large-v3. Hãy chạy scripts/setup-whisper-runtime.ps1."
            )
            return
        self._set_ocr_job_state("start_requested")
        self._set_ocr_busy(True)
        self._ocr_job_method = method
        self.runtime.request(
            "create_ocr_job",
            {
                "project_id": self.project_state.projectId,
                "sample_interval_us": interval_us,
                "scene_text_enabled": self._ocr_scene_text_enabled,
                "engine_python": str(runtime),
                "device": "auto" if method == "whisper_audio" else "gpu:0",
                "extraction_method": method,
                "language": "zh",
                "model_id": WHISPER_MODEL_ID,
                "model_path": str(model) if model is not None else None,
            },
        )
        self._set_status(
            "Đang tạo tác vụ Whisper âm thanh"
            if method == "whisper_audio"
            else "Đang tạo tác vụ OCR khung hình"
        )

    @Slot(float)
    def toggleOcr(self, sample_fps: float = 1.25) -> None:
        """Dispatch the one primary OCR control from durable job state."""

        if self._ocr_job_method != self._ocr_extraction_method and self._ocr_job_state in {
            "paused",
            "interrupted",
            "failed",
            "cancelled",
            "succeeded",
        }:
            self.startOcr(sample_fps)
            return

        if self._ocr_job_state == "running":
            self.pauseOcr()
            return
        if self._ocr_job_state in {"paused", "interrupted", "failed", "queued"}:
            self.resumeOcr()
            return
        if self._ocr_job_state in {
            "start_requested",
            "pause_requested",
            "resume_requested",
            "cancel_requested",
        }:
            self._set_status("OCR đang chuyển trạng thái; hãy chờ xác nhận")
            return
        self.startOcr(sample_fps)

    @Slot()
    def pauseOcr(self) -> None:
        if self._ocr_job_id and self._ocr_job_state == "running":
            self._set_ocr_job_state("pause_requested")
            self.runtime.request(
                "pause_job",
                {"job_id": self._ocr_job_id},
                context={"job_kind": "ocr"},
            )

    @Slot()
    def resumeOcr(self) -> None:
        if self._ocr_job_id and self._ocr_job_state == "paused":
            self._set_ocr_job_state("resume_requested")
            self.runtime.request(
                "resume_job",
                {"job_id": self._ocr_job_id},
                context={"job_kind": "ocr"},
            )
        elif self._ocr_job_id and self._ocr_job_state == "queued":
            self._set_ocr_job_state("start_requested")
            self._start_ocr_worker(self._ocr_job_id)
        elif self._ocr_job_id and self._ocr_job_state in {"interrupted", "failed"}:
            self._set_ocr_job_state("resume_requested")
            self.runtime.request(
                "retry_job",
                {"job_id": self._ocr_job_id},
                context={"resume_ocr": True, "job_id": self._ocr_job_id},
            )

    @Slot()
    def cancelOcr(self) -> None:
        if self._ocr_job_id:
            self._set_ocr_job_state("cancel_requested")
            self.runtime.request(
                "cancel_job",
                {"job_id": self._ocr_job_id},
                context={"job_kind": "ocr"},
            )

    @Slot(str, str)
    @Slot(str, str, str)
    def setTranslationPreferences(
        self, preset: str, custom_rules: str, active_style_name: str = ""
    ) -> None:
        if not self._backend_ready or not self.project_state.hasProject:
            return
        if self._translation_preferences_busy:
            self._set_status("Văn phong đang được lưu; hãy chờ thao tác hiện tại hoàn tất")
            return
        if len(custom_rules) > 4_000:
            self._set_status("Quy tắc riêng tối đa 4.000 ký tự")
            return
        project_id = self.project_state.projectId
        self._set_translation_preferences_busy(True)
        try:
            self.runtime.request(
                "set_translation_preferences",
                {
                    "project_id": project_id,
                    "preset": preset,
                    "custom_rules": custom_rules,
                    "compact": True,
                    **({"active_style_name": active_style_name} if active_style_name else {}),
                },
                context={"project_id": project_id},
            )
        except (OSError, RuntimeError) as exc:
            self._set_translation_preferences_busy(False)
            self._set_status(f"Không thể gửi yêu cầu lưu văn phong: {exc}")
            return
        self._set_status("Đang lưu văn phong dịch cho project")

    @Slot(str, str, str)
    def saveTranslationStyle(self, name: str, preset: str, custom_rules: str) -> None:
        if not self._backend_ready or not self.project_state.hasProject:
            return
        if self._translation_preferences_busy:
            self._set_status("Văn phong đang được lưu; hãy chờ thao tác hiện tại hoàn tất")
            return
        normalized_name = name.strip()
        if not normalized_name:
            self._set_status("Hãy đặt tên cho văn phong trước khi lưu")
            return
        if len(normalized_name) > 80:
            self._set_status("Tên văn phong tối đa 80 ký tự")
            return
        if any(ord(character) < 32 for character in normalized_name):
            self._set_status("Tên văn phong không được chứa ký tự điều khiển")
            return
        if len(custom_rules) > 4_000:
            self._set_status("Quy tắc riêng tối đa 4.000 ký tự")
            return
        project_id = self.project_state.projectId
        self._set_translation_preferences_busy(True)
        try:
            self.runtime.request(
                "save_translation_style",
                {
                    "project_id": project_id,
                    "name": normalized_name,
                    "preset": preset,
                    "custom_rules": custom_rules,
                    "compact": True,
                },
                context={"project_id": project_id, "translation_style_name": normalized_name},
            )
        except (OSError, RuntimeError) as exc:
            self._set_translation_preferences_busy(False)
            self._set_status(f"Không thể gửi yêu cầu lưu văn phong: {exc}")
            return
        self._set_status(f"Đang lưu văn phong ‘{normalized_name}’")

    @Slot(str, str, bool, bool, str)
    @Slot(str, str, bool, bool, str, str)
    def startTranslation(
        self,
        provider_id: str,
        model_id: str,
        headed: bool = False,
        guest: bool = False,
        credential_input: str = "",
        base_url: str = "",
    ) -> None:
        if self._translation_preferences_busy:
            self._set_status("Đang lưu văn phong mới; chưa bắt đầu tác vụ dịch")
            return
        if self._guard_manual_parallel(
            "translate",
            (provider_id, model_id, headed, guest, credential_input, base_url),
        ):
            return
        if not self._backend_ready or not self.project_state.hasProject:
            self._set_status("Hãy mở dự án và nhập SRT Trung trước khi dịch")
            return
        if self.project_state.subtitle_cues.rowCount() <= 0:
            self._set_status("Project chưa có cue nguồn để dịch")
            return
        if self._translation_busy and self._translation_job_state not in {"paused", "queued"}:
            self._set_status("Dịch đang chạy; hãy dùng Tạm dừng hoặc Hủy")
            return
        if self._defer_until_subtitle_saves_flushed(
            "translation",
            lambda: self.startTranslation(
                provider_id,
                model_id,
                headed,
                guest,
                credential_input,
                base_url,
            ),
            "Đang lưu phụ đề trước khi tạo tác vụ dịch…",
            durable=True,
        ):
            return
        provider_id = provider_id.strip().casefold()
        model_id = model_id.strip()
        supported_provider_ids = {
            "gemini-web",
            "gemini-api",
            "gemini-cli",
            *OPENAI_COMPATIBLE_PROVIDER_IDS,
        }
        if provider_id not in supported_provider_ids:
            self._set_status("Nguồn dịch API không được hỗ trợ")
            return
        if not model_id:
            self._set_status("Hãy nhập model dịch")
            return
        matches_selection = self.translationSelectionMatches(provider_id, model_id, base_url)
        if self._translation_job_state in {"paused", "queued"} and not (
            matches_selection and not credential_input.strip()
        ):
            self._translation_switch_pending = {
                "project_id": self.project_state.projectId, "job_id": self._translation_job_id,
                "arguments": (provider_id, model_id, headed, guest, credential_input, base_url),
            }
            self._set_translation_job_state("cancel_requested")
            self.runtime.request("cancel_job", {"job_id": self._translation_job_id},
                                 context={"job_kind": "translate", "switch_provider": True})
            self._set_status(
                "Đang dừng lượt dịch cũ an toàn để dùng lựa chọn mới; cache được giữ lại"
            )
            return
        stopped_job_matches_selection = (
            bool(self._translation_job_id)
            and self._translation_job_state in {"cancelled", "failed", "interrupted", "review"}
            and matches_selection
        )
        if (
            provider_id in {"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS}
            and credential_input.strip()
        ):
            try:
                credentials = _parse_translation_api_credentials(
                    credential_input,
                    provider_id=provider_id,
                    existing_references=tuple(self._translation_api_references(provider_id)),
                )
            except ValueError as exc:
                self._set_status(str(exc))
                return
            references = [item["reference"] for item in credentials]
            self._pending_translation_start = {
                "provider_id": provider_id,
                "model_id": model_id,
                "headed": headed,
                "guest": guest,
                "base_url": base_url.strip(),
                "resume_job_id": (
                    self._translation_job_id if stopped_job_matches_selection else ""
                ),
            }
            self._set_translation_busy(True)
            self.runtime.request(
                "store_credentials",
                {"credentials": credentials},
                context={
                    "credential_references": references,
                    "credential_provider_id": provider_id,
                    "start_translation": True,
                },
            )
            self._set_status("Đang lưu danh sách API key trước khi bắt đầu dịch")
            return
        # A stopped translation owns an immutable ledger checkpoint, but that
        # checkpoint is valid only for the provider/model recorded in its job
        # spec. A changed selection must create a new matching job instead of
        # reviving stale Gemini Web/API state.
        if stopped_job_matches_selection:
            self.resumeTranslation(headed=headed, guest=guest)
            return
        self._begin_translation(provider_id, model_id, headed, guest, base_url=base_url.strip())

    @Slot(str, str, str, result=bool)
    @Slot(str, str, str, str, result=bool)
    def translationSelectionMatches(
        self, provider_id: str, model_id: str, base_url: str, reference: str = "",
    ) -> bool:
        provider_id = provider_id.strip().casefold()
        reference = reference or self.uiStringValue(
            "translation/selectedCredential/" + provider_id, ""
        )
        credentials_match = (not reference or not self._translation_job_credential_references
                             or self._translation_job_credential_references == [reference])
        return (self._translation_provider_id == provider_id
                and self._translation_model_id == model_id.strip()
                and (provider_id != "openai-compatible"
                     or self._translation_base_url.rstrip("/") == base_url.strip().rstrip("/"))
                and credentials_match
                and self._translation_job_style_preset == self.project_state.translationStylePreset
                and self._translation_job_custom_rules == self.project_state.translationCustomRules)

    def _bind_translation_configuration(self, config: object) -> None:
        config = config if isinstance(config, Mapping) else {}
        previous = (self._translation_base_url, self._translation_job_credential_references,
                    self._translation_job_style_preset, self._translation_job_custom_rules)
        self._translation_base_url = str(config.get("base_url", ""))
        self._translation_job_credential_references = list(
            config.get("credential_references") or []
        )
        self._translation_job_style_preset = str(config.get("translation_style_preset", "auto"))
        self._translation_job_custom_rules = str(config.get("translation_custom_rules", ""))
        if previous != (self._translation_base_url, self._translation_job_credential_references,
                        self._translation_job_style_preset, self._translation_job_custom_rules):
            self.translationBusyChanged.emit()

    def _translation_controls_own_preview(self) -> bool:
        return self._translation_busy and self._translation_job_state in {
            "queued", "start_requested", "running", "pause_requested", "paused",
            "resume_requested", "cancel_requested",
        }

    def _translation_preview_matches_job(self, job_id: str) -> bool:
        # Before create-job acknowledgement there is no current job ID. Reject
        # older replies rather than temporarily rebinding the new selection.
        if self._translation_cache_selection_explicit and self._selected_translation_cache_id:
            return job_id == self._selected_translation_cache_id
        if self._translation_controls_own_preview():
            return bool(self._translation_job_id) and job_id == self._translation_job_id
        return (not self._selected_translation_cache_id
                or job_id == self._selected_translation_cache_id)

    def _selected_translation_api_references(self, provider_id: str) -> list[str]:
        references = self._translation_api_references(provider_id)
        selected = self.uiStringValue("translation/selectedCredential/" + provider_id, "")
        return [selected] if selected in references else references

    def _complete_translation_switch(self, jobs: list[dict]) -> None:
        pending = self._translation_switch_pending
        if not pending:
            return
        if pending["project_id"] != self.project_state.projectId:
            self._translation_switch_pending = None
            return
        previous = next((item for item in jobs if item.get("id") == pending["job_id"]), None)
        if previous is None or previous.get("state") not in {
            "cancelled", "failed", "interrupted", "succeeded",
        }:
            return
        self._translation_switch_pending = None
        self._selected_translation_cache_id = ""
        self._set_translation_busy(False)
        self.startTranslation(*pending["arguments"])

    @Slot(str, str, bool, bool, str)
    @Slot(str, str, bool, bool, str, str)
    def toggleTranslation(
        self,
        provider_id: str,
        model_id: str,
        headed: bool = False,
        guest: bool = False,
        credential_input: str = "",
        base_url: str = "",
    ) -> None:
        """Dispatch one primary translation control from durable job state."""

        if self._translation_job_state == "running":
            self.pauseTranslation()
            return
        if self._translation_preferences_busy:
            self._set_status("Đang lưu văn phong mới; chưa tiếp tục tác vụ dịch")
            return
        if self._translation_job_state in {"paused", "queued"}:
            if (not credential_input.strip()
                    and self.translationSelectionMatches(provider_id, model_id, base_url)):
                self.resumeTranslation(headed=headed, guest=guest)
            else:
                self.startTranslation(
                    provider_id, model_id, headed, guest, credential_input, base_url,
                )
            return
        if self._translation_job_state in {
            "cancelled",
            "failed",
            "interrupted",
            "review",
        }:
            requested_provider_id = provider_id.strip().casefold()
            requested_model_id = model_id.strip()
            if (
                not credential_input.strip()
                and bool(requested_model_id)
                and self.translationSelectionMatches(requested_provider_id, requested_model_id,
                                                     base_url)
            ):
                self.resumeTranslation(headed=headed, guest=guest)
                return
            self.startTranslation(
                provider_id,
                model_id,
                headed,
                guest,
                credential_input,
                base_url,
            )
            return
        if self._translation_job_state in {
            "start_requested",
            "pause_requested",
            "resume_requested",
            "cancel_requested",
        }:
            self._set_status("Dịch đang chuyển trạng thái; hãy chờ xác nhận")
            return
        self.startTranslation(
            provider_id,
            model_id,
            headed,
            guest,
            credential_input,
            base_url,
        )

    @Slot(str, str, bool, bool, str, str, str, str)
    def toggleTranslationWithStyle(
        self, provider_id: str, model_id: str, headed: bool, guest: bool,
        credential_input: str, base_url: str, preset: str, custom_rules: str,
    ) -> None:
        """Persist a changed draft before continuing with the user's latest selection."""
        arguments = (provider_id, model_id, headed, guest, credential_input, base_url)
        if self._translation_job_state == "running":
            self.toggleTranslation(*arguments)
            return
        if self._translation_preferences_busy:
            self._set_status("Văn phong đang được lưu; hãy chờ xác nhận")
            return
        if (preset, custom_rules) == (self.project_state.translationStylePreset,
                                     self.project_state.translationCustomRules):
            self.toggleTranslation(*arguments)
            return
        self._translation_style_control_pending = {
            "project_id": self.project_state.projectId, "arguments": arguments,
        }
        self.setTranslationPreferences(preset, custom_rules)
        if not self._translation_preferences_busy:
            self._translation_style_control_pending = None

    def _begin_translation(
        self,
        provider_id: str,
        model_id: str,
        headed: bool,
        guest: bool,
        *,
        credential_references: list[str] | None = None,
        base_url: str = "",
    ) -> None:
        options: dict[str, object] = {
            "banned_latin_terms": [
                "Daozhang",
                "Renzong",
                "yamen",
            ],
            "qa_mode": "integrated",
        }
        if provider_id == "gemini-web":
            options.update(self._gemini_web_options(headed=headed, guest=guest))
        elif provider_id in {"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS}:
            references = credential_references or self._selected_translation_api_references(
                provider_id
            )
            if not references:
                self._set_translation_busy(False)
                self._set_status("Hãy thêm ít nhất một API key vào Windows Credential Manager")
                return
            options["credential_references"] = references
            if provider_id == "openai-compatible":
                options["base_url"] = base_url
        else:
            options["cli_executable"] = self.settings.value(
                "translation/geminiCliExecutable", "gemini", type=str
            )
        self.settings.setValue("translation/provider", provider_id)
        self.settings.setValue("translation/model", model_id)
        if provider_id == "openai-compatible" and base_url:
            self.settings.setValue("translation/openaiCompatibleBaseUrl", base_url)
        self._translation_provider_id = provider_id
        self._translation_model_id = model_id
        self._translation_base_url = base_url
        self._translation_job_credential_references = list(options.get("credential_references", []))
        self._translation_job_style_preset = self.project_state.translationStylePreset
        self._translation_job_custom_rules = self.project_state.translationCustomRules
        self._translation_job_id = ""
        self._selected_translation_cache_id = ""
        self._translation_cache_selection_explicit = False
        self.translationCachesChanged.emit()
        self.statusNotificationDismissRequested.emit()
        self._set_translation_job_state("start_requested")
        self._set_translation_busy(True)
        self._set_translation_qa(
            {
                "status": "PENDING",
                "completed_cues": 0,
                "checked_cues": 0,
                "cue_total": self.project_state.subtitle_cues.count,
                "issues": [],
            },
            reconcile_with_project=False,
        )
        self.runtime.request(
            "create_translation_job",
            {
                "project_id": self.project_state.projectId,
                "provider_id": provider_id,
                "model_id": model_id,
                "provider_options": options,
            },
        )
        self._set_status("Đang tạo tác vụ dịch có Film Bible, cache và QA tích hợp")

    @Slot()
    def pauseTranslation(self) -> None:
        if self._translation_job_id and self._translation_job_state == "running":
            self._set_translation_job_state("pause_requested")
            self.runtime.request(
                "pause_job",
                {"job_id": self._translation_job_id},
                context={"job_kind": "translate"},
            )

    @Slot()
    @Slot(bool, bool)
    def resumeTranslation(self, headed: bool = False, guest: bool = False) -> None:
        if not self._translation_job_id or self._translation_job_state in {
            "start_requested",
            "resume_requested",
        }:
            return
        if self._translation_job_state == "paused":
            self._set_translation_busy(True)
            self._set_translation_job_state("resume_requested")
            self.runtime.request(
                "resume_job",
                {"job_id": self._translation_job_id},
                context={"job_kind": "translate"},
            )
        elif self._translation_job_state == "queued":
            self._set_translation_busy(True)
            self._set_translation_job_state("start_requested")
            self._start_translation_worker(self._translation_job_id)
        elif self._translation_job_state in {
            "cancelled",
            "interrupted",
            "failed",
            "review",
        }:
            payload: dict[str, object] = {"job_id": self._translation_job_id}
            if self._translation_provider_id in {"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS}:
                references = self._selected_translation_api_references(
                    self._translation_provider_id
                )
                if not references:
                    self._set_status("Chưa có API key trong Windows Credential Manager")
                    return
                payload["credential_references"] = references
            elif self._translation_provider_id == "gemini-web":
                payload["provider_options"] = self._gemini_web_options(
                    headed=headed,
                    guest=guest,
                )
            self._set_translation_busy(True)
            self._set_translation_job_state("resume_requested")
            self.runtime.request(
                "retry_job",
                payload,
                context={
                    "resume_translation": True,
                    "job_id": self._translation_job_id,
                },
            )

    def _gemini_web_options(self, *, headed: bool, guest: bool) -> dict[str, object]:
        return {
            "profile_path": str(self.runtime.database_path.parent / "gemini-web-profile"),
            "headed": headed,
            "guest": guest,
            "temporary_chat": True,
            "web_session_retries": 1,
            "web_first_response_timeout_seconds": 30.0,
            "web_response_stall_timeout_seconds": 45.0,
            "web_max_requests_per_session": 5,
            "max_chunk_cues": 150,
            "max_chunk_source_chars": 8_000,
            "codex_fallback_enabled": False,
            "qa_mode": "integrated",
        }

    @Slot()
    def cancelTranslation(self) -> None:
        if self._translation_job_id and self._translation_busy:
            self._set_translation_job_state("cancel_requested")
            self.runtime.request(
                "cancel_job",
                {"job_id": self._translation_job_id},
                context={"job_kind": "translate"},
            )

    @Slot(str, str)
    def saveGeminiApiCredential(self, reference: str, secret: str) -> None:
        reference = reference.strip()
        if not reference or not secret:
            self._set_status("Nhập tên tham chiếu và API key trước khi lưu")
            return
        self.runtime.request(
            "store_credential",
            {"reference": reference, "secret": secret},
            context={"credential_reference": reference, "credential_provider_id": "gemini-api"},
        )
        self._set_status("Đang lưu API key vào Windows Credential Manager")

    @Slot(str)
    def saveGeminiApiCredentials(self, raw: str) -> None:
        self.saveTranslationApiCredentials("gemini-api", raw)

    @Slot(str, str, str)
    def saveTranslationApiCredential(self, provider_id: str, name: str, secret: str) -> None:
        provider_id, name, secret = provider_id.strip().casefold(), name.strip(), secret.strip()
        if provider_id not in {"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS}:
            self._set_status("Nguồn dịch API không được hỗ trợ")
            return
        if not name or len(name) > 120 or any(c in name for c in "\r\n\0") or not secret:
            self._set_status("Nhập tên key (tối đa 120 ký tự) và API key trước khi lưu")
            return
        self.runtime.request("store_credentials", {
            "credentials": [{"reference": name, "secret": secret}],
        }, context={"credential_provider_id": provider_id,
                    "credential_references": [name], "start_translation": False})
        self._set_status("Đang lưu API key vào Windows Credential Manager")

    @Property("QStringList", notify=translationModelCatalogChanged)
    def translationAvailableModels(self) -> list[str]:
        return self._translation_catalog_models

    @Property(bool, notify=translationModelCatalogChanged)
    def translationModelCatalogBusy(self) -> bool:
        return self._translation_catalog_busy

    @Property(str, notify=translationModelCatalogChanged)
    def translationModelCatalogStatus(self) -> str:
        return self._translation_catalog_status

    @Property(str, notify=translationModelCatalogChanged)
    def translationModelCatalogProvider(self) -> str:
        return self._translation_catalog_provider

    @Slot()
    def clearTranslationModelCatalog(self) -> None:
        self._translation_catalog_generation += 1
        self._translation_catalog_models = []
        self._translation_catalog_provider = ""
        self._translation_catalog_busy = False
        self._translation_catalog_status = "Gợi ý model. Có thể chọn hoặc nhập tên thủ công."
        self.translationModelCatalogChanged.emit()

    @Slot(str, str, str)
    def refreshTranslationModels(self, provider_id: str, reference: str, base_url: str) -> None:
        if not self._backend_ready or not reference.strip():
            self._translation_catalog_status = "Hãy lưu và chọn một API key trước khi tải model."
            self.translationModelCatalogChanged.emit()
            return
        self._translation_catalog_generation += 1
        self._translation_catalog_provider = provider_id.strip().casefold()
        self._translation_catalog_models = []
        self._translation_catalog_busy = True
        self._translation_catalog_status = "Đang tải model từ API…"
        self.translationModelCatalogChanged.emit()
        self.runtime.request("list_translation_models", {
            "provider_id": provider_id, "credential_reference": reference,
            "base_url": base_url,
        }, context={"catalog_generation": self._translation_catalog_generation,
                    "catalog_provider": self._translation_catalog_provider})

    @Slot(str, str)
    def saveTranslationApiCredentials(self, provider_id: str, raw: str) -> None:
        provider_id = provider_id.strip().casefold()
        if provider_id not in {"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS}:
            self._set_status("Nguồn dịch API không được hỗ trợ")
            return
        try:
            credentials = _parse_translation_api_credentials(
                raw,
                provider_id=provider_id,
                existing_references=tuple(self._translation_api_references(provider_id)),
            )
        except ValueError as exc:
            self._set_status(str(exc))
            return
        if not credentials:
            self._set_status("Hãy dán ít nhất một API key")
            return
        self.runtime.request(
            "store_credentials",
            {"credentials": credentials},
            context={
                "credential_references": [item["reference"] for item in credentials],
                "credential_provider_id": provider_id,
                "start_translation": False,
            },
        )
        self._set_status("Đang lưu danh sách API key vào Windows Credential Manager")

    @Slot(str)
    def deleteGeminiApiCredential(self, reference: str) -> None:
        self.deleteTranslationApiCredential("gemini-api", reference)

    @Slot(str, str)
    def deleteTranslationApiCredential(self, provider_id: str, reference: str) -> None:
        provider_id = provider_id.strip().casefold()
        reference = reference.strip()
        if provider_id not in {"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS} or not reference:
            return
        self.runtime.request(
            "delete_credential",
            {"reference": reference},
            context={"credential_reference": reference, "credential_provider_id": provider_id},
        )

    @Slot(str)
    def importTranslationPath(self, raw_path: str) -> None:
        if not self._backend_ready or not self.project_state.hasProject:
            self._set_status("Hãy mở dự án có SRT Trung trước khi nhập bản dịch")
            return
        if self._translation_busy:
            self._set_status("Hãy tạm dừng hoặc hoàn tất tác vụ dịch trước khi nhập SRT ngoài")
            return
        path = _local_path_from_ui(raw_path)
        if not path.is_file() or path.suffix.casefold() != ".srt":
            self._set_status("File bản dịch ngoài phải là SRT tồn tại trên máy")
            return
        if self._defer_until_subtitle_saves_flushed(
            "import-translation", lambda: self.importTranslationPath(raw_path),
            "Đang lưu dự án trước khi nhập bản dịch…", durable=True,
        ):
            return
        self.runtime.request(
            "import_translation_srt",
            {
                "project_id": self.project_state.projectId,
                "subtitle_path": str(path),
            },
        )
        self._set_status("Đang kiểm tra cue và timestamp của SRT Việt ngoài")

    @Slot(str, str)
    def exportSubtitleSrt(self, language: str, raw_path: str) -> None:
        if not self._backend_ready or not self.project_state.hasProject:
            self._set_status("Hãy mở dự án trước khi xuất SRT")
            return
        normalized_language = language.strip().casefold()
        if normalized_language not in {"source", "translated"}:
            self._set_status("Không xác định được loại phụ đề cần xuất")
            return
        if self._subtitle_export_busy:
            self._set_status("Đang xuất một file SRT; hãy chờ hoàn tất")
            return
        path = _local_path_from_ui(raw_path)
        if path.suffix.casefold() != ".srt":
            self._set_status("File xuất phụ đề phải có đuôi .srt")
            return
        if self._defer_until_subtitle_saves_flushed(
            "export-srt", lambda: self.exportSubtitleSrt(language, raw_path),
            "Đang lưu chỉnh sửa trước khi xuất SRT…", durable=True,
        ):
            return
        self._set_subtitle_export_busy(True)
        self.runtime.request(
            "export_subtitle_srt",
            {
                "project_id": self.project_state.projectId,
                "language": normalized_language,
                "output_path": str(path),
            },
            context={"language": normalized_language},
        )
        self._set_status("Đang xuất SRT từ nội dung mới nhất của dự án")

    @Slot(str)
    def selectVoice(self, voice_id: str) -> None:
        entry = self.voice_catalog.find(voice_id.strip())
        if not entry or entry.get("installed") is not True:
            self._set_status("Giọng được chọn chưa sẵn sàng trên máy")
            return
        if voice_id == self._selected_voice_id:
            return
        self._selected_voice_id = voice_id
        self.settings.setValue("voice/selectedId", voice_id)
        self.voiceSettingsChanged.emit()
        self._set_status(f"Đã chọn {entry.get('voiceName', entry.get('name', voice_id))}")

    @Slot(str)
    def previewCatalogVoice(self, voice_id: str) -> None:
        if not self._features_available():
            return
        entry = self.voice_catalog.find(voice_id.strip())
        path = Path(str(entry.get("preview_source", ""))).expanduser()
        if not entry or entry.get("preview_available") is not True or not path.is_file():
            self._set_status("Giọng này chưa có mẫu nghe hợp lệ")
            return
        if self._voice_catalog_preview_id == voice_id:
            self.stopVoiceCatalogPreview()
            return
        self._voice_catalog_preview_id = voice_id
        self._voice_catalog_preview_source = QUrl.fromLocalFile(str(path.resolve())).toString()
        self.voiceCatalogPlaybackChanged.emit()
        self._set_status(f"Đang nghe thử {entry.get('name', voice_id)}")

    @Slot()
    def stopVoiceCatalogPreview(self) -> None:
        if not self._voice_catalog_preview_id and not self._voice_catalog_preview_source:
            return
        self._voice_catalog_preview_id = ""
        self._voice_catalog_preview_source = ""
        self.voiceCatalogPlaybackChanged.emit()

    def _replace_dialogue_casting(self, raw: object) -> None:
        payload = dict(raw) if isinstance(raw, dict) else {}
        rows = payload.get("rows", [])
        prepared_rows = []
        if isinstance(rows, list):
            for raw_row in rows:
                if not isinstance(raw_row, dict):
                    continue
                row = dict(raw_row)
                parts = row.get("parts", [])
                if isinstance(parts, list):
                    prepared_parts = []
                    for raw_part in parts:
                        if not isinstance(raw_part, dict):
                            continue
                        part = dict(raw_part)
                        media_path = str(part.get("media_path", "")).strip()
                        if media_path:
                            part["source_url"] = QUrl.fromLocalFile(
                                str(Path(media_path).expanduser().resolve())
                            ).toString()
                        prepared_parts.append(part)
                    row["parts"] = prepared_parts
                prepared_rows.append(row)
        payload["rows"] = prepared_rows
        payload.setdefault("config", {
            "enabled": False,
            "male_voice": None,
            "female_voice": None,
            "fallback_voice": None,
        })
        payload["experimental"] = True
        self._dialogue_casting = payload
        self._dialogue_casting_loading = False
        self.dialogueCastingChanged.emit()

    def _set_dialogue_casting_job_state(self, value: str) -> None:
        state = str(value or "")
        if state == self._dialogue_casting_job_state:
            return
        self._dialogue_casting_job_state = state
        self.dialogueCastingChanged.emit()

    def _current_dialogue_casting_config(self) -> dict[str, object]:
        config = self._dialogue_casting.get("config", {})
        if isinstance(config, dict):
            return {
                key: deepcopy(config.get(key))
                for key in ("enabled", "male_voice", "female_voice", "fallback_voice")
            }
        return {
            "enabled": False,
            "male_voice": None,
            "female_voice": None,
            "fallback_voice": None,
        }

    def _dialogue_casting_job_context(self, job_id: str) -> dict[str, object]:
        return {
            "job_kind": "dialogue_casting",
            "project_id": self.project_state.projectId,
            "job_id": job_id,
        }

    def _dialogue_casting_callback_is_current(self, context: object) -> bool:
        if not isinstance(context, dict):
            return False
        project_id = str(context.get("project_id", ""))
        job_id = str(context.get("job_id", ""))
        return bool(
            project_id
            and job_id
            and project_id == self.project_state.projectId
            and job_id == self._dialogue_casting_job_id
        )

    def _dialogue_casting_voice(self, voice_id: str) -> dict[str, object] | None:
        entry = self.voice_catalog.find(voice_id.strip())
        if not entry or entry.get("installed") is not True:
            return None
        provider_id = str(entry.get("provider_id", ""))
        voice = {"provider_id": provider_id, "voice_id": str(entry.get("voice_id", ""))}
        if provider_id != "vieneu-3.8" or not entry.get("is_clone"):
            return voice
        profile_path = str(entry.get("profile_path", "")).strip()
        if profile_path and Path(profile_path).is_file():
            voice["profile_path"] = profile_path
            return voice
        reference_path = self.vieneuReferenceAudio
        if reference_path:
            voice.update({
                "reference_audio_path": reference_path,
                "reference_text": self.vieneuReferenceText,
                "reference_denoise": self.vieneuReferenceDenoise,
            })
            return voice
        self._set_status("Giọng clone chưa có profile; hãy chọn file mẫu hoặc tạo profile trước")
        return None

    def _save_dialogue_casting_config(
        self, config: dict[str, object], *, preview_role: str = "",
    ) -> None:
        project_id = self.project_state.projectId
        if not project_id or not self._backend_ready:
            self._set_status("Hãy mở project và chờ Job Manager sẵn sàng trước khi lưu phân vai")
            return
        if self._dialogue_casting_saving:
            return
        self._dialogue_casting_saving = True
        self.dialogueCastingChanged.emit()
        context: dict[str, object] = {
            "project_id": project_id, "dialogue_casting_config": True,
        }
        if preview_role:
            context.update({
                "import_preview_role": preview_role,
                "import_preview_serial": self._casting_preview_request_serial,
            })
        self.runtime.request(
            "configure_dialogue_casting",
            {"project_id": project_id, "config": config},
            context=context,
        )

    @Slot()
    def refreshDialogueCasting(self) -> None:
        project_id = self.project_state.projectId
        if not project_id or not self._backend_ready or not self.runtime.ready:
            return
        self._dialogue_casting_loading = True
        self.dialogueCastingChanged.emit()
        self.runtime.request(
            "get_dialogue_casting",
            {"project_id": project_id},
            context={"project_id": project_id},
        )

    @Slot(bool)
    def setDialogueCastingEnabled(self, enabled: bool) -> None:
        if not enabled:
            self._pending_casting_after_ocr = None
        config = self._current_dialogue_casting_config()
        config["enabled"] = bool(enabled)
        if enabled and config.get("fallback_voice") is None:
            config["fallback_voice"] = self._dialogue_casting_voice(self._selected_voice_id)
        self._save_dialogue_casting_config(config)

    @Slot(str, str)
    def setDialogueCastingVoice(self, role: str, voice_id: str) -> None:
        key = role.strip()
        if key not in {"male", "female", "fallback"}:
            return
        config = self._current_dialogue_casting_config()
        voice = self._dialogue_casting_voice(voice_id) if voice_id.strip() else None
        if voice_id.strip() and voice is None:
            return
        self._release_voice_preview()
        config[f"{key}_voice"] = voice
        self._save_dialogue_casting_config(config)

    @Slot(str, str)
    def setDialogueCastingReferenceAudio(self, role: str, value: str) -> None:
        if role not in {"male", "female", "fallback"} or self._dialogue_casting_saving:
            return
        if not self.project_state.hasProject or not self._backend_ready:
            self._set_status("Hãy mở project trước khi chọn file giọng")
            return
        if self._tts_busy or self.dialogueCastingBusy:
            self._set_status("Hãy chờ tác vụ giọng hiện tại xong trước khi đổi file mẫu")
            return
        raw = value.strip()
        raw = QUrl(raw).toLocalFile() if raw.casefold().startswith("file:") else raw
        if not raw:
            return
        self._release_voice_preview()
        path = Path(raw).expanduser().resolve()
        if (not path.is_file() or path.suffix.casefold() not in {
                ".wav", ".mp3", ".m4a", ".flac", ".ogg"} or path.stat().st_size == 0):
            self._set_status("Hãy chọn file giọng WAV, MP3, M4A, FLAC hoặc OGG có dữ liệu")
            return
        clone = next((self.voice_catalog.get(row) for row in range(self.voice_catalog.count)
                      if self.voice_catalog.get(row).get("is_clone") is True
                      and self.voice_catalog.get(row).get("installed") is True), None)
        if clone is None:
            self._set_status("Cần cài runtime VieNeu để dùng file giọng riêng cho phân vai")
            return
        config = self._current_dialogue_casting_config()
        config[role + "_voice"] = {
            "provider_id": "vieneu-3.8", "voice_id": str(clone["voice_id"]),
            "reference_audio_path": str(path), "reference_text": "", "reference_denoise": False,
        }
        self._save_dialogue_casting_config(config, preview_role=role)

    @Slot("QVariantList", str)
    def setDialogueCastingLabels(self, cue_ids: list, label: str) -> None:
        project_id = self.project_state.projectId
        normalized = list(
            dict.fromkeys(str(value).strip() for value in cue_ids if str(value).strip())
        )
        if not project_id or not normalized or label not in {"male", "female", "unknown", "auto"}:
            return
        if self._dialogue_casting_saving:
            return
        self._dialogue_casting_saving = True
        self.dialogueCastingChanged.emit()
        self.runtime.request(
            "set_dialogue_casting_labels",
            {"project_id": project_id, "cue_ids": normalized, "label": label},
            context={"project_id": project_id, "dialogue_casting_labels": True},
        )

    @Slot()
    def analyzeDialogueCasting(self) -> None:
        project_id = self.project_state.projectId
        if not project_id or not self._backend_ready:
            self._set_status("Hãy mở project có video trước khi nhận diện thoại")
            return
        if self.dialogueCastingBusy or self._tts_busy:
            self._set_status("Chờ tác vụ phân vai hoặc tạo giọng hiện tại kết thúc trước")
            return
        self._dialogue_casting_job_id = ""
        self._dialogue_casting_auto_tts = False
        self._dialogue_casting_auto_tts_job_id = ""
        self._set_dialogue_casting_job_state("start_requested")
        self.runtime.request(
            "create_dialogue_casting_job",
            {"project_id": project_id},
            context={"project_id": project_id, "automatic_tts": False},
        )
        self._set_status("Đang chuẩn bị nhận diện giọng thoại từ âm thanh gốc")

    def _start_casting_after_ocr(self, snapshot: dict, context: dict) -> None:
        job_id = str(context.get("ocr_job_id", ""))
        project_id = str(context.get("project_id", ""))
        state = snapshot.get("dialogue_casting")
        if (context.get("ocr_succeeded") is not True
                or context.get("ocr_live_completion") is False or not job_id
                or not project_id or project_id != self.project_state.projectId
                or job_id in self._casting_after_ocr_jobs
                or not isinstance(state, dict) or state.get("enabled") is not True
                or not self._backend_ready
                or self._multi_batch.get("state") in {"running", "pause_requested", "paused"}
                or not any(cue.get("role", "dialogue") == "dialogue"
                           for cue in snapshot.get("source_cues", []))):
            return
        if self._tts_busy or self.dialogueCastingBusy:
            self._pending_casting_after_ocr = {
                "project_id": project_id,
                "ocr_job_id": job_id,
            }
            return
        self._casting_after_ocr_jobs.add(job_id)
        self.analyzeDialogueCasting()

    def _resume_pending_casting_after_ocr(self) -> None:
        pending = self._pending_casting_after_ocr
        if not isinstance(pending, dict):
            return
        if self._tts_busy or self.dialogueCastingBusy:
            return
        self._pending_casting_after_ocr = None
        project_id = str(pending.get("project_id", ""))
        job_id = str(pending.get("ocr_job_id", ""))
        current_snapshot = self.project_state._canonical_snapshot
        if (not project_id or project_id != self.project_state.projectId or not job_id
                or job_id in self._casting_after_ocr_jobs or not self._backend_ready
                or self._current_dialogue_casting_config().get("enabled") is not True
                or self.dialogueCastingBusy
                or self._multi_batch.get("state") in {"running", "pause_requested", "paused"}
                or not any(cue.get("role", "dialogue") == "dialogue"
                           for cue in current_snapshot.get("source_cues", []))):
            return
        self._casting_after_ocr_jobs.add(job_id)
        self.analyzeDialogueCasting()

    def _finish_dialogue_casting_job(self, job_id: str, state: str) -> None:
        if not job_id:
            return
        marker = (job_id, state)
        if marker in self._dialogue_casting_terminal_markers:
            return
        self._dialogue_casting_terminal_markers.add(marker)
        self._set_dialogue_casting_job_state(state)
        if state == "succeeded":
            self.refreshDialogueCasting()
            if self._dialogue_casting_auto_tts_job_id == job_id:
                self._dialogue_casting_auto_tts_job_id = ""
                self._continue_pending_tts_start()
            self._resume_pending_casting_after_ocr()
            return
        if state not in {"failed", "cancelled", "interrupted"}:
            return
        if self._dialogue_casting_auto_tts_job_id != job_id:
            self._resume_pending_casting_after_ocr()
            return
        self._dialogue_casting_auto_tts = False
        self._dialogue_casting_auto_tts_job_id = ""
        self._pending_tts_start = None
        self._set_tts_job_state("failed" if state == "failed" else state)
        self._set_voice_playback_enabled(True)
        if state == "failed":
            self._set_status(
                "Phân vai thoại gặp lỗi; D1 cũ được giữ nguyên. Kiểm tra runtime và thử lại."
            )
        elif state == "cancelled":
            self._set_status("Đã hủy phân vai thoại; chưa tạo hoặc thay D1.")
        else:
            self._set_status("Phân vai thoại bị gián đoạn; D1 cũ được giữ nguyên.")
        self._set_tts_busy(False)
        self._resume_pending_casting_after_ocr()

    @Slot(str)
    def previewDialogueCastingVoice(self, role: str) -> None:
        normalized_role = role.strip()
        if normalized_role not in {"male", "female", "fallback"}:
            return
        if self._tts_busy or self.dialogueCastingBusy or self._dialogue_casting_saving:
            self._set_status("Chờ tác vụ giọng hiện tại kết thúc trước khi nghe thử")
            return
        config = self._current_dialogue_casting_config()
        voice = config.get(f"{normalized_role}_voice")
        if not isinstance(voice, dict):
            self._set_status("Hãy chọn giọng cho vai này trước khi nghe thử")
            return
        options: dict[str, object] = {
            "purpose": "preview",
            "preview_text": "Xin chào, đây là mẫu nghe thử giọng trong DUCAI Studio.",
            "batch_size": 1,
        }
        if (voice.get("provider_id") == "vieneu-3.8"
                and not str(voice.get("voice_id", "")).startswith("preset:")):
            for key in (
                "profile_path",
                "reference_audio_path",
                "reference_text",
                "reference_denoise",
            ):
                if voice.get(key):
                    options[key] = voice[key]
        self._release_voice_preview()
        request_serial = self._casting_preview_request_serial
        context = {
            "tts_purpose": "preview",
            "casting_preview_lookup": True,
            "casting_preview_role": normalized_role,
            "casting_preview_serial": request_serial,
            "project_id": self.project_state.projectId,
            "voice": dict(voice),
            "provider_options": dict(options),
        }
        self.runtime.request(
            "get_tts_voice_preview",
            {
                "provider_id": voice["provider_id"],
                "voice_id": voice["voice_id"],
                "provider_options": options,
            },
            context=context,
        )
        self._set_status("Đang mở mẫu nghe thử đã chuẩn bị")

    def _casting_preview_lookup_is_current(self, context: object) -> bool:
        return bool(
            isinstance(context, dict)
            and context.get("casting_preview_lookup") is True
            and context.get("casting_preview_serial") == self._casting_preview_request_serial
            and str(context.get("project_id", "")) == self.project_state.projectId
        )

    def _start_casting_preview_job(self, context: dict[str, object]) -> None:
        if not self._casting_preview_lookup_is_current(context):
            return
        voice = context.get("voice")
        options = context.get("provider_options")
        if not isinstance(voice, dict) or not isinstance(options, dict):
            return
        if not self.project_state.hasProject:
            self._set_status("Mẫu này chưa được chuẩn bị; hãy mở project để tạo mẫu nghe thử")
            return
        self._casting_preview_role = str(context.get("casting_preview_role", ""))
        self._set_tts_job_purpose("preview")
        self._set_tts_job_state("start_requested")
        self._set_tts_busy(True)
        self.runtime.request(
            "create_tts_job",
            {
                "project_id": self.project_state.projectId,
                "provider_id": voice["provider_id"],
                "voice_id": voice["voice_id"],
                "provider_options": dict(options),
            },
            context={
                "tts_purpose": "preview",
                "casting_preview_role": context.get("casting_preview_role", ""),
                "casting_preview_serial": context["casting_preview_serial"],
                "project_id": self.project_state.projectId,
            },
        )
        self._set_status("Đang tạo mẫu nghe thử cho giọng phân vai")

    def _release_voice_preview(self) -> None:
        self._casting_preview_request_serial += 1
        if self._casting_preview_role:
            self._casting_preview_role = ""
            self.ttsBusyChanged.emit()
        self._voice_preview_target_job_id = ""
        self.stopVoiceCatalogPreview()
        if self._voice_preview_source:
            self._voice_preview_source = ""
            self.voicePreviewChanged.emit()

    @Slot(str)
    def installVoice(self, voice_id: str) -> None:
        if not self._features_available():
            return
        normalized = voice_id.strip()
        entry = self.voice_catalog.find(normalized)
        if not entry:
            self._set_status("Không tìm thấy giọng cần cài")
            return
        if entry.get("installed") is True:
            self._set_status("Giọng này đã được cài")
            return
        if entry.get("installable") is not True:
            self._set_status("Giọng này chưa có nguồn tải được xác minh")
            return
        if normalized in self._voice_installing:
            self._set_status("Giọng này đang được tải")
            return
        self._voice_installing.add(normalized)
        self.voice_catalog.set_install_state(normalized, "downloading", 0)
        task = _VoiceInstallTask(normalized, self._voice_installer, self.voiceInstallEvent.emit)
        QThreadPool.globalInstance().start(task)
        self._set_status(f"Đang tải {entry.get('name', normalized)}")

    @Slot(str, str, int, str)
    def _handle_voice_install_event(
        self,
        voice_id: str,
        state: str,
        progress: int,
        error: str,
    ) -> None:
        if state == "downloading":
            self.voice_catalog.set_install_state(voice_id, state, progress)
            return
        self._voice_installing.discard(voice_id)
        if state == "installed":
            self.voice_catalog.set_install_state(voice_id, state, 100)
            self.voice_catalog.refresh()
            entry = self.voice_catalog.find(voice_id)
            self._set_status(f"Đã cài {entry.get('name', voice_id)}")
            return
        message = error.strip() or "Không thể cài giọng"
        self.voice_catalog.set_install_state(voice_id, "error", 0, message)
        self._set_status(f"Lỗi cài giọng: {message}")

    @Slot()
    def refreshVoiceCatalog(self) -> None:
        self.voice_catalog.refresh()
        if not self.voice_catalog.find(self._selected_voice_id).get("installed"):
            fallback = self.voice_catalog.find("preset:Minh Quân Pro")
            if not fallback.get("installed"):
                fallback = self.voice_catalog.first_installed("piper")
            self._selected_voice_id = str(fallback.get("voice_id", ""))
        self.voiceSettingsChanged.emit()

    @Slot(str)
    def setVieNeuReferenceAudio(self, value: str) -> None:
        raw = value.strip()
        if raw.casefold().startswith("file:"):
            raw = QUrl(raw).toLocalFile()
        if raw:
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                self._set_status("File mẫu VieNeu không tồn tại")
                return
            raw = str(path)
        self.settings.setValue("voice/vieneuReferenceAudio", raw)
        self.voiceSettingsChanged.emit()

    @Slot(str)
    def setVieNeuReferenceText(self, value: str) -> None:
        self.settings.setValue("voice/vieneuReferenceText", value.strip())
        self.voiceSettingsChanged.emit()

    @Slot(bool)
    def setVieNeuReferenceDenoise(self, value: bool) -> None:
        self.settings.setValue("voice/vieneuReferenceDenoise", bool(value))
        self.voiceSettingsChanged.emit()

    @Slot(bool)
    def setVoiceAdaptiveTimingEnabled(self, value: bool) -> None:
        del value
        self.settings.setValue("voice/adaptiveTimingEnabled", True)
        self.voiceSettingsChanged.emit()
        self._set_status(
            f"Khớp cue tự động luôn bật; cue dài tăng đến tổng {self._voice_cue_max_speed:.1f}×"
        )

    @Slot(float)
    def setVoiceReadSpeed(self, value: float) -> None:
        if not math.isfinite(value):
            return
        speed = round(max(1.1, min(3.0, float(value))), 1)
        changed = abs(speed - self._voice_read_speed) > 1e-9
        cap_changed = self._voice_cue_max_speed < speed
        if not changed and not cap_changed:
            return
        self._voice_read_speed = speed
        self.settings.setValue("voice/readSpeed", speed)
        if cap_changed:
            self._voice_cue_max_speed = speed
            self.settings.setValue("voice/cueMaxTotalSpeed", speed)
        self.voiceSettingsChanged.emit()
        self._set_status(f"Tốc độ giọng gốc: {speed:.1f}×; VieNeu vẫn tổng hợp ở 1,00×")

    @Slot(float)
    def setVoiceCueMaxSpeed(self, value: float) -> None:
        if not math.isfinite(value):
            return
        speed = round(
            max(self._voice_read_speed, min(3.0, float(value))),
            1,
        )
        if abs(speed - self._voice_cue_max_speed) < 1e-9:
            return
        self._voice_cue_max_speed = speed
        self.settings.setValue("voice/cueMaxTotalSpeed", speed)
        self.voiceSettingsChanged.emit()
        self._set_status(f"Giới hạn tổng cho cue dài: {speed:.1f}×")

    @Slot(bool)
    def setVoiceAutoCondenseLongCuesEnabled(self, value: bool) -> None:
        del value
        self.settings.setValue("voice/autoCondenseLongCuesEnabled", True)
        self.voiceSettingsChanged.emit()
        self._set_status("Tự rút gọn cue voice quá dài luôn được bật")
        if self._voice_timing_qa.get("repairable") is True:
            self.repairVoiceTimingCues()

    @Slot()
    def repairVoiceTimingCues(self) -> None:
        job_id = str(self._voice_timing_qa.get("job_id", ""))
        if not job_id or self._voice_timing_qa.get("repairable") is not True:
            self._set_status("Không có cue dài có thể tự rút gọn lúc này")
            return
        self._request_voice_timing_repair(job_id)

    @Slot()
    def addCurrentVoiceToTimeline(self) -> None:
        job_id = str(self._voice_timing_qa.get("job_id", ""))
        if not job_id or not self.project_state.projectId:
            self._set_status("Không có voice lỗi timing để thêm vào Timeline")
            return
        if self._translation_busy:
            self._set_status(
                "Chưa tạo override job: tác vụ tự rút gọn đang chạy; "
                "hãy chờ tác vụ dịch/rút gọn kết thúc rồi thêm nguyên voice"
            )
            return
        if job_id in self._voice_timing_override_requests:
            self._set_status("Voice hiện tại đang được ghép vào Timeline")
            return
        self._voice_timing_override_requests.add(job_id)
        self._set_tts_job_state("start_requested")
        self._set_tts_busy(True)
        self.runtime.request(
            "create_tts_timing_override_job",
            {
                "project_id": self.project_state.projectId,
                "parent_tts_job_id": job_id,
            },
            context={"voice_timing_override": True, "parent_tts_job_id": job_id},
        )
        self._set_status("Đang ghép nguyên voice vào Timeline; không cắt câu, có thể dài hơn video")

    @Slot(str)
    def setVoiceConformMode(self, value: str) -> None:
        mode = value.strip().casefold()
        if mode not in {"stretch_video", "speed_audio", "hybrid"}:
            self._set_status("Chế độ đồng bộ voice không hợp lệ")
            return
        if self._voice_conform_busy:
            self._set_status("Đang xử lý đồng bộ voice; hãy chờ tác vụ hiện tại")
            return
        if mode == self._voice_conform_mode:
            return
        self._voice_conform_mode = mode
        self.settings.setValue("voice/conformMode", mode)
        self._voice_conform_applied = False
        self._voice_conform_estimate_generation += 1
        self._voice_conform_estimate_refresh_queued = True
        self._voice_conform_estimate = {
            "status": "PENDING",
            "project_id": self.project_state.projectId,
            "parent_tts_job_id": self._voice_conform_parent_tts_job_id,
            "mode": mode,
            "max_audio_speed": self._voice_conform_max_audio_speed,
            "unresolved_count": 0,
            "issues": [],
        }
        self.voiceConformChanged.emit()
        self.refreshVoiceConformEstimate()

    @Slot(float)
    def setVoiceConformMaxAudioSpeed(self, value: float) -> None:
        if not math.isfinite(value):
            return
        speed = round(max(1.0, min(2.0, float(value))), 2)
        # Keep the documented 0.05x increments even when called from a
        # non-QML client.
        speed = round(round((speed - 1.0) / 0.05) * 0.05 + 1.0, 2)
        if self._voice_conform_busy or abs(speed - self._voice_conform_max_audio_speed) < 1e-9:
            return
        self._voice_conform_max_audio_speed = speed
        self.settings.setValue("voice/conformMaxAudioSpeed", speed)
        self._voice_conform_applied = False
        self._voice_conform_estimate_generation += 1
        self._voice_conform_estimate_refresh_queued = True
        self._voice_conform_estimate = {
            "status": "PENDING",
            "project_id": self.project_state.projectId,
            "parent_tts_job_id": self._voice_conform_parent_tts_job_id,
            "mode": self._voice_conform_mode,
            "max_audio_speed": speed,
            "unresolved_count": 0,
            "issues": [],
        }
        self.voiceConformChanged.emit()
        self.refreshVoiceConformEstimate()

    @Slot()
    def refreshVoiceConformEstimate(self) -> None:
        if not self._backend_ready or not self.project_state.hasProject:
            return
        if self._voice_conform_busy or self._voice_conform_estimate_request_pending:
            self._voice_conform_estimate_refresh_queued = True
            return
        generation = self._voice_conform_estimate_generation
        self._voice_conform_estimate_refresh_queued = False
        self._voice_conform_estimate_request_pending = True
        self.runtime.request(
            "estimate_voice_conform",
            {
                "project_id": self.project_state.projectId,
                "parent_tts_job_id": self._voice_conform_parent_tts_job_id,
                "mode": self._voice_conform_mode,
                "max_audio_speed": self._voice_conform_max_audio_speed,
                "min_video_speed": 0.5,
                "allow_duration_growth": True,
            },
            context={
                "voice_conform_estimate": True,
                "generation": generation,
                "project_id": self.project_state.projectId,
                "mode": self._voice_conform_mode,
                "max_audio_speed": self._voice_conform_max_audio_speed,
            },
        )

    @Slot()
    def applyVoiceConform(self) -> None:
        if self._voice_conform_busy:
            return
        if not self.voiceConformCanApply:
            self._set_status("Đồng bộ chưa đạt QA hoặc chưa có D1 hoàn chỉnh")
            return
        parent_id = str(self._voice_conform_estimate.get("parent_tts_job_id", ""))
        if not parent_id:
            self._set_status("Không xác định được tác vụ TTS nền để đồng bộ")
            return
        self._voice_conform_job_state = "start_requested"
        self._voice_conform_busy = True
        self._voice_conform_applied = False
        self.voiceConformChanged.emit()
        self.runtime.request(
            "create_voice_conform_job",
            {
                "project_id": self.project_state.projectId,
                "parent_tts_job_id": parent_id,
                "mode": self._voice_conform_mode,
                "max_audio_speed": self._voice_conform_max_audio_speed,
                "min_video_speed": 0.5,
                "allow_duration_growth": True,
            },
            context={"voice_conform_create": True},
        )
        self._set_status("Đang tạo tác vụ đồng bộ từ cache WAV; VieNeu không được gọi")

    @Slot()
    def resetVoiceConform(self) -> None:
        if self._voice_conform_busy:
            self._set_status("Đang xử lý đồng bộ voice; chưa thể đặt lại")
            return
        self._voice_conform_busy = True
        self._voice_conform_job_state = "reset_requested"
        self.voiceConformChanged.emit()
        self.runtime.request(
            "reset_voice_conform",
            {"project_id": self.project_state.projectId},
            context={"voice_conform_reset": True},
        )
        self._set_status("Đang khôi phục D1 nền và xóa bản đồ đồng bộ")

    @Slot(int)
    def selectVoiceConformIssue(self, cue_index: int) -> None:
        if cue_index <= 0:
            return
        model = self.project_state.subtitle_cues
        target_row = -1
        for row in range(model.count):
            cue = model.get(row)
            if cue.get("index") == cue_index:
                cue_id = str(cue.get("cue_id", cue.get("id", "")))
                if cue_id:
                    self.selectCue(cue_id, float(cue.get("start_us", 0)))
                    target_row = row
                break
        if target_row < 0:
            self._set_status(f"Không tìm thấy cue {cue_index} trong phụ đề hiện tại")
            return
        self.navigate("translate")
        # The editor's ListView is zero-based.  Emitting the cue number here
        # used to scroll one row too far (and could land on a different cue
        # when the issue was near the end of a long subtitle list).
        self.translationQaNavigationRequested.emit(target_row)
        self._set_status(f"Đã chọn cue {cue_index} cần xử lý đồng bộ")

    @Slot(str, float, int)
    def openCueInEditor(
        self,
        cue_id: str,
        source_start_us: float = 0,
        cue_index: int = 0,
    ) -> None:
        """Open a Timeline voice cue in the subtitle editor.

        Timeline voice rows carry the original SRT timestamp, while the
        playhead may be on a conformed output timeline.  Resolve the cue from
        the subtitle model first so both the selected row and the seek use the
        canonical source timestamp; the model then maps it to output time.
        """

        requested_id = str(cue_id or "").strip()
        if self.project_state.title_cues.find(requested_id):
            self.selectTimelineCue(requested_id, False)
            self.navigate("timeline")
            self._set_status("Chỉnh tên phim/kênh trong tab Text của Timeline")
            return
        try:
            requested_index = int(cue_index)
        except (TypeError, ValueError):
            requested_index = 0
        model = self.project_state.subtitle_cues
        target_row = -1
        target: dict[str, object] = {}
        for row in range(model.count):
            cue = model.get(row)
            if requested_id and str(cue.get("cue_id", "")) == requested_id:
                target_row = row
                target = cue
                break
            if requested_index > 0 and cue.get("index") == requested_index:
                target_row = row
                target = cue
                break
        if target_row < 0:
            self._set_status("Không tìm thấy cue voice trong phụ đề hiện tại")
            return

        canonical_id = str(target.get("cue_id", requested_id))
        canonical_start = target.get("start_us", source_start_us)
        try:
            canonical_start_us = float(canonical_start)
        except (TypeError, ValueError):
            canonical_start_us = float(source_start_us or 0)
        self.selectCue(canonical_id, canonical_start_us)
        self.navigate("translate")
        self.translationQaNavigationRequested.emit(target_row)
        shown_index = int(target.get("index", requested_index or target_row + 1))
        self._set_status(f"Đã mở cue {shown_index} để chỉnh trong Biên tập dịch thuật")

    @Slot(str, float, int)
    def openSubtitleCueInEditor(
        self,
        cue_id: str,
        source_start_us: float = 0,
        cue_index: int = 0,
    ) -> None:
        """Open an S1/S2/S3 cue in the subtitle editing workspace."""

        requested_id = str(cue_id or "").strip()
        if self.project_state.title_cues.find(requested_id):
            self.selectTimelineCue(requested_id, False)
            self.navigate("timeline")
            self._set_status("Chỉnh tên phim/kênh trong tab Text của Timeline")
            return
        try:
            requested_index = int(cue_index)
        except (TypeError, ValueError):
            requested_index = 0
        model = self.project_state.subtitle_cues
        target_row = -1
        target: dict[str, object] = {}
        for row in range(model.count):
            cue = model.get(row)
            if requested_id and str(cue.get("cue_id", "")) == requested_id:
                target_row = row
                target = cue
                break
            if requested_index > 0 and cue.get("index") == requested_index:
                target_row = row
                target = cue
                break
        if target_row < 0:
            self._set_status("Không tìm thấy dòng chữ tương ứng trong Biên tập phụ đề")
            return
        canonical_id = str(target.get("cue_id", requested_id))
        canonical_start = target.get("start_us", source_start_us)
        try:
            canonical_start_us = float(canonical_start)
        except (TypeError, ValueError):
            canonical_start_us = float(source_start_us or 0)
        self.selectCue(canonical_id, canonical_start_us)
        self.navigate("subtitles")
        self.subtitleNavigationRequested.emit(canonical_id)
        shown_index = int(target.get("index", requested_index or target_row + 1))
        self._set_status(f"Đã mở cue {shown_index} trong Biên tập phụ đề")

    @Slot()
    def startVoicePreview(self) -> None:
        if not self._features_available():
            return
        if self._defer_until_subtitle_saves_flushed(
            "voice-preview",
            self.startVoicePreview,
            "Đang lưu phụ đề trước khi tạo giọng nghe thử…",
            durable=True,
        ):
            return
        self.stopVoiceCatalogPreview()
        self._start_voice_job("preview")

    @Slot()
    def startVoiceGeneration(self) -> None:
        if not self._features_available():
            return
        if self._defer_until_subtitle_saves_flushed(
            "voice-production",
            self.startVoiceGeneration,
            "Đang lưu phụ đề trước khi tạo D1…",
            durable=True,
        ):
            return
        self._start_voice_job("production")

    @Slot()
    def regenerateSelectedVoiceCue(self) -> None:
        if not self._selected_cue_id:
            self._set_status("Hãy chọn cue trong Biên tập phụ đề trước")
            return
        if self._defer_until_subtitle_saves_flushed(
            "voice-selected",
            self.regenerateSelectedVoiceCue,
            "Đang lưu cue đã sửa trước khi tạo lại giọng…",
            durable=True,
        ):
            return
        self._start_voice_job("production", force_cue_ids=[self._selected_cue_id])

    @Slot()
    def pauseVoiceGeneration(self) -> None:
        if self._tts_job_id and self._tts_job_state == "running":
            self._set_tts_job_state("pause_requested")
            self.runtime.request(
                "pause_job",
                {"job_id": self._tts_job_id},
                context={"job_kind": "tts"},
            )

    @Slot()
    def resumeVoiceGeneration(self) -> None:
        if not self._tts_job_id:
            return
        if self._tts_job_state == "paused":
            self._set_tts_job_state("resume_requested")
            self.runtime.request(
                "resume_job",
                {"job_id": self._tts_job_id},
                context={"job_kind": "tts"},
            )
        elif self._tts_job_state == "queued":
            self._set_tts_job_state("start_requested")
            self._start_tts_worker(self._tts_job_id)
        elif self._tts_job_state in {"interrupted", "failed", "review"}:
            self._set_tts_job_state("resume_requested")
            self.runtime.request(
                "retry_job",
                {"job_id": self._tts_job_id},
                context={"resume_tts": True, "job_id": self._tts_job_id},
            )

    @Slot()
    def cancelVoiceGeneration(self) -> None:
        if self._tts_job_id and self._tts_busy:
            self._set_tts_job_state("cancel_requested")
            self.runtime.request(
                "cancel_job",
                {"job_id": self._tts_job_id},
                context={"job_kind": "tts"},
            )

    @Slot()
    def pauseDialogueCasting(self) -> None:
        if self._dialogue_casting_job_id and self._dialogue_casting_job_state == "running":
            job_id = self._dialogue_casting_job_id
            self._set_dialogue_casting_job_state("pause_requested")
            self.runtime.request(
                "pause_job",
                {"job_id": job_id},
                context=self._dialogue_casting_job_context(job_id),
            )

    @Slot()
    def resumeDialogueCasting(self) -> None:
        job_id = self._dialogue_casting_job_id
        if not job_id:
            return
        if self._dialogue_casting_job_state == "paused":
            self._set_dialogue_casting_job_state("resume_requested")
            self.runtime.request(
                "resume_job", {"job_id": job_id},
                context=self._dialogue_casting_job_context(job_id),
            )
        elif self._dialogue_casting_job_state in {"queued", "start_requested"}:
            self._set_dialogue_casting_job_state("start_requested")
            self._start_dialogue_casting_worker(job_id)
        elif self._dialogue_casting_job_state in {"interrupted", "failed"}:
            self._dialogue_casting_terminal_markers = {
                marker for marker in self._dialogue_casting_terminal_markers
                if marker[0] != job_id
            }
            self._set_dialogue_casting_job_state("resume_requested")
            request_context = self._dialogue_casting_job_context(job_id)
            request_context["resume_dialogue_casting"] = True
            self.runtime.request(
                "retry_job", {"job_id": job_id},
                context=request_context,
            )

    @Slot()
    def cancelDialogueCasting(self) -> None:
        if not self._dialogue_casting_job_id or not self.dialogueCastingBusy:
            return
        job_id = self._dialogue_casting_job_id
        if self._dialogue_casting_auto_tts_job_id == job_id:
            self._dialogue_casting_auto_tts = False
            self._dialogue_casting_auto_tts_job_id = ""
            self._pending_tts_start = None
            self._set_tts_busy(False)
            self._set_tts_job_state("cancelled")
        self._set_dialogue_casting_job_state("cancel_requested")
        self.runtime.request(
            "cancel_job",
            {"job_id": job_id},
            context=self._dialogue_casting_job_context(job_id),
        )

    @Slot(str)
    def setExportOutputPath(self, value: str) -> None:
        path = QUrl(value).toLocalFile() if value.startswith("file:") else value
        key = f"export/outputPath/{self.project_state.projectId or 'none'}"
        self.settings.setValue(key, path.strip())
        self.exportSettingsChanged.emit()

    @Slot(str)
    def setExportPresetId(self, value: str) -> None:
        if value not in {"source-quality", "1080p", "720p", "480p"}:
            return
        self.settings.setValue("export/presetId", value)
        self.exportSettingsChanged.emit()

    @Slot(str)
    def setExportBackend(self, value: str) -> None:
        if value not in {"auto", "cpu", "nvenc"}:
            return
        self.settings.setValue("export/backend", value)
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportChunkMinutes(self, value: int) -> None:
        self.settings.setValue("export/chunkMinutes", max(1, min(60, value)))
        self.exportSettingsChanged.emit()

    @Slot(bool)
    def setExportOverwrite(self, value: bool) -> None:
        self.settings.setValue("export/overwrite", bool(value))
        self.exportSettingsChanged.emit()

    @Slot(bool)
    def setExportSubtitleEnabled(self, value: bool) -> None:
        self.settings.setValue("export/subtitleEnabled", bool(value))
        self.exportSettingsChanged.emit()

    @Slot(str)
    def setExportFontFamily(self, value: str) -> None:
        if not value.strip():
            return
        self.settings.setValue("export/fontFamily", value.strip())
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportFontSize(self, value: int) -> None:
        self.settings.setValue("export/fontSize", max(12, min(120, value)))
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportSubtitleMarginBottom(self, value: int) -> None:
        self.settings.setValue("export/subtitleMarginBottom", max(0, min(500, value)))
        self.exportSettingsChanged.emit()

    @Slot(str)
    def setExportSubtitlePrimaryColor(self, value: str) -> None:
        normalized = _normalized_hex_color(value)
        if normalized is None:
            return
        self.settings.setValue("export/subtitlePrimaryColor", normalized)
        self.exportSettingsChanged.emit()

    @Slot(str)
    def setExportSubtitleOutlineColor(self, value: str) -> None:
        normalized = _normalized_hex_color(value)
        if normalized is None:
            return
        self.settings.setValue("export/subtitleOutlineColor", normalized)
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportSubtitleOutlineWidth(self, value: int) -> None:
        self.settings.setValue("export/subtitleOutlineWidth", max(0, min(12, value)))
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportSubtitleAlignment(self, value: int) -> None:
        if value not in {1, 2, 3}:
            return
        self.settings.setValue("export/subtitleAlignment", value)
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportSubtitleOpacityPercent(self, value: int) -> None:
        self.settings.setValue("export/subtitleOpacityPercent", max(0, min(100, value)))
        self.exportSettingsChanged.emit()

    @Slot(str)
    def setExportVideoFilterPreset(self, value: str) -> None:
        if value not in {"none", "cinematic-warm", "cool", "grayscale"}:
            return
        self.settings.setValue("export/videoFilterPreset", value)
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportVideoFilterIntensity(self, value: int) -> None:
        self.settings.setValue("export/videoFilterIntensity", max(0, min(100, value)))
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportVideoBrightness(self, value: int) -> None:
        self.settings.setValue("export/videoBrightness", max(-50, min(50, value)))
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportVideoContrast(self, value: int) -> None:
        self.settings.setValue("export/videoContrast", max(50, min(150, value)))
        self.exportSettingsChanged.emit()

    @Slot(int)
    def setExportVideoSaturation(self, value: int) -> None:
        self.settings.setValue("export/videoSaturation", max(0, min(200, value)))
        self.exportSettingsChanged.emit()

    @Slot()
    def resetExportSubtitleStyle(self) -> None:
        defaults = {
            "export/fontFamily": "Arial",
            "export/fontSize": 42,
            "export/subtitleMarginBottom": 28,
            "export/subtitlePrimaryColor": "#FFFFFF",
            "export/subtitleOutlineColor": "#000000",
            "export/subtitleOutlineWidth": 3,
            "export/subtitleAlignment": 2,
            "export/subtitleOpacityPercent": 100,
        }
        for key, value in defaults.items():
            self.settings.setValue(key, value)
        self.exportSettingsChanged.emit()

    @Slot()
    def resetExportVideoFilter(self) -> None:
        defaults = {
            "export/videoFilterPreset": "none",
            "export/videoFilterIntensity": 100,
            "export/videoBrightness": 0,
            "export/videoContrast": 100,
            "export/videoSaturation": 100,
        }
        for key, value in defaults.items():
            self.settings.setValue(key, value)
        self.exportSettingsChanged.emit()

    @Slot()
    def startExport(self) -> None:
        if not self._features_available():
            return
        if self._defer_until_subtitle_saves_flushed(
            "export", self.startExport, "Đang lưu project trước khi khóa snapshot xuất video…",
            durable=True,
        ):
            return
        if not self._backend_ready or not self.project_state.hasVideo:
            self._set_status("Hãy mở project có video trước khi xuất")
            return
        if self._export_busy:
            self._set_status("Một tác vụ xuất đang chạy; hãy tạm dừng hoặc hủy trước")
            return
        output = self.exportOutputPath
        if not output:
            self._set_status("Hãy chọn đường dẫn MP4 đầu ra")
            return
        self._set_export_job_state("start_requested")
        self._set_export_busy(True)
        self.runtime.request(
            "create_export_job",
            {
                "project_id": self.project_state.projectId,
                "output_path": output,
                "export_options": {
                    "preset_id": self.exportPresetId,
                    "backend": self.exportBackend,
                    "chunk_minutes": self.exportChunkMinutes,
                    "overwrite": self.exportOverwrite,
                    "subtitle_style": {
                        "enabled": self.exportSubtitleEnabled,
                        "font_family": self.exportFontFamily,
                        "font_size": self.exportFontSize,
                        "margin_bottom": self.exportSubtitleMarginBottom,
                        "primary_color": self.exportSubtitlePrimaryColor,
                        "outline_color": self.exportSubtitleOutlineColor,
                        "outline_width": self.exportSubtitleOutlineWidth,
                        "alignment": self.exportSubtitleAlignment,
                        "opacity_percent": self.exportSubtitleOpacityPercent,
                        "background_enabled": self.exportSubtitleBackgroundEnabled,
                        "background_mode": self.exportSubtitleBackgroundMode,
                    },
                    "video_filter": {
                        "preset_id": self.exportVideoFilterPreset,
                        "intensity_percent": self.exportVideoFilterIntensity,
                        "brightness_percent": self.exportVideoBrightness,
                        "contrast_percent": self.exportVideoContrast,
                        "saturation_percent": self.exportVideoSaturation,
                    },
                },
            },
            context={"export_output": output},
        )
        self._set_status("Đang khóa snapshot timeline bất biến để đưa vào hàng đợi")

    @Slot()
    def pauseExport(self) -> None:
        if self._export_job_id and self._export_job_state == "running":
            self._set_export_job_state("pause_requested")
            self.runtime.request(
                "pause_job",
                {"job_id": self._export_job_id},
                context={"job_kind": "export"},
            )

    @Slot()
    def resumeExport(self) -> None:
        if not self._export_job_id:
            return
        if self._export_job_state == "paused":
            self._set_export_job_state("resume_requested")
            self.runtime.request(
                "resume_job",
                {"job_id": self._export_job_id},
                context={"job_kind": "export"},
            )
        elif self._export_job_state == "queued":
            self._set_export_job_state("start_requested")
            self._start_export_worker(self._export_job_id)
        elif self._export_job_state in {"interrupted", "failed"}:
            self._set_export_job_state("resume_requested")
            self.runtime.request(
                "retry_job",
                {"job_id": self._export_job_id},
                context={"resume_export": True, "job_id": self._export_job_id},
            )

    @Slot()
    def cancelExport(self) -> None:
        if self._export_job_id and self._export_busy:
            self._set_export_job_state("cancel_requested")
            self.runtime.request(
                "cancel_job",
                {"job_id": self._export_job_id},
                context={"job_kind": "export"},
            )

    @Slot(str)
    def setLogoPath(self, value: str) -> None:
        path = QUrl(value).toLocalFile() if value.startswith("file:") else value
        if not path or not Path(path).expanduser().is_file():
            self._set_status("File logo không tồn tại")
            return
        self.settings.setValue("effects/logoPath", str(Path(path).expanduser().resolve()))
        self.overlaySelectionRequested.emit("overlay-default-logo")
        self._send_effect_command("set_logo_asset", values={"path": path})

    @Slot(str)
    def selectProjectOverlay(self, overlay_id: str) -> None:
        if self.project_state.overlay(overlay_id):
            self.overlaySelectionRequested.emit(overlay_id)

    @Slot(str, bool)
    def setOverlayEnabled(self, overlay_id: str, enabled: bool) -> None:
        self._send_effect_command("update", overlay_id=overlay_id, values={"enabled": enabled})

    @Slot(str, float, float, float, float)
    def setOverlayGeometry(
        self,
        overlay_id: str,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> None:
        self._send_effect_command(
            "update",
            overlay_id=overlay_id,
            values={"x": x, "y": y, "width": width, "height": height},
        )

    @Slot(str, float)
    def setOverlayOpacity(self, overlay_id: str, opacity: float) -> None:
        self._send_effect_command("update", overlay_id=overlay_id, values={"opacity": opacity})

    @Slot(str, float, float)
    def setOverlayRange(self, overlay_id: str, start_seconds: float, end_seconds: float) -> None:
        if not math.isfinite(start_seconds) or not math.isfinite(end_seconds):
            return
        self._send_effect_command(
            "update",
            overlay_id=overlay_id,
            values={
                "start_us": max(0, int(start_seconds * 1_000_000)),
                "end_us": max(0, int(end_seconds * 1_000_000)),
                "display_time": True,
            },
        )

    @Slot(str, int)
    def setOverlayBlurRadius(self, overlay_id: str, radius: int) -> None:
        self._send_effect_command("update", overlay_id=overlay_id, values={"blur_radius": radius})

    @Slot(str, str)
    def setOverlayTrackingMode(self, overlay_id: str, mode: str) -> None:
        self._send_effect_command(
            "update",
            overlay_id=overlay_id,
            values={"tracking_mode": mode},
        )

    @Slot(str, str)
    def setOverlayConcealmentMode(self, overlay_id: str, mode: str) -> None:
        self._send_effect_command(
            "update",
            overlay_id=overlay_id,
            values={"concealment_mode": mode},
        )

    @Slot(str, float, float)
    def setOverlayPadding(self, overlay_id: str, padding_x: float, padding_y: float) -> None:
        self._send_effect_command(
            "update",
            overlay_id=overlay_id,
            values={"padding_x": padding_x, "padding_y": padding_y},
        )

    @Slot()
    def addBlurRegion(self) -> None:
        overlay_id = f"overlay-blur-{uuid4().hex[:12]}"
        self.overlaySelectionRequested.emit(overlay_id)
        self._send_effect_command("add_blur", overlay_id=overlay_id)

    @Slot(str)
    def deleteOverlay(self, overlay_id: str) -> None:
        if overlay_id == "overlay-default-blur":
            self._set_status("Vùng blur mặc định có thể tắt nhưng không xóa")
            return
        self._send_effect_command("delete_overlay", overlay_id=overlay_id)

    def _send_effect_command(
        self,
        command: str,
        *,
        overlay_id: str | None = None,
        values: dict[str, object] | None = None,
    ) -> None:
        if not self.project_state.hasProject:
            return
        is_geometry = (
            command == "update"
            and bool(overlay_id)
            and isinstance(values, dict)
            and bool(values)
            and set(values).issubset({"x", "y", "width", "height"})
        )
        pending = {
            "command": command,
            "project_id": self.project_state.projectId,
            "overlay_id": overlay_id or "",
            "values": dict(values or {}),
            "geometry": is_geometry,
        }
        rollback = self.project_state.apply_local_effect_command(
            command,
            overlay_id or "",
            dict(values or {}),
        )
        if rollback:
            pending["rollback"] = rollback
        generation_key = overlay_id or f"command:{command}"
        generation = self._effect_generations.get(generation_key, 0) + 1
        self._effect_generations[generation_key] = generation
        pending["generation_key"] = generation_key
        pending["generation"] = generation
        if (self._timeline_busy or self._subtitle_command_context
                or self._subtitle_save_in_flight or self._subtitle_save_queue):
            self._enqueue_effect_command(pending)
            self._set_status("Đã xếp thay đổi logo/blur; sẽ lưu ngay sau thao tác hiện tại")
            self._defer_until_subtitle_saves_flushed(
                "effect-commands", self._dispatch_pending_effect_command,
                "Đã xếp thay đổi logo/blur; sẽ lưu ngay sau thao tác hiện tại",
            )
            return
        params: dict[str, object] = {
            "project_id": self.project_state.projectId,
            "command": command,
            "values": values or {},
        }
        if overlay_id:
            params["overlay_id"] = overlay_id
        self._set_timeline_busy(True)
        self.runtime.request(
            "effect_command",
            params,
            context=dict(pending),
        )
        self._set_status("Đang lưu lớp logo/blur vào project")

    def _save_overlay_preset_value(self, overlay_id: str, key: str, value: object) -> None:
        if overlay_id not in {"overlay-default-logo", "overlay-default-blur"}:
            return
        kind = "blur" if overlay_id == "overlay-default-blur" else "logo"
        self.settings.setValue(f"effects/{kind}/{key}", value)

    def _enqueue_effect_command(self, command: dict[str, object]) -> None:
        if command.get("geometry") is True:
            overlay_id = str(command.get("overlay_id", ""))
            for index in range(len(self._pending_effect_commands) - 1, -1, -1):
                pending = self._pending_effect_commands[index]
                if pending.get("geometry") is not True:
                    break
                if str(pending.get("overlay_id", "")) == overlay_id:
                    if "rollback" in pending:
                        command["rollback"] = pending["rollback"]
                    self._pending_effect_commands[index] = command
                    return
        self._pending_effect_commands.append(command)

    def _take_pending_effect_command(self) -> dict[str, object] | None:
        if not self._pending_effect_commands:
            return None
        return self._pending_effect_commands.pop(0)

    def _dispatch_pending_effect_command(self, pending: dict[str, object] | None = None) -> None:
        if (self._timeline_busy or self._subtitle_command_context
                or self._subtitle_save_in_flight or self._subtitle_save_queue
                or self._timeline_reconcile_pending):
            if pending is not None:
                self._enqueue_effect_command(pending)
            return
        command = pending if pending is not None else self._take_pending_effect_command()
        if command is None:
            return
        if command.get("project_id", self.project_state.projectId) != self.project_state.projectId:
            self._dispatch_pending_effect_command()
            return
        overlay_id = str(command.get("overlay_id", ""))
        params: dict[str, object] = {
            "project_id": self.project_state.projectId,
            "command": str(command.get("command", "update")),
            "values": command.get("values") if isinstance(command.get("values"), dict) else {},
        }
        if overlay_id:
            params["overlay_id"] = overlay_id
        self.project_state.apply_local_effect_command(
            params["command"], overlay_id, params["values"])
        self._set_timeline_busy(True)
        self.runtime.request(
            "effect_command",
            params,
            context=dict(command),
        )

    def _default_overlay_import_options(self) -> dict[str, object]:
        logo_path = self.settings.value("effects/logoPath", "", type=str).strip()
        if logo_path and not Path(logo_path).expanduser().is_file():
            logo_path = ""

        def number(kind: str, key: str, default: float) -> float:
            try:
                return float(self.settings.value(f"effects/{kind}/{key}", default))
            except (TypeError, ValueError):
                return default

        preset = {
            "blur": {
                "x": number("blur", "x", 0.04),
                "y": number("blur", "y", 0.82),
                "width": number("blur", "width", 0.92),
                "height": number("blur", "height", 0.15),
                "enabled": self.settings.value("effects/blur/enabled", True, type=bool),
                "blur_radius": self.settings.value("effects/blur/blur_radius", 46, type=int),
                "padding_x": number("blur", "padding_x", 0.02),
                "padding_y": number("blur", "padding_y", 0.014),
                "concealment_mode": self.settings.value(
                    "effects/blur/concealment_mode", "cover", type=str
                ),
            },
            "logo": {
                "x": number("logo", "x", 0.82),
                "y": number("logo", "y", 0.035),
                "width": number("logo", "width", 0.15),
                "height": number("logo", "height", 0.085),
                "enabled": self.settings.value("effects/logo/enabled", True, type=bool),
                "opacity": number("logo", "opacity", 0.9),
            },
        }
        return {"default_logo_path": logo_path, "overlay_preset": preset}

    def _start_voice_job(
        self,
        purpose: str,
        *,
        force_cue_ids: list[str] | None = None,
    ) -> None:
        if self._guard_manual_parallel("tts", (purpose, list(force_cue_ids or []))):
            return
        if not self._backend_ready or not self.project_state.hasProject:
            self._set_status("Hãy mở project có video trước khi tạo giọng")
            return
        if self._tts_busy:
            self._set_status("Một tác vụ giọng đang chạy; hãy tạm dừng hoặc hủy trước")
            return
        selected = self.voice_catalog.find(self._selected_voice_id)
        if not selected or selected.get("installed") is not True:
            self._set_status("Hãy chọn một giọng đã cài và sẵn sàng")
            return
        casting_config = self._current_dialogue_casting_config()
        if purpose == "production" and casting_config.get("enabled") is True:
            missing_roles = [
                role
                for key, role in (
                    ("male_voice", "nam"),
                    ("female_voice", "nữ"),
                    ("fallback_voice", "dự phòng"),
                )
                if not isinstance(casting_config.get(key), dict)
                or not str(casting_config[key].get("voice_id", "")).strip()
                or not str(casting_config[key].get("provider_id", "")).strip()
            ]
            if missing_roles:
                self._set_status(
                    "Hãy chọn đủ giọng " + ", ".join(missing_roles)
                    + " trong Phân vai thoại trước khi tạo giọng"
                )
                return
        provider_id = str(selected.get("provider_id", ""))
        options: dict[str, object] = {"purpose": purpose}
        if force_cue_ids:
            options["force_cue_ids"] = force_cue_ids
        if provider_id == "vieneu-3.8":
            if selected.get("is_clone") is True:
                if self.vieneuReferenceAudio:
                    options["reference_audio_path"] = self.vieneuReferenceAudio
                    options["reference_text"] = self.vieneuReferenceText
                    options["reference_denoise"] = self.vieneuReferenceDenoise
                else:
                    profile_path = selected.get("profile_path")
                    if isinstance(profile_path, str) and profile_path:
                        options["profile_path"] = profile_path
            options["batch_size"] = 32
        else:
            options["batch_size"] = 16
        adaptive_timing = purpose == "production"
        options["adaptive_timing"] = adaptive_timing
        options["voice_read_speed"] = self._voice_read_speed
        options["max_absolute_speed"] = max(
            self._voice_read_speed,
            self._voice_cue_max_speed if adaptive_timing else self._voice_read_speed,
        )
        # Adaptive fitting must happen while D1 is created. When disabled, keep
        # the complete base-speed voice for the post-conform workflow.
        options["qa_non_blocking"] = True
        options["defer_timing_conform"] = False
        if purpose == "preview":
            self._release_voice_preview()
        self._set_tts_job_purpose(purpose)
        self._set_tts_job_state("start_requested")
        self._set_tts_busy(True)
        self._pending_tts_start = {
            "params": {
                "project_id": self.project_state.projectId,
                "provider_id": provider_id,
                "voice_id": self._selected_voice_id,
                "provider_options": options,
            },
            "context": {
                "tts_purpose": purpose,
                "project_id": self.project_state.projectId,
            },
            "force_cue_ids": list(force_cue_ids or []),
        }
        if purpose == "production" and casting_config.get("enabled") is True:
            if self.dialogueCastingBusy:
                self._dialogue_casting_auto_tts = True
                self._dialogue_casting_auto_tts_job_id = self._dialogue_casting_job_id
                self._set_status("Đang chờ phân vai hiện tại xong để tạo D1; không nhận diện lại")
                return
            self._dialogue_casting_job_id = ""
            self._set_dialogue_casting_job_state("start_requested")
            self._dialogue_casting_auto_tts = True
            self._dialogue_casting_auto_tts_job_id = ""
            self.runtime.request(
                "create_dialogue_casting_job",
                {"project_id": self.project_state.projectId},
                context={
                    "project_id": self.project_state.projectId,
                    "automatic_tts": True,
                },
            )
            self._set_status("Đang nhận diện hoặc kiểm tra nhãn thoại trước khi tạo D1")
            return
        self._continue_pending_tts_start()

    def _continue_pending_tts_start(self) -> None:
        pending = self._pending_tts_start
        if not isinstance(pending, dict):
            self._set_tts_busy(False)
            self._set_tts_job_state("failed")
            self._set_status("Không tìm thấy cấu hình tạo giọng đang chờ")
            return
        pending_context = pending.get("context")
        purpose = (
            str(pending_context.get("tts_purpose", ""))
            if isinstance(pending_context, dict)
            else ""
        )
        if purpose == "production" and self.project_state.hasVoiceTrack:
            # Drop the QML sources before asking the service to remove D1.  Qt
            # Multimedia otherwise keeps the stable WAV open on Windows and
            # the final atomic promotion fails with PermissionError.
            self._set_voice_playback_enabled(False)
            self.runtime.request(
                "prepare_tts_rerun",
                {"project_id": self.project_state.projectId},
                context={"tts_purpose": purpose},
            )
            self._set_status(
                "Đang gỡ D1 cũ và giải phóng file WAV trước khi tạo lại; cache vẫn được giữ"
            )
            return
        self._dispatch_pending_tts_start()

    def _dispatch_pending_tts_start(
        self,
        *,
        force_cue_ids: list[str] | None = None,
    ) -> None:
        pending = self._pending_tts_start
        self._pending_tts_start = None
        if not isinstance(pending, dict):
            self._set_tts_busy(False)
            self._set_tts_job_state("failed")
            self._set_status("Không tìm thấy cấu hình tạo giọng đang chờ")
            return
        params = pending.get("params")
        context = pending.get("context")
        if not isinstance(params, dict) or not isinstance(context, dict):
            self._set_tts_busy(False)
            self._set_tts_job_state("failed")
            self._set_status("Cấu hình tạo giọng đang chờ không hợp lệ")
            return
        pending_force_ids = pending.get("force_cue_ids")
        if force_cue_ids is None and isinstance(pending_force_ids, list):
            force_cue_ids = [str(value) for value in pending_force_ids if str(value)]
        self.runtime.request("create_tts_job", params, context=context)
        purpose = str(context.get("tts_purpose", ""))
        self._set_status(
            "Đang chuẩn bị nghe thử giọng"
            if purpose == "preview"
            else (
                "Đang tạo lại cue đã chọn và ráp lại WAV đầy đủ"
                if force_cue_ids
                else (
                    f"Đang tạo voice gốc {self._voice_read_speed:.1f}×"
                    + (
                        f", cue dài tối đa tổng {self._voice_cue_max_speed:.1f}×"
                        if purpose == "production"
                        else "; cue dài chuyển sang Đồng bộ sau lồng tiếng"
                    )
                )
            )
        )

    @Slot(str, float)
    def selectCue(self, cue_id: str, start_us: float) -> None:
        if not self.project_state.subtitle_cues.find(cue_id):
            return
        if self._selected_clip_id:
            self._selected_clip_id = ""
            self.selectedClipChanged.emit()
        if cue_id != self._selected_cue_id or self._selected_cue_ids != [cue_id]:
            self._selected_cue_id = cue_id
            self._selected_cue_ids = [cue_id]
            self.selectedCueChanged.emit()
        output_us = self.project_state.sourceToOutputUs(max(0.0, start_us))
        self.setPlayheadMs(output_us / 1_000)

    @Slot(str, bool)
    def selectTimelineCue(self, cue_id: str, additive: bool = False) -> None:
        """Select a Timeline cue without moving the playhead."""

        if not self.project_state.subtitle_cues.find(cue_id):
            return
        if self._selected_clip_id and self.selectedClipTrackKind not in {"subtitle", "text"}:
            self._selected_clip_id = ""
            self.selectedClipChanged.emit()
        if not additive:
            selected = [cue_id]
        else:
            selected = list(self._selected_cue_ids)
            is_title = bool(self.project_state.title_cues.find(cue_id))
            selected = [item for item in selected
                        if bool(self.project_state.title_cues.find(item)) == is_title]
            if cue_id in selected:
                selected.remove(cue_id)
            else:
                selected.append(cue_id)
        if selected == self._selected_cue_ids:
            return
        self._selected_cue_ids = selected
        self._selected_cue_id = selected[-1] if selected else ""
        self.selectedCueChanged.emit()

    @Slot()
    def clearTimelineCueSelection(self) -> None:
        """Clear cue/clip selection after a plain click on empty Timeline space."""

        cue_changed = bool(self._selected_cue_id or self._selected_cue_ids)
        clip_changed = bool(self._selected_clip_id)
        if not cue_changed and not clip_changed:
            return
        self._selected_cue_id = ""
        self._selected_cue_ids = []
        self._selected_clip_id = ""
        if cue_changed:
            self.selectedCueChanged.emit()
        if clip_changed:
            self.selectedClipChanged.emit()

    @Slot(str, float, int)
    def selectCueWithModifiers(
        self,
        cue_id: str,
        start_us: float,
        modifiers: int,
    ) -> None:
        self.selectCueWithAdditive(
            cue_id,
            start_us,
            bool(int(modifiers) & Qt.KeyboardModifier.ControlModifier.value),
        )

    @Slot(str, float, bool)
    def selectCueWithAdditive(
        self,
        cue_id: str,
        start_us: float,
        additive: bool,
    ) -> None:
        cue = self.project_state.subtitle_cues.find(cue_id)
        if not cue:
            return
        if not additive:
            self.selectCue(cue_id, start_us)
            return
        selected = list(self._selected_cue_ids)
        if cue_id in selected:
            selected.remove(cue_id)
        else:
            selected.append(cue_id)
        self._selected_cue_ids = selected
        self._selected_cue_id = selected[-1] if selected else ""
        self.selectedCueChanged.emit()
        if cue_id in selected:
            output_us = self.project_state.sourceToOutputUs(max(0.0, start_us))
            self.setPlayheadMs(output_us / 1_000)

    @Slot(str, result=bool)
    def isCueSelected(self, cue_id: str) -> bool:
        if getattr(self, "_cue_selection_list", None) is not self._selected_cue_ids:
            self._cue_selection_list = self._selected_cue_ids
            self._cue_selection_set = set(self._selected_cue_ids)
        return cue_id in self._cue_selection_set

    @Slot("QVariantMap", int, int, result="QVariantMap")
    def subtitleArtwork(self, cue: dict, width: int, height: int, *, measure_only=False) -> dict:
        if not cue.get("cue_id") or not (cue.get("translated_text") or cue.get("source_text")):
            return {}
        from urllib.parse import quote

        from ducai_studio.providers.subtitle_artwork import editor_payload, layout_json

        style = self._subtitle_artwork_style()
        payload = editor_payload(cue, max(2, width), max(2, height), style)
        key = json.dumps(payload, sort_keys=True)
        layout = layout_json(key, include_paths=not measure_only)
        if measure_only:
            return layout
        return {
            "source": "image://subtitle-artwork/" + quote(key, safe=""),
            "text_rect": layout["text_rect"],
            "concealment_rect": layout["concealment_rect"],
            "font_size": layout["font_size"],
            "placement_rect": layout["placement_rect"],
            "selection_rect": layout["selection_rect"],
        }

    def _subtitle_artwork_style(self):
        from ducai_studio.domain.effects import SubtitleStyle

        return SubtitleStyle(
            enabled=self.exportSubtitleEnabled,
            font_family=self.exportFontFamily,
            font_size=self.exportFontSize,
            margin_bottom=self.exportSubtitleMarginBottom,
            primary_color=self.exportSubtitlePrimaryColor,
            outline_color=self.exportSubtitleOutlineColor,
            outline_width=self.exportSubtitleOutlineWidth,
            alignment=self.exportSubtitleAlignment,
            opacity_percent=self.exportSubtitleOpacityPercent,
            background_enabled=self.exportSubtitleBackgroundEnabled,
            background_mode=self.exportSubtitleBackgroundMode,
        )

    @Slot("QVariantList", bool)
    def selectSubtitleCueIds(self, cue_ids: list, additive: bool = False) -> None:
        available: list[str] = []
        for value in cue_ids:
            cue_id = str(value or "")
            if cue_id and self.project_state.subtitle_cues.find(cue_id):
                available.append(cue_id)
        values = list(dict.fromkeys(available))
        if additive:
            values = list(dict.fromkeys((*self._selected_cue_ids, *values)))
        if values:
            title_selection = bool(self.project_state.title_cues.find(values[-1]))
            values = [item for item in values
                      if bool(self.project_state.title_cues.find(item)) == title_selection]
        if values == self._selected_cue_ids:
            return
        self._selected_cue_ids = values
        self._selected_cue_id = values[-1] if values else ""
        self.selectedCueChanged.emit()

    @Slot(str)
    @Slot(str, bool)
    def selectTimelineTrackCues(self, track_id: str, additive: bool = False) -> None:
        track = self.project_state.track_info(track_id) or {}
        if track.get("kind") not in {"subtitle", "text"}:
            return
        model = (self.project_state.title_cues if track.get("kind") == "text"
                 else self.project_state.subtitle_cues)
        cue_ids = [str(cue.get("cue_id", ""))
                   for row in range(model.count)
                   if (cue := model.get(row)).get(
                       "subtitle_track_id") == track_id]
        self.selectSubtitleCueIds(cue_ids, additive)

    @Slot()
    def selectAllSubtitleCues(self) -> None:
        if self.project_state.title_cues.find(self._selected_cue_id):
            self.selectSubtitleCueIds(self.project_state.titleCueIds(), False)
            return
        values = [
            str(self.project_state.subtitle_cues.get(row).get("cue_id", ""))
            for row in range(self.project_state.subtitle_cues.count)
        ]
        self.selectSubtitleCueIds([value for value in values if value], False)

    @Slot(int)
    def selectTranslationQaIssue(self, cue_index: int) -> None:
        """Select and seek the cue represented by a live translation QA row."""

        if type(cue_index) is not int or cue_index <= 0:
            return
        model = self.project_state.subtitle_cues
        for row in range(model.count):
            cue = model.get(row)
            if cue.get("index") != cue_index:
                continue
            cue_id = str(cue.get("cue_id", ""))
            if cue_id:
                self.selectCue(cue_id, float(cue.get("start_us", 0)))
                self.translationQaNavigationRequested.emit(row)
            return

    @Slot(str, str, str)
    def updateSubtitleText(self, cue_id: str, text: str, language: str = "source") -> None:
        optimistic_changes = self.project_state.subtitle_cues.optimistic_update_text(
            cue_id,
            text.strip(),
            language=language,
        )
        if optimistic_changes:
            self._set_translation_qa(self._translation_qa)
        self._send_subtitle_command(
            "update_text",
            cue_id=cue_id,
            language=language,
            values={"text": text},
            optimistic_changes=optimistic_changes,
        )

    @Slot(str, str)
    def updateSceneTextTranslation(self, source_cue_id: str, text: str) -> None:
        cue = self.project_state.subtitle_cues.find(source_cue_id)
        if cue.get("cue_role") != "scene_text":
            self._set_status("Chỉ cue chữ trong cảnh mới dùng ô sửa này")
            return
        translation_cue_id = str(cue.get("translation_cue_id", ""))
        if not translation_cue_id:
            self._set_status("Cue chữ trong cảnh này chưa có bản dịch tiếng Việt để sửa")
            return
        self.updateSubtitleText(translation_cue_id, text, "translated")

    @Slot(str, int, str, str)
    def setSubtitleTextFormat(
        self,
        cue_id: str,
        font_size: int,
        orientation: str,
        background_color: str,
    ) -> None:
        cue = self.project_state.subtitle_cues.find(cue_id)
        if not cue:
            self._set_status("Hãy chọn một cue chữ trước")
            return
        self._set_selected_subtitle_text_format(
            font_size,
            orientation,
            background_color,
            str(cue.get("text_color", "")),
            str(cue.get("style_hint", "standard")),
        )

    @Slot(int, str, str, str, str, int, int, str, int, bool, bool, int, int)
    def setSelectedSubtitleTextFormat(
        self,
        font_size: int,
        orientation: str,
        background_color: str,
        text_color: str,
        style_hint: str,
        background_padding_x: int = 20,
        background_padding_y: int = 10,
        background_style: str = "solid",
        letter_spacing_percent: int = 0,
        font_bold: bool = False,
        font_italic: bool = False,
        text_opacity_percent: int = 100,
        background_opacity_percent: int = 90,
    ) -> None:
        self._set_selected_subtitle_text_format(
            font_size,
            orientation,
            background_color,
            text_color,
            style_hint,
            background_padding_x,
            background_padding_y,
            background_style,
            letter_spacing_percent,
            font_bold,
            font_italic,
            text_opacity_percent,
            background_opacity_percent,
        )

    @Slot(str)
    def setSelectedCueRole(self, role: str) -> None:
        if role not in {"dialogue", "scene_text"}:
            self._set_status("Loại cue phải là thoại S1 hoặc chữ trong cảnh S2")
            return
        cue_ids = list(self._selected_cue_ids)
        if not cue_ids and self._selected_cue_id:
            cue_ids = [self._selected_cue_id]
        if not cue_ids:
            self._set_status("Hãy chọn ít nhất một cue để chuyển loại")
            return
        if not self.selectedCueClassification["can_convert"]:
            self._set_status("Chỉ chuyển cue phụ đề S; chữ tiêu đề trên track T được giữ riêng")
            return
        self._send_subtitle_command(
            "set_cue_role", cue_id=cue_ids[-1],
            values={"cue_ids": cue_ids, "role": role},
            status_context={"converted_cue_count": len(cue_ids), "converted_cue_role": role},
        )

    def _set_selected_subtitle_text_format(
        self,
        font_size: int,
        orientation: str,
        background_color: str,
        text_color: str,
        style_hint: str,
        background_padding_x: int = 20,
        background_padding_y: int = 10,
        background_style: str = "solid",
        letter_spacing_percent: int = 0,
        font_bold: bool = False,
        font_italic: bool = False,
        text_opacity_percent: int = 100,
        background_opacity_percent: int = 90,
    ) -> None:
        cue_ids = list(self._selected_cue_ids)
        if not cue_ids and self._selected_cue_id:
            cue_ids = [self._selected_cue_id]
        if not cue_ids:
            self._set_status("Hãy chọn ít nhất một cue chữ trước")
            return
        if self._text_cue_ids_locked(cue_ids):
            self._set_status("Hãy mở khóa dòng chữ trước khi chỉnh kiểu chữ")
            return
        normalized_size = max(1, int(font_size))
        normalized_orientation = "vertical" if orientation == "vertical" else "horizontal"
        normalized_color = str(background_color or "").strip().upper()
        normalized_text_color = str(text_color or "").strip().upper()
        normalized_style = (
            style_hint
            if style_hint
            in {
                "standard",
                "display",
                "calligraphy",
                "calligraphy_soft",
                "calligraphy_bold",
                "calligraphy_ink",
                "calligraphy_brush",
                "calligraphy_water",
                "calligraphy_splash",
                "calligraphy_hurricane",
            }
            else "standard"
        )
        normalized_padding_x = max(0, int(background_padding_x))
        normalized_padding_y = max(0, int(background_padding_y))
        normalized_background_style = (
            background_style if background_style in CUE_BACKGROUND_STYLES else "solid"
        )
        normalized_letter_spacing = max(0, int(letter_spacing_percent))
        normalized_bold = bool(font_bold)
        normalized_italic = bool(font_italic)
        normalized_text_opacity = max(0, min(100, int(text_opacity_percent)))
        normalized_background_opacity = max(0, min(100, int(background_opacity_percent)))
        optimistic_changes = self.project_state.subtitle_cues.optimistic_update_text_format_many(
            cue_ids,
            font_size=normalized_size,
            orientation=normalized_orientation,
            background_color=normalized_color,
            text_color=normalized_text_color,
            style_hint=normalized_style,
            background_padding_x=normalized_padding_x,
            background_padding_y=normalized_padding_y,
            background_style=normalized_background_style,
            letter_spacing_percent=normalized_letter_spacing,
            font_bold=normalized_bold,
            font_italic=normalized_italic,
            text_opacity_percent=normalized_text_opacity,
            background_opacity_percent=normalized_background_opacity,
        )
        if optimistic_changes:
            self.selectedCueChanged.emit()
        self._send_subtitle_command(
            "set_text_format",
            cue_id=cue_ids[-1],
            values={
                "cue_ids": cue_ids,
                "font_size": normalized_size,
                "orientation": normalized_orientation,
                "background_color": normalized_color,
                "text_color": normalized_text_color,
                "style_hint": normalized_style,
                "background_padding_x": normalized_padding_x,
                "background_padding_y": normalized_padding_y,
                "background_style": normalized_background_style,
                "letter_spacing_percent": normalized_letter_spacing,
                "font_bold": normalized_bold,
                "font_italic": normalized_italic,
                "text_opacity_percent": normalized_text_opacity,
                "background_opacity_percent": normalized_background_opacity,
            },
            optimistic_changes=optimistic_changes,
            status_context={
                "text_font_size": normalized_size,
                "text_orientation": normalized_orientation,
                "formatted_cue_count": len(cue_ids),
            },
        )

    @Slot(bool, int)
    def setSelectedSceneTextFade(self, enabled: bool, duration_ms: int) -> None:
        cue_ids = list(self._selected_cue_ids)
        if not cue_ids and self._selected_cue_id:
            cue_ids = [self._selected_cue_id]
        scene_ids = [
            cue_id
            for cue_id in cue_ids
            if self.project_state.subtitle_cues.find(cue_id).get("cue_role")
            in {"scene_text", "title_text"}
        ]
        if not scene_ids:
            self._set_status("Chế độ mờ dần chỉ áp dụng cho chữ trong cảnh")
            return
        normalized_duration = max(50, min(2_000, int(duration_ms)))
        optimistic_changes: list[dict[str, object]] = []
        for cue_id in scene_ids:
            optimistic_changes.extend(
                self.project_state.subtitle_cues.optimistic_update_scene_fade(
                    cue_id,
                    enabled=bool(enabled),
                    duration_ms=normalized_duration,
                )
            )
        if optimistic_changes:
            self.selectedCueChanged.emit()
        self._send_subtitle_command(
            "set_scene_text_fade",
            cue_id=scene_ids[-1],
            values={
                "cue_ids": scene_ids,
                "enabled": bool(enabled),
                "duration_ms": normalized_duration,
            },
            optimistic_changes=optimistic_changes,
            status_context={"scene_fade_cue_count": len(scene_ids)},
        )

    @Slot(QImage, int, int, result=str)
    def samplePreviewColor(self, image: QImage, x: int, y: int) -> str:
        """Return one opaque #RRGGBB pixel from an in-memory Preview grab."""

        if not isinstance(image, QImage) or image.isNull():
            return ""
        pixel_x = max(0, min(image.width() - 1, int(x)))
        pixel_y = max(0, min(image.height() - 1, int(y)))
        return image.pixelColor(pixel_x, pixel_y).name(QColor.NameFormat.HexRgb).upper()

    @Slot(float, float, float, float, result=bool)
    @Slot(float, float, float, float, int, int, result=bool)
    def transformSelectedSubtitleGeometry(
        self,
        delta_x: float,
        delta_y: float,
        scale_x: float,
        scale_y: float,
        render_width: int = 1920,
        render_height: int = 1080,
    ) -> bool:
        cue_ids = list(self._selected_cue_ids)
        if not cue_ids:
            self._set_status("Hãy chọn cue chữ cần di chuyển trên Preview")
            return False
        values = (delta_x, delta_y, scale_x, scale_y)
        if (not all(math.isfinite(value) for value in values)
                or min(render_width, render_height, scale_x, scale_y) <= 0
                or self.subtitleBusy or self.timelineBusy or not self.project_state.hasProject):
            return False
        self._subtitle_geometry_generation += 1
        context = {"generation": self._subtitle_geometry_generation,
                   "project_id": self.project_state.projectId,
                   "revision": self.project_state.revision,
                   "cue_ids": cue_ids, "delta_x": float(delta_x), "delta_y": float(delta_y),
                   "scale_x": float(scale_x), "scale_y": float(scale_y)}
        self._subtitle_geometry_context = context
        cues = [cue for cue_id in cue_ids
                if (cue := self.project_state.subtitle_cues.find(cue_id))
                and not cue.get("panel_box") and cue.get("cue_role") != "scene_text"]
        if len(cues) > 128:
            from ducai_studio.providers.subtitle_artwork import _ensure_fonts

            _ensure_fonts()  # Font registration belongs to the GUI thread.
            context["measured_cues"] = cues
            context["measured_style"] = self._subtitle_artwork_style()
            self._set_subtitle_busy(True)
            self._save_state = f"Đang lưu vị trí {len(cue_ids)} cue"
            self.saveStateChanged.emit()
            QThreadPool.globalInstance().start(_SubtitleGeometryMeasureTask(
                cues, context["measured_style"], render_width, render_height,
                context, self.subtitleGeometryMeasured.emit))
            return True
        seeds = {}
        for cue in cues:
            artwork = self.subtitleArtwork(cue, render_width, render_height, measure_only=True)
            rect = artwork.get("placement_rect")
            if rect:
                seeds[cue["cue_id"]] = [rect[0] / render_width, rect[1] / render_height,
                                 rect[2] / render_width, rect[3] / render_height]
        return self._submit_subtitle_geometry(seeds, context)

    @Slot(bool, object, object)
    def _subtitle_geometry_measured(self, succeeded: bool, result: object, context: object):
        if not isinstance(context, dict) or context != self._subtitle_geometry_context:
            return
        same_project = context["project_id"] == self.project_state.projectId
        inputs_unchanged = same_project and (
            context["revision"] == self.project_state.revision
            or (context.get("measured_style") == self._subtitle_artwork_style()
                and all(self._subtitle_geometry_cue_is_unchanged(cue)
                        for cue in context.get("measured_cues", []))))
        if not succeeded or not inputs_unchanged:
            self._finish_subtitle_geometry(False)
            self._set_status("Không thể lưu vị trí chữ: dự án đã thay đổi hoặc đo chữ thất bại. "
                             "Hãy chọn và kéo lại; dữ liệu đã lưu vẫn được giữ.")
            return
        self._subtitle_busy = False  # Preserve the pending preview until its own command ACK.
        self._submit_subtitle_geometry(result, context)

    def _subtitle_geometry_cue_is_unchanged(self, cue: dict) -> bool:
        current = self.project_state.subtitle_cues.find(cue["cue_id"])
        # An ACK may advance only the durable revision of already-previewed
        # text/style. That metadata does not change the measured placement.
        current.pop("revision", None)
        return current == {key: value for key, value in cue.items() if key != "revision"}

    def _submit_subtitle_geometry(self, seeds: dict, context: dict) -> bool:
        values = {key: value for key, value in context.items()
                  if key not in {"project_id", "revision", "generation",
                                 "measured_cues", "measured_style"}}
        try:
            accepted = bool(self._send_subtitle_command(
                "transform_cue_geometry",
                cue_id=context["cue_ids"][-1],
                values={**values, "panel_seeds": seeds},
                status_context={"geometry_cue_count": len(context["cue_ids"]),
                                "subtitle_geometry_generation": context["generation"]},
            ))
        except Exception as exc:
            self._set_status("Không thể gửi vị trí chữ để lưu: " + str(exc))
            accepted = False
        if not accepted:
            self._finish_subtitle_geometry(False)
        return accepted

    def _finish_subtitle_geometry(self, succeeded: bool) -> None:
        generation = int(self._subtitle_geometry_context.get("generation", 0))
        self._subtitle_geometry_context = {}
        self._set_subtitle_busy(False)
        if generation:
            self.subtitleGeometrySettled.emit(generation, succeeded)
        if succeeded:
            self._run_subtitle_flush_callbacks()
        else:
            self._save_state = "Chỉnh vị trí chữ thất bại"
            self.saveStateChanged.emit()
            self._subtitle_flush_callbacks.clear()

    def _subtitle_geometry_response_is_current(self, context: object) -> bool:
        if not isinstance(context, dict) or not context.get("subtitle_geometry_generation"):
            return True
        return (context.get("subtitle_geometry_generation")
                == self._subtitle_geometry_context.get("generation")
                and context.get("project_id") == self.project_state.projectId)

    @Slot(float, int, result=int)
    def moveSelectedSceneTexts(self, delta_us: float, lane_delta: int) -> int:
        cue_ids = list(self._selected_cue_ids)
        if not cue_ids:
            self._set_status("Hãy chọn cue phụ đề cần di chuyển")
            return 0
        if not math.isfinite(delta_us):
            return 0
        minimum_delta, maximum_delta = self._selected_scene_timing_bounds("move")
        applied_delta = max(
            minimum_delta,
            min(maximum_delta, int(round(delta_us))),
        )
        optimistic_changes: list[dict[str, object]] = []
        for cue_id in cue_ids:
            timing = self.project_state.subtitle_cues.scene_text_base_timing(cue_id)
            if timing is None:
                continue
            previous_start, previous_end = timing
            applied_start = max(0, previous_start + applied_delta)
            duration = max(100_000, previous_end - previous_start)
            applied_end = applied_start + duration
            self.project_state.subtitle_cues.preview_scene_text_timing(
                cue_id,
                applied_start,
                applied_end,
            )
            optimistic_changes.append(
                {
                    "kind": "scene_timing",
                    "cue_id": cue_id,
                    "previous_start_us": previous_start,
                    "previous_end_us": previous_end,
                    "applied_start_us": applied_start,
                    "applied_end_us": applied_end,
                }
            )
        return self._send_subtitle_command(
            "move_scene_texts",
            cue_id=cue_ids[-1],
            values={
                "cue_ids": cue_ids,
                "delta_us": applied_delta,
                "lane_delta": int(lane_delta),
                "display_time": True,
            },
            optimistic_changes=optimistic_changes,
            status_context={"moved_cue_count": len(cue_ids)},
        )

    def _selected_scene_timing_bounds(self, edge: str) -> tuple[int, int]:
        timings = [
            timing
            for cue_id in self._selected_cue_ids
            if (timing := self.project_state.subtitle_cues.scene_text_base_timing(cue_id))
            is not None
        ]
        if not timings:
            return 0, 0
        latest_end_us = max(end_us for _start_us, end_us in timings)
        duration_us = int(self.project_state.durationUs)
        # Lightweight unit/project snapshots may omit video tracks entirely.
        # Do not invent an end clamp in that compatibility case; the server is
        # still authoritative when a real project command is submitted.
        if duration_us < latest_end_us:
            duration_us = 9_007_199_254_740_991
        if edge == "left":
            return (
                -min(start_us for start_us, _end_us in timings),
                min(end_us - start_us - 100_000 for start_us, end_us in timings),
            )
        if edge == "right":
            return (
                max(100_000 - (end_us - start_us) for start_us, end_us in timings),
                duration_us - max(end_us for _start_us, end_us in timings),
            )
        return (
            -min(start_us for start_us, _end_us in timings),
            duration_us - max(end_us for _start_us, end_us in timings),
        )

    @Slot(str, result="QVariantMap")
    def selectedSceneTimingDeltaBounds(self, edge: str) -> dict[str, object]:
        minimum, maximum = self._selected_scene_timing_bounds(edge)
        lanes: list[int] = []
        for cue_id in self._selected_cue_ids:
            cue = self.project_state.subtitle_cues.find(cue_id)
            raw_track_id = str(cue.get("subtitle_track_id", "S2"))
            try:
                lanes.append(max(1, int(raw_track_id.removeprefix("S"))))
            except ValueError:
                lanes.append(1 if cue.get("cue_role") == "dialogue" else 2)
        return {
            "minimum": float(minimum),
            "maximum": float(maximum),
            "minimumLaneDelta": float(1 - min(lanes)) if lanes else 0.0,
            "fixedLane": bool(self.project_state.title_cues.find(self._selected_cue_id)),
            "timings": {
                cue_id: list(timing)
                for cue_id in self._selected_cue_ids
                if (timing := self.project_state.subtitle_cues.scene_text_base_timing(cue_id))
                is not None
            },
        }

    @Slot(str, float, float, "QVariantList", "QVariantMap")
    def addTitleText(self, text: str, start_us: float, end_us: float,
                     visual_box: list, options: dict) -> None:
        if not self.project_state.hasVideo:
            self._set_status("Hãy thêm video trước khi đặt tên phim/kênh")
            return
        start = max(0, int(round(start_us)))
        end = min(int(self.project_state.durationUs), int(round(end_us)))
        if not text.strip() or end - start < 100_000:
            self._set_status("Text cần nội dung và thời lượng ít nhất 100 ms trong video")
            return
        values = {**dict(options), "title_mode": True, "text": text.strip(),
                  "start_us": start, "end_us": end, "visual_box": list(visual_box),
                  "display_time": True}
        values.pop("cue_role", None)
        self._send_subtitle_command("add_title_text", values=values, needs_selection=False,
                                    status_context={"title_created": True})

    @Slot(str, float, result="QVariantMap")
    def manualSubtitleCueDefaults(self, cue_role: str, start_us: float) -> dict:
        """Return stable add-dialog defaults near the requested playhead."""

        role = "dialogue" if cue_role == "dialogue" else "scene_text"
        position = max(0, round(start_us)) if math.isfinite(start_us) else 0
        candidates: list[dict] = []
        for row in range(self.project_state.subtitle_cues.count):
            cue = self.project_state.subtitle_cues.get(row)
            if cue.get("cue_role", "dialogue") == role:
                candidates.append(cue)
        selected = self.project_state.subtitle_cues.find(self._selected_cue_id)
        template = (
            selected
            if role == "scene_text"
            and bool(selected)
            and selected.get("cue_role", "dialogue") == role
            else min(
                candidates,
                key=lambda cue: (
                    min(
                        abs(position - int(cue.get("start_us", 0))),
                        abs(position - int(cue.get("end_us", 0))),
                    ),
                    int(cue.get("index", 0)),
                ),
                default={},
            )
        )
        raw_box = template.get("panel_box") or template.get("visual_box")
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            raw_box = [0.10, 0.74, 0.80, 0.16] if role == "dialogue" else [0.35, 0.35, 0.30, 0.14]
        return {
            "cue_role": role,
            "visual_box": [float(value) for value in raw_box],
            "font_size": 40 if role == "scene_text" else int(template.get("text_font_size", 12)),
            "orientation": str(template.get("orientation", "horizontal")),
            "background_color": (
                SCENE_TEXT_DEFAULT_BACKGROUND_COLOR
                if role == "scene_text"
                else str(template.get("background_color", ""))
            ),
            "text_color": str(
                SCENE_TEXT_DEFAULT_TEXT_COLOR
                if role == "scene_text"
                else template.get("text_color", "") or self.exportSubtitlePrimaryColor
            ),
            "style_hint": (
                SCENE_TEXT_DEFAULT_STYLE_HINT
                if role == "scene_text"
                else str(template.get("style_hint", "standard"))
            ),
            "background_padding_x": int(template.get("background_padding_x", 20)),
            "background_padding_y": int(template.get("background_padding_y", 10)),
            "background_style": (
                SCENE_TEXT_DEFAULT_BACKGROUND_STYLE
                if role == "scene_text"
                else str(template.get("background_style", "solid"))
            ),
            "letter_spacing_percent": int(template.get("letter_spacing_percent", 0)),
            "font_bold": bool(template.get("font_bold", False)),
            "font_italic": bool(template.get("font_italic", False)),
            "text_opacity_percent": int(template.get("text_opacity_percent", 100)),
            "background_opacity_percent": int(
                SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT
                if role == "scene_text"
                else template.get("background_opacity_percent", 90)
            ),
            "fade_enabled": (
                SCENE_TEXT_DEFAULT_FADE_ENABLED
                if role == "scene_text"
                else bool(template.get("fade_enabled", False))
            ),
            "fade_duration_ms": int(
                template.get("fade_duration_ms", SCENE_TEXT_DEFAULT_FADE_DURATION_MS)
            ),
        }

    @Slot(str, float, float, "QVariantList", "QVariantMap")
    def addManualSubtitleCue(
        self,
        text: str,
        start_us: float,
        end_us: float,
        visual_box: list,
        options: dict,
    ) -> None:
        raw_box = list(visual_box) if isinstance(visual_box, list) else []
        if len(raw_box) != 4 or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in raw_box
        ):
            self._set_status("Vùng đặt chữ thủ công không hợp lệ")
            return
        raw_options = dict(options) if isinstance(options, dict) else {}
        cue_role = "dialogue" if raw_options.get("cue_role") == "dialogue" else "scene_text"
        values: dict[str, object] = {
            "text": text,
            "start_us": max(0, round(start_us)),
            "end_us": max(0, round(end_us)),
            "display_time": True,
            "visual_box": [float(value) for value in raw_box],
            "cue_role": cue_role,
            "font_size": max(1, int(raw_options.get("font_size", 12))),
            "orientation": "vertical"
            if raw_options.get("orientation") == "vertical"
            else "horizontal",
            "background_color": str(raw_options.get("background_color", "") or "").strip().upper(),
            "text_color": str(raw_options.get("text_color", "") or "").strip().upper(),
            "style_hint": str(raw_options.get("style_hint", "standard")),
            "track_id": str(raw_options.get("track_id", "")),
            "background_padding_x": max(
                0, int(raw_options.get("background_padding_x", 20))
            ),
            "background_padding_y": max(
                0, int(raw_options.get("background_padding_y", 10))
            ),
            "background_style": str(raw_options.get("background_style", "solid")),
            "letter_spacing_percent": max(
                0, int(raw_options.get("letter_spacing_percent", 0))
            ),
            "font_bold": bool(raw_options.get("font_bold", False)),
            "font_italic": bool(raw_options.get("font_italic", False)),
            "text_opacity_percent": max(
                0, min(100, int(raw_options.get("text_opacity_percent", 100)))
            ),
            "background_opacity_percent": max(
                0, min(100, int(raw_options.get("background_opacity_percent", 90)))
            ),
            "fade_enabled": bool(
                raw_options.get(
                    "fade_enabled",
                    SCENE_TEXT_DEFAULT_FADE_ENABLED if cue_role == "scene_text" else False,
                )
            )
            if cue_role == "scene_text"
            else False,
            "fade_duration_ms": max(
                50,
                min(
                    2_000,
                    int(raw_options.get("fade_duration_ms", SCENE_TEXT_DEFAULT_FADE_DURATION_MS)),
                ),
            ),
        }
        if self._subtitle_busy:
            if len(self._pending_manual_scene_text_commands) >= 32:
                self._set_status("Hàng chờ thêm chữ đã đầy; hãy chờ các thay đổi hiện tại lưu xong")
                return
            self._pending_manual_scene_text_commands.append(
                {"project_id": self.project_state.projectId, "values": values}
            )
            self._set_status(
                "Đã nhận cue mới; sẽ thêm ngay sau khi thay đổi phụ đề hiện tại lưu xong"
            )
            return
        self._dispatch_manual_scene_text(values)

    @Slot(str, float, float, float, float, float, float, int, str, str)
    def addManualSceneText(
        self,
        text: str,
        start_us: float,
        end_us: float,
        x: float,
        y: float,
        width: float,
        height: float,
        font_size: int,
        orientation: str,
        background_color: str,
    ) -> None:
        self.addManualSubtitleCue(
            text,
            start_us,
            end_us,
            [float(x), float(y), float(width), float(height)],
            {
                "cue_role": "scene_text",
                "font_size": font_size,
                "orientation": orientation,
                "background_color": background_color,
            },
        )

    def _dispatch_manual_scene_text(self, values: dict[str, object]) -> None:
        self._send_subtitle_command(
            "add_subtitle_cue",
            needs_selection=False,
            values=values,
            status_context={"manual_scene_text": True},
        )

    def _dispatch_pending_manual_scene_text(self) -> None:
        if self._subtitle_busy or not self.project_state.hasProject:
            return
        while self._pending_manual_scene_text_commands:
            pending = self._pending_manual_scene_text_commands.pop(0)
            if str(pending.get("project_id", "")) != self.project_state.projectId:
                continue
            values = pending.get("values")
            if isinstance(values, dict):
                self._dispatch_manual_scene_text(dict(values))
            return

    @Slot("QVariantList", "QVariantList")
    def setOcrRegions(self, dialogue_region: list, exclusion_regions: list) -> None:
        dialogue = list(dialogue_region) if dialogue_region else None
        exclusions = [list(item) for item in exclusion_regions if isinstance(item, list)]
        self._send_subtitle_command(
            "set_ocr_regions",
            needs_selection=False,
            values={
                "dialogue_region": dialogue,
                "exclusion_regions": exclusions,
            },
            status_context={"ocr_regions_updated": True},
        )

    @Slot()
    def addSubtitleCue(self) -> None:
        self._send_subtitle_command("add", cue_id=self._selected_cue_id or None)

    @Slot()
    def splitSelectedCue(self) -> None:
        cue = self.selectedCue
        if not cue:
            self._set_status("Hãy chọn cue cần tách")
            return
        start_us = int(cue.get("start_us", 0))
        end_us = int(cue.get("end_us", 0))
        split_us = self._playhead_us
        values = (
            {"split_us": split_us, "display_time": True}
            if start_us < split_us < end_us
            else {"display_time": True}
        )
        self._send_subtitle_command("split", cue_id=self._selected_cue_id, values=values)

    @Slot()
    def mergeSelectedCue(self) -> None:
        self._send_subtitle_command("merge_next", cue_id=self._selected_cue_id)

    @Slot(float)
    def shiftSelectedCue(self, milliseconds: float) -> None:
        if not math.isfinite(milliseconds):
            return
        self._send_subtitle_command(
            "shift",
            cue_id=self._selected_cue_id,
            values={
                "delta_us": int(round(milliseconds * 1_000)),
                "display_time": True,
            },
        )

    @Slot(bool)
    def autoBreakSubtitles(self, selected_only: bool = False) -> None:
        self._send_subtitle_command(
            "auto_break",
            cue_id=self._selected_cue_id if selected_only else None,
            needs_selection=selected_only,
        )

    @Slot(str, result="QVariantMap")
    def previewSubtitleReplacement(self, find: str) -> dict:
        summary = self.project_state.subtitle_cues.match_summary(
            find,
            language="translated",
        )
        if not find:
            self._set_status("Hãy nhập nội dung tiếng Việt cần tìm")
        elif summary["match_count"] == 0:
            self._set_status("Không tìm thấy nội dung này trong bản dịch Việt")
        else:
            self._set_status(
                f"Tìm thấy {summary['match_count']} lần trong {summary['cue_count']} cue Việt"
            )
        return summary

    @Slot(str, str, int)
    def findReplaceSubtitles(
        self,
        find: str,
        replacement: str,
        expected_count: int,
    ) -> None:
        optimistic_changes = self.project_state.subtitle_cues.optimistic_find_replace(
            find,
            replacement,
            language="translated",
        )
        if optimistic_changes:
            self._set_translation_qa(self._translation_qa)
        self._send_subtitle_command(
            "find_replace",
            language="translated",
            values={
                "find": find,
                "replacement": replacement,
                "expected_count": expected_count,
            },
            needs_selection=False,
            optimistic_changes=optimistic_changes,
            status_context={
                "replacement_count": expected_count,
                "replacement_cue_count": len(optimistic_changes),
            },
        )

    @Slot(str)
    def deleteSubtitleCue(self, cue_id: str) -> None:
        cue = self.project_state.subtitle_cues.find(cue_id)
        if not cue:
            self._set_status("Cue này không còn trong dự án; hãy chọn lại cue cần xóa")
            return
        if self._defer_until_subtitle_saves_flushed(
            "delete-cue", lambda: self.deleteSubtitleCue(cue_id),
            "Đang lưu chỉnh sửa trước khi xóa cue…",
        ):
            return
        self._send_subtitle_command(
            "delete",
            cue_id=cue_id,
            status_context={"deleted_cue_index": cue.get("index", 0)},
        )

    @Slot()
    def deleteTimelineSelection(self) -> None:
        """One deletion route for buttons, context menus and the Delete key."""
        clip = self.project_state.clip_info(self._selected_clip_id)
        if clip and self.selectedClipTrackKind == "video":
            self._request_video_edit("delete_video_segment", clip_id=self._selected_clip_id)
            return
        ids = list(self._selected_cue_ids)
        if not ids and clip and self.selectedClipTrackKind in {"subtitle", "text"}:
            cue_id = str(clip.get("cue_id") or "")
            if self.project_state.subtitle_cues.find(cue_id):
                ids = [cue_id]
        if ids:
            self._delete_timeline_cues(tuple(ids))
        elif clip:
            self.deleteSelected(True)
        else:
            self._set_status("Hãy chọn cue hoặc đoạn video cần xóa trên Timeline")

    def _delete_timeline_cues(self, ids: tuple[str, ...]) -> None:
        if self._defer_until_subtitle_saves_flushed(
            "delete-cues", lambda: self._delete_timeline_cues(ids),
            "Đang lưu chỉnh sửa trước khi xóa cue…",
        ):
            return
        if not all(self.project_state.subtitle_cues.find(cue_id) for cue_id in ids):
            self._set_status("Cue đã thay đổi; hãy chọn lại trước khi xóa")
            return
        self._send_subtitle_command("delete_cues", values={"cue_ids": list(ids)},
                                    needs_selection=False)

    @Slot()
    def deleteSelectedSubtitleCues(self) -> None:
        if self._selected_cue_ids:
            self._delete_timeline_cues(tuple(self._selected_cue_ids))
        else:
            self._set_status("Hãy chọn cue chữ trước khi xóa")

    @Slot(str)
    def acceptSceneTextCue(self, cue_id: str) -> None:
        self._send_subtitle_command(
            "accept_scene_text",
            cue_id=cue_id,
            status_context={"accepted_scene_text": True},
        )

    @Slot()
    def acceptSelectedSceneTextCues(self) -> None:
        cue_ids = [cue_id for cue_id in self._selected_cue_ids
                   if self.project_state.subtitle_cues.find(cue_id).get("cue_role")
                   == "scene_text"]
        if not cue_ids:
            self._set_status("Hãy chọn chữ trong cảnh cần chấp nhận")
            return
        if self._text_cue_ids_locked(cue_ids):
            self._set_status("Hãy mở khóa dòng chữ trước khi chấp nhận")
            return
        self._send_subtitle_command(
            "accept_scene_text", cue_id=cue_ids[-1], values={"cue_ids": cue_ids},
            status_context={"accepted_scene_text": True, "accepted_scene_text_count": len(cue_ids)},
        )

    @Slot(str, float)
    def previewSceneTextFontScale(self, cue_id: str, scale: float) -> None:
        if not math.isfinite(scale):
            return
        cue = self.project_state.subtitle_cues.find(cue_id)
        if cue.get("cue_role") != "scene_text":
            return
        if (
            self.project_state.subtitle_cues.optimistic_update_scene_font_scale(cue_id, scale)
            and cue_id == self._selected_cue_id
        ):
            self.selectedCueChanged.emit()

    @Slot(str, float, float)
    def setSceneTextFontScale(
        self,
        cue_id: str,
        scale: float,
        previous_scale: float,
    ) -> None:
        if not math.isfinite(scale) or not math.isfinite(previous_scale):
            return
        cue = self.project_state.subtitle_cues.find(cue_id)
        if cue.get("cue_role") != "scene_text":
            self._set_status("Chỉ cue chữ trong cảnh mới dùng thanh tỷ lệ kiểu cũ")
            return
        normalized = round(max(0.5, min(2.0, float(scale))), 2)
        previous = round(max(0.5, min(2.0, float(previous_scale))), 2)
        self.previewSceneTextFontScale(cue_id, normalized)
        if normalized == previous:
            return
        self._send_subtitle_command(
            "set_scene_text_font_scale",
            cue_id=cue_id,
            values={"scale": normalized},
            optimistic_changes=[
                {
                    "kind": "scene_font_scale",
                    "cue_id": cue_id,
                    "previous": previous,
                    "applied": normalized,
                }
            ],
            status_context={"scene_font_scale_percent": round(normalized * 100)},
        )

    @Slot(str, float, float)
    def previewSceneTextTiming(self, cue_id: str, start_us: float, end_us: float) -> None:
        if not math.isfinite(start_us) or not math.isfinite(end_us):
            return
        start = max(0, round(start_us))
        end = round(end_us)
        if end - start < 100_000:
            return
        cue = self.project_state.subtitle_cues.find(cue_id)
        if not cue:
            return
        if (
            self.project_state.subtitle_cues.preview_scene_text_timing(cue_id, start, end)
            and cue_id == self._selected_cue_id
        ):
            self.selectedCueChanged.emit()

    @Slot(float)
    def previewSelectedSceneTextMove(self, delta_us: float) -> None:
        """Preview one shared time delta for every selected subtitle cue."""

        if not math.isfinite(delta_us):
            return
        minimum_delta, maximum_delta = self._selected_scene_timing_bounds("move")
        delta = max(minimum_delta, min(maximum_delta, round(delta_us)))
        selected = set(self._selected_cue_ids)
        if not selected:
            return
        changed_ids = self.project_state.subtitle_cues.preview_scene_text_delta(
            selected,
            delta,
        )
        if self._selected_cue_id in changed_ids:
            self.selectedCueChanged.emit()

    @Slot(str, float)
    def previewSelectedSceneTextResize(self, edge: str, delta_us: float) -> None:
        if edge not in {"left", "right"} or not math.isfinite(delta_us):
            return
        selected = set(self._selected_cue_ids)
        if not selected:
            return
        minimum_delta, maximum_delta = self._selected_scene_timing_bounds(edge)
        delta = max(minimum_delta, min(maximum_delta, round(delta_us)))
        changed_ids = self.project_state.subtitle_cues.preview_scene_text_edge_delta(
            selected,
            edge,
            delta,
        )
        if self._selected_cue_id in changed_ids:
            self.selectedCueChanged.emit()

    @Slot(str, float, result=int)
    def resizeSelectedSceneTexts(self, edge: str, delta_us: float) -> int:
        cue_ids = list(self._selected_cue_ids)
        if not cue_ids:
            self._set_status("Hãy chọn cue phụ đề cần chỉnh thời gian")
            return 0
        if edge not in {"left", "right"} or not math.isfinite(delta_us):
            return 0
        minimum_delta, maximum_delta = self._selected_scene_timing_bounds(edge)
        applied_delta = max(
            minimum_delta,
            min(maximum_delta, int(round(delta_us))),
        )
        optimistic_changes: list[dict[str, object]] = []
        for cue_id in cue_ids:
            timing = self.project_state.subtitle_cues.scene_text_base_timing(cue_id)
            if timing is None:
                continue
            previous_start, previous_end = timing
            applied_start = previous_start + (applied_delta if edge == "left" else 0)
            applied_end = previous_end + (applied_delta if edge == "right" else 0)
            self.project_state.subtitle_cues.preview_scene_text_timing(
                cue_id,
                applied_start,
                applied_end,
            )
            optimistic_changes.append(
                {
                    "kind": "scene_timing",
                    "cue_id": cue_id,
                    "previous_start_us": previous_start,
                    "previous_end_us": previous_end,
                    "applied_start_us": applied_start,
                    "applied_end_us": applied_end,
                }
            )
        return self._send_subtitle_command(
            "resize_scene_texts",
            cue_id=cue_ids[-1],
            values={
                "cue_ids": cue_ids,
                "edge": edge,
                "delta_us": applied_delta,
                "display_time": True,
            },
            optimistic_changes=optimistic_changes,
            status_context={"resized_cue_count": len(cue_ids)},
        )

    @Slot(str, float, float, float, float)
    def setSceneTextTiming(
        self,
        cue_id: str,
        start_us: float,
        end_us: float,
        previous_start_us: float,
        previous_end_us: float,
    ) -> None:
        values = (start_us, end_us, previous_start_us, previous_end_us)
        if not all(math.isfinite(value) for value in values):
            return
        start = max(0, round(start_us))
        end = round(end_us)
        previous_start = max(0, round(previous_start_us))
        previous_end = round(previous_end_us)
        if end - start < 100_000:
            self.previewSceneTextTiming(cue_id, previous_start, previous_end)
            self._set_status("Thời lượng cue phải từ 100 ms trở lên")
            return
        cue = self.project_state.subtitle_cues.find(cue_id)
        if not cue:
            self._set_status("Không tìm thấy cue phụ đề cần chỉnh")
            return
        self.previewSceneTextTiming(cue_id, start, end)
        if start == previous_start and end == previous_end:
            return
        self._send_subtitle_command(
            "set_scene_text_timing",
            cue_id=cue_id,
            values={"start_us": start, "end_us": end, "display_time": True},
            optimistic_changes=[
                {
                    "kind": "scene_timing",
                    "cue_id": cue_id,
                    "previous_start_us": previous_start,
                    "previous_end_us": previous_end,
                    "applied_start_us": start,
                    "applied_end_us": end,
                }
            ],
            status_context={
                "scene_timing_start_us": start,
                "scene_timing_end_us": end,
            },
        )

    @Slot()
    def undoSubtitle(self) -> None:
        if self._defer_until_subtitle_saves_flushed(
            "undo-subtitle", self.undoSubtitle, "Đang lưu thay đổi trước khi hoàn tác…"
        ):
            return
        self._send_subtitle_command("undo", needs_selection=False)

    @Slot()
    def redoSubtitle(self) -> None:
        if self._defer_until_subtitle_saves_flushed(
            "redo-subtitle", self.redoSubtitle, "Đang lưu thay đổi trước khi làm lại…"
        ):
            return
        self._send_subtitle_command("redo", needs_selection=False)

    @Slot()
    def rescanSelectedCue(self) -> None:
        if self._ocr_busy or not self._selected_cue_id or not self.project_state.hasVideo:
            return
        if self._defer_until_subtitle_saves_flushed(
            "rescan-cue", self.rescanSelectedCue,
            "Đang lưu chỉnh sửa trước khi quét lại cue…", durable=True,
        ):
            return
        self._set_ocr_busy(True)
        self.runtime.request(
            "create_ocr_rescan_job",
            {
                "project_id": self.project_state.projectId,
                "cue_id": self._selected_cue_id,
                "sample_interval_us": 400_000,
                "engine_python": self.ocrEnginePython,
                "device": "gpu:0",
            },
        )
        self._set_status("Đang tạo tác vụ quét lại đúng vùng cue")

    @Slot(str)
    def navigate(self, workspace_id: str) -> None:
        if workspace_id != "settings" and not self.license_model.active:
            self._set_status("Chưa kích hoạt. Mở Cài đặt → Kích hoạt để sử dụng chức năng.")
            workspace_id = "settings"
        if workspace_id == "import":
            self.showImportDialog()
            return
        if workspace_id in {"effects", "mixer"}:
            self.openTimelineSettings()
            return
        for item in NAVIGATION:
            if item.workspace_id == workspace_id:
                if workspace_id == "import":
                    self.showImportDialog()
                    return
                if workspace_id == "multi" and self._backend_ready:
                    self.runtime.request("multi_list")
                if workspace_id == self._current_workspace:
                    return
                self._current_workspace = workspace_id
                self._current_index = self._screen_index(workspace_id)
                self.settings.setValue("ui/currentWorkspace", workspace_id)
                self.currentWorkspaceChanged.emit()
                self.currentIndexChanged.emit()
                self._set_status(f"Đã mở {item.title}")
                return

    @Slot()
    def openTimelineSettings(self) -> None:
        self.navigate("timeline")
        self.timelineSettingsRequested.emit()

    @Slot(str)
    def openProject(self, project_id: str) -> None:
        value = project_id.strip()
        if not self._backend_ready:
            self._set_status("Job Manager chưa sẵn sàng để mở dự án")
            return
        if not value:
            self._set_status("Dự án được chọn không hợp lệ")
            return
        if self._defer_until_subtitle_saves_flushed(
            "open-project",
            lambda: self.openProject(value),
            "Đang lưu project hiện tại trước khi chuyển dự án…",
            durable=True,
        ):
            return
        self.runtime.request("get_project", {"project_id": value})
        self._set_status("Đang mở dự án")

    @Slot(str, str)
    def deleteProject(self, project_id: str, project_name: str = "") -> None:
        value = project_id.strip()
        if not self._backend_ready or not value:
            self._set_status("Không thể xóa dự án lúc này")
            return
        if self._project_delete_busy:
            self._set_status(f"Đang xoá: {self._deleting_project_name or value}")
            return
        self._project_delete_busy = True
        self._deleting_project_id = value
        self._deleting_project_name = project_name.strip() or value
        self.projectDeleteChanged.emit()
        self.runtime.request(
            "delete_project",
            {"project_id": value},
            context={
                "project_id": value,
                "project_name": self._deleting_project_name,
            },
        )
        self._set_status(f"Đang xoá: {self._deleting_project_name}")

    def _set_job_history_busy(self, value: bool, *, job_id: str = "") -> None:
        next_id = job_id if value else ""
        if value == self._job_history_busy and next_id == self._deleting_job_id:
            return
        self._job_history_busy = value
        self._deleting_job_id = next_id
        self.jobHistoryBusyChanged.emit()

    @Slot(str)
    def deleteJobHistory(self, job_id: str) -> None:
        value = job_id.strip()
        if not self._backend_ready or not value:
            return
        if self._job_history_busy:
            self._set_status("Đang xoá lịch sử tác vụ; hãy chờ hoàn tất")
            return
        self._set_job_history_busy(True, job_id=value)
        self.runtime.request(
            "delete_job_history",
            {"job_id": value},
            context={"job_history_delete": True, "job_id": value},
        )
        self._set_status("Đang xoá lịch sử tác vụ đã kết thúc")

    @Slot()
    def clearSubtitleJobHistory(self) -> None:
        self.clearJobHistory("subtitle")

    @Slot(str)
    def clearJobHistory(self, scope: str) -> None:
        project_id = self.project_state.projectId
        if not self._backend_ready or not project_id:
            self._set_status("Hãy mở dự án trước khi xoá lịch sử")
            return
        if self._job_history_busy:
            self._set_status("Đang xoá lịch sử tác vụ; hãy chờ hoàn tất")
            return
        normalized = scope.strip().casefold()
        kinds_by_scope = {
            "subtitle": ["ocr", "translate"],
            "voice": ["tts"],
            "tts": ["tts"],
        }
        kinds = kinds_by_scope.get(normalized)
        if kinds is None:
            self._set_status("Nhóm lịch sử tác vụ không hợp lệ")
            return
        self._set_job_history_busy(True)
        self.runtime.request(
            "clear_project_job_history",
            {"project_id": project_id, "kinds": kinds},
            context={
                "job_history_clear": True,
                "project_id": project_id,
                "history_scope": normalized,
            },
        )
        self._set_status(
            "Đang hủy tác vụ dở và xoá toàn bộ lịch sử tạo giọng"
            if normalized in {"voice", "tts"}
            else "Đang hủy tác vụ dở và xoá toàn bộ lịch sử OCR/dịch"
        )

    @Slot(str)
    def selectTranslationCache(self, job_id: str) -> None:
        value = job_id.strip()
        if not self._backend_ready or not self.project_state.projectId or not value:
            return
        if self._translation_busy:
            self._set_status("Dịch đang chạy; hãy tạm dừng hoặc hủy trước khi đổi cache")
            return
        selected = next(
            (item for item in self._translation_caches if item.get("job_id") == value),
            None,
        )
        if selected is None:
            self._set_status("Cache không còn tương thích với phụ đề nguồn hiện tại")
            return
        self._translation_cache_selection_explicit = True
        if value != self._selected_translation_cache_id:
            self._selected_translation_cache_id = value
            self.translationCachesChanged.emit()
        # The preview request is asynchronous. Set the selected cache as the
        # resume target now so a quick click cannot retry a previous job.
        self._bind_translation_cache_record(selected)
        self.runtime.request(
            "get_translation_preview",
            {"project_id": self.project_state.projectId, "job_id": value},
            context={
                "project_id": self.project_state.projectId,
                "cache_selection": True,
                "cache_job_id": value,
            },
        )
        self._set_status(
            f"Đang nạp cache {selected.get('completed_cues', 0)}/{selected.get('cue_total', 0)} cue"
        )

    def _bind_translation_cache_record(self, cache: dict[str, object]) -> None:
        """Make a cache selection actionable before its preview arrives.

        Cache listing and preview are separate asynchronous requests.  If the
        preview is delayed (or the previous job left the busy flag set), the
        UI must still expose the selected ledger's exact state and resume
        target immediately instead of leaving the primary control disabled.
        """

        job_id = str(cache.get("job_id", "")).strip()
        if not job_id:
            return
        self._translation_job_id = job_id
        self._translation_provider_id = str(cache.get("provider_id", ""))
        self._translation_model_id = str(cache.get("model_id", ""))
        self._bind_translation_configuration(cache.get("provider_config"))
        display_state = str(cache.get("display_state", cache.get("state", "")))
        self._set_translation_job_state(display_state)
        self._set_translation_busy(
            display_state
            in {
                "queued",
                "start_requested",
                "running",
                "pause_requested",
                "paused",
                "resume_requested",
                "cancel_requested",
            }
        )

    @Slot()
    def showImportDialog(self) -> None:
        if not self._features_available():
            return
        if not self._backend_ready:
            self._set_status("Job Manager chưa sẵn sàng để nhập media")
            return
        if self._import_busy:
            self._set_status("Một file đang được nhập")
            return
        self.importDialogRequested.emit()

    @Slot()
    def showMultiImportDialog(self) -> None:
        if not self._backend_ready:
            self._set_status("Job Manager chưa sẵn sàng để nhập video vào Multi")
            return
        if self._import_busy:
            self._set_status("Một tác vụ nhập media đang được xử lý")
            return
        self.multiImportDialogRequested.emit()

    @Slot()
    def showNewProjectDialog(self) -> None:
        if not self._features_available():
            return
        if not self._backend_ready:
            self._set_status("Job Manager chưa sẵn sàng để tạo dự án")
            return
        if self._import_busy:
            self._set_status("Một file đang được nhập")
            return
        self.newProjectDialogRequested.emit()

    @Slot(str)
    def createProjectFromPath(self, raw_path: str) -> None:
        if not self._backend_ready or self._import_busy:
            return
        path = _local_path_from_ui(raw_path)
        if not path.is_file():
            self._set_status("File tạo dự án không tồn tại")
            return
        if self._defer_until_subtitle_saves_flushed(
            "new-project", lambda: self.createProjectFromPath(raw_path),
            "Đang lưu dự án trước khi tạo dự án mới…", durable=True,
        ):
            return
        self._set_import_busy(True)
        self._save_state = "Đang kiểm tra media"
        self.saveStateChanged.emit()
        self._inspect_media_before_import(path, intent="new_project")

    @Slot(str)
    def importPath(self, raw_path: str) -> None:
        if not self._backend_ready or self._import_busy:
            return
        path = _local_path_from_ui(raw_path)
        if not path.is_file():
            self._set_status("File nhập không tồn tại")
            return
        if self._defer_until_subtitle_saves_flushed(
            "import-media", lambda: self.importPath(raw_path),
            "Đang lưu dự án trước khi nhập media…", durable=True,
        ):
            return
        self._set_import_busy(True)
        self._save_state = "Đang kiểm tra media"
        self.saveStateChanged.emit()
        intent = "append" if self.project_state.hasProject else "new_project"
        self._inspect_media_before_import(path, intent=intent)

    @Slot("QVariantList")
    def importPaths(self, raw_paths: object) -> None:
        if not self._backend_ready or self._import_busy:
            return
        if not isinstance(raw_paths, (list, tuple)):
            self._set_status("Danh sách video nhập không hợp lệ")
            return
        paths: list[Path] = []
        seen: set[str] = set()
        video_suffixes = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
        for raw in raw_paths:
            if not isinstance(raw, str):
                continue
            path = _local_path_from_ui(raw)
            key = str(path).casefold()
            if key in seen:
                continue
            seen.add(key)
            if not path.is_file() or path.suffix.casefold() not in video_suffixes:
                self._set_status(
                    "Multi video chỉ nhận file video hợp lệ; audio/SRT hãy nhập từng file."
                )
                return
            paths.append(path)
        if len(paths) == 1:
            self.importPath(str(paths[0]))
            return
        if len(paths) < 2:
            self._set_status("Hãy chọn ít nhất hai video để mở Multi video")
            return
        self._begin_multi_import(paths)

    @Slot("QVariantList")
    def importMultiPaths(self, raw_paths: object) -> None:
        """Import one or many videos explicitly into the Multi catalog."""

        if not self._backend_ready or self._import_busy:
            return
        if not isinstance(raw_paths, (list, tuple)):
            self._set_status("Danh sách video nhập không hợp lệ")
            return
        paths: list[Path] = []
        seen: set[str] = set()
        video_suffixes = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
        for raw in raw_paths:
            if not isinstance(raw, str):
                continue
            path = _local_path_from_ui(raw)
            key = str(path).casefold()
            if key in seen:
                continue
            seen.add(key)
            if not path.is_file() or path.suffix.casefold() not in video_suffixes:
                self._set_status("Multi video chỉ nhận file video hợp lệ")
                return
            paths.append(path)
        if not paths:
            self._set_status("Hãy chọn ít nhất một video để nhập vào Multi video")
            return
        self._begin_multi_import(paths)

    def _begin_multi_import(self, paths: list[Path]) -> None:
        if self._defer_until_subtitle_saves_flushed(
            "import-multi", lambda: self._begin_multi_import(paths),
            "Đang lưu dự án trước khi nhập Multi video…", durable=True,
        ):
            return
        self._multi_import_paths = paths
        self._multi_import_project_ids = []
        self._set_import_busy(True)
        self._save_state = f"Đang nhập 0/{len(paths)} video"
        self.saveStateChanged.emit()
        self._create_next_multi_project()

    def _create_next_multi_project(self) -> None:
        if not self._multi_import_paths:
            project_ids = list(dict.fromkeys(self._multi_import_project_ids))
            if not project_ids:
                self._set_import_busy(False)
                self._set_status("Không có video nào được thêm vào Multi video")
                return
            self.runtime.request(
                "multi_add",
                {"project_ids": project_ids},
                context={"multi_import_complete": True, "count": len(project_ids)},
            )
            return
        path = self._multi_import_paths[0]
        project_root = self.runtime.database_path.parent / "projects"
        project_root.mkdir(parents=True, exist_ok=True)
        project_path = project_root / f"{path.stem[:80]}-{uuid4().hex[:8]}.ducai"
        self.runtime.request(
            "create_project_with_media",
            {
                "project_path": str(project_path),
                "name": path.stem,
                "media_path": str(path),
                "excluded_project_ids": list(self._multi_import_project_ids),
                **self._default_overlay_import_options(),
            },
            context={"multi_import_path": str(path)},
        )
        completed = len(self._multi_import_project_ids)
        total = completed + len(self._multi_import_paths)
        self._set_status(f"Đang tạo project Multi video {completed + 1}/{total}")

    def _finish_failed_multi_import(self, message: str, failed_path: str = "") -> None:
        """Commit successful imports before surfacing a later file failure."""

        completed_ids = list(dict.fromkeys(self._multi_import_project_ids))
        total = len(completed_ids) + len(self._multi_import_paths)
        self._multi_import_paths = []
        if completed_ids:
            self._save_state = f"Đang lưu {len(completed_ids)}/{total} video đã nhập"
            self.saveStateChanged.emit()
            self.runtime.request(
                "multi_add",
                {"project_ids": completed_ids},
                context={
                    "multi_import_complete": True,
                    "count": len(completed_ids),
                    "total": total,
                    "multi_import_partial_error": message,
                    "failed_path": failed_path,
                },
            )
            return
        self._multi_import_project_ids = []
        self._set_import_busy(False)
        self._save_state = "Nhập Multi video thất bại"
        self.saveStateChanged.emit()
        self._set_status("Không thể nhập danh sách Multi video: " + message)

    def _apply_multi_selection(self, values: dict[str, bool]) -> None:
        """Update visible checkboxes without waiting for the database round-trip."""

        if not values:
            return
        items: list[dict[str, object]] = []
        changed = False
        for item in self._multi_items:
            project_id = str(item.get("project_id", ""))
            if project_id not in values:
                items.append(item)
                continue
            selected = bool(values[project_id])
            if bool(item.get("selected", False)) == selected:
                items.append(item)
                continue
            items.append({**item, "selected": selected})
            changed = True
        if not changed:
            return
        self._multi_items = items
        self.multi_videos.merge(items)
        self.multiVideoChanged.emit()

    def _refresh_local_multi_translation_review(self) -> None:
        """Project an edited CJK count onto its Multi card immediately."""

        project_id = self.project_state.projectId
        if not project_id:
            return
        current = next(
            (item for item in self._multi_items if str(item.get("project_id", "")) == project_id),
            None,
        )
        if current is None or not (
            current.get("manual_review_available") is True
            or current.get("manual_review_ready") is True
            or str(current.get("error_code", "")) == "translation_review_required"
        ):
            return
        rows = [
            self.project_state.subtitle_cues.get(row)
            for row in range(self.project_state.subtitle_cues.count)
        ]
        if not rows:
            return
        cjk_ids = sorted(
            int(row.get("index", index))
            for index, row in enumerate(rows, start=1)
            if contains_cjk(str(row.get("translated_text", "")))
        )
        complete = all(
            str(row.get("translation_cue_id", "")).strip()
            and str(row.get("translated_text", "")).strip()
            for row in rows
        )
        ready = complete and not cjk_ids
        payload = (
            dict(current.get("error_payload", {}))
            if isinstance(current.get("error_payload"), dict)
            else {}
        )
        payload.update(
            {
                "cjk_cue_ids": cjk_ids,
                "cjk_blocker_count": len(cjk_ids),
                "manual_review_remaining_cjk": len(cjk_ids),
                "manual_review_ready": ready,
            }
        )
        update: dict[str, object] = {
            **current,
            "stage": "translate",
            "error_payload": payload,
            "manual_review_available": True,
            "manual_review_remaining_cjk": len(cjk_ids),
            "manual_review_ready": ready,
        }
        if ready:
            update.update(
                state="completed",
                progress=1.0,
                error_code="",
                project_status_override=True,
                project_status_state="completed",
                project_status_text="Đã sửa xong chữ Trung · sẵn sàng lồng tiếng",
                project_status_stage="tts",
                project_status_progress=1.0,
                project_error_message="",
            )
        else:
            update.update(
                state="failed",
                error_code="translation_review_required",
                project_status_override=False,
            )
        items = [
            update if str(item.get("project_id", "")) == project_id else item
            for item in self._multi_items
        ]
        if items == self._multi_items:
            return
        self._multi_items = items
        self.multi_videos.merge(items)
        self.multiVideoChanged.emit()

    @Slot(str, bool)
    def selectMultiVideo(self, project_id: str, selected: bool) -> None:
        if self.multiListLocked:
            self._set_status("Hãy tạm dừng Multi video và chờ checkpoint trước khi đổi lựa chọn.")
            return
        value = project_id.strip()
        item = next(
            (
                current
                for current in self._multi_items
                if str(current.get("project_id", "")) == value
            ),
            None,
        )
        if item is None:
            self._set_status("Video không còn trong danh sách Multi")
            return
        selected_value = bool(selected)
        previous = {value: bool(item.get("selected", False))}
        self._multi_selection_generation += 1
        generation = self._multi_selection_generation
        self._apply_multi_selection({value: selected_value})
        self.runtime.request(
            "multi_select",
            {"project_id": value, "selected": selected_value},
            context={
                "selection_generation": generation,
                "previous_selection": previous,
            },
        )

    @Slot(bool)
    def selectAllMultiVideos(self, selected: bool) -> None:
        if self.multiListLocked:
            self._set_status("Hãy tạm dừng Multi video và chờ checkpoint trước khi đổi lựa chọn.")
            return
        previous = {
            str(item.get("project_id", "")): bool(item.get("selected", False))
            for item in self._multi_items
        }
        self._multi_selection_generation += 1
        generation = self._multi_selection_generation
        selected_value = bool(selected)
        self._apply_multi_selection({project_id: selected_value for project_id in previous})
        self.runtime.request(
            "multi_select_all",
            {"selected": selected_value},
            context={
                "selection_generation": generation,
                "previous_selection": previous,
            },
        )

    @Slot(str)
    def removeMultiVideo(self, project_id: str) -> None:
        if self.multiListLocked:
            self._set_status("Hãy tạm dừng Multi video trước khi xóa video khỏi danh sách.")
            return
        value = project_id.strip()
        self.runtime.request(
            "multi_remove",
            {"project_id": value},
            context={"project_id": value},
        )

    @Slot("QVariantList")
    def reorderMultiVideos(self, raw_project_ids: object) -> None:
        if self.multiListLocked:
            self._set_status("Hãy tạm dừng Multi video trước khi đổi thứ tự.")
            return
        if not isinstance(raw_project_ids, (list, tuple)) or not all(
            isinstance(value, str) and value.strip() for value in raw_project_ids
        ):
            self._set_status("Thứ tự Multi video không hợp lệ")
            return
        self.runtime.request(
            "multi_reorder",
            {"project_ids": [value.strip() for value in raw_project_ids]},
        )

    @Slot(str)
    def openMultiVideoDetail(self, project_id: str) -> None:
        if self._defer_until_subtitle_saves_flushed(
            "multi-detail", lambda: self.openMultiVideoDetail(project_id),
            "Đang lưu chỉnh sửa trước khi mở video khác…",
            durable=True,
        ):
            return
        if self._multi_detail_busy:
            self._set_status("Chỉ có thể mở chỉnh sửa chi tiết một video mỗi lần.")
            return
        value = project_id.strip()
        if not value or not self._backend_ready:
            if not self._backend_ready:
                self._set_status("Job Manager chưa sẵn sàng để mở video vào Timeline.")
            return
        item = self.multi_videos.item(value)
        review_job_id = str(item.get("child_job_id", ""))
        is_translation_review = (
            str(item.get("stage", "")) == "translate"
            and str(item.get("error_code", "")) == "translation_review_required"
        )
        target_screen = "translate" if is_translation_review else "timeline"
        self._multi_detail_generation += 1
        generation = self._multi_detail_generation
        self._pending_multi_detail_project_id = value
        self._multi_detail_busy = True
        self.multiVideoChanged.emit()
        self.navigate(target_screen)
        try:
            self.runtime.request(
                "get_project",
                {"project_id": value},
                context={
                    "multi_detail": True,
                    "project_id": value,
                    "generation": generation,
                    "multi_detail_target": target_screen,
                    "translation_review_job_id": review_job_id if is_translation_review else "",
                },
            )
        except Exception as exc:
            self._multi_detail_generation += 1
            self._finish_multi_detail()
            self.navigate("multi")
            self._set_status("Không thể mở video vào màn chỉnh sửa: " + str(exc))
            return
        self._set_status(
            "Đang mở video vào Dịch để sửa các cue còn chữ Trung"
            if is_translation_review
            else "Đang mở video vào Timeline để chỉnh sửa chi tiết"
        )
        QTimer.singleShot(
            30_000,
            lambda current_generation=generation: self._multi_detail_timeout(current_generation),
        )

    def _multi_detail_context_is_current(self, context: object) -> bool:
        return bool(
            isinstance(context, dict)
            and context.get("multi_detail") is True
            and int(context.get("generation", -1)) == self._multi_detail_generation
            and str(context.get("project_id", "")) == self._pending_multi_detail_project_id
        )

    def _finish_multi_detail(self) -> None:
        if not self._multi_detail_busy and not self._pending_multi_detail_project_id:
            return
        self._multi_detail_busy = False
        self._pending_multi_detail_project_id = ""
        self.multiVideoChanged.emit()

    def _begin_multi_control(self, action: str) -> bool:
        """Reserve one Multi control request and remember a rollback state."""

        if self._multi_control_snapshot is not None:
            self._set_status("Đang cập nhật Multi video; hãy đợi phản hồi gần nhất.")
            return False
        self._multi_control_snapshot = {
            "action": action,
            "batch": dict(self._multi_batch),
            "items": [dict(item) for item in self._multi_items],
        }
        return True

    def _finish_multi_control(self, action: str) -> None:
        snapshot = self._multi_control_snapshot
        if isinstance(snapshot, dict) and snapshot.get("action") == action:
            self._multi_control_snapshot = None

    def _rollback_multi_control(self, action: str) -> None:
        snapshot = self._multi_control_snapshot
        if not isinstance(snapshot, dict) or snapshot.get("action") != action:
            return
        self._multi_control_snapshot = None
        self._replace_multi_videos(
            {
                "batch": snapshot.get("batch", {"state": "idle"}),
                "items": snapshot.get("items", []),
                "partial": False,
            }
        )

    def _optimistic_multi_batch_state(
        self,
        state: str,
        *,
        retry: bool = False,
        fresh_start: bool = False,
    ) -> None:
        """Reflect a control click before the IPC round trip completes."""

        batch = {**self._multi_batch, "state": state}
        items: list[dict[str, object]] = []
        for raw in self._multi_items:
            item = dict(raw)
            item["batch_state"] = state
            if retry and item.get("selected") is True:
                # A completed card is already valid; only retry cards that did
                # not finish in the previous pass.  Manual-ready translation
                # cards jump directly to TTS in the optimistic projection.
                if item.get("manual_review_ready") is True or item.get("state") != "completed":
                    if item.get("manual_review_ready") is True:
                        item["stage"] = "tts"
                    item["state"] = "waiting"
                    item["progress"] = {
                        "ocr": 0.0,
                        "translate": 1.0 / 3.0,
                        "tts": 2.0 / 3.0,
                    }.get(str(item.get("stage", "ocr")), 0.0)
                    item["error_code"] = ""
                    item["error_payload"] = {}
            elif fresh_start and item.get("selected") is True:
                item.update(
                    {
                        "stage": "ocr",
                        "state": "waiting",
                        "progress": 0.0,
                        "child_job_id": "",
                        "retry_count": 0,
                        "final_pass": False,
                        "error_code": "",
                        "error_payload": {},
                    }
                )
            items.append(item)
        self._replace_multi_videos({"batch": batch, "items": items})

    def _multi_detail_timeout(self, generation: int) -> None:
        if generation != self._multi_detail_generation or not self._multi_detail_busy:
            return
        self._multi_detail_generation += 1
        self._finish_multi_detail()
        self.navigate("multi")
        self._set_status(
            "Mở video vào Timeline quá thời gian chờ; project hiện tại chưa bị thay đổi."
        )

    @Slot(str, bool)
    def requestMultiVideoPreview(self, project_id: str, use_voice: bool = False) -> None:
        value = project_id.strip()
        if not self._backend_ready or not value:
            return
        self.runtime.request(
            "multi_preview_descriptor",
            {"project_id": value},
            context={"use_voice": bool(use_voice)},
        )

    @Slot()
    def startMultiVideo(self) -> None:
        if not self._backend_ready or self.multiSelectedCount <= 0:
            self._set_status("Hãy chọn ít nhất một video để chạy Multi video")
            return
        if self._defer_until_subtitle_saves_flushed(
            "start-multi", self.startMultiVideo,
            "Đang lưu chỉnh sửa trước khi chạy Multi video…", durable=True,
        ):
            return
        try:
            settings = self._multi_settings_snapshot()
        except ValueError as exc:
            self._set_status(str(exc))
            return
        if not self._begin_multi_control("multi_start"):
            return
        retry = self.multiBatchState == "completed_with_errors"
        self._optimistic_multi_batch_state(
            "running",
            retry=retry,
            fresh_start=not retry,
        )
        try:
            self.runtime.request("multi_start", {"settings": settings})
        except Exception:
            self._rollback_multi_control("multi_start")
            raise
        self._set_status(
            "Đang chuẩn bị chạy lại các video lỗi từ stage hiện tại…"
            if retry
            else "Đang kiểm tra cấu hình và bắt đầu Multi video"
        )

    @Slot()
    def pauseMultiVideo(self) -> None:
        if self.multiBatchState != "running":
            return
        if not self._begin_multi_control("multi_pause"):
            return
        self._optimistic_multi_batch_state("pause_requested")
        try:
            self.runtime.request("multi_pause")
        except Exception:
            self._rollback_multi_control("multi_pause")
            raise
        self._set_status(
            "Đã nhận lệnh tạm dừng; đang chốt checkpoint hoặc dừng worker trong vài giây…"
        )

    @Slot()
    def resumeMultiVideo(self) -> None:
        if self.multiBatchState not in {
            "paused",
            "interrupted",
        }:
            return
        if self._defer_until_subtitle_saves_flushed(
            "resume-multi", self.resumeMultiVideo,
            "Đang lưu chỉnh sửa trước khi tiếp tục Multi video…", durable=True,
        ):
            return
        if not self._begin_multi_control("multi_resume"):
            return
        self._optimistic_multi_batch_state("running")
        try:
            self.runtime.request("multi_resume")
        except Exception:
            self._rollback_multi_control("multi_resume")
            raise
        self._set_status("Đang tiếp tục Multi video từ checkpoint/cache")

    @Slot()
    def restoreHiddenMultiVideos(self) -> None:
        if self.multiListLocked:
            self._set_status("Hãy tạm dừng Multi video trước khi khôi phục video đã ẩn.")
            return
        self.runtime.request("multi_restore_hidden")

    def _multi_settings_snapshot(self) -> dict[str, object]:
        whisper_model = (
            whisper_model_path() if self._ocr_extraction_method == "whisper_audio" else None
        )
        if self._ocr_extraction_method == "whisper_audio":
            if not whisper_runtime_is_ready():
                raise ValueError(
                    "Multi Whisper chưa thể chạy vì thiếu runtime. "
                    "Hãy chạy scripts/setup-whisper-runtime.ps1."
                )
            if whisper_model is None:
                raise ValueError(
                    "Multi Whisper chưa thể chạy vì thiếu model large-v3. "
                    "Hãy chạy scripts/setup-whisper-runtime.ps1."
                )
        provider_id = (
            self.settings.value("translation/provider", "gemini-web", type=str).strip().casefold()
        )
        default_models = {
            "gemini-web": "selected-on-web",
            "gemini-api": "gemini-3.5-flash-lite",
            "gemini-cli": "gemini-2.5-flash",
            "deepseek-api": "deepseek-v4-flash",
            "tokenhub-api": "hy3",
            "qwen-api": "qwen3.7-flash",
            "glm-api": "glm-5.2",
            "openai-compatible": "",
        }
        model_id = self.settings.value(
            "translation/model",
            default_models.get(provider_id, ""),
            type=str,
        ).strip()
        if not model_id:
            raise ValueError("Hãy chọn model dịch trước khi chạy Multi video")
        translation_options: dict[str, object] = {
            "banned_latin_terms": ["Daozhang", "Renzong", "yamen"],
            "qa_mode": "integrated",
            "qa_non_blocking": True,
            "qa_policy": "multi_auto",
            "cjk_is_blocker": True,
            "auto_compact_overlong_once": True,
            "max_overlong_compaction_passes": 1,
        }
        if provider_id == "gemini-web":
            # Multi is an unattended queue.  A visible browser would steal
            # focus for every project and can leave the batch waiting behind a
            # window, so its immutable snapshot always uses background Web.
            translation_options.update(self._gemini_web_options(headed=False, guest=True))
        elif provider_id in {"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS}:
            references = self._translation_api_references(provider_id)
            if not references:
                raise ValueError(
                    "Hãy lưu API key cho nguồn dịch đã chọn trước khi chạy Multi video"
                )
            translation_options["credential_references"] = references
            if provider_id == "openai-compatible":
                base_url = self.settings.value(
                    "translation/openaiCompatibleBaseUrl", "", type=str
                ).strip()
                if not base_url:
                    raise ValueError("Hãy nhập endpoint OpenAI-compatible trước khi chạy Multi")
                translation_options["base_url"] = base_url
        else:
            translation_options["cli_executable"] = self.settings.value(
                "translation/geminiCliExecutable", "gemini", type=str
            )
        selected_voice = self.voice_catalog.find(self._selected_voice_id)
        if not selected_voice or selected_voice.get("installed") is not True:
            raise ValueError("Hãy chọn một giọng đã cài trước khi chạy Multi video")
        tts_provider = str(selected_voice.get("provider_id", ""))
        tts_options: dict[str, object] = {
            "purpose": "production",
            "batch_size": 32 if tts_provider == "vieneu-3.8" else 16,
            "adaptive_timing": True,
            "voice_read_speed": self._voice_read_speed,
            "max_absolute_speed": max(self._voice_read_speed, self._voice_cue_max_speed),
            "qa_non_blocking": True,
            "defer_timing_conform": False,
        }
        if tts_provider == "vieneu-3.8" and selected_voice.get("is_clone") is True:
            if self.vieneuReferenceAudio:
                tts_options.update(
                    {
                        "reference_audio_path": self.vieneuReferenceAudio,
                        "reference_text": self.vieneuReferenceText,
                        "reference_denoise": self.vieneuReferenceDenoise,
                    }
                )
            else:
                profile_path = selected_voice.get("profile_path")
                if isinstance(profile_path, str) and profile_path:
                    tts_options["profile_path"] = profile_path
        return {
            "ocr": {
                # Keep Multi on the exact sampling rate selected for the
                # single-video OCR screen instead of silently falling back to
                # a hard-coded 1.25 FPS.
                "sample_interval_us": int(round(1_000_000 / self._ocr_sample_fps)),
                "scene_text_enabled": self._ocr_scene_text_enabled,
                "engine_python": (
                    str(whisper_runtime_python())
                    if self._ocr_extraction_method == "whisper_audio"
                    else self.ocrEnginePython
                ),
                "device": ("auto" if self._ocr_extraction_method == "whisper_audio" else "gpu:0"),
                "extraction_method": self._ocr_extraction_method,
                "language": "zh",
                "model_id": WHISPER_MODEL_ID,
                "model_path": (str(whisper_model) if whisper_model is not None else None),
            },
            "translation": {
                "provider_id": provider_id,
                "model_id": model_id,
                "provider_options": translation_options,
            },
            "tts": {
                "provider_id": tts_provider,
                "voice_id": self._selected_voice_id,
                "provider_options": tts_options,
            },
        }

    def _guard_manual_parallel(self, action: str, arguments: tuple[object, ...]) -> bool:
        if self._parallel_warning_approved_once:
            self._parallel_warning_approved_once = False
            return False
        if self.multiBatchState not in {"running", "pause_requested"}:
            return False
        current_project = str(self._multi_batch.get("current_project_id", ""))
        if current_project and current_project == self.project_state.projectId:
            current_item = next(
                (
                    item
                    for item in self._multi_items
                    if str(item.get("project_id", "")) == current_project
                ),
                {},
            )
            if str(current_item.get("stage", "")) == action:
                self._set_status(
                    "Project này đang được Multi video xử lý đúng bước đó; "
                    "không thể khởi chạy tác vụ trùng."
                )
                return True
        self._pending_parallel_action = (action, arguments)
        self.parallelJobWarningRequested.emit(
            "Multi video đang hoạt động. Tác vụ thủ công có thể tranh CPU, GPU, "
            "VRAM và mạng; tốc độ của cả hai có thể giảm."
        )
        return True

    @Slot(bool)
    def resolveParallelJobWarning(self, run_anyway: bool) -> None:
        pending = self._pending_parallel_action
        self._pending_parallel_action = None
        if not run_anyway or pending is None:
            self._set_status("Đã quay lại; không khởi chạy tác vụ song song")
            return
        action, arguments = pending
        self._parallel_warning_approved_once = True
        if action == "ocr":
            self.startOcr(float(arguments[0]))
        elif action == "translate":
            self.startTranslation(*arguments)
        elif action == "tts":
            purpose = str(arguments[0])
            force_ids = arguments[1]
            self._start_voice_job(
                purpose,
                force_cue_ids=(list(force_ids) if isinstance(force_ids, list) else None),
            )

    def _inspect_media_before_import(self, path: Path, *, intent: str) -> None:
        self.runtime.request(
            "inspect_media_usage",
            {"media_path": str(path)},
            context={"media_path": str(path), "intent": intent},
        )
        self._set_status("Đang kiểm tra media đã có trong dự án nào")

    def _continue_media_import(
        self,
        path: Path,
        *,
        intent: str,
        project_name: str | None = None,
    ) -> None:
        if intent == "new_project" or not self.project_state.hasProject:
            self._request_project_creation(path, project_name=project_name)
            return
        self.runtime.request(
            "import_media",
            {
                "project_id": self.project_state.projectId,
                "media_path": str(path),
                **self._default_overlay_import_options(),
            },
            context={"media_path": str(path)},
        )
        self._set_status("Đang đọc thông tin media")

    def _request_project_creation(self, path: Path, *, project_name: str | None = None) -> None:
        project_root = self.runtime.database_path.parent / "projects"
        project_root.mkdir(parents=True, exist_ok=True)
        project_path = project_root / f"{path.stem[:80]}-{uuid4().hex[:8]}.ducai"
        name = (project_name or path.stem).strip()
        if not name:
            self._set_import_busy(False)
            self._set_status("Tên dự án mới không được để trống")
            return
        self.runtime.request(
            "create_project",
            {"project_path": str(project_path), "name": name},
            context={
                "media_path": str(path),
                **self._default_overlay_import_options(),
            },
        )
        self._set_status("Đã tạo project mới, đang đọc media")

    @Slot(str, str)
    def resolveDuplicateMedia(self, action: str, project_name: str = "") -> None:
        if not self._pending_media_path or not self._pending_media_intent:
            return
        path = Path(self._pending_media_path)
        intent = self._pending_media_intent
        usages = self._duplicate_media_info.get("usages", [])
        if action == "use_existing":
            project_id = ""
            if isinstance(usages, list) and usages and isinstance(usages[0], dict):
                value = usages[0].get("project_id", "")
                project_id = value if isinstance(value, str) else ""
            self._clear_duplicate_media(keep_busy=True)
            if not project_id:
                self._set_import_busy(False)
                self._set_status("Không tìm thấy dự án đang chứa media")
                return
            if project_id == self.project_state.projectId:
                self._set_import_busy(False)
                self._save_state = "Đã lưu"
                self.saveStateChanged.emit()
                self._set_status("Media này đã nằm trong dự án hiện tại; không có gì bị thay đổi")
                return
            self.runtime.request("get_project", {"project_id": project_id})
            self._save_state = "Đang mở dự án đã có"
            self.saveStateChanged.emit()
            self._set_status("Đang dùng lại dự án đã chứa media")
            return
        if action == "create_copy":
            name = project_name.strip()
            if intent == "new_project" and not name:
                self._set_status("Hãy nhập tên cho dự án mới")
                return
            self._clear_duplicate_media(keep_busy=True)
            self._continue_media_import(
                path,
                intent=intent,
                project_name=name if intent == "new_project" else None,
            )
            return
        self.cancelDuplicateMediaResolution()

    @Slot()
    def cancelDuplicateMediaResolution(self) -> None:
        if not self._pending_media_path and not self._duplicate_media_info:
            return
        self._clear_duplicate_media(keep_busy=False)
        self._save_state = "Không có thay đổi"
        self.saveStateChanged.emit()
        self._set_status("Đã đóng thông báo media trùng; dự án không thay đổi")

    def _clear_duplicate_media(self, *, keep_busy: bool) -> None:
        self._duplicate_media_info = {}
        self._pending_media_path = ""
        self._pending_media_intent = ""
        self.duplicateMediaChanged.emit()
        if not keep_busy:
            self._set_import_busy(False)

    @Slot(str)
    def selectClip(self, clip_id: str) -> None:
        value = clip_id if self.project_state.clip_info(clip_id) is not None else ""
        track_selection_changed = bool(self._selected_track_id or self._selected_track_ids)
        clip_selection_changed = value != self._selected_clip_id
        if not track_selection_changed and not clip_selection_changed:
            return
        self._selected_clip_id = value
        self._selected_track_id = ""
        self._selected_track_ids.clear()
        if (value and self.selectedClipTrackKind not in {"subtitle", "text"}
                and (self._selected_cue_id or self._selected_cue_ids)):
            self._selected_cue_id = ""
            self._selected_cue_ids = []
            self.selectedCueChanged.emit()
        if clip_selection_changed:
            self.selectedClipChanged.emit()
        if track_selection_changed:
            self.selectedTrackChanged.emit()

    @Slot(str, bool)
    def selectTimelineClip(self, clip_id: str, additive: bool = False) -> None:
        self.selectClip(clip_id)
        clip = self.project_state.clip_info(clip_id)
        if clip and self.selectedClipTrackKind in {"subtitle", "text"}:
            self.selectTimelineCue(str(clip.get("cue_id") or ""), additive)

    @Slot(str)
    def selectTrack(self, track_id: str) -> None:
        value = track_id if self.project_state.track_is_persisted(track_id) else ""
        next_ids = {value} if value else set()
        track_selection_changed = (
            value != self._selected_track_id or self._selected_track_ids != next_ids
        )
        clip_selection_changed = bool(self._selected_clip_id)
        if not track_selection_changed and not clip_selection_changed:
            return
        self._selected_clip_id = ""
        self._selected_track_id = value
        self._selected_track_ids = next_ids
        if clip_selection_changed:
            self.selectedClipChanged.emit()
        if track_selection_changed:
            self.selectedTrackChanged.emit()

    def _clear_clip_for_track_selection(self) -> None:
        if not self._selected_clip_id:
            return
        self._selected_clip_id = ""
        self.selectedClipChanged.emit()

    @Slot(str)
    def toggleTrackSelection(self, track_id: str) -> None:
        if not self.project_state.track_is_persisted(track_id):
            return
        self._clear_clip_for_track_selection()
        if track_id in self._selected_track_ids:
            self._selected_track_ids.remove(track_id)
        else:
            self._selected_track_ids.add(track_id)
        self._selected_track_id = (
            track_id
            if track_id in self._selected_track_ids
            else (next(iter(self._selected_track_ids), ""))
        )
        self.selectedTrackChanged.emit()

    @Slot()
    def selectAllTracks(self) -> None:
        self._clear_clip_for_track_selection()
        ids = {
            str(item.get("id"))
            for item in self.project_state._track_by_id.values()
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and (item.get("kind") != "text" or item.get("clips"))
        }
        if ids != self._selected_track_ids:
            self._selected_track_ids = ids
            self._selected_track_id = next(iter(ids), "")
            self.selectedTrackChanged.emit()

    @Slot(float)
    def setPlayheadMs(self, milliseconds: float) -> None:
        value = max(0, int(milliseconds * 1_000))
        if value == self._playhead_us:
            return
        self._playhead_us = value
        self.playheadChanged.emit()

    @Slot()
    def addTimelineMarker(self) -> None:
        """Add one display-time bookmark at the current playhead."""

        if self._timeline_busy or not self.project_state.hasProject:
            return
        duration = max(0, int(self.project_state.durationUs))
        position = max(0, min(self._playhead_us, duration))
        markers = set(int(value) for value in self.project_state.timelineMarkers)
        if position in markers:
            self._set_status("Đã có mark tại vị trí này")
            return
        markers.add(position)
        self._send_timeline_command(
            "set_markers",
            value={"markers": sorted(markers)},
            needs_selection=False,
        )
        self._set_status("Đang lưu mark Timeline")

    @Slot()
    def removeTimelineMarkerAtPlayhead(self) -> None:
        """Remove the closest bookmark when it is close to the playhead."""

        if self._timeline_busy or not self.project_state.hasProject:
            return
        markers = list(self.project_state.timelineMarkers)
        if not markers:
            self._set_status("Timeline chưa có mark")
            return
        closest = min(markers, key=lambda value: abs(value - self._playhead_us))
        tolerance = max(500_000, self._timeline_seek_step_ms * 5_000)
        if abs(closest - self._playhead_us) > tolerance:
            self._set_status("Đưa playhead gần mark rồi thử xoá lại")
            return
        markers.remove(closest)
        self._send_timeline_command(
            "set_markers",
            value={"markers": markers},
            needs_selection=False,
        )
        self._set_status("Đang xoá mark Timeline")

    @Slot(float)
    def removeTimelineMarker(self, marker_us: float) -> None:
        """Remove a specific marker from its non-blocking Timeline handle."""

        if self._timeline_busy or not self.project_state.hasProject:
            return
        if not math.isfinite(marker_us):
            return
        target = int(round(marker_us))
        markers = list(self.project_state.timelineMarkers)
        if target not in markers:
            return
        markers.remove(target)
        self._send_timeline_command(
            "set_markers",
            value={"markers": markers},
            needs_selection=False,
        )
        self._set_status("Đang xoá mark Timeline")

    @Property(int, notify=previewMasterVolumeChanged)
    def previewMasterVolumePercent(self) -> int:
        return self._preview_master_volume_percent

    @Slot(int)
    def setPreviewMasterVolumePercent(self, percent: int) -> None:
        value = max(0, min(100, int(percent)))
        if value == self._preview_master_volume_percent:
            return
        self._preview_master_volume_percent = value
        self.settings.setValue("preview/masterVolumePercent", value)
        self.previewMasterVolumeChanged.emit()

    @Slot(bool)
    def splitSelected(self, apply_linked: bool = True) -> None:
        self._send_timeline_command("split", apply_linked=apply_linked, use_playhead=True)

    @Slot()
    def splitVideoAtPlayhead(self) -> None:
        self._request_video_edit("split_video", clip_id=(self._selected_clip_id
                                 if self.selectedClipTrackKind == "video" else ""))

    @Slot()
    def copySelectedVideo(self) -> None:
        if self.selectedClipTrackKind != "video" or self._timeline_busy:
            return
        self._copy_video(self.project_state.projectId, self._selected_clip_id)

    def _copy_video(self, project_id: str, clip_id: str) -> None:
        if self.project_state.projectId != project_id or not self.project_state.clip_info(clip_id):
            self._set_status("Đoạn video cần sao chép đã thay đổi; hãy chọn lại")
            return
        if self._defer_until_subtitle_saves_flushed("video-copy",
                                                  lambda: self._copy_video(project_id, clip_id),
                                                  "Đang lưu trước khi sao chép video…"):
            return
        try:
            self.runtime.request("copy_video_clip", {
                "project_id": project_id, "clip_id": clip_id})
        except Exception as exc:
            self._set_status("Không thể sao chép video: " + str(exc))

    @Slot()
    def pasteVideoAtPlayhead(self) -> None:
        if not self.canPasteVideo:
            self._set_status("Hãy sao chép video trong dự án này trước khi dán")
            return
        self._request_video_edit("paste_video")

    @Slot()
    def duplicateSelectedVideo(self) -> None:
        if self.selectedClipTrackKind == "video":
            self._request_video_edit("duplicate_video", clip_id=self._selected_clip_id)

    @Slot()
    def toggleSelectedVideoMuted(self) -> None:
        if self.selectedClipTrackKind == "video":
            self._request_video_edit("clip_mute", clip_id=self._selected_clip_id,
                                     value=not self.selectedClipMuted)

    def _request_video_edit(self, command: str, *, clip_id: str = "", value=None,
                            timeline_us: int | None = None, project_id: str | None = None) -> None:
        if project_id is not None and project_id != self.project_state.projectId:
            return
        project_id = self.project_state.projectId
        requested_us = self._playhead_us if timeline_us is None else timeline_us
        if self._timeline_busy or not self.project_state.hasProject:
            self._set_status("Hãy chờ thao tác Timeline hiện tại hoàn tất")
            return
        if self._defer_until_subtitle_saves_flushed(
            "video-edit", lambda: self._request_video_edit(command, clip_id=clip_id, value=value,
                                                           timeline_us=requested_us,
                                                           project_id=project_id),
            "Đang lưu phụ đề trước khi chỉnh đoạn video…",
        ):
            return
        join_us = requested_us
        if command == "delete_video_segment":
            clip = self.project_state.clip_info(clip_id)
            if not clip:
                self._set_status("Đoạn video không còn trong dự án; hãy chọn lại")
                return
            join_us = int(clip["timeline_range"]["start_us"])
        request_id = uuid4().hex
        context = {
            "timeline_cut": True, "project_id": self.project_state.projectId,
            "request_id": request_id, "operation": command,
            "join_us": join_us, "previous_playhead_us": self._playhead_us,
        }
        params = {
            "project_id": self.project_state.projectId, "command": command,
            "base_revision": self.project_state.revision, "request_id": request_id,
            "timeline_us": requested_us,
        }
        if clip_id:
            params["clip_id"] = clip_id
        if value is not None:
            params["value"] = value
        self._active_timeline_cut_request_id = request_id
        self._timeline_cut_context = context
        self._set_timeline_busy(True)
        self._timeline_cut_timeout_timer.start()
        try:
            self.runtime.request("timeline_command", params, context=context)
        except Exception as exc:
            self._finish_timeline_cut(False, context)
            self._set_status("Không thể gửi thao tác video: " + str(exc))
            return
        self._set_status({"split_video": "Đang tách tại playhead…",
                          "delete_video_segment": "Đang xóa đoạn video…",
                          "paste_video": "Đang dán video tại playhead…",
                          "duplicate_video": "Đang nhân bản video…",
                          "clip_mute": "Đang đổi trạng thái âm thanh…"}.get(
                              command, "Đang lưu video…"))

    @Slot(bool)
    def trimSelectedHead(self, apply_linked: bool = True) -> None:
        self._send_timeline_command("trim_head", apply_linked=apply_linked, use_playhead=True)

    @Slot(bool)
    def trimSelectedTail(self, apply_linked: bool = True) -> None:
        self._send_timeline_command("trim_tail", apply_linked=apply_linked, use_playhead=True)

    @Slot(str, float, float)
    def applyTimelineCut(self, operation: str, start_us: float, end_us: float) -> None:
        if operation not in {"keep_from", "keep_until", "keep_range", "remove_range"}:
            self._set_status("Thao tác cắt video không hợp lệ")
            return
        if not math.isfinite(start_us) or not math.isfinite(end_us):
            self._set_status("Mốc A/B không hợp lệ")
            return
        start = int(round(start_us))
        end = int(round(end_us))
        duration = int(self.project_state.durationUs)
        if (
            self._timeline_busy
            or not self.project_state.hasProject
            or start < 0
            or end <= start
            or end > duration
        ):
            if not self._timeline_busy:
                self._set_status("Mốc A/B phải nằm trong thời lượng video và A phải nhỏ hơn B")
            return
        if self._defer_until_subtitle_saves_flushed(
            "timeline-cut",
            lambda: self.applyTimelineCut(operation, start, end),
            "Đang lưu phụ đề trước khi cắt video…",
        ):
            return
        request_id = uuid4().hex
        base_revision = self.project_state.revision
        context: dict[str, object] = {
            "timeline_cut": True,
            "project_id": self.project_state.projectId,
            "request_id": request_id,
            "operation": operation,
            "range_start_us": start,
            "range_end_us": end,
            "previous_playhead_us": self._playhead_us,
        }
        params = {
            "project_id": self.project_state.projectId,
            "command": "cut",
            "operation": operation,
            "range_start_us": start,
            "range_end_us": end,
            "base_revision": base_revision,
            "request_id": request_id,
        }
        self._active_timeline_cut_request_id = request_id
        self._timeline_cut_context = context
        self._set_timeline_busy(True)
        self._timeline_cut_timeout_timer.start()
        try:
            self.runtime.request("timeline_command", params, context=context)
        except Exception:
            self._finish_timeline_cut(False, context)
            raise
        self._set_status("Đang áp dụng vùng cắt A–B")

    def _timeline_cut_context_is_current(self, context: object) -> bool:
        return (
            isinstance(context, dict)
            and context.get("timeline_cut") is True
            and bool(self._active_timeline_cut_request_id)
            and str(context.get("request_id", "")) == self._active_timeline_cut_request_id
        )

    def _finish_timeline_cut(self, succeeded: bool, context: object) -> None:
        details = context if isinstance(context, dict) else self._timeline_cut_context
        if details.get("timeline_cut") is not True:
            return
        request_id = str(details.get("request_id", ""))
        if request_id and request_id != self._active_timeline_cut_request_id:
            return
        operation = str(details.get("operation", ""))
        join_us = int(details["join_us"]) if "join_us" in details else (
            int(details.get("range_start_us", 0) or 0)
            if operation == "remove_range"
            else 0
        )
        if succeeded:
            self.setPlayheadMs(join_us / 1_000)
        else:
            previous = int(details.get("previous_playhead_us", self._playhead_us) or 0)
            self.setPlayheadMs(previous / 1_000)
        self._active_timeline_cut_request_id = ""
        self._timeline_cut_context = {}
        self._timeline_cut_timeout_timer.stop()
        self._set_timeline_busy(False)
        self.timelineCutSettled.emit(succeeded, operation, float(join_us))

    @Slot()
    def _timeline_cut_timed_out(self) -> None:
        if not self._active_timeline_cut_request_id:
            return
        context = dict(self._timeline_cut_context)
        project_id = str(context.get("project_id", self.project_state.projectId))
        self._finish_timeline_cut(False, context)
        self._begin_timeline_reconcile(project_id)
        self._save_state = "Chỉnh timeline thất bại"
        self.saveStateChanged.emit()
        self._set_status(
            "Cắt video phản hồi quá lâu; màn hình đã giữ nguyên. "
            "Hãy kiểm tra Job Manager rồi thử lại."
        )

    def _begin_timeline_reconcile(self, project_id: str) -> None:
        if not project_id:
            return
        self._timeline_reconcile_generation += 1
        self._timeline_reconcile_pending = True
        self._timeline_reconcile_project_id = project_id
        self._set_timeline_busy(True)
        self._dispatch_timeline_reconcile()

    def _dispatch_timeline_reconcile(self) -> None:
        if not self._timeline_reconcile_pending or not self._backend_ready:
            return
        generation = self._timeline_reconcile_generation
        project_id = self._timeline_reconcile_project_id
        try:
            self.runtime.request(
                "get_project",
                {"project_id": project_id},
                context={
                    "timeline_reconcile": True,
                    "project_id": project_id,
                    "generation": generation,
                },
            )
        except Exception:
            QTimer.singleShot(1_000, self._dispatch_timeline_reconcile)

    def _timeline_reconcile_context_is_current(self, context: object) -> bool:
        return (
            isinstance(context, dict)
            and context.get("timeline_reconcile") is True
            and self._timeline_reconcile_pending
            and str(context.get("project_id", "")) == self._timeline_reconcile_project_id
            and int(context.get("generation", -1) or -1)
            == self._timeline_reconcile_generation
        )

    @Slot(bool)
    def deleteSelected(self, apply_linked: bool = True) -> None:
        if self.selectedClipTrackKind in {"video", "subtitle", "text"}:
            self.deleteTimelineSelection()
            return
        self._send_timeline_command("delete", apply_linked=apply_linked)

    @Slot(float, bool)
    def setSelectedSpeed(self, percent: float, apply_linked: bool = True) -> None:
        self._send_timeline_command("speed", value=percent, apply_linked=apply_linked)

    @Slot(float)
    def setSelectedVolume(self, percent: float) -> None:
        self._send_timeline_command("volume", value=percent / 100.0)

    @Slot(float, float)
    def resizeSelectedClip(self, start_us: float, end_us: float) -> None:
        if not math.isfinite(start_us) or not math.isfinite(end_us):
            return
        if (
            self._timeline_busy
            or not self.project_state.hasProject
            or not self._selected_clip_id
            or end_us <= start_us
        ):
            return
        self._set_timeline_busy(True)
        self.runtime.request(
            "timeline_command",
            {
                "project_id": self.project_state.projectId,
                "command": "resize",
                "clip_id": self._selected_clip_id,
                "timeline_start_us": round(start_us),
                "timeline_end_us": round(end_us),
                "apply_linked": False,
            },
        )
        self._set_status("Đang thay đổi tốc độ cue D1")

    @Slot(float)
    def moveSelectedClip(self, start_us: float) -> None:
        if not math.isfinite(start_us):
            return
        if (
            self._timeline_busy
            or not self.project_state.hasProject
            or not self._selected_clip_id
            or start_us < 0
        ):
            return
        self._set_timeline_busy(True)
        if (self.project_state.voiceConformApplied
                and self.selectedClipTrackKind in {"video", "original_audio"}):
            start_us = self.project_state.outputToSourceUs(start_us)
        self.runtime.request(
            "timeline_command",
            {
                "project_id": self.project_state.projectId,
                "command": "move",
                "clip_id": self._selected_clip_id,
                "timeline_start_us": round(start_us),
                "apply_linked": True,
            },
        )
        self._set_status("Đang lưu vị trí video và âm thanh liên kết")

    @Slot()
    def autoFitVoiceTiming(self) -> None:
        if not self.project_state.hasProject or self._timeline_busy:
            return
        self._set_timeline_busy(True)
        self.runtime.request(
            "timeline_command",
            {
                "project_id": self.project_state.projectId,
                "command": "auto_fit_voice_timing",
                "track_id": "D1",
                "track_ids": [],
            },
        )
        self._set_status("Đang tự khớp các cue D1 bị lệch")

    @Slot()
    def deleteSelectedTracks(self) -> None:
        ids = tuple(
            sorted(
                self._selected_track_ids
                or ({self._selected_track_id} if self._selected_track_id else set())
            )
        )
        if not ids or self._timeline_busy or not self.project_state.hasProject:
            return
        self._set_timeline_busy(True)
        self.runtime.request(
            "timeline_command",
            {
                "project_id": self.project_state.projectId,
                "command": "delete_tracks",
                "track_ids": list(ids),
            },
        )
        self._set_status("Đang xoá " + ", ".join(ids))

    @Slot(bool)
    def setSelectedTrackMuted(self, muted: bool) -> None:
        self._send_track_command("track_mute", muted)

    @Slot(bool)
    def setSelectedTrackLocked(self, locked: bool) -> None:
        self._send_track_command("track_lock", locked)

    @Slot(str, float)
    def setTrackVolumePercent(self, track_id: str, percent: float) -> None:
        if not math.isfinite(percent):
            return
        self._send_track_command_by_id(
            "track_volume", track_id, max(0.0, min(200.0, percent)) / 100.0
        )

    @Slot(str, bool)
    def setTrackMuted(self, track_id: str, muted: bool) -> None:
        self._send_track_command_by_id("track_mute", track_id, muted)

    @Slot()
    def undoTimeline(self) -> None:
        if self._defer_until_subtitle_saves_flushed(
            "undo-timeline", self.undoTimeline, "Đang lưu phụ đề trước khi hoàn tác Timeline…"
        ):
            return
        self._send_timeline_command("undo", needs_selection=False)

    @Slot()
    def redoTimeline(self) -> None:
        if self._defer_until_subtitle_saves_flushed(
            "redo-timeline", self.redoTimeline, "Đang lưu phụ đề trước khi làm lại Timeline…"
        ):
            return
        self._send_timeline_command("redo", needs_selection=False)

    @Slot(str, int, result=int)
    def uiIntValue(self, key: str, default_value: int) -> int:
        return int(self.settings.value(f"ui/layout/{key}", default_value, type=int))

    @Slot(str, int)
    def setUiIntValue(self, key: str, value: int) -> None:
        self.settings.setValue(f"ui/layout/{key}", int(value))

    @Slot(str, float, result=float)
    def uiRealValue(self, key: str, default_value: float) -> float:
        try:
            value = float(self.settings.value(f"ui/layout/{key}", default_value, type=float))
        except (TypeError, ValueError):
            value = default_value
        if not math.isfinite(value):
            value = default_value
        return max(0.05, min(0.95, value))

    @Slot(str, float)
    def setUiRealValue(self, key: str, value: float) -> None:
        if not math.isfinite(value):
            return
        self.settings.setValue(f"ui/layout/{key}", max(0.05, min(0.95, value)))

    @Slot(str, str, result=str)
    def uiStringValue(self, key: str, default_value: str) -> str:
        return self.settings.value(f"ui/layout/{key}", default_value, type=str)

    @Slot(str, str)
    def setUiStringValue(self, key: str, value: str) -> None:
        self.settings.setValue(f"ui/layout/{key}", value)

    @staticmethod
    def _screen_index(workspace_id: str) -> int:
        return {
            "dashboard": 0,
            "projects": 1,
            "transcribe": 2,
            "translate": 2,
            "subtitles": 2,
            "voice": 3,
            "multi": 4,
            "timeline": 5,
            "effects": 6,
            "mixer": 6,
            "export": 6,
            "queue": 6,
            "settings": 7,
        }.get(workspace_id, 0)

    @Slot(str)
    def _set_status(self, value: str) -> None:
        if value == self._status_message:
            return
        self._status_message = value
        self.statusMessageChanged.emit()
        # Keep the footer as the durable status surface, while promoting
        # terminal failures and meaningful completions to a readable toast.
        # Progress chatter ("Đang …") intentionally stays in the footer so a
        # long-running job does not keep replacing the user's notification.
        normalized = value.strip().casefold()
        is_error = any(
            marker in normalized
            for marker in (
                "không thể",
                "không tạo được",
                "không đọc được",
                "không tìm thấy",
                "không hợp lệ",
                "chưa thể tiếp tục",
                "thất bại",
                "lỗi",
            )
        ) or (self._runtime_state == "error" and normalized not in {"sẵn sàng"})
        is_success = (
            normalized.startswith("đã ")
            or "hoàn tất" in normalized
            or "sẵn sàng" in normalized
            or "đạt qa" in normalized
        )
        is_warning = any(
            marker in normalized
            for marker in (
                "cần sửa",
                "vượt timeline",
                "chưa đạt",
                "chưa thể",
                "chưa tạo",
                "bị gián đoạn",
                "dừng ở bước qa",
            )
        )
        if is_error:
            self.statusNotificationRequested.emit(value, "error", 10_000)
        elif is_success:
            self.statusNotificationRequested.emit(value, "success", 10_000)
        elif is_warning:
            self.statusNotificationRequested.emit(value, "warning", 8_000)

    @staticmethod
    def _job_kind_label(item: dict[str, object]) -> str:
        spec = item.get("spec")
        kind = spec.get("kind") if isinstance(spec, dict) else ""
        config = spec.get("provider_config") if isinstance(spec, dict) else {}
        config = config if isinstance(config, dict) else {}
        if kind == "ocr":
            return (
                "Whisper âm thanh"
                if config.get("extraction_method") == "whisper_audio"
                else "OCR khung hình"
            )
        if kind == "translate":
            purpose = str(config.get("purpose", ""))
            return "Rút gọn cue dài" if purpose == "voice_timing_repair" else "Dịch thuật"
        if kind == "tts":
            return "Đồng bộ voice" if config.get("job_mode") == "voice_conform" else "Tạo giọng"
        if kind == "export":
            return "Xuất video"
        return str(kind or "Tác vụ")

    @staticmethod
    def _localized_error_message(code: str, fallback: str = "") -> str:
        """Translate internal failure identifiers into user-facing guidance.

        Error identifiers remain persisted for diagnostics, but the GUI must
        not force users to interpret implementation names such as
        ``tts_timeline_overlap_guard_failed``. Worker/provider messages that
        are already human-readable are retained as a fallback.
        """

        normalized = code.strip().casefold()
        messages = {
            "tts_timeline_overlap_guard_failed": (
                "Các đoạn voice đang chồng lên nhau khi ráp timeline. "
                "Hãy chọn Bỏ qua cảnh báo để xếp câu tuần tự và kéo dài timeline, "
                "hoặc rút gọn cue dài rồi tiếp tục từ cache."
            ),
            "voice_conform_timeline_overlap_guard_failed": (
                "Các đoạn voice vẫn chồng sau khi đồng bộ. Hãy kiểm tra timestamp "
                "nguồn hoặc chọn Giãn video/Kết hợp; WAV cache vẫn được giữ."
            ),
            "tts_review_required": (
                "Tạo giọng đã dừng ở bước kiểm tra thời lượng. Hãy mở QA voice, "
                "rút gọn cue dài hoặc chọn Bỏ qua cảnh báo để giữ nguyên voice."
            ),
            "cue_sync_limit_exceeded": (
                "Một hoặc nhiều cue cần tốc độ vượt giới hạn. Hãy rút gọn câu, "
                "kéo dài đoạn video hoặc chọn chế độ đồng bộ phù hợp."
            ),
            "cue_too_long": (
                "Một hoặc nhiều cue có voice dài hơn khung phụ đề. Hãy rút gọn "
                "các cue được đánh dấu hoặc chọn Bỏ qua cảnh báo."
            ),
            "worker_exit_without_ack": (
                "Bộ máy tạo giọng đã dừng trước khi xác nhận checkpoint. Hãy "
                "tiếp tục từ cache; dữ liệu đã xử lý không cần tạo lại."
            ),
            "child_interrupted": (
                "Tác vụ Multi bị gián đoạn khi khởi động hoặc điều khiển worker. "
                "Cache/checkpoint vẫn được giữ; bấm Tiếp tục Multi video để chạy lại "
                "đúng công đoạn, không quay về OCR."
            ),
            "insufficient_disk_space": (
                "Ổ đĩa không còn đủ chỗ để lưu checkpoint. Hãy dọn dung lượng trong "
                "thư mục dữ liệu DUCAI rồi tiếp tục từ cache hiện có."
            ),
            "tts_audio_invalid": (
                "Một WAV voice không hợp lệ hoặc không đọc được. Hãy tiếp tục từ "
                "cache để tạo lại riêng cue lỗi."
            ),
            "tts_audio_quality_failed": (
                "Một WAV voice không đạt kiểm tra chất lượng. Hãy tiếp tục từ "
                "cache để xử lý lại riêng cue lỗi."
            ),
            "tts_missing_approved_clip": (
                "Đang thiếu WAV đã kiểm tra của một hoặc nhiều cue. Hãy tiếp tục "
                "từ cache để bổ sung các cue còn thiếu."
            ),
            "voice_clip_exceeds_timeline": (
                "Voice vượt thời lượng timeline hiện tại. Hãy bật cho phép kéo "
                "dài timeline hoặc dùng chế độ Giãn video/Kết hợp."
            ),
            "strict_voice_exceeds_media_duration": (
                "Voice dài hơn video và chế độ hiện tại không cho phép kéo dài. "
                "Hãy chọn Bỏ qua cảnh báo hoặc rút gọn cue dài."
            ),
            "adaptive_voice_exceeds_media_duration": (
                "Voice vẫn dài hơn video sau khi tăng tốc trong giới hạn. Hãy "
                "rút gọn cue còn lỗi hoặc cho phép kéo dài timeline."
            ),
            "provider_access_blocked": (
                "Gateway hoặc CDN đang chặn kết nối từ ứng dụng. Hãy kiểm tra "
                "endpoint, mạng và chính sách truy cập của nhà cung cấp."
            ),
            "authentication_required": (
                "Nhà cung cấp yêu cầu xác thực lại. Nếu dùng API, hãy kiểm tra "
                "key và quyền model; nếu dùng Gemini Web, hãy thử lại từ cache "
                "hoặc chuyển sang Gemini API/CLI."
            ),
            "whisper_engine_python_missing": (
                "Không tìm thấy runtime Whisper. Hãy chạy "
                "scripts/setup-whisper-runtime.ps1 rồi thử lại."
            ),
            "whisper_model_missing": (
                "Không tìm thấy model Whisper large-v3 hoặc model chưa đầy đủ. "
                "Hãy chạy scripts/setup-whisper-runtime.ps1 rồi thử lại."
            ),
            "whisper_audio_track_missing": (
                "Video không có track âm thanh để Whisper nhận dạng. Hãy dùng OCR "
                "khung hình hoặc nhập video có tiếng."
            ),
            "whisper_audio_extract_failed": (
                "Không tách được audio 16 kHz từ video. Hãy kiểm tra file nguồn và "
                "FFmpeg trong Cài đặt → Chẩn đoán runtime."
            ),
            "whisper_checkpoint_corrupt": (
                "Checkpoint Whisper bị hỏng nên không thể tiếp tục an toàn. Hãy "
                "chạy lại Whisper cho video này; SRT đã publish trước đó không bị đổi."
            ),
            "whisper_no_speech_detected": (
                "Whisper không tìm thấy lời nói trong track âm thanh đã chọn. Hãy "
                "kiểm tra audio nguồn hoặc chuyển sang OCR khung hình."
            ),
            "whisper_segment_invalid": (
                "Whisper trả về timestamp không hợp lệ. Chưa có SRT dở dang nào "
                "được đưa vào dự án; hãy thử lại hoặc dùng OCR khung hình."
            ),
            "whisper_cuda_failed": (
                "Whisper không chạy được bằng CUDA và cũng không hoàn tất bằng CPU. "
                "Hãy kiểm tra driver/GPU hoặc chọn lại video rồi thử tiếp từ cache."
            ),
        }
        translated = messages.get(normalized)
        if translated:
            return translated
        raw_fallback = str(fallback or "").strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+", raw_fallback):
            return "Tác vụ không thể hoàn tất. Cache/checkpoint vẫn được giữ lại nếu có."
        cleaned = ApplicationViewModel._sanitize_error_detail(raw_fallback, code)
        # Do not echo a raw snake_case implementation identifier when no
        # dedicated translation exists.
        if cleaned and not (
            normalized
            and cleaned.casefold() == normalized
            and all(character.isalnum() or character == "_" for character in cleaned)
        ):
            return cleaned[:500]
        return "Tác vụ không thể hoàn tất. Cache/checkpoint vẫn được giữ lại nếu có."

    @staticmethod
    def _localized_issue_label(code: str) -> str:
        """Return a short Vietnamese label for a QA issue identifier.

        Issue identifiers are intentionally kept in the persisted QA payload so
        workers and diagnostics retain a stable contract.  They are not useful
        to an editor, however, so every identifier shown in the UI is converted
        to a readable Vietnamese label here.  Unknown identifiers fall back to
        a generic label rather than leaking an implementation name.
        """

        labels = {
            "name_inconsistency": "Tên nhân vật không nhất quán",
            "translation_error": "Lỗi nội dung bản dịch",
            "context": "Sai ngữ cảnh",
            "wrong": "Dịch sai nghĩa",
            "style": "Lỗi văn phong",
            "broken": "Câu dịch bị cụt",
            "cjk": "Còn chữ Hán chưa dịch",
            "cjk_remaining": "Còn chữ Hán chưa dịch",
            "untranslated_latin_term": "Còn thuật ngữ chưa Việt hóa",
            "possibly_untranslated_latin": "Có thể còn chữ chưa Việt hóa",
            "overlong_translation": "Câu dịch quá dài",
            "long_line": "Dòng phụ đề quá dài",
            "film_bible_name_mismatch": "Tên riêng lệch Film Bible",
            "film_bible_term_mismatch": "Thuật ngữ lệch Film Bible",
            "cue_too_long": "Cue dài hơn khung thời gian",
            "cue_sync_limit_exceeded": "Cue vượt giới hạn tốc độ",
            "tts_review_required": "Cần rà soát thời lượng voice",
            "tts_timeline_overlap_guard_failed": "Các đoạn voice bị chồng",
            "voice_conform_timeline_overlap_guard_failed": ("Các đoạn voice bị chồng sau đồng bộ"),
            "worker_exit_without_ack": "Bộ máy dừng trước khi lưu checkpoint",
            "child_interrupted": "Tác vụ Multi bị gián đoạn",
            "insufficient_disk_space": "Không đủ dung lượng lưu checkpoint",
            "tts_audio_invalid": "WAV voice không hợp lệ",
            "tts_audio_quality_failed": "WAV voice chưa đạt kiểm tra chất lượng",
            "tts_missing_approved_clip": "Thiếu WAV voice đã kiểm tra",
            "translation_review_required": "Cần rà soát bản dịch",
            "ocr_review_required": "Cần rà soát kết quả OCR",
        }
        parts = [part.strip() for part in str(code or "").replace(",", " ").split()]
        if not parts:
            return "QA cần kiểm tra"
        localized = []
        for part in parts:
            label = labels.get(part.casefold(), "Lỗi kiểm tra bản dịch")
            if label not in localized:
                localized.append(label)
        return " / ".join(localized)

    @Slot(str, result=str)
    def localizedIssueLabel(self, code: str) -> str:
        """Expose Vietnamese QA labels to QML without exposing internal codes."""

        return self._localized_issue_label(code)

    @staticmethod
    def _sanitize_error_detail(value: object, code: str = "") -> str:
        """Remove implementation-style identifiers from user-facing detail."""

        cleaned = redact_sensitive_text(str(value or "").strip())
        normalized = str(code or "").strip().casefold()

        def replace_identifier(match: re.Match[str]) -> str:
            token = match.group(0)
            if token.casefold() == normalized:
                return ApplicationViewModel._localized_issue_label(token)
            known = ApplicationViewModel._localized_issue_label(token)
            return known if known != "Lỗi kiểm tra bản dịch" else "chi tiết kỹ thuật"

        # Error/QA codes use snake_case; replacing those tokens keeps provider
        # diagnostics useful without exposing implementation names in a toast.
        return re.sub(
            r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![A-Za-z0-9_])",
            replace_identifier,
            cleaned,
        )

    @classmethod
    def _format_terminal_job_status(cls, item: dict[str, object]) -> str:
        """Build a readable, recoverable status from a polled terminal job.

        Workers persist only a bounded, redacted error payload.  Pick the
        useful human-facing fields instead of dumping JSON (which can contain
        paths or provider diagnostics that are too noisy for a toast).
        """

        label = cls._job_kind_label(item)
        job_id = str(item.get("id", "")).strip()
        short_id = job_id[:8] if job_id else "không rõ"
        state = str(item.get("state", "")).strip().casefold()
        code = str(item.get("error_code", "")).strip()
        payload = item.get("error_payload")
        payload = payload if isinstance(payload, dict) else {}
        detail = ""
        for key in ("message", "reason", "detail", "error", "stderr"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                detail = cls._sanitize_error_detail(value, code)
                break
        if detail:
            detail = detail[:300]
        task_suffix = f" (tác vụ {short_id})"
        if state == "succeeded":
            return f"Đã hoàn tất {label.lower()}{task_suffix} và lưu kết quả."
        if state == "cancelled":
            return f"Đã hủy {label.lower()}{task_suffix}; checkpoint/cache đã được giữ lại."
        if state == "interrupted":
            reason = f" Nguyên nhân: {detail}." if detail else ""
            return (
                f"{label} bị gián đoạn{task_suffix}; checkpoint/cache đã được giữ lại. "
                f"Có thể tiếp tục từ cache.{reason}"
            )

        review_hint = {
            "ocr_review_required": (
                "OCR đã dừng ở bước QA; cần rà soát vùng lỗi trước khi nhập SRT."
            ),
            "translation_review_required": (
                "Dịch đã dừng ở bước QA; hãy mở Biên tập dịch thuật để xử lý cue lỗi."
            ),
            "tts_review_required": (
                "Tạo giọng đã dừng ở QA timing; hãy xử lý cue dài hoặc chọn cách đồng bộ voice."
            ),
            "tts_timeline_overlap_guard_failed": (
                "Tạo giọng thất bại: các đoạn voice đang chồng lên nhau; hãy chọn "
                "Bỏ qua cảnh báo để xếp tuần tự "
                "và kéo dài timeline, hoặc rút gọn cue dài rồi tiếp tục từ cache."
            ),
            "voice_conform_timeline_overlap_guard_failed": (
                "Đồng bộ voice thất bại: các đoạn voice vẫn chồng sau khi đồng bộ; "
                "hãy kiểm tra timestamp nguồn "
                "hoặc chọn Giãn video/Kết hợp."
            ),
        }.get(code)
        if review_hint:
            reason = (
                f" Chi tiết: {detail}." if detail and detail.casefold() != code.casefold() else ""
            )
            return f"{review_hint}{reason} Cache/checkpoint vẫn được giữ lại.{task_suffix}"
        translated = cls._localized_error_message(code, detail)
        reason = f" Chi tiết: {detail}." if detail and detail.casefold() != code.casefold() else ""
        return (
            f"{label} thất bại{task_suffix}. {translated}{reason} "
            "Cache/checkpoint vẫn được giữ lại nếu có."
        )

    def _notify_terminal_job_changes(self, raw_items: list[object]) -> None:
        """Notify once when a current-project job reaches a terminal state."""

        # Jobs are polled before the first project snapshot during startup. Do
        # not treat that unscoped list as a transition for whichever project is
        # opened later; otherwise old failures would appear as fresh errors.
        if not self.project_state.projectId:
            return
        current_markers: dict[str, tuple[object, ...]] = {}
        terminal_states = {"succeeded", "failed", "interrupted", "cancelled"}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            spec = raw_item.get("spec")
            if not isinstance(spec, dict):
                continue
            if str(spec.get("project_id", "")) != self.project_state.projectId:
                continue
            job_id = str(raw_item.get("id", "")).strip()
            if not job_id:
                continue
            state = str(raw_item.get("state", "")).strip().casefold()
            marker = (
                state,
                str(raw_item.get("error_code", "")),
                str(raw_item.get("finished_at", "")),
                repr(raw_item.get("error_payload", {})),
            )
            current_markers[job_id] = marker
            if job_id in self._superseded_job_ids:
                continue
            if not self._jobs_snapshot_initialized or state not in terminal_states:
                continue
            if self._terminal_job_notification_markers.get(job_id) == marker:
                continue
            if state == "succeeded" and self._success_status_is_reloadable(raw_item, job_id):
                # OCR, translation and production TTS publish through a
                # follow-up get_project action, whose status explains the
                # destination (for example, "đã chuyển sang Timeline"). Do
                # not replace that more useful toast with a generic success.
                continue
            self._set_status(self._format_terminal_job_status(raw_item))
        self._terminal_job_notification_markers = current_markers
        self._jobs_snapshot_initialized = True

    def _success_status_is_reloadable(self, item: dict[str, object], job_id: str) -> bool:
        spec = item.get("spec")
        if not isinstance(spec, dict):
            return False
        kind = str(spec.get("kind", ""))
        if kind == "ocr":
            return job_id in self._reloaded_ocr_jobs
        if kind == "translate":
            return job_id in self._reloaded_translation_jobs
        if kind == "tts":
            config = spec.get("provider_config")
            config = config if isinstance(config, dict) else {}
            return (
                config.get("purpose") == "production"
                and config.get("job_mode") != "voice_conform"
                and job_id in self._reloaded_tts_jobs
            )
        return False

    @Slot(bool)
    def _set_backend_ready(self, value: bool) -> None:
        if value == self._backend_ready:
            return
        self._backend_ready = value
        self.backendReadyChanged.emit()
        if not value and self._project_save_context:
            self._project_save_failed({"message": "Mất kết nối trong khi lưu"},
                                      dict(self._project_save_context))
        if not value and self._subtitle_command_context:
            context = dict(self._subtitle_command_context)
            self._action_failed("subtitle_command", {
                "message": "Mất kết nối khi sửa phụ đề; đang đọc lại bản đã lưu",
            }, context)
            self._begin_timeline_reconcile(str(context.get("project_id", "")))
        if not value and (self._subtitle_save_in_flight or self._subtitle_save_queue):
            project_id = str(self._subtitle_save_context.get(
                "project_id", self.project_state.projectId))
            self._action_failed("subtitle_command", {
                "message": ("Mất kết nối khi lưu phụ đề; thao tác xóa chờ đã hủy, "
                            "đang đọc lại bản đã lưu"),
            }, self._subtitle_save_context or {
                "project_id": project_id, "background_subtitle_save": True,
            })
            self._begin_timeline_reconcile(project_id)
        if not value and self._scene_timing_context:
            context = dict(self._scene_timing_context)
            self._action_failed("subtitle_command", {
                "message": "Mất kết nối khi lưu thời gian cue; đang đọc lại bản đã lưu",
            }, context)
            self._begin_timeline_reconcile(str(context.get("project_id", "")))
        if not value and self._subtitle_geometry_context:
            project_id = str(self._subtitle_geometry_context.get("project_id", ""))
            self._finish_subtitle_geometry(False)
            self._set_status("Mất kết nối khi lưu vị trí chữ; đang đọc lại bản đã lưu")
            self._begin_timeline_reconcile(project_id)
        if not value and self._active_timeline_cut_request_id:
            context = dict(self._timeline_cut_context)
            project_id = str(context.get("project_id", self.project_state.projectId))
            self._finish_timeline_cut(False, context)
            self._begin_timeline_reconcile(project_id)
            self._save_state = "Chỉnh timeline thất bại"
            self.saveStateChanged.emit()
        if value and self._timeline_reconcile_pending:
            self._dispatch_timeline_reconcile()
        if not value and self._multi_detail_busy:
            self._multi_detail_generation += 1
            self._finish_multi_detail()
        if not value and self._pending_tts_start is not None:
            self._pending_tts_start = None
            self._set_voice_playback_enabled(True)
            self._set_tts_busy(False)
            self._set_tts_job_state("interrupted")

    @Slot(str)
    def _set_runtime_state(self, value: str) -> None:
        if value == self._runtime_state:
            return
        self._runtime_state = value
        self.runtimeStateChanged.emit()

    def _sync_dialogue_casting_job(self, raw_items: list[object]) -> None:
        project_id = self.project_state.projectId
        if not project_id:
            return
        casting_jobs = [
            item for item in raw_items
            if isinstance(item, dict)
            and isinstance(item.get("spec"), dict)
            and item["spec"].get("kind") == "dialogue_casting"
            and item["spec"].get("project_id") == project_id
        ]
        active_states = {
            "queued", "start_requested", "running", "pause_requested",
            "paused", "resume_requested", "cancel_requested",
        }
        current = next(
            (item for item in casting_jobs if str(item.get("state", "")) in active_states),
            casting_jobs[0] if casting_jobs else None,
        )
        if current is None:
            return
        if (
            self._dialogue_casting_job_state == "start_requested"
            and not self._dialogue_casting_job_id
            and str(current.get("state", "")) not in active_states
        ):
            return
        job_id = str(current.get("id", ""))
        if not job_id:
            return
        state = str(current.get("state", ""))
        self._dialogue_casting_job_id = job_id
        self._set_dialogue_casting_job_state(state)
        if state in {"succeeded", "failed", "cancelled", "interrupted"}:
            self._finish_dialogue_casting_job(job_id, state)

    @Slot(object)
    def _replace_jobs(self, raw_items: object) -> None:
        project_id = self.project_state.projectId
        for model in (self.jobs, self.subtitle_jobs, self.voice_jobs, self.export_jobs):
            model.setProjectFilter(project_id)
        self.jobs.replace(raw_items)
        self.subtitle_jobs.replace(raw_items)
        self.voice_jobs.replace(raw_items)
        self.export_jobs.replace(raw_items)
        active_id = self.subtitle_jobs.active_job_id(self.project_state.projectId, "ocr")
        self._ocr_job_id = active_id
        self._set_ocr_busy(bool(active_id))
        if not isinstance(raw_items, list):
            return
        self._observed_active_ocr_jobs.update(
            str(item.get("id", "")) for item in raw_items
            if isinstance(item, dict) and isinstance(item.get("spec"), dict)
            and item["spec"].get("kind") == "ocr"
            and item.get("state") in {"queued", "start_requested", "running", "pause_requested",
                                     "paused", "resume_requested", "cancel_requested"}
        )
        self._sync_dialogue_casting_job(raw_items)
        superseded_before = len(self._superseded_job_ids)
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            spec = item.get("spec")
            if not isinstance(spec, dict) or spec.get("kind") != "tts":
                continue
            config = spec.get("provider_config")
            if not isinstance(config, dict):
                continue
            replaced_job_id = str(config.get("replaces_job_id", "")).strip()
            if replaced_job_id:
                self._superseded_job_ids.add(replaced_job_id)
        if len(self._superseded_job_ids) != superseded_before:
            self.statusNotificationDismissRequested.emit()
        latest_ocr = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict)
                and isinstance(item.get("spec"), dict)
                and item["spec"].get("kind") == "ocr"
                and item["spec"].get("project_id") == self.project_state.projectId
            ),
            None,
        )
        if latest_ocr:
            latest_spec = latest_ocr.get("spec")
            latest_config = (
                latest_spec.get("provider_config", {}) if isinstance(latest_spec, dict) else {}
            )
            self._ocr_job_method = (
                str(latest_config.get("extraction_method", "frame_ocr"))
                if isinstance(latest_config, dict)
                else "frame_ocr"
            )
        next_state = str(latest_ocr.get("state", "")) if latest_ocr else ""
        if not active_id and latest_ocr and next_state in {"interrupted", "failed"}:
            self._ocr_job_id = str(latest_ocr.get("id", ""))
        self._set_ocr_job_state(next_state)
        visible_job_ids = {
            str(item.get("id", ""))
            for item in raw_items
            if isinstance(item, dict) and item.get("id")
        }
        completed_history_deletions = self._pending_history_deletion_ids - visible_job_ids
        if completed_history_deletions:
            self._request_translation_cache_list(project_id)
        active_translation_id = self.subtitle_jobs.active_job_id(
            self.project_state.projectId, "translate"
        )
        if active_translation_id:
            self._translation_job_id = active_translation_id
            if (not self._translation_cache_selection_explicit
                    and self._selected_translation_cache_id != active_translation_id):
                self._selected_translation_cache_id = active_translation_id
                self.translationCachesChanged.emit()
        translation_start_pending = (
            not active_translation_id
            and self._translation_busy
            and self._translation_job_state == "start_requested"
        )
        if not translation_start_pending:
            self._set_translation_busy(bool(active_translation_id))
        newest_translation = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict)
                and isinstance(item.get("spec"), dict)
                and item["spec"].get("kind") == "translate"
                and item["spec"].get("project_id") == self.project_state.projectId
            ),
            None,
        )
        if newest_translation:
            newest_state = str(newest_translation.get("state", ""))
            newest_marker = (
                str(newest_translation.get("id", "")),
                newest_state,
                str(newest_translation.get("finished_at", "")),
                str(newest_translation.get("error_code", "")),
            )
            if (
                newest_state in {"succeeded", "failed", "interrupted", "cancelled"}
                and newest_marker != self._translation_cache_refresh_marker
            ):
                self._translation_cache_refresh_marker = newest_marker
                self._request_translation_cache_list(project_id)
        selected_translation = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict)
                and str(item.get("id", "")) == self._selected_translation_cache_id
            ),
            None,
        )
        active_translation = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict) and str(item.get("id", "")) == active_translation_id
            ),
            None,
        )
        active_translation_config = (
            active_translation.get("spec", {}).get("provider_config", {})
            if isinstance(active_translation, dict)
            else {}
        )
        active_timing_repair = (
            isinstance(active_translation_config, dict)
            and active_translation_config.get("translation_purpose") == "voice_timing_repair"
        )
        self._set_voice_timing_auto_repair_busy(
            active_timing_repair
            or (bool(self._voice_timing_repair_requests) and self._translation_busy)
        )
        latest_translation = (
            active_translation
            if active_translation_id
            else selected_translation or newest_translation
        )
        if not translation_start_pending:
            self._translation_provider_id = ""
            self._translation_model_id = ""
            if latest_translation and isinstance(latest_translation.get("spec"), dict):
                self._translation_provider_id = str(
                    latest_translation["spec"].get("provider_id", "")
                )
                self._translation_model_id = str(latest_translation["spec"].get("model_id", ""))
                config = latest_translation["spec"].get("provider_config", {})
                self._bind_translation_configuration(config)
            else:
                self._bind_translation_configuration({})
        translation_state = str(latest_translation.get("state", "")) if latest_translation else ""
        if (
            latest_translation
            and latest_translation.get("error_code") == "translation_review_required"
        ):
            translation_state = "review"
        if (
            not active_translation_id
            and latest_translation
            and translation_state in {"interrupted", "failed", "review", "cancelled"}
        ):
            self._translation_job_id = str(latest_translation.get("id", ""))
        if not translation_start_pending:
            self._set_translation_job_state(translation_state)
        selected_cache_protects_preview = bool(
            self._selected_translation_cache_id
            and latest_translation
            and str(latest_translation.get("id", "")) != self._selected_translation_cache_id
        )
        if (
            latest_translation
            and translation_state not in {"succeeded", "review"}
            and not selected_cache_protects_preview
            and not translation_start_pending
        ):
            latest_translation_id = str(latest_translation.get("id", ""))
            terminal_translation = translation_state in {"failed", "cancelled"}
            if latest_translation_id and (
                not terminal_translation
                or latest_translation_id not in self._translation_terminal_event_jobs
            ):
                self._request_translation_deltas(
                    latest_translation_id,
                    self.project_state.projectId,
                    terminal=terminal_translation,
                )
                self._request_translation_qa_status(
                    latest_translation_id,
                    self.project_state.projectId,
                    terminal=terminal_translation,
                )
        elif not latest_translation:
            self._set_translation_qa({"status": "NONE", "issues": []})
        active_tts = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict)
                and isinstance(item.get("spec"), dict)
                and item["spec"].get("kind") == "tts"
                and item["spec"].get("project_id") == self.project_state.projectId
                and isinstance(item["spec"].get("provider_config"), dict)
                and item["spec"]["provider_config"].get("job_mode") != "voice_conform"
                and item["spec"]["provider_config"].get("purpose") in {"production", "preview"}
                and item.get("state")
                in {
                    "queued",
                    "start_requested",
                    "running",
                    "pause_requested",
                    "paused",
                    "resume_requested",
                    "cancel_requested",
                }
            ),
            None,
        )
        active_tts_id = str(active_tts.get("id", "")) if active_tts else ""
        latest_tts = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict)
                and isinstance(item.get("spec"), dict)
                and item["spec"].get("kind") == "tts"
                and item["spec"].get("project_id") == self.project_state.projectId
                and isinstance(item["spec"].get("provider_config"), dict)
                and item["spec"]["provider_config"].get("purpose") == "production"
                and item["spec"]["provider_config"].get("job_mode") != "voice_conform"
            ),
            None,
        )
        latest_preview_tts = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict)
                and isinstance(item.get("spec"), dict)
                and item["spec"].get("kind") == "tts"
                and item["spec"].get("project_id") == self.project_state.projectId
                and isinstance(item["spec"].get("provider_config"), dict)
                and item["spec"]["provider_config"].get("purpose") == "preview"
            ),
            None,
        )
        current_tts = active_tts or latest_tts or latest_preview_tts
        if not self._dialogue_casting_auto_tts:
            if current_tts is not None:
                self._tts_job_id = str(current_tts.get("id", ""))
                current_config = current_tts.get("spec", {}).get("provider_config", {})
                current_config = current_config if isinstance(current_config, dict) else {}
                current_purpose = (
                    "voice_conform"
                    if current_config.get("job_mode") == "voice_conform"
                    else str(current_config.get("purpose", ""))
                )
                self._set_tts_job_purpose(current_purpose)
                current_state = str(current_tts.get("state", ""))
                if current_tts.get("error_code") == "tts_review_required":
                    current_state = "review"
                self._set_tts_job_state(current_state)
            else:
                self._tts_job_id = ""
                self._set_tts_job_purpose("")
                self._set_tts_job_state("")
            self._set_tts_busy(bool(active_tts_id))
        tts_state = str(latest_tts.get("state", "")) if latest_tts else ""
        if latest_tts and latest_tts.get("error_code") == "tts_review_required":
            tts_state = "review"
        conform_jobs = [
            item
            for item in raw_items
            if isinstance(item, dict)
            and isinstance(item.get("spec"), dict)
            and item["spec"].get("kind") == "tts"
            and item["spec"].get("project_id") == self.project_state.projectId
            and isinstance(item["spec"].get("provider_config"), dict)
            and item["spec"]["provider_config"].get("job_mode") == "voice_conform"
        ]
        latest_conform = conform_jobs[0] if conform_jobs else None
        if latest_conform is not None:
            self._voice_conform_job_id = str(latest_conform.get("id", ""))
            conform_state = str(latest_conform.get("state", ""))
            self._voice_conform_job_state = conform_state
            self._voice_conform_busy = conform_state in {
                "queued",
                "start_requested",
                "running",
                "pause_requested",
                "paused",
                "resume_requested",
                "cancel_requested",
            }
            # A newly failed/interrupted conform job does not roll back a
            # previously published project revision.  Job outcome and the
            # persisted project state are independent; derive the applied
            # flag from the latter for every conform refresh.
            self._voice_conform_applied = self.project_state.voiceConformApplied
            self.voiceConformChanged.emit()
        elif not self.project_state.voiceConformApplied and not self._voice_conform_busy:
            self._voice_conform_job_id = ""
            self._voice_conform_job_state = ""
            self._voice_conform_applied = False
        latest_tts_config = (
            latest_tts.get("spec", {}).get("provider_config", {})
            if isinstance(latest_tts, dict)
            else {}
        )
        if isinstance(latest_tts_config, dict) and (
            latest_tts_config.get("allow_timeline_overflow") is True
            and tts_state
            in {
                "succeeded",
                "failed",
                "interrupted",
                "cancelled",
            }
        ):
            self._voice_timing_override_requests.discard(
                str(latest_tts_config.get("timing_override_parent_job_id", ""))
            )
        latest_tts_is_production = (
            isinstance(latest_tts_config, dict) and latest_tts_config.get("purpose") == "production"
        )
        if latest_tts and latest_tts_is_production and tts_state in {"review", "succeeded"}:
            self._request_voice_timing_review(str(latest_tts.get("id", "")))
        if latest_tts and latest_tts_is_production and tts_state == "succeeded":
            timing_override = latest_tts_config.get("allow_timeline_overflow") is True
            latest_tts_result = latest_tts.get("result", {})
            latest_tts_result = latest_tts_result if isinstance(latest_tts_result, dict) else {}
            adjusted_cue_count = int(latest_tts_result.get("adjusted_cue_count", 0) or 0)
            self._set_voice_timing_qa(
                {
                    "status": "PASS",
                    "job_id": str(latest_tts.get("id", "")),
                    "issue_count": 0,
                    "issues": [],
                    "voice_read_speed": float(
                        latest_tts_result.get(
                            "voice_read_speed",
                            latest_tts_config.get("voice_read_speed", 1.0),
                        )
                        or 1.0
                    ),
                    "cue_max_total_speed": float(
                        latest_tts_result.get(
                            "cue_max_total_speed",
                            latest_tts_config.get("max_absolute_speed", 1.0),
                        )
                        or 1.0
                    ),
                    "adjusted_cue_count": adjusted_cue_count,
                    "maximum_additional_factor": float(
                        latest_tts_result.get("maximum_additional_factor", 1.0) or 1.0
                    ),
                    "unresolved_timing_cue_count": 0,
                    "message": (
                        "Đã thêm nguyên voice vào Timeline; hãy chỉnh tốc độ voice hoặc "
                        "video trước khi xuất."
                        if timing_override
                        else (
                            "Voice đã khớp timeline và đạt QA; "
                            f"{adjusted_cue_count} cue dài đã được tăng thêm."
                        )
                    ),
                }
            )
        preview_tts = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict)
                and isinstance(item.get("spec"), dict)
                and item["spec"].get("kind") == "tts"
                and item["spec"].get("project_id") == self.project_state.projectId
                and item.get("state") == "succeeded"
                and bool(self._voice_preview_target_job_id)
                and item.get("id") == self._voice_preview_target_job_id
                and isinstance(item.get("result"), dict)
                and item["result"].get("purpose") == "preview"
            ),
            None,
        )
        if preview_tts and isinstance(preview_tts.get("result"), dict):
            tts_result = preview_tts["result"]
            output_path = tts_result.get("output_path")
            if isinstance(output_path, str) and Path(output_path).is_file():
                preview_source = QUrl.fromLocalFile(output_path).toString()
                preview_job_id = str(preview_tts.get("id", ""))
                if preview_job_id != self._voice_preview_job_id:
                    self._voice_preview_job_id = preview_job_id
                    self._voice_preview_source = preview_source
                    self.voicePreviewChanged.emit()
        active_export_id = self.export_jobs.active_job_id(self.project_state.projectId, "export")
        if active_export_id:
            self._export_job_id = active_export_id
        self._set_export_busy(bool(active_export_id))
        latest_export = next(
            (
                item
                for item in raw_items
                if isinstance(item, dict)
                and isinstance(item.get("spec"), dict)
                and item["spec"].get("kind") == "export"
                and item["spec"].get("project_id") == self.project_state.projectId
            ),
            None,
        )
        export_state = str(latest_export.get("state", "")) if latest_export else ""
        if not active_export_id and latest_export and export_state in {"interrupted", "failed"}:
            self._export_job_id = str(latest_export.get("id", ""))
        self._set_export_job_state(export_state)
        if (latest_export and export_state == "succeeded"
                and isinstance(latest_export.get("result"), dict)):
            export_result = latest_export["result"]
            output_path = export_result.get("output_path")
            qa_path = export_result.get("qa_path")
            if isinstance(output_path, str) and output_path and Path(output_path).is_file():
                preview_source = QUrl.fromLocalFile(output_path).toString()
                changed = preview_source != self._last_export_source
                self._last_export_source = preview_source
                self._last_export_qa_path = qa_path if isinstance(qa_path, str) else ""
                if changed:
                    self.exportPreviewChanged.emit()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            is_importable_terminal = item.get("state") == "succeeded" or (
                item.get("state") == "failed"
                and item.get("error_code")
                in {
                    "ocr_review_required",
                    "translation_review_required",
                    "tts_review_required",
                }
            )
            if not is_importable_terminal:
                continue
            spec = item.get("spec", {})
            job_id = str(item.get("id", ""))
            if (
                isinstance(spec, dict)
                and spec.get("kind") == "ocr"
                and spec.get("project_id") == self.project_state.projectId
                and job_id
                and job_id not in self._reloaded_ocr_jobs
            ):
                self._reloaded_ocr_jobs.add(job_id)
                self.runtime.request(
                    "get_project",
                    {"project_id": self.project_state.projectId},
                    context={"ocr_completed": True, "ocr_job_id": job_id,
                             "ocr_succeeded": item.get("state") == "succeeded",
                             "ocr_live_completion": job_id in self._observed_active_ocr_jobs,
                             "project_id": self.project_state.projectId},
                )
            # A production TTS review report does not modify the project: the
            # worker deliberately withholds the final WAV until timing/quality
            # blockers are resolved.  Refreshing the project for that failed
            # review used to trigger the generic get_project handler, which
            # navigated to Timeline and made the UI look as if a voice track
            # had been created even though D1 was still absent.  Only a PASS
            # can change the project snapshot (via the guarded WAV import).
            is_tts_terminal = item.get("state") == "succeeded"
            if (
                is_tts_terminal
                and isinstance(spec, dict)
                and spec.get("kind") == "tts"
                and spec.get("project_id") == self.project_state.projectId
                and isinstance(spec.get("provider_config"), dict)
                and spec["provider_config"].get("purpose") == "production"
                and job_id
                and job_id not in self._reloaded_tts_jobs
            ):
                self._reloaded_tts_jobs.add(job_id)
                self.runtime.request(
                    "get_project",
                    {"project_id": self.project_state.projectId},
                    context={"tts_completed": True, "tts_job_id": job_id,
                             "project_id": self.project_state.projectId},
                )
            is_translation_terminal = item.get("state") == "succeeded" or (
                item.get("state") == "failed"
                and item.get("error_code") == "translation_review_required"
            )
            if (
                is_translation_terminal
                and isinstance(spec, dict)
                and spec.get("kind") == "translate"
                and spec.get("project_id") == self.project_state.projectId
                and job_id
                and job_id not in self._reloaded_translation_jobs
            ):
                self._reloaded_translation_jobs.add(job_id)
                provider_config = spec.get("provider_config", {})
                is_timing_repair = (
                    isinstance(provider_config, dict)
                    and provider_config.get("purpose") == "voice_timing_repair"
                )
                self.runtime.request(
                    "get_project",
                    {"project_id": self.project_state.projectId},
                    context=(
                        {
                            "voice_timing_repair_job_id": job_id,
                            "parent_tts_job_id": str(provider_config.get("parent_tts_job_id", "")),
                            "voice_timing_repair_succeeded": item.get("state") == "succeeded",
                        }
                        if is_timing_repair
                        else {"terminal_translation_job_id": job_id}
                    ),
                )
        self._notify_terminal_job_changes(raw_items)
        self._complete_translation_switch(raw_items)

    @Slot(object)
    def _replace_multi_videos(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        raw_items = payload.get("items", [])
        batch = payload.get("batch", {})
        incoming = (
            [dict(item) for item in raw_items if isinstance(item, dict)]
            if isinstance(raw_items, list)
            else []
        )
        next_batch = dict(batch) if isinstance(batch, dict) else {"state": "idle"}
        hidden_count = payload.get("hidden_count", self._multi_hidden_count)
        try:
            next_hidden_count = max(0, int(hidden_count))
        except (TypeError, ValueError):
            next_hidden_count = self._multi_hidden_count
        batch_state = str(next_batch.get("state", "idle"))
        incoming = [{**item, "batch_state": batch_state} for item in incoming]
        partial = payload.get("partial") is True
        if partial:
            existing = {str(item.get("project_id", "")): dict(item) for item in self._multi_items}
            items = [
                {**existing.get(str(item.get("project_id", "")), {}), **item} for item in incoming
            ]
        else:
            items = incoming
        changed = (
            items != self._multi_items
            or next_batch != self._multi_batch
            or next_hidden_count != self._multi_hidden_count
        )
        self._multi_items = items
        self._multi_batch = next_batch
        self._multi_hidden_count = next_hidden_count
        if partial:
            self.multi_videos.merge(items)
        else:
            self.multi_videos.replace(items)
        if changed:
            self.multiVideoChanged.emit()

    def _request_translation_deltas(
        self,
        job_id: str,
        project_id: str,
        *,
        terminal: bool = False,
    ) -> None:
        if (
            not self._backend_ready
            or not job_id
            or not project_id
            or job_id in self._translation_event_requests
        ):
            return
        self._translation_event_requests.add(job_id)
        try:
            self.runtime.request(
                "list_events",
                {
                    "job_id": job_id,
                    "after_sequence": self._translation_event_sequences.get(job_id, 0),
                    "kind": "translation_delta",
                },
                context={
                    "translation_deltas": True,
                    "job_id": job_id,
                    "project_id": project_id,
                    "terminal": terminal,
                },
            )
        except Exception:
            self._translation_event_requests.discard(job_id)
            raise

    def _request_translation_qa_status(
        self,
        job_id: str,
        project_id: str,
        *,
        terminal: bool = False,
    ) -> None:
        if (
            not self._backend_ready
            or not job_id
            or not project_id
            or job_id in self._translation_qa_event_requests
        ):
            return
        self._translation_qa_event_requests.add(job_id)
        try:
            self.runtime.request(
                "list_events",
                {
                    "job_id": job_id,
                    "after_sequence": self._translation_qa_event_sequences.get(job_id, 0),
                    "kind": "translation_qa_status",
                },
                context={
                    "translation_qa_status": True,
                    "job_id": job_id,
                    "project_id": project_id,
                    "terminal": terminal,
                },
            )
        except Exception:
            self._translation_qa_event_requests.discard(job_id)
            raise

    def _request_voice_timing_review(self, job_id: str) -> None:
        if not self._backend_ready or not job_id or job_id in self._voice_timing_review_requests:
            return
        self._voice_timing_review_requests.add(job_id)
        self.runtime.request(
            "get_voice_timing_review",
            {"job_id": job_id},
            context={"voice_timing_review": True, "job_id": job_id},
        )

    def _request_voice_timing_repair(self, tts_job_id: str) -> None:
        if (
            not self._backend_ready
            or not self.project_state.projectId
            or not tts_job_id
            or tts_job_id in self._voice_timing_repair_requests
        ):
            return
        self._voice_timing_repair_requests.add(tts_job_id)
        self._set_voice_timing_auto_repair_busy(True)
        self._set_translation_job_state("start_requested")
        self._set_translation_busy(True)
        self.runtime.request(
            "create_voice_timing_repair_job",
            {
                "project_id": self.project_state.projectId,
                "tts_job_id": tts_job_id,
                "translation_job_id": self._selected_translation_cache_id,
            },
            context={
                "voice_timing_repair": True,
                "tts_job_id": tts_job_id,
            },
        )
        self._set_status("Đang tạo tác vụ rút gọn riêng các cue voice quá dài")

    def _request_voice_timing_retry(
        self,
        *,
        parent_tts_job_id: str,
        repair_job_id: str,
    ) -> None:
        if (
            not self._backend_ready
            or not self.project_state.projectId
            or not parent_tts_job_id
            or not repair_job_id
            or repair_job_id in self._voice_timing_retry_requests
        ):
            return
        self._voice_timing_retry_requests.add(repair_job_id)
        self._set_tts_job_state("start_requested")
        self._set_tts_busy(True)
        self.runtime.request(
            "create_tts_timing_retry_job",
            {
                "project_id": self.project_state.projectId,
                "parent_tts_job_id": parent_tts_job_id,
                "repair_job_id": repair_job_id,
            },
            context={
                "voice_timing_retry": True,
                "parent_tts_job_id": parent_tts_job_id,
                "repair_job_id": repair_job_id,
            },
        )
        self._set_status("Đã rút gọn cue dài; đang chuẩn bị tạo lại đúng WAV đã đổi")

    @Slot(str, object, object)
    def _action_succeeded(self, action: str, result: object, context: object) -> None:
        if action == "get_tts_voice_preview":
            if not self._casting_preview_lookup_is_current(context):
                return
            descriptor = result if isinstance(result, dict) else {}
            path = Path(str(descriptor.get("path", ""))).expanduser()
            if descriptor.get("available") is True and path.is_file():
                self._voice_preview_target_job_id = ""
                self.stopVoiceCatalogPreview()
                self._voice_preview_source = QUrl.fromLocalFile(str(path.resolve())).toString()
                self.voicePreviewChanged.emit()
                self._set_tts_job_purpose("preview")
                self._set_tts_job_state("succeeded")
                self._set_status("Đang phát mẫu nghe thử đúng giọng đã chọn")
                return
            self._start_casting_preview_job(context)
            return
        if action == "list_translation_models":
            if (not isinstance(context, dict)
                    or context.get("catalog_generation") != self._translation_catalog_generation):
                return
            self._translation_catalog_busy = False
            data = result if isinstance(result, dict) else {}
            self._translation_catalog_models = [str(m) for m in data.get("models", [])]
            self._translation_catalog_status = str(data.get("error") or (
                f"API trả {len(self._translation_catalog_models)} model"
                + (" (danh sách một phần)" if data.get("partial") else "")
                + ". Vẫn có thể nhập tên thủ công."
            ))
            self.translationModelCatalogChanged.emit()
            return
        if action == "save_project_edits":
            self._project_save_finished(result, context)
            return
        if action in {
            "get_dialogue_casting",
            "configure_dialogue_casting",
            "set_dialogue_casting_labels",
        }:
            request_context = context if isinstance(context, dict) else {}
            if str(request_context.get("project_id", "")) != self.project_state.projectId:
                return
            if action in {"configure_dialogue_casting", "set_dialogue_casting_labels"}:
                self._dialogue_casting_saving = False
            self._replace_dialogue_casting(result)
            if action == "configure_dialogue_casting":
                self._set_status("Đã lưu cấu hình Phân vai thoại trong project")
                role = request_context.get("import_preview_role", "")
                if (role in {"male", "female", "fallback"}
                        and request_context.get("import_preview_serial")
                        == self._casting_preview_request_serial):
                    self.previewDialogueCastingVoice(role)
            elif action == "set_dialogue_casting_labels":
                self._set_status("Đã lưu nhãn thoại và ghi đè thủ công trong project")
            return
        if action == "create_dialogue_casting_job":
            request_context = context if isinstance(context, dict) else {}
            if str(request_context.get("project_id", "")) != self.project_state.projectId:
                return
            automatic_tts = (request_context.get("automatic_tts") is True
                             or (self._dialogue_casting_auto_tts
                                 and isinstance(self._pending_tts_start, dict)))
            if isinstance(result, dict) and result.get("ready") is True:
                self._replace_dialogue_casting(result)
                self._dialogue_casting_auto_tts = automatic_tts
                self._dialogue_casting_auto_tts_job_id = ""
                self._set_dialogue_casting_job_state("succeeded")
                if automatic_tts:
                    self._continue_pending_tts_start()
                else:
                    self._set_status("Nhãn thoại đã khớp âm thanh hiện tại")
                self._resume_pending_casting_after_ocr()
                return
            job_id = str(result.get("id", "")) if isinstance(result, dict) else ""
            if not job_id:
                self._dialogue_casting_auto_tts = False
                self._dialogue_casting_auto_tts_job_id = ""
                self._set_dialogue_casting_job_state("failed")
                if automatic_tts:
                    self._pending_tts_start = None
                    self._set_tts_busy(False)
                    self._set_tts_job_state("failed")
                self._set_status("Job Manager không trả về mã tác vụ phân vai thoại")
                self._resume_pending_casting_after_ocr()
                return
            state = str(result.get("state", "queued"))
            self._dialogue_casting_job_id = job_id
            self._dialogue_casting_auto_tts = automatic_tts
            self._dialogue_casting_auto_tts_job_id = job_id if automatic_tts else ""
            self._set_dialogue_casting_job_state(state)
            if state in {"queued", "start_requested"}:
                self._start_dialogue_casting_worker(job_id)
            elif state in {"succeeded", "failed", "cancelled", "interrupted"}:
                self._finish_dialogue_casting_job(job_id, state)
            self._set_status(
                "Đang phân vai trước khi tạo D1" if automatic_tts else "Đang nhận diện giọng thoại"
            )
            return
        if (action == "effect_command" and isinstance(context, dict)
                and context.get("project_id")
                and context["project_id"] != self.project_state.projectId):
            return
        if action == "subtitle_command" and not self._subtitle_command_response_is_current(context):
            return
        if action == "copy_video_clip":
            if isinstance(result, dict) and result.get("copied") is True:
                self._video_clipboard_project_id = str(result.get("project_id", ""))
                self.videoClipboardChanged.emit()
                self._set_status("Đã sao chép video và âm thanh liên kết")
            return
        if action == "subtitle_command" and not self._background_save_response_is_current(context):
            return
        if action == "subtitle_command" and not self._scene_timing_response_is_current(context):
            return
        if (action == "subtitle_command"
                and not self._subtitle_geometry_response_is_current(context)):
            return
        if (
            action == "subtitle_command" and isinstance(result, dict)
            and isinstance(context, dict) and context.get("background_subtitle_save") is True
            and result.get("_subtitle_patch") is True
            and result.get("id") == self.project_state.projectId
            and type(result.get("revision")) is int
            and result["revision"] < self.project_state.content_revision
        ):
            # Decode can finish out of order. The newer durable head already
            # includes this saved edit; do not roll it back as a stale patch.
            self._finish_background_subtitle_save()
            return
        if (
            action == "timeline_command"
            and isinstance(context, dict)
            and context.get("timeline_cut") is True
            and self._timeline_cut_context_is_current(context)
            and not isinstance(result, dict)
        ):
            self._action_failed(
                action,
                {
                    "code": "timeline_patch_invalid",
                    "message": "Job Manager trả về dữ liệu cắt video không hợp lệ",
                },
                context,
            )
            return
        if (
            action == "timeline_command"
            and isinstance(result, dict)
            and result.get("_timeline_patch") is True
            and isinstance(context, dict)
            and context.get("timeline_cut") is True
            and self._timeline_cut_context_is_current(context)
        ):
            self.runtime.project_timeline_patch(
                self.project_state.content_patch_snapshot(),
                result,
                context,
            )
            self._set_status("Đang cập nhật hiển thị sau khi cắt…")
            return
        if (action == "subtitle_command" and isinstance(result, dict)
                and result.get("_subtitle_patch") is True
                and "_prepared_subtitle_snapshot" not in result
                and (not isinstance(context, dict) or not context.get("background_subtitle_save"))
                and (not isinstance(context, dict) or not (
                    context.get("scene_timing_generation")
                    or context.get("subtitle_geometry_generation")))
                and len(self.project_state._canonical_snapshot.get("source_cues", [])) >= 1000):
            self.runtime.project_timeline_patch(
                self.project_state.content_patch_snapshot(), result,
                {**(context if isinstance(context, dict) else {}),
                 "subtitle_patch_projection": True},
            )
            return
        if action in {
            "reserve_update_install",
            "release_update_install",
            "get_update_install_eligibility",
        }:
            self.updater_model.action_succeeded(action, result)
            return
        if action in {"set_translation_preferences", "save_translation_style"}:
            self._translation_preferences_saved(action, result, context)
            return
        if action == "export_subtitle_srt":
            self._set_subtitle_export_busy(False)
            if not isinstance(result, dict):
                self._set_status("Job Manager trả về kết quả xuất SRT không hợp lệ")
                return
            path = str(result.get("path", ""))
            cue_count = int(result.get("cue_count", 0) or 0)
            language = str(result.get("language", ""))
            label = "nguồn" if language == "source" else "tiếng Việt"
            self._set_status(f"Đã xuất {cue_count} cue SRT {label}: {path}")
            return
        if action == "create_project_with_media":
            request_context = context if isinstance(context, dict) else {}
            if "multi_import_path" not in request_context:
                return
            project = result.get("project") if isinstance(result, dict) else None
            project_id = str(project.get("id", "")) if isinstance(project, dict) else ""
            if not project_id:
                self._finish_failed_multi_import(
                    "Không tạo được project cho video này",
                    str(request_context.get("multi_import_path", "")),
                )
                return
            self._multi_import_project_ids.append(project_id)
            if self._multi_import_paths:
                self._multi_import_paths.pop(0)
            completed = len(self._multi_import_project_ids)
            remaining = len(self._multi_import_paths)
            self._save_state = f"Đang nhập {completed}/{completed + remaining} video"
            self.saveStateChanged.emit()
            self._create_next_multi_project()
            return
        if action == "multi_add":
            request_context = context if isinstance(context, dict) else {}
            partial_error = str(request_context.get("multi_import_partial_error", ""))
            failed_path = str(request_context.get("failed_path", ""))
            total = int(request_context.get("total", 0) or 0)
            self._set_import_busy(False)
            self._save_state = "Đã lưu"
            self.saveStateChanged.emit()
            self._multi_import_paths = []
            count = len(self._multi_import_project_ids)
            self._multi_import_project_ids = []
            self.runtime.request("list_projects")
            self.navigate("multi")
            if partial_error:
                failed_name = Path(failed_path).name if failed_path else "video tiếp theo"
                suffix = f"/{total}" if total else ""
                self._set_status(
                    f"Đã thêm và chọn {count}{suffix} video. "
                    f"Dừng tại {failed_name}: {partial_error}"
                )
            else:
                self._set_status(f"Đã thêm {count} video vào Multi video")
            return
        if action == "multi_remove":
            request_context = context if isinstance(context, dict) else {}
            project_id = str(request_context.get("project_id", ""))
            items = result.get("items", []) if isinstance(result, dict) else []
            pending = next(
                (
                    item
                    for item in items
                    if isinstance(item, dict)
                    and str(item.get("project_id", "")) == project_id
                    and item.get("error_code") == "membership_removal_pending"
                ),
                None,
            )
            if pending is not None:
                self._set_status(
                    "Đang chờ tác vụ dừng an toàn; video sẽ tự biến mất khỏi Multi sau khi dừng."
                )
                return
            self._set_status(
                "Đã xóa khỏi Multi video; project, media và cache vẫn được giữ nguyên."
            )
            return
        if action == "multi_reorder":
            self._set_status("Đã cập nhật thứ tự Multi video")
            return
        if action in {"multi_select", "multi_select_all"}:
            return
        if action in {"multi_start", "multi_pause", "multi_resume"}:
            self._finish_multi_control(action)
            if action == "multi_start":
                self._set_status(
                    "Multi video đã bắt đầu; các video lỗi đang tiếp tục từ stage/cache hiện tại"
                )
            elif action == "multi_pause":
                self._set_status("Đã nhận yêu cầu tạm dừng; worker sẽ dừng trong vài giây")
            else:
                self._set_status("Đã tiếp tục Multi video từ checkpoint/cache")
            return
        if action == "multi_restore_hidden":
            self._set_status("Đã khôi phục các video đã ẩn khỏi Multi video")
            return
        if action == "multi_preview_descriptor":
            descriptor = dict(result) if isinstance(result, dict) else {}
            request_context = context if isinstance(context, dict) else {}
            for key in ("video_path", "original_audio_path", "voice_audio_path"):
                path = str(descriptor.get(key, ""))
                descriptor[key.removesuffix("_path") + "_url"] = (
                    QUrl.fromLocalFile(path) if path else QUrl()
                )
            self.multiPreviewReady.emit(
                descriptor,
                bool(request_context.get("use_voice", False)),
            )
            return
        if action == "estimate_voice_conform":
            self._voice_conform_estimate_request_pending = False
            request_context = context if isinstance(context, dict) else {}
            request_generation = request_context.get("generation")
            try:
                request_generation = int(request_generation)
            except (TypeError, ValueError):
                request_generation = -1
            request_project_id = str(request_context.get("project_id", ""))
            is_current_request = "generation" not in request_context or (
                request_generation == self._voice_conform_estimate_generation
                and request_project_id == self.project_state.projectId
                and str(request_context.get("mode", "")) == self._voice_conform_mode
                and abs(
                    float(request_context.get("max_audio_speed", -1.0))
                    - self._voice_conform_max_audio_speed
                )
                < 1e-9
            )
            if not is_current_request:
                # A mode/slider change arrived while the old estimate was
                # running.  Never let that old response overwrite the newly
                # selected settings; immediately service the queued request.
                self._voice_conform_estimate_refresh_queued = True
                self.refreshVoiceConformEstimate()
                return
            if not isinstance(result, dict):
                self._set_status("Ước lượng đồng bộ voice trả về dữ liệu không hợp lệ")
                return
            if str(result.get("project_id", "")) != self.project_state.projectId:
                return
            self._voice_conform_estimate = dict(result)
            self._voice_conform_parent_tts_job_id = str(result.get("parent_tts_job_id", ""))
            self._voice_conform_applied = result.get("applied") is True
            self.voiceConformChanged.emit()
            self._set_status(
                "Ước lượng đồng bộ đạt QA; có thể áp dụng từ cache WAV"
                if result.get("can_apply") is True
                else f"Ước lượng đồng bộ còn {result.get('unresolved_count', 0)} cue cần xử lý"
            )
            return
        if action == "create_voice_conform_job":
            if not isinstance(result, dict):
                self._voice_conform_busy = False
                self._voice_conform_job_state = "failed"
                self.voiceConformChanged.emit()
                self._set_status("Không tạo được tác vụ đồng bộ voice")
                return
            job_id = str(result.get("id", ""))
            if not job_id:
                self._voice_conform_busy = False
                self._voice_conform_job_state = "failed"
                self.voiceConformChanged.emit()
                self._set_status("Job Manager không trả về mã tác vụ đồng bộ voice")
                return
            self._voice_conform_job_id = job_id
            state = str(result.get("state", "queued"))
            self._voice_conform_job_state = state
            self._voice_conform_busy = state not in {"succeeded", "failed", "interrupted"}
            self.voiceConformChanged.emit()
            if state in {"queued", "start_requested"}:
                self.runtime.request(
                    "start_job",
                    {
                        "job_id": job_id,
                        "program": str(runtime_program_path()),
                        "arguments": ["-m", "ducai_studio.workers.tts_worker", job_id],
                        "resume_stage_key": "tts",
                        "resume_checkpoint_key": "latest",
                    },
                    context={"job_kind": "tts", "voice_conform": True},
                )
            elif state == "interrupted":
                # ``create_voice_conform_job`` deliberately reuses an
                # interrupted cache-only job.  Requeue that exact job so the
                # worker resumes its conform checkpoint instead of leaving
                # the UI idle with an unusable job id.
                self._voice_conform_job_state = "resume_requested"
                self._voice_conform_busy = True
                self.voiceConformChanged.emit()
                self.runtime.request(
                    "retry_job",
                    {"job_id": job_id},
                    context={
                        "job_kind": "tts",
                        "voice_conform": True,
                        "resume_voice_conform": True,
                        "job_id": job_id,
                    },
                )
            elif state == "succeeded":
                self._voice_conform_applied = True
                self.runtime.request(
                    "get_project",
                    {"project_id": self.project_state.projectId},
                    context={"voice_conform_applied": True},
                )
            self._set_status("Đang đồng bộ voice từ cache WAV; không gọi lại VieNeu")
            return
        if action == "reset_voice_conform":
            self._voice_conform_busy = False
            self._voice_conform_job_state = ""
            self._voice_conform_estimate = {}
            self._voice_conform_applied = False
            self.voiceConformChanged.emit()
            if isinstance(result, dict):
                self._apply_project(result)
            self._set_status("Đã khôi phục nhịp D1 nền")
            return
        if action == "get_voice_timing_review":
            review_context = context if isinstance(context, dict) else {}
            job_id = str(review_context.get("job_id", ""))
            self._voice_timing_review_requests.discard(job_id)
            if not isinstance(result, dict):
                self._set_status("Job Manager trả về QA timing voice không hợp lệ")
                return
            if str(result.get("project_id", self.project_state.projectId)) != (
                self.project_state.projectId
            ):
                return
            self._voice_timing_review_requests.add(job_id)
            self._set_voice_timing_qa(result)
            issues = result.get("issues", [])
            if isinstance(issues, list) and issues:
                if result.get("qa_non_blocking") is not True:
                    self.navigate("translate")
                    first = issues[0]
                    if isinstance(first, dict) and type(first.get("id")) is int:
                        self.selectTranslationQaIssue(int(first["id"]))
                review_message = str(result.get("message", "Voice có cue quá dài"))
                if result.get("error_code"):
                    speed = float(result.get("cue_max_total_speed", 1.0) or 1.0)
                    review_message = (
                        f"{review_message}: {len(issues)} cue vượt "
                        f"giới hạn tổng {speed:.2f}×; cache WAV vẫn được giữ, "
                        "nên Tiếp tục từ cache sẽ kiểm tra lại chứ không tự bỏ qua QA."
                    )
                self._set_status(review_message)
                if (
                    result.get("qa_non_blocking") is not True
                    and result.get("status") == "REVIEW"
                    and self.voiceAutoCondenseLongCuesEnabled
                    and result.get("repairable") is True
                ):
                    self._request_voice_timing_repair(job_id)
            return
        if action == "create_voice_timing_repair_job":
            repair_context = context if isinstance(context, dict) else {}
            parent_tts_job_id = str(repair_context.get("tts_job_id", ""))
            self._voice_timing_repair_requests.discard(parent_tts_job_id)
            if not isinstance(result, dict):
                self._set_voice_timing_auto_repair_busy(False)
                self._set_translation_busy(False)
                self._set_translation_job_state("review")
                self._set_status("Không tạo được tác vụ rút gọn cue dài")
                return
            job_id = str(result.get("id", ""))
            if not job_id:
                self._set_voice_timing_auto_repair_busy(False)
                self._set_translation_busy(False)
                self._set_translation_job_state("review")
                self._set_status("Job Manager không trả về mã tác vụ rút gọn cue dài")
                return
            self._voice_timing_repair_requests.add(parent_tts_job_id)
            self._translation_job_id = job_id
            self._selected_translation_cache_id = job_id
            spec = result.get("spec", {})
            self._translation_provider_id = (
                str(spec.get("provider_id", "")) if isinstance(spec, dict) else ""
            )
            self._translation_model_id = (
                str(spec.get("model_id", "")) if isinstance(spec, dict) else ""
            )
            self.translationCachesChanged.emit()
            self.translationBusyChanged.emit()
            state = str(result.get("state", "queued"))
            if state == "succeeded":
                self._set_voice_timing_auto_repair_busy(False)
                self._set_translation_busy(False)
                self._set_translation_job_state("succeeded")
                self.runtime.request(
                    "get_project",
                    {"project_id": self.project_state.projectId},
                    context={
                        "voice_timing_repair_job_id": job_id,
                        "parent_tts_job_id": parent_tts_job_id,
                        "voice_timing_repair_succeeded": True,
                    },
                )
                return
            if state not in {
                "queued",
                "start_requested",
                "running",
                "pause_requested",
                "paused",
                "resume_requested",
            }:
                self._set_voice_timing_auto_repair_busy(False)
                self._set_translation_busy(False)
                self._set_translation_job_state("review")
                self.navigate("translate")
                self._set_status("Tác vụ rút gọn cue dài cần kiểm tra thủ công")
                return
            self._set_translation_job_state("start_requested")
            self._set_translation_busy(True)
            if state in {"queued", "start_requested"}:
                self._start_translation_worker(job_id)
            else:
                self.runtime.request("list_jobs")
            self._set_status("Đang rút gọn riêng các cue voice quá dài từ cache dịch")
            return
        if action == "create_tts_timing_retry_job":
            retry_context = context if isinstance(context, dict) else {}
            repair_job_id = str(retry_context.get("repair_job_id", ""))
            self._voice_timing_retry_requests.discard(repair_job_id)
            if not isinstance(result, dict):
                self._set_tts_busy(False)
                self._set_tts_job_state("review")
                self._set_status("Không tạo được tác vụ voice sau khi rút gọn")
                return
            job_id = str(result.get("id", ""))
            if not job_id:
                self._set_tts_busy(False)
                self._set_tts_job_state("review")
                self._set_status("Job Manager không trả về mã tác vụ tạo lại voice")
                return
            self._voice_timing_retry_requests.add(repair_job_id)
            self._tts_job_id = job_id
            self.ttsBusyChanged.emit()
            state = str(result.get("state", "queued"))
            if state == "succeeded":
                self._set_tts_busy(False)
                self._set_tts_job_state("succeeded")
                self.runtime.request("list_jobs")
                return
            if state not in {
                "queued",
                "start_requested",
                "running",
                "pause_requested",
                "paused",
                "resume_requested",
            }:
                self._set_tts_busy(False)
                self._set_tts_job_state("review")
                self.navigate("translate")
                self._set_status("Tạo lại voice vẫn cần rà soát cue dài")
                return
            self._set_tts_job_state("start_requested")
            self._set_tts_busy(True)
            if state in {"queued", "start_requested"}:
                self._start_tts_worker(job_id)
            else:
                self.runtime.request("list_jobs")
            self._set_status("Đang dò cache WAV; chỉ cue đã rút gọn mới được tạo lại")
            return
        if action == "create_tts_timing_override_job":
            override_context = context if isinstance(context, dict) else {}
            parent_tts_job_id = str(override_context.get("parent_tts_job_id", ""))
            self._voice_timing_override_requests.discard(parent_tts_job_id)
            if not isinstance(result, dict) or not str(result.get("id", "")):
                self._set_tts_busy(False)
                self._set_tts_job_state("review")
                self._set_status("Không tạo được tác vụ thêm voice lỗi vào Timeline")
                return
            self._voice_timing_override_requests.add(parent_tts_job_id)
            job_id = str(result["id"])
            self._tts_job_id = job_id
            self.ttsBusyChanged.emit()
            state = str(result.get("state", "queued"))
            if state == "succeeded":
                self._set_tts_busy(False)
                self._set_tts_job_state("succeeded")
                self.runtime.request("list_jobs")
                return
            if state not in {
                "queued",
                "start_requested",
                "running",
                "pause_requested",
                "paused",
                "resume_requested",
            }:
                self._set_tts_busy(False)
                self._set_tts_job_state("review")
                self._set_status("Tác vụ thêm voice lỗi vào Timeline chưa thể chạy")
                return
            self._set_tts_job_state("start_requested")
            self._set_tts_busy(True)
            if state in {"queued", "start_requested"}:
                self._start_tts_worker(job_id)
            else:
                self.runtime.request("list_jobs")
            self._set_status("Đang dùng cache WAV để ghép nguyên voice vào Timeline")
            return
        if action == "list_translation_caches":
            self._translation_cache_refresh_pending = False
            project_id = (
                str(context.get("project_id", "")) if isinstance(context, dict) else ""
            ) or self.project_state.projectId
            if project_id != self.project_state.projectId:
                return
            caches = (
                [dict(item) for item in result if isinstance(item, dict)]
                if isinstance(result, list)
                else []
            )
            available = {str(item.get("job_id", "")) for item in caches}
            self._pending_history_deletion_ids.intersection_update(available)
            selected = self._selected_translation_cache_id
            controls_own_preview = self._translation_controls_own_preview()
            if controls_own_preview:
                if not self._translation_cache_selection_explicit:
                    selected = self._translation_job_id
            elif selected not in available:
                selected = str(caches[0].get("job_id", "")) if caches else ""
                self._translation_cache_selection_explicit = False
            changed = (
                caches != self._translation_caches
                or selected != self._selected_translation_cache_id
            )
            self._translation_caches = caches
            self._selected_translation_cache_id = selected
            if changed:
                self.translationCachesChanged.emit()
            if selected and selected in available:
                selected_cache = next(
                    (item for item in caches if str(item.get("job_id", "")) == selected),
                    None,
                )
                # Cache replies can predate creation/resume of the current
                # worker. Only job polling owns its live lifecycle state.
                if isinstance(selected_cache, dict) and not controls_own_preview:
                    self._bind_translation_cache_record(selected_cache)
                self.runtime.request(
                    "get_translation_preview",
                    {"project_id": project_id, "job_id": selected},
                    context={
                        "project_id": project_id,
                        "cache_selection": True,
                        "cache_job_id": selected,
                    },
                )
            return
        if action == "get_translation_preview" and isinstance(result, dict):
            project_id = str(result.get("project_id", ""))
            if not project_id or project_id != self.project_state.projectId:
                return
            job_id = str(result.get("job_id", ""))
            if not self._translation_preview_matches_job(job_id):
                return
            preview_context = context if isinstance(context, dict) else {}
            requested_job_id = str(preview_context.get("cache_job_id", ""))
            if requested_job_id and requested_job_id != self._selected_translation_cache_id:
                return
            if requested_job_id and job_id != requested_job_id:
                return
            if (
                not requested_job_id
                and self._selected_translation_cache_id
                and job_id != self._selected_translation_cache_id
            ):
                return
            if job_id and job_id != self._selected_translation_cache_id:
                self._selected_translation_cache_id = job_id
                self.translationCachesChanged.emit()
            if job_id and not self._translation_busy:
                self._translation_job_id = job_id
                self._translation_provider_id = str(result.get("provider_id", ""))
                self._translation_model_id = str(result.get("model_id", ""))
                self._bind_translation_configuration(result.get("provider_config"))
                display_state = str(result.get("display_state", result.get("state", "")))
                self._set_translation_job_state(display_state)
                self.translationBusyChanged.emit()
            if isinstance(result.get("qa"), dict):
                self._set_translation_qa(result["qa"], reconcile_with_project=False)
            values = result.get("values")
            if not isinstance(values, list):
                return
            changed = self.project_state.subtitle_cues.apply_translation_delta(values)
            if changed:
                self.project_state.subtitle_cues.finalize_translation_preview()
                if self._selected_cue_id:
                    self.selectedCueChanged.emit()
            return
        if (
            action == "list_events"
            and isinstance(context, dict)
            and context.get("translation_deltas") is True
        ):
            job_id = str(context.get("job_id", ""))
            project_id = str(context.get("project_id", ""))
            self._translation_event_requests.discard(job_id)
            if project_id != self.project_state.projectId:
                return
            if not self._translation_preview_matches_job(job_id):
                return
            events = result if isinstance(result, list) else []
            cursor = self._translation_event_sequences.get(job_id, 0)
            selected_changed = False
            for event in events:
                if not isinstance(event, dict):
                    continue
                sequence = event.get("sequence")
                if type(sequence) is int:
                    cursor = max(cursor, sequence)
                if event.get("kind") != "translation_delta":
                    continue
                payload = event.get("payload")
                values = payload.get("values") if isinstance(payload, dict) else None
                if project_id != self.project_state.projectId or not isinstance(values, list):
                    continue
                self.project_state.subtitle_cues.apply_translation_delta(values)
                if self._selected_cue_id and any(
                    isinstance(value, dict) and value.get("id") == self._selected_cue_id
                    for value in values
                ):
                    selected_changed = True
            self._translation_event_sequences[job_id] = cursor
            if context.get("terminal") is True:
                if self.project_state.subtitle_cues.finalize_translation_preview() > 0:
                    selected_changed = bool(self._selected_cue_id)
                self._translation_terminal_event_jobs.add(job_id)
                # A terminal checkpoint may be the first durable cache for a
                # newly failed/cancelled job. Refresh the selector so every
                # compatible historical job becomes available without a
                # project reload.
                self._request_translation_cache_list(project_id)
            if selected_changed:
                self.selectedCueChanged.emit()
            return
        if (
            action == "list_events"
            and isinstance(context, dict)
            and context.get("translation_qa_status") is True
        ):
            job_id = str(context.get("job_id", ""))
            project_id = str(context.get("project_id", ""))
            self._translation_qa_event_requests.discard(job_id)
            if project_id != self.project_state.projectId:
                return
            if not self._translation_preview_matches_job(job_id):
                return
            events = result if isinstance(result, list) else []
            cursor = self._translation_qa_event_sequences.get(job_id, 0)
            for event in events:
                if not isinstance(event, dict):
                    continue
                sequence = event.get("sequence")
                if type(sequence) is int:
                    cursor = max(cursor, sequence)
                if event.get("kind") != "translation_qa_status":
                    continue
                payload = event.get("payload")
                if isinstance(payload, dict):
                    self._set_translation_qa(payload, reconcile_with_project=False)
            self._translation_qa_event_sequences[job_id] = cursor
            return
        if action == "list_credentials" and isinstance(result, dict):
            raw_vault = result.get("references", [])
            vault = _stable_credential_references(raw_vault)
            vault_by_name = {value.casefold(): value for value in vault}
            provider_ids = {"gemini-api", *OPENAI_COMPATIBLE_PROVIDER_IDS}
            claimed_elsewhere = {
                reference.casefold()
                for provider_id in provider_ids - {"gemini-api"}
                for reference in self._translation_api_references(provider_id)
            }
            for provider_id in provider_ids:
                saved = [
                    vault_by_name[value.casefold()]
                    for value in self._translation_api_references(provider_id)
                    if value.casefold() in vault_by_name
                ]
                if provider_id == "gemini-api":
                    saved = [value for value in saved if value.casefold() not in claimed_elsewhere]
                self.settings.setValue(
                    _credential_setting_key(provider_id),
                    _stable_credential_references(saved),
                )
            self.translationSettingsChanged.emit()
            return
        if action == "inspect_media_usage" and isinstance(result, dict):
            media_path = context.get("media_path") if isinstance(context, dict) else None
            intent = context.get("intent") if isinstance(context, dict) else None
            if not isinstance(media_path, str) or intent not in {"new_project", "append"}:
                self._set_import_busy(False)
                self._set_status("Kết quả kiểm tra media không hợp lệ")
                return
            usages = result.get("usages", [])
            if not isinstance(usages, list):
                usages = []
            if not usages:
                self._continue_media_import(Path(media_path), intent=intent)
                return
            path = Path(media_path)
            names = {
                str(item.get("project_name", "")).strip().casefold()
                for item in usages
                if isinstance(item, dict)
            }
            suffix = 2
            suggested_name = f"{path.stem} ({suffix})"
            while suggested_name.casefold() in names:
                suffix += 1
                suggested_name = f"{path.stem} ({suffix})"
            self._pending_media_path = str(path)
            self._pending_media_intent = intent
            self._duplicate_media_info = {
                "media_path": str(path),
                "media_name": path.name,
                "intent": intent,
                "suggested_project_name": suggested_name,
                "usage_count": len(usages),
                "usages": usages,
            }
            self.duplicateMediaChanged.emit()
            self._save_state = "Chờ xử lý media trùng"
            self.saveStateChanged.emit()
            self._set_status("Media đã có trong dự án; hãy chọn dùng lại hoặc tạo bản mới")
            self.duplicateMediaDialogRequested.emit()
            return
        if action == "runtime_diagnostics" and isinstance(result, dict):
            self._runtime_diagnostics = result
            self.diagnosticsChanged.emit()
            self.ocrSettingsChanged.emit()
            state = str(result.get("overall_status", "unknown"))
            self._set_status(f"Chẩn đoán runtime hoàn tất: {state}")
            return
        if action in {
            "optimize_project_database",
            "get_database_optimization",
            "cancel_database_optimization",
        } and isinstance(result, dict):
            self._database_maintenance = dict(result)
            self.databaseMaintenanceChanged.emit()
            state = str(result.get("state", ""))
            if state in {"running", "cancelling"}:
                if not self._database_maintenance_timer.isActive():
                    self._database_maintenance_timer.start()
                stage_labels = {
                    "starting": "Đang chuẩn bị",
                    "copying": "Đang sao chép an toàn",
                    "compressing": "Đang nén revision",
                    "vacuuming": "Đang thu gọn SQLite",
                    "verifying": "Đang kiểm tra toàn vẹn",
                    "swapping": "Đang đổi database",
                }
                stage = stage_labels.get(str(result.get("stage", "")), "Đang tối ưu")
                current = int(result.get("current", 0) or 0)
                total = int(result.get("total", 0) or 0)
                detail = f" {current}/{total}" if total > 0 else ""
                self._set_status(
                    "Đang chờ checkpoint hủy an toàn…"
                    if state == "cancelling"
                    else f"{stage}{detail}"
                )
                return
            self._database_maintenance_timer.stop()
            if state == "cancelled":
                self._set_status(
                    str(result.get("message", "Đã hủy tối ưu; database hiện tại không đổi."))
                )
                return
            if state == "failed":
                self._set_status(
                    "Tối ưu dữ liệu dự án thất bại: "
                    + str(result.get("message", "lỗi không xác định"))
                )
                return
            before = int(result.get("before_bytes", 0) or 0)
            after = int(result.get("after_bytes", 0) or 0)
            saved = max(0, before - after)
            self._set_status(
                "Đã tối ưu dữ liệu dự án và giữ bản sao an toàn; "
                f"giảm {saved / (1024 * 1024):.1f} MB."
            )
            return
        if action == "inspect_legacy_project" and isinstance(result, dict):
            self._legacy_migration_report = result
            self.legacyMigrationChanged.emit()
            self._set_status("Dry-run hoàn tất; hãy xem skipped/warning trước khi nhập")
            return
        if action == "import_legacy_project" and isinstance(result, dict):
            project = result.get("project")
            report = result.get("report")
            if isinstance(project, dict):
                self._apply_project(project)
            if isinstance(report, dict):
                imported_settings = report.get("imported_settings")
                applied: list[str] = []
                if isinstance(imported_settings, dict):
                    for key, value in _legacy_qsettings_values(
                        imported_settings, self.project_state.projectId
                    ).items():
                        self.settings.setValue(key, value)
                        applied.append(key)
                self._legacy_migration_report = {
                    **report,
                    "applied_setting_keys": applied,
                }
                self.legacyMigrationChanged.emit()
                self.exportSettingsChanged.emit()
                self.voiceSettingsChanged.emit()
            self.runtime.request("list_projects")
            self._set_status("Đã nhập dự án 0.1.99 một chiều")
            return
        if action in {"create_ocr_job", "create_ocr_rescan_job"} and isinstance(result, dict):
            job_id = result.get("id")
            if not isinstance(job_id, str) or not job_id:
                self._set_ocr_busy(False)
                self._set_status("Job Manager không trả về mã tác vụ OCR")
                return
            self._ocr_job_id = job_id
            spec = result.get("spec")
            config = spec.get("provider_config", {}) if isinstance(spec, dict) else {}
            self._ocr_job_method = (
                str(config.get("extraction_method", "frame_ocr"))
                if isinstance(config, dict)
                else "frame_ocr"
            )
            self.ocrBusyChanged.emit()
            self._start_ocr_worker(job_id)
            self._set_status("Đang nạp bộ máy trích xuất; giao diện vẫn có thể sử dụng")
            return
        if action == "create_translation_job" and isinstance(result, dict):
            job_id = result.get("id")
            if not isinstance(job_id, str) or not job_id:
                self._set_translation_busy(False)
                self._set_status("Job Manager không trả về mã tác vụ dịch")
                return
            self._translation_job_id = job_id
            self._selected_translation_cache_id = job_id
            self._translation_cache_selection_explicit = False
            self.translationCachesChanged.emit()
            spec = result.get("spec")
            self._translation_provider_id = (
                str(spec.get("provider_id", "")) if isinstance(spec, dict) else ""
            )
            self._translation_model_id = (
                str(spec.get("model_id", "")) if isinstance(spec, dict) else ""
            )
            self._bind_translation_configuration(
                spec.get("provider_config") if isinstance(spec, dict) else None
            )
            self.translationBusyChanged.emit()
            self._start_translation_worker(job_id)
            self._set_status("Dịch chạy nền độc lập; chuyển tab không làm dừng tác vụ")
            return
        if action == "prepare_tts_rerun" and isinstance(result, dict):
            pending = self._pending_tts_start
            expected_project_id = (
                str(pending.get("params", {}).get("project_id", ""))
                if isinstance(pending, dict) and isinstance(pending.get("params"), dict)
                else ""
            )
            if not expected_project_id or str(result.get("id", "")) != expected_project_id:
                self._pending_tts_start = None
                self._set_voice_playback_enabled(True)
                self._set_tts_busy(False)
                self._set_tts_job_state("failed")
                self._set_status(
                    "Không thể xác nhận D1 đã được gỡ đúng project; chưa tạo tác vụ voice mới"
                )
                return
            self._apply_project(result)
            self._can_undo_timeline = False
            self._can_redo_timeline = False
            self.timelineHistoryChanged.emit()
            if self.project_state.hasVoiceTrack:
                self._pending_tts_start = None
                self._set_voice_playback_enabled(True)
                self._set_tts_busy(False)
                self._set_tts_job_state("failed")
                self._set_status("D1 cũ chưa được gỡ; chưa tạo tác vụ voice mới")
                return
            self._set_voice_playback_enabled(True)
            self._dispatch_pending_tts_start()
            return
        if action == "create_tts_job" and isinstance(result, dict):
            if (isinstance(context, dict) and "casting_preview_serial" in context
                    and context.get("casting_preview_serial")
                    != self._casting_preview_request_serial):
                return
            if (isinstance(context, dict) and context.get("project_id")
                    and context["project_id"] != self.project_state.projectId):
                return
            self._dialogue_casting_auto_tts = False
            self._dialogue_casting_auto_tts_job_id = ""
            job_id = result.get("id")
            if not isinstance(job_id, str) or not job_id:
                self._set_tts_busy(False)
                self._set_status("Job Manager không trả về mã tác vụ giọng")
                return
            self._tts_job_id = job_id
            if isinstance(context, dict) and context.get("tts_purpose") == "preview":
                self._voice_preview_target_job_id = job_id
            self.ttsBusyChanged.emit()
            self._start_tts_worker(job_id)
            purpose = context.get("tts_purpose") if isinstance(context, dict) else ""
            self._set_status(
                "Đang tạo preview trong tiến trình riêng"
                if purpose == "preview"
                else "Tạo giọng chạy nền; chuyển tab không làm dừng tác vụ"
            )
            return
        if action == "create_export_job" and isinstance(result, dict):
            job_id = result.get("id")
            if not isinstance(job_id, str) or not job_id:
                self._set_export_busy(False)
                self._set_status("Job Manager không trả về mã tác vụ export")
                return
            self._export_job_id = job_id
            self.exportBusyChanged.emit()
            self._start_export_worker(job_id)
            self._set_status("Export chạy nền; chuyển tab không làm dừng FFmpeg")
            return
        if action == "retry_job" and isinstance(context, dict) and context.get(
            "resume_dialogue_casting"
        ):
            if not self._dialogue_casting_callback_is_current(context):
                return
            job_id = str(result.get("id", "")) if isinstance(result, dict) else ""
            job_id = job_id or str(context.get("job_id", ""))
            if job_id:
                self._dialogue_casting_job_id = job_id
                self._set_dialogue_casting_job_state("start_requested")
                self._start_dialogue_casting_worker(job_id)
            return
        if action == "retry_job" and isinstance(context, dict) and context.get("resume_ocr"):
            job_id = context.get("job_id")
            if isinstance(job_id, str):
                self._start_ocr_worker(job_id)
            return
        if (
            action == "retry_job"
            and isinstance(context, dict)
            and context.get("resume_translation")
        ):
            job_id = context.get("job_id")
            if isinstance(job_id, str):
                self._start_translation_worker(job_id)
            return
        if (
            action == "retry_job"
            and isinstance(context, dict)
            and context.get("resume_voice_conform")
        ):
            job_id = context.get("job_id")
            if isinstance(job_id, str):
                self._voice_conform_job_id = job_id
                self._voice_conform_job_state = "start_requested"
                self._voice_conform_busy = True
                self.voiceConformChanged.emit()
                self._start_tts_worker(job_id)
            return
        if action == "retry_job" and isinstance(context, dict) and context.get("resume_tts"):
            job_id = result.get("id") if isinstance(result, dict) else context.get("job_id")
            if isinstance(job_id, str):
                replaced_job_id = str(result.get("replaced_job_id", ""))
                if replaced_job_id:
                    self._superseded_job_ids.add(replaced_job_id)
                    self.statusNotificationDismissRequested.emit()
                self._tts_job_id = job_id
                self._set_tts_job_state("start_requested")
                self.ttsBusyChanged.emit()
                self._start_tts_worker(job_id)
            return
        if action == "retry_job" and isinstance(context, dict) and context.get("resume_export"):
            job_id = context.get("job_id")
            if isinstance(job_id, str):
                self._start_export_worker(job_id)
            return
        if action == "store_credential" and isinstance(context, dict):
            reference = context.get("credential_reference")
            if isinstance(reference, str):
                provider_id = str(context.get("credential_provider_id", "gemini-api"))
                references = _stable_credential_references(
                    [*self._translation_api_references(provider_id), reference]
                )
                self.settings.setValue(_credential_setting_key(provider_id), references)
                self.translationSettingsChanged.emit()
                self._set_status("Đã lưu API key an toàn; tool chỉ giữ tên tham chiếu")
            return
        if action == "store_credentials" and isinstance(context, dict):
            raw_references = result.get("references", []) if isinstance(result, dict) else []
            submitted = _stable_credential_references(raw_references)
            provider_id = str(context.get("credential_provider_id", "gemini-api"))
            submitted_folded = {value.casefold() for value in submitted}
            references = _stable_credential_references(
                [
                    *submitted,
                    *(
                        value
                        for value in self._translation_api_references(provider_id)
                        if value.casefold() not in submitted_folded
                    ),
                ]
            )
            self.settings.setValue(_credential_setting_key(provider_id), references)
            self.translationSettingsChanged.emit()
            self.geminiApiCredentialsStored.emit()
            pending = self._pending_translation_start
            self._pending_translation_start = None
            if context.get("start_translation") is True and isinstance(pending, dict):
                resume_job_id = str(pending.get("resume_job_id", ""))
                if resume_job_id:
                    self._set_translation_busy(True)
                    self._set_translation_job_state("resume_requested")
                    self.runtime.request(
                        "retry_job",
                        {
                            "job_id": resume_job_id,
                            "credential_references": references,
                        },
                        context={
                            "resume_translation": True,
                            "job_id": resume_job_id,
                        },
                    )
                    self._set_status("Đã lưu API key mới; đang tiếp tục bản dịch từ cache hiện có")
                else:
                    self._begin_translation(
                        str(pending["provider_id"]),
                        str(pending["model_id"]),
                        pending.get("headed") is True,
                        pending.get("guest") is True,
                        credential_references=references,
                        base_url=str(pending.get("base_url", "")),
                    )
            else:
                self._set_status(
                    f"Đã lưu {len(submitted)} API key; tool chỉ hiển thị tên tham chiếu"
                )
            return
        if action == "delete_credential" and isinstance(context, dict):
            reference = context.get("credential_reference")
            if isinstance(reference, str):
                provider_id = str(context.get("credential_provider_id", "gemini-api"))
                references = [
                    value
                    for value in self._translation_api_references(provider_id)
                    if value != reference
                ]
                self.settings.setValue(_credential_setting_key(provider_id), references)
                self.translationSettingsChanged.emit()
                self._set_status("Đã xóa API key khỏi Windows Credential Manager")
            return
        if action in {"delete_job_history", "clear_project_job_history"}:
            self._set_job_history_busy(False)
            if isinstance(result, dict):
                pending_ids = (
                    [result.get("job_id")]
                    if action == "delete_job_history" and result.get("pending") is True
                    else result.get("pending_job_ids", [])
                )
                if isinstance(pending_ids, list):
                    self._pending_history_deletion_ids.update(
                        str(job_id) for job_id in pending_ids if job_id
                    )
            self._request_current_translation_preview()
            self.runtime.request("list_jobs")
            if action == "delete_job_history":
                pending = result.get("pending") is True if isinstance(result, dict) else False
                self._set_status(
                    "Đang hủy tác vụ; lịch sử sẽ tự xóa ngay khi tiến trình dừng"
                    if pending
                    else "Đã xoá lịch sử tác vụ"
                )
            else:
                removed = result.get("deleted_jobs", 0) if isinstance(result, dict) else 0
                pending = result.get("pending_jobs", 0) if isinstance(result, dict) else 0
                self._set_status(
                    f"Đã xoá {removed} lịch sử; đang hủy và tự xóa {pending} tác vụ dở"
                )
            return
        if action == "delete_project" and isinstance(context, dict):
            deleted_id = str(context.get("project_id", ""))
            self._project_delete_busy = False
            self._deleting_project_id = ""
            self._deleting_project_name = ""
            self.projectDeleteChanged.emit()
            if deleted_id and deleted_id == self.project_state.projectId:
                self._apply_project({})
                self._playhead_us = 0
                self.playheadChanged.emit()
            self.runtime.request("list_projects")
            self._set_status("Đã xóa dự án nội bộ; media và SRT nguồn được giữ nguyên")
            return
        if action in {"start_job", "pause_job", "resume_job", "cancel_job"}:
            if (isinstance(context, dict)
                    and context.get("job_kind") == "dialogue_casting"
                    and not self._dialogue_casting_callback_is_current(context)):
                return
            self.runtime.request("list_jobs")
            if action == "cancel_job" and isinstance(result, dict):
                self._complete_translation_switch([result])
            return
        if action == "create_project" and isinstance(result, dict):
            self._apply_project(result)
            media_path = context.get("media_path") if isinstance(context, dict) else None
            if isinstance(media_path, str):
                import_options = {
                    key: value
                    for key, value in context.items()
                    if key in {"default_logo_path", "overlay_preset"}
                }
                self.runtime.request(
                    "import_media",
                    {
                        "project_id": self.project_state.projectId,
                        "media_path": media_path,
                        **import_options,
                    },
                    context={"media_path": media_path},
                )
            return
        if action in {
            "get_project",
            "import_media",
            "import_translation_srt",
            "timeline_command",
            "subtitle_command",
            "effect_command",
        } and isinstance(result, dict):
            project_context = context if isinstance(context, dict) else {}
            ocr_completed = (
                action == "get_project" and project_context.get("ocr_completed") is True
            )
            tts_completed = (
                action == "get_project" and project_context.get("tts_completed") is True
            )
            if ocr_completed or tts_completed:
                requested_project_id = str(project_context.get("project_id", ""))
                if (not requested_project_id
                        or requested_project_id != self.project_state.projectId
                        or str(result.get("id", "")) != requested_project_id):
                    return
            timeline_reconcile = (
                action == "get_project"
                and project_context.get("timeline_reconcile") is True
            )
            if timeline_reconcile and not self._timeline_reconcile_context_is_current(
                project_context
            ):
                return
            if timeline_reconcile and str(result.get("id", "")) != str(
                project_context.get("project_id", "")
            ):
                self._action_failed(
                    action,
                    {
                        "code": "timeline_reconcile_invalid",
                        "message": "Dữ liệu đồng bộ không thuộc project hiện tại",
                    },
                    project_context,
                )
                return
            if (
                action == "timeline_command"
                and project_context.get("timeline_cut") is True
                and not self._timeline_cut_context_is_current(project_context)
            ):
                # A timed-out or superseded cut must not overwrite the latest
                # project revision or release the busy gate of a newer cut.
                return
            is_multi_detail = (
                action == "get_project" and project_context.get("multi_detail") is True
            )
            if is_multi_detail and not self._multi_detail_context_is_current(project_context):
                return
            if is_multi_detail and str(result.get("id", "")) != str(
                project_context.get("project_id", "")
            ):
                self._multi_detail_generation += 1
                self._finish_multi_detail()
                self.navigate("multi")
                self._set_status(
                    "Không thể mở video: dữ liệu Timeline trả về không đúng project đã chọn."
                )
                return
            if action == "import_media":
                self._can_undo_timeline = False
                self._can_redo_timeline = False
                self.timelineHistoryChanged.emit()
            elif action == "subtitle_command":
                self._can_undo_subtitle = result.get("_subtitle_can_undo") is True
                self._can_redo_subtitle = result.get("_subtitle_can_redo") is True
                self.subtitleHistoryChanged.emit()
                self._can_undo_timeline = result.get("_timeline_can_undo") is True
                self._can_redo_timeline = result.get("_timeline_can_redo") is True
                self.timelineHistoryChanged.emit()
            elif action == "effect_command":
                self._can_undo_timeline = result.get("_timeline_can_undo") is True
                self._can_redo_timeline = result.get("_timeline_can_redo") is True
                self.timelineHistoryChanged.emit()
                self._can_undo_subtitle = result.get("_subtitle_can_undo") is True
                self._can_redo_subtitle = result.get("_subtitle_can_redo") is True
                self.subtitleHistoryChanged.emit()
            compact_subtitle_patch = (
                action == "subtitle_command" and result.get("_subtitle_patch") is True
            )
            compact_timeline_patch = (
                action in {"timeline_command", "subtitle_command"}
                and result.get("_timeline_patch") is True
            )
            try:
                if compact_subtitle_patch:
                    if "_prepared_subtitle_snapshot" in result:
                        self.project_state.apply_prepared_subtitle_patch(
                            result["_prepared_subtitle_snapshot"],
                            result["_prepared_subtitle_projected"],
                        )
                    else:
                        self.project_state.apply_subtitle_patch(result)
                    self._set_translation_qa(self._translation_qa)
                    self._selected_cue_ids = [
                        cue_id
                        for cue_id in self._selected_cue_ids
                        if self.project_state.subtitle_cues.find(cue_id)
                    ]
                    if not self.project_state.subtitle_cues.find(self._selected_cue_id):
                        self._selected_cue_id = (
                            self._selected_cue_ids[-1] if self._selected_cue_ids else ""
                        )
                        self.selectedCueChanged.emit()
                    elif self._selected_cue_id:
                        self.selectedCueChanged.emit()
                    if project_context.get("manual_scene_text") is True:
                        manual = [
                            self.project_state.subtitle_cues.get(row)
                            for row in range(self.project_state.subtitle_cues.count)
                        ]
                        manual = [
                            cue for cue in manual if cue.get("provenance") == "manual_scene_text"
                        ]
                        if manual:
                            created = max(
                                manual,
                                key=lambda cue: int(cue.get("index", 0) or 0),
                            )
                            self.selectCue(
                                str(created.get("cue_id", "")),
                                float(created.get("start_us", 0) or 0),
                            )
                elif compact_timeline_patch:
                    self.project_state.apply_timeline_patch(result)
                    self._refresh_edit_selection()
                    if project_context.get("title_created"):
                        created = result.get("title_cue_upserts", [])
                        if created:
                            self.selectTimelineCue(str(created[-1]["id"]), False)
                elif action == "effect_command" and result.get("_effect_patch") is True:
                    generation_key = str(project_context.get("generation_key", ""))
                    generation = int(project_context.get("generation", 0) or 0)
                    if not generation_key or generation >= self._effect_generations.get(
                        generation_key, 0
                    ):
                        self.project_state.apply_effect_patch(result)
                else:
                    self._apply_project(result)
            except (TypeError, ValueError, KeyError, LookupError) as exc:
                # A late or malformed subtitle response must never leave the
                # editor locked.  Route it through the same rollback/error
                # path as an explicit backend failure so the busy flag and
                # optimistic text are restored deterministically.
                if action in {
                    "subtitle_command",
                    "timeline_command",
                }:
                    self._action_failed(
                        action,
                        {
                            "code": (
                                "subtitle_patch_invalid"
                                if action == "subtitle_command"
                                else "timeline_patch_invalid"
                            ),
                            "message": str(exc),
                        },
                        project_context,
                    )
                    return
                raise
            if action == "timeline_command":
                self._can_undo_timeline = result.get("_timeline_can_undo") is True
                self._can_redo_timeline = result.get("_timeline_can_redo") is True
                self.timelineHistoryChanged.emit()
                self._can_undo_subtitle = result.get("_subtitle_can_undo") is True
                self._can_redo_subtitle = result.get("_subtitle_can_redo") is True
                self.subtitleHistoryChanged.emit()
            if action == "timeline_command" and project_context.get("timeline_cut") is True:
                self._finish_timeline_cut(True, project_context)
            if action in {
                "get_project",
                "import_media",
                "timeline_command",
            }:
                review_job_id = str(project_context.get("translation_review_job_id", ""))
                review_target = str(project_context.get("multi_detail_target", ""))
                if (
                    action == "get_project"
                    and is_multi_detail
                    and review_target == "translate"
                    and review_job_id
                ):
                    self._selected_translation_cache_id = review_job_id
                    self.runtime.request(
                        "get_translation_preview",
                        {"project_id": self.project_state.projectId, "job_id": review_job_id},
                        context={
                            "project_id": self.project_state.projectId,
                            "cache_job_id": review_job_id,
                            "multi_detail_translation_review": True,
                        },
                    )
                    self._request_translation_cache_list(self.project_state.projectId)
                else:
                    self._request_current_translation_preview()
            if is_multi_detail:
                self._finish_multi_detail()
            # A hand-edited translation changes the project revision but not
            # the durable Multi batch error row.  Ask only for the lightweight
            # status projection so the CJK count/green state updates without
            # reloading the full catalog or touching TTS.
            if (
                action == "subtitle_command"
                and self._backend_ready
                and self.multi_videos.item(self.project_state.projectId)
            ):
                self.runtime.request(
                    "multi_status",
                    context={
                        "multi_refresh_after_subtitle": True,
                        "project_id": self.project_state.projectId,
                    },
                )
            timing_repair_job_id = str(project_context.get("voice_timing_repair_job_id", ""))
            if (action in {"get_project", "import_media"}
                    and self.project_state.projectId and not timeline_reconcile):
                if timing_repair_job_id:
                    self.navigate("translate")
                    if project_context.get("voice_timing_repair_succeeded") is True:
                        self._request_voice_timing_retry(
                            parent_tts_job_id=str(project_context.get("parent_tts_job_id", "")),
                            repair_job_id=timing_repair_job_id,
                        )
                    else:
                        self._set_status("Rút gọn cue dài chưa đạt QA; hãy sửa các cue nháy vàng")
                else:
                    target_screen = str(project_context.get("multi_detail_target", "timeline"))
                    self.navigate("translate" if target_screen == "translate" else "timeline")
            self._set_import_busy(False)
            if timeline_reconcile:
                self._timeline_reconcile_pending = False
                self._timeline_reconcile_project_id = ""
            background_subtitle_save = (
                action == "subtitle_command"
                and project_context.get("background_subtitle_save") is True
            )
            if action != "subtitle_command":
                self._set_timeline_busy(False)
            if project_context.get("scene_timing_generation"):
                self._finish_scene_timing_command(True)
            elif project_context.get("subtitle_geometry_generation"):
                self._finish_subtitle_geometry(True)
            elif (not background_subtitle_save and not self._scene_timing_context
                  and not self._subtitle_geometry_context
                  and (action == "subtitle_command" or not self._subtitle_command_context)):
                self._set_subtitle_busy(False)
            if self._scene_timing_context:
                # A text/style ACK may project a new head while a timing edit
                # is still saving. Keep its preview until its own ACK arrives.
                for change in self._scene_timing_context.get("optimistic_changes", []):
                    self.project_state.subtitle_cues.preview_scene_text_timing(
                        str(change["cue_id"]), int(change["applied_start_us"]),
                        int(change["applied_end_us"]),
                    )
            if background_subtitle_save:
                self._finish_background_subtitle_save()
            else:
                pending_count = len(self._subtitle_save_queue) + int(self._subtitle_save_in_flight)
                self._save_state = (f"Đang lưu {pending_count} thay đổi" if pending_count
                                    else "Đang lưu vị trí chữ" if self._subtitle_geometry_context
                                    else "Đã lưu")
                self.saveStateChanged.emit()
            status_by_action = {
                "get_project": "Đã mở dự án",
                "import_media": "Đã nhập media và đưa vào timeline",
                "import_translation_srt": "Đã nhập SRT Việt và khớp toàn bộ cue/timestamp",
                "timeline_command": "Đã cập nhật timeline",
                "subtitle_command": "Đã cập nhật phụ đề",
                "effect_command": "Đã cập nhật timeline",
            }
            if not timing_repair_job_id:
                terminal_translation_job_id = str(
                    project_context.get("terminal_translation_job_id", "")
                )
                if timeline_reconcile:
                    self._set_status("Đã đồng bộ trạng thái timeline mới nhất")
                elif action == "get_project" and terminal_translation_job_id:
                    self._set_status("Đã dịch xong và đã chuyển sang Timeline")
                elif (
                    action == "get_project"
                    and project_context.get("multi_detail_target") == "translate"
                ):
                    self._set_status(
                        "Đã mở bản dịch cần sửa; bấm vào từng cue còn chữ Trung để chỉnh tay"
                    )
                elif action == "get_project" and project_context.get("ocr_completed"):
                    self._set_status("Đã trích xuất SRT xong và đã chuyển sang Timeline")
                elif action == "get_project" and project_context.get("tts_completed"):
                    self._set_status("Đã tạo voice xong và đã cập nhật Timeline")
                elif action == "subtitle_command":
                    subtitle_command = str(project_context.get("subtitle_command", ""))
                    if background_subtitle_save:
                        pass
                    elif subtitle_command == "find_replace":
                        replacement_count = int(project_context.get("replacement_count", 0) or 0)
                        replacement_cue_count = int(
                            project_context.get("replacement_cue_count", 0) or 0
                        )
                        self._set_status(
                            f"Đã thay {replacement_count} lần trong "
                            f"{replacement_cue_count} cue Việt"
                        )
                    elif subtitle_command == "delete":
                        cue_index = int(project_context.get("deleted_cue_index", 0) or 0)
                        self._set_status(
                            f"Đã xoá cue #{cue_index}; có thể bấm Hoàn tác"
                            if cue_index > 0
                            else "Đã xoá cue; có thể bấm Hoàn tác"
                        )
                    elif subtitle_command == "set_scene_text_font_scale":
                        scale_percent = int(
                            project_context.get("scene_font_scale_percent", 100) or 100
                        )
                        self._set_status(
                            f"Đã lưu tỷ lệ chữ trong cảnh {scale_percent}%; có thể bấm Hoàn tác"
                        )
                    elif subtitle_command == "set_scene_text_timing":
                        start_us = int(project_context.get("scene_timing_start_us", 0) or 0)
                        end_us = int(project_context.get("scene_timing_end_us", 0) or 0)
                        self._set_status(
                            "Đã lưu thời gian chữ trong cảnh "
                            f"{start_us / 1_000_000:.3f}s–{end_us / 1_000_000:.3f}s; "
                            "có thể bấm Hoàn tác"
                        )
                    elif subtitle_command == "set_cue_role":
                        label = ("S1 · Thoại" if project_context.get("converted_cue_role")
                                 == "dialogue" else "S2 · Chữ cảnh")
                        count = int(project_context.get("converted_cue_count", 1))
                        self._set_status(
                            f"Đã chuyển {count} cue sang {label}; "
                            "Ctrl+S để lưu, tạo lại giọng để áp dụng loại mới cho D1"
                        )
                    else:
                        self._set_status(
                            "Đã cập nhật bản đang chỉnh" if result.get("_project_dirty")
                            else "Đã lưu thay đổi phụ đề"
                        )
                else:
                    self._set_status(status_by_action[action])
            if not is_multi_detail and action not in {"subtitle_command", "effect_command"}:
                self.runtime.request("list_projects")
            if action == "effect_command":
                effect_context = context if isinstance(context, dict) else {}
                overlay_id = str(effect_context.get("overlay_id", ""))
                geometry = effect_context.get("geometry") is True
                values = effect_context.get("values")
                if overlay_id and isinstance(values, dict):
                    for key in (
                        "x",
                        "y",
                        "width",
                        "height",
                        "enabled",
                        "opacity",
                        "blur_radius",
                        "tracking_mode",
                        "padding_x",
                        "padding_y",
                        "concealment_mode",
                    ):
                        if key in values:
                            self._save_overlay_preset_value(overlay_id, key, values[key])
                pending = self._take_pending_effect_command()
                pending_is_same_geometry = (
                    pending is not None
                    and pending.get("geometry") is True
                    and str(pending.get("overlay_id", "")) == overlay_id
                )
                if geometry and overlay_id and not pending_is_same_geometry:
                    self.overlayGeometrySettled.emit(overlay_id, True)
                if pending is not None:
                    self._dispatch_pending_effect_command(pending)
            elif action == "timeline_command":
                self._dispatch_pending_effect_command()
            elif action == "subtitle_command":
                if not background_subtitle_save:
                    self._run_subtitle_flush_callbacks()
                self._dispatch_pending_manual_scene_text()
            self._observe_project_persistence(result)
            self._run_subtitle_flush_callbacks()
            if action == "get_project" and project_context.get("ocr_completed") is True:
                self._start_casting_after_ocr(result, project_context)

    @Slot(bool, object, object)
    def _timeline_patch_projected(
        self,
        succeeded: bool,
        result: object,
        context: object,
    ) -> None:
        if isinstance(context, dict) and context.get("subtitle_patch_projection") is True:
            if not succeeded or not isinstance(result, dict):
                self._action_failed("subtitle_command", result, context)
            else:
                patch = {**result["patch"], "_prepared_subtitle_snapshot": result["canonical"],
                         "_prepared_subtitle_projected": result["projected"]}
                self._action_succeeded("subtitle_command", patch, context)
            return
        if not self._timeline_cut_context_is_current(context):
            return
        if not succeeded or not isinstance(result, dict):
            error = result if isinstance(result, dict) else {"message": str(result)}
            self._action_failed("timeline_command", error, context)
            return
        canonical = result.get("canonical")
        projected = result.get("projected")
        patch = result.get("patch")
        if not isinstance(canonical, dict) or not isinstance(projected, dict):
            self._action_failed(
                "timeline_command",
                {
                    "code": "timeline_patch_invalid",
                    "message": "Dữ liệu hiển thị sau khi cắt không hợp lệ",
                },
                context,
            )
            return
        try:
            self.project_state.apply_prepared_timeline_patch(canonical, projected)
        except (TypeError, ValueError, KeyError, LookupError) as exc:
            self._action_failed(
                "timeline_command",
                {"code": "timeline_patch_invalid", "message": str(exc)},
                context,
            )
            return
        patch = patch if isinstance(patch, dict) else {}
        self._refresh_edit_selection()
        self._can_undo_timeline = patch.get("_timeline_can_undo") is True
        self._can_redo_timeline = patch.get("_timeline_can_redo") is True
        self.timelineHistoryChanged.emit()
        self._finish_timeline_cut(True, context)
        self._save_state = "Đã lưu"
        self.saveStateChanged.emit()
        self._set_status("Đã cập nhật timeline")
        self._dispatch_pending_effect_command()
        self._observe_project_persistence(patch)
        self._run_subtitle_flush_callbacks()

    @staticmethod
    def _format_action_error(error: object) -> str:
        if not isinstance(error, dict):
            return redact_sensitive_text(str(error or "không rõ nguyên nhân"))
        message_value = error.get("message")
        if not message_value and isinstance(error.get("error"), dict):
            message_value = error["error"].get("message")
        code = str(error.get("code", error.get("error_code", ""))).strip()
        message = str(message_value or error.get("reason") or "không rõ nguyên nhân").strip()
        return ApplicationViewModel._localized_error_message(code, message)

    @Slot(str, object, object)
    def _action_failed(self, action: str, error: object, context: object) -> None:
        if action == "get_tts_voice_preview":
            if not self._casting_preview_lookup_is_current(context):
                return
            self._set_status(
                "Không thể xác minh mẫu nghe thử: " + self._format_action_error(error)
            )
            return
        if action == "list_translation_models":
            if (isinstance(context, dict)
                    and context.get("catalog_generation") == self._translation_catalog_generation):
                self._translation_catalog_busy = False
                self._translation_catalog_status = (
                    "Không tải được model. Hãy thử lại hoặc nhập tên thủ công."
                )
                self.translationModelCatalogChanged.emit()
            return
        if action == "save_project_edits":
            self._project_save_failed(error, context)
            return
        if action in {
            "get_dialogue_casting",
            "configure_dialogue_casting",
            "set_dialogue_casting_labels",
        }:
            request_context = context if isinstance(context, dict) else {}
            if str(request_context.get("project_id", "")) != self.project_state.projectId:
                return
            self._dialogue_casting_loading = False
            self._dialogue_casting_saving = False
            self.dialogueCastingChanged.emit()
            message = self._format_action_error(error)
            self._set_status(f"Không thể cập nhật Phân vai thoại: {message}")
            return
        if action == "create_dialogue_casting_job":
            request_context = context if isinstance(context, dict) else {}
            if str(request_context.get("project_id", "")) != self.project_state.projectId:
                return
            waiting_tts = (request_context.get("automatic_tts") is True
                           or (self._dialogue_casting_auto_tts
                               and isinstance(self._pending_tts_start, dict)))
            self._dialogue_casting_auto_tts = False
            self._dialogue_casting_auto_tts_job_id = ""
            self._set_dialogue_casting_job_state("failed")
            if waiting_tts:
                self._pending_tts_start = None
                self._set_tts_busy(False)
                self._set_tts_job_state("failed")
                self._set_voice_playback_enabled(True)
            message = self._format_action_error(error)
            self._set_status(
                "Phân vai thoại chưa chạy được; D1 cũ vẫn được giữ nguyên: " + message
            )
            self._resume_pending_casting_after_ocr()
            return
        if (action == "effect_command" and isinstance(context, dict)
                and context.get("project_id")
                and context["project_id"] != self.project_state.projectId):
            return
        if action == "subtitle_command" and not self._subtitle_command_response_is_current(context):
            return
        if action == "subtitle_command" and not self._background_save_response_is_current(context):
            return
        if action == "subtitle_command" and not self._scene_timing_response_is_current(context):
            return
        if (action == "subtitle_command"
                and not self._subtitle_geometry_response_is_current(context)):
            return
        if (
            action == "get_project"
            and isinstance(context, dict)
            and context.get("timeline_reconcile") is True
        ):
            if not self._timeline_reconcile_context_is_current(context):
                return
            self._set_timeline_busy(True)
            self._set_status(
                "Chưa thể đồng bộ trạng thái sau lần cắt; sẽ thử lại trước thao tác tiếp theo."
            )
            QTimer.singleShot(1_000, self._dispatch_timeline_reconcile)
            return
        if (
            action == "timeline_command"
            and isinstance(context, dict)
            and context.get("timeline_cut") is True
            and not self._timeline_cut_context_is_current(context)
        ):
            return
        if action in {
            "reserve_update_install",
            "release_update_install",
            "get_update_install_eligibility",
        }:
            self.updater_model.action_failed(action, error)
            return
        if action == "export_subtitle_srt":
            self._set_subtitle_export_busy(False)
            self._set_status(
                "Không thể xuất SRT; dự án và cache không bị thay đổi: "
                + self._format_action_error(error)
            )
            return
        if action in {"set_translation_preferences", "save_translation_style"}:
            self._translation_style_control_pending = None
            self._set_translation_preferences_busy(False)
            self._set_status("Không thể lưu văn phong dịch: " + self._format_action_error(error))
            return
        if action == "subtitle_command":
            request_context = context if isinstance(context, dict) else {}
            request_project_id = str(request_context.get("project_id", ""))
            if not request_project_id or request_project_id == self.project_state.projectId:
                self.project_state.subtitle_cues.rollback_text_changes(
                    request_context.get("optimistic_changes", [])
                )
                if self._selected_cue_id:
                    self.selectedCueChanged.emit()
            self._set_translation_qa(self._translation_qa)
            self._refresh_local_multi_translation_review()
            if request_context.get("scene_timing_generation"):
                self._finish_scene_timing_command(False)
            elif request_context.get("subtitle_geometry_generation"):
                self._finish_subtitle_geometry(False)
            elif request_context.get("background_subtitle_save") is not True:
                self._set_subtitle_busy(False)
                if request_context.get("subtitle_command_generation"):
                    self._cancel_dependent_editor_changes()
            if request_context.get("background_subtitle_save") is True:
                self._finish_background_subtitle_save(False)
            self._save_state = "Chỉnh phụ đề thất bại"
            self.saveStateChanged.emit()
            self._set_status(
                "Không thể lưu thay đổi phụ đề; nội dung trên màn hình đã được hoàn nguyên: "
                + self._format_action_error(error)
            )
            if (request_context.get("subtitle_patch_projection")
                    or (isinstance(error, dict) and error.get("code") == "subtitle_patch_invalid")):
                self._begin_timeline_reconcile(request_project_id)
            if self._backend_ready and self.multi_videos.item(self.project_state.projectId):
                # Restore the card from the durable project after a failed
                # optimistic edit; this is a lightweight status-only refresh.
                self.runtime.request(
                    "multi_status",
                    context={
                        "multi_refresh_after_subtitle_failure": True,
                        "project_id": self.project_state.projectId,
                    },
                )
            self._dispatch_pending_manual_scene_text()
            return
        if action in {"multi_select", "multi_select_all"}:
            request_context = context if isinstance(context, dict) else {}
            generation = int(request_context.get("selection_generation", -1) or -1)
            previous = request_context.get("previous_selection", {})
            if generation == self._multi_selection_generation and isinstance(previous, dict):
                self._apply_multi_selection(
                    {str(project_id): bool(selected) for project_id, selected in previous.items()}
                )
            else:
                self.runtime.request("multi_list")
            self._set_status(
                "Không thể lưu lựa chọn Multi video: " + self._format_action_error(error)
            )
            return
        if (
            action == "create_project_with_media"
            and isinstance(context, dict)
            and ("multi_import_path" in context)
        ):
            self._finish_failed_multi_import(
                self._format_action_error(error),
                str(context.get("multi_import_path", "")),
            )
            return
        if action == "multi_add":
            self._multi_import_paths = []
            self._multi_import_project_ids = []
            self._set_import_busy(False)
            self._save_state = "Thêm Multi video thất bại"
            self.saveStateChanged.emit()
            self._set_status("Không thể thêm Multi video: " + self._format_action_error(error))
            return
        if action in {"multi_start", "multi_pause", "multi_resume"}:
            self._rollback_multi_control(action)
            labels = {
                "multi_start": "bắt đầu Multi video",
                "multi_pause": "tạm dừng Multi video",
                "multi_resume": "tiếp tục Multi video",
            }
            self._set_status(f"Không thể {labels[action]}: " + self._format_action_error(error))
            return
        if (
            action == "get_project"
            and isinstance(context, dict)
            and (context.get("multi_detail") is True)
        ):
            if not self._multi_detail_context_is_current(context):
                return
            self._multi_detail_generation += 1
            self._finish_multi_detail()
            self.navigate("multi")
            self._set_status(
                "Không thể mở video để chỉnh sửa chi tiết: " + self._format_action_error(error)
            )
            return
        if action.startswith("multi_"):
            self._set_status("Multi video không thể tiếp tục: " + self._format_action_error(error))
            return
        if action == "estimate_voice_conform":
            self._voice_conform_estimate_request_pending = False
            request_context = context if isinstance(context, dict) else {}
            try:
                request_generation = int(request_context.get("generation", -1))
            except (TypeError, ValueError):
                request_generation = -1
            is_current_request = "generation" not in request_context or (
                request_generation == self._voice_conform_estimate_generation
                and str(request_context.get("project_id", "")) == self.project_state.projectId
                and str(request_context.get("mode", "")) == self._voice_conform_mode
            )
            if not is_current_request:
                self._voice_conform_estimate_refresh_queued = True
                self.refreshVoiceConformEstimate()
                return
            self._voice_conform_estimate = {}
            # An estimate request is read-only.  If it fails, keep showing the
            # conform plan already persisted on the project instead of making
            # a transient request error look like a rollback.
            self._voice_conform_applied = self.project_state.voiceConformApplied
            self.voiceConformChanged.emit()
            message = self._format_action_error(error)
            self._set_status(f"Không thể ước lượng đồng bộ voice: {message}")
            return
        if action == "create_voice_conform_job":
            self._voice_conform_busy = False
            self._voice_conform_job_state = "failed"
            self._voice_conform_applied = self.project_state.voiceConformApplied
            self.voiceConformChanged.emit()
            message = self._format_action_error(error)
            self._set_status(f"Đồng bộ voice không thể tiếp tục: {message}")
            return
        if action == "reset_voice_conform":
            self._voice_conform_busy = False
            self._voice_conform_job_state = "failed"
            self._voice_conform_applied = self.project_state.voiceConformApplied
            self.voiceConformChanged.emit()
            message = self._format_action_error(error)
            self._set_status(f"Không thể đặt lại đồng bộ voice: {message}")
            return
        if action == "get_voice_timing_review":
            review_context = context if isinstance(context, dict) else {}
            self._voice_timing_review_requests.discard(str(review_context.get("job_id", "")))
            message = self._format_action_error(error)
            self._set_status(f"Không đọc được QA timing voice: {message}")
            return
        if action == "create_voice_timing_repair_job":
            repair_context = context if isinstance(context, dict) else {}
            self._voice_timing_repair_requests.discard(str(repair_context.get("tts_job_id", "")))
            self._set_voice_timing_auto_repair_busy(False)
            self._set_translation_busy(False)
            self._set_translation_job_state("review")
            self.navigate("translate")
            message = self._format_action_error(error)
            self._set_status(f"Không thể tự rút gọn cue dài: {message}")
            return
        if action == "create_tts_timing_retry_job":
            retry_context = context if isinstance(context, dict) else {}
            self._voice_timing_retry_requests.discard(str(retry_context.get("repair_job_id", "")))
            self._set_tts_busy(False)
            self._set_tts_job_state("review")
            self.navigate("translate")
            message = self._format_action_error(error)
            self._set_status(f"Không thể tạo lại voice từ cache: {message}")
            return
        if action == "create_tts_timing_override_job":
            override_context = context if isinstance(context, dict) else {}
            self._voice_timing_override_requests.discard(
                str(override_context.get("parent_tts_job_id", ""))
            )
            self._set_tts_busy(False)
            self._set_tts_job_state("review")
            message = self._format_action_error(error)
            self._set_status(f"Không thể thêm voice lỗi vào Timeline: {message}")
            return
        if (
            action == "list_events"
            and isinstance(context, dict)
            and context.get("translation_deltas") is True
        ):
            self._translation_event_requests.discard(str(context.get("job_id", "")))
            message = self._format_action_error(error)
            self._set_status(f"Không cập nhật được bản dịch đang chạy: {message}")
            return
        if (
            action == "list_events"
            and isinstance(context, dict)
            and context.get("translation_qa_status") is True
        ):
            self._translation_qa_event_requests.discard(str(context.get("job_id", "")))
            message = self._format_action_error(error)
            self._set_status(f"Không cập nhật được QA dịch: {message}")
            return
        if action in {"get_translation_preview", "list_translation_caches"}:
            if action == "list_translation_caches":
                self._translation_cache_refresh_pending = False
            message = self._format_action_error(error)
            self._set_status(
                "Không đọc được danh sách cache dịch: " + message
                if action == "list_translation_caches"
                else "Không đọc được bản dịch xem trước: " + message
            )
            return
        if action in {
            "runtime_diagnostics",
            "inspect_legacy_project",
            "import_legacy_project",
            "optimize_project_database",
            "get_database_optimization",
            "cancel_database_optimization",
        }:
            message = self._format_action_error(error)
            if action in {
                "optimize_project_database",
                "get_database_optimization",
                "cancel_database_optimization",
            }:
                self._database_maintenance_timer.stop()
                self._database_maintenance = {
                    "state": "failed",
                    "message": message,
                }
                self.databaseMaintenanceChanged.emit()
            self._set_status(f"{action} thất bại: {message}")
            return
        if action == "inspect_media_usage":
            self._set_import_busy(False)
            self._save_state = "Kiểm tra media thất bại"
            self.saveStateChanged.emit()
            message = self._format_action_error(error)
            self._set_status(f"Không thể kiểm tra media trùng: {message}")
            return
        if action == "import_translation_srt":
            message = self._format_action_error(error)
            self._set_status(f"Không thể nhập SRT Việt; dự án chưa bị thay đổi: {message}")
            return
        if action == "prepare_tts_rerun":
            self._pending_tts_start = None
            self._dialogue_casting_auto_tts = False
            self._dialogue_casting_auto_tts_job_id = ""
            self._set_voice_playback_enabled(True)
            self._set_tts_busy(False)
            self._set_tts_job_state("failed")
            message = self._format_action_error(error)
            self._set_status(
                "Không thể chuẩn bị tạo lại D1; chưa tạo tác vụ voice mới. "
                "Hãy tải lại project rồi thử lại: " + message
            )
            return
        if action in {
            "create_translation_job",
            "create_tts_job",
            "create_export_job",
            "store_credential",
            "store_credentials",
            "delete_credential",
        }:
            if action == "create_translation_job":
                self._set_translation_busy(False)
                self._set_translation_job_state("failed")
            elif action == "create_tts_job":
                if (isinstance(context, dict) and "casting_preview_serial" in context
                        and context.get("casting_preview_serial")
                        != self._casting_preview_request_serial):
                    return
                if (isinstance(context, dict) and context.get("project_id")
                        and context["project_id"] != self.project_state.projectId):
                    return
                self._pending_tts_start = None
                self._dialogue_casting_auto_tts = False
                self._dialogue_casting_auto_tts_job_id = ""
                self._set_tts_busy(False)
                self._set_tts_job_state("failed")
            elif action == "create_export_job":
                self._set_export_busy(False)
            elif action == "store_credentials":
                self._pending_translation_start = None
                self._set_translation_busy(False)
            message = self._format_action_error(error)
            self._set_status(
                (
                    f"Tạo giọng chưa thể tiếp tục: {message}"
                    if action == "create_tts_job"
                    else f"Xuất video chưa thể tiếp tục: {message}"
                )
                if action in {"create_tts_job", "create_export_job"}
                else f"Dịch thuật chưa thể tiếp tục: {message}"
            )
            return
        if action == "delete_project":
            self._project_delete_busy = False
            self._deleting_project_id = ""
            self._deleting_project_name = ""
            self.projectDeleteChanged.emit()
            message = self._format_action_error(error)
            self._set_status(f"Không thể xóa dự án: {message}")
            return
        if action in {"delete_job_history", "clear_project_job_history"}:
            self._set_job_history_busy(False)
            message = self._format_action_error(error)
            self._set_status(f"Không thể xoá lịch sử tác vụ: {message}")
            return
        if action == "effect_command":
            effect_context = context if isinstance(context, dict) else {}
            overlay_id = str(effect_context.get("overlay_id", ""))
            geometry = effect_context.get("geometry") is True
            command = str(effect_context.get("command", ""))
            newer_same_overlay = next(
                (
                    item
                    for item in self._pending_effect_commands
                    if str(item.get("overlay_id", "")) == overlay_id
                ),
                None,
            )
            if command == "add_blur" and overlay_id:
                self.project_state.rollback_local_effect(effect_context.get("rollback", {}))
                self._pending_effect_commands = [
                    item
                    for item in self._pending_effect_commands
                    if str(item.get("overlay_id", "")) != overlay_id
                ]
            elif overlay_id and newer_same_overlay is not None:
                # The next queued mutation was composed on top of this
                # optimistic value. If the current save failed, propagate the
                # older rollback baseline so a second failure returns to the
                # last state actually confirmed by the backend.
                newer_same_overlay["rollback"] = effect_context.get("rollback", {})
            elif overlay_id:
                self.project_state.rollback_local_effect(effect_context.get("rollback", {}))
            pending = self._take_pending_effect_command()
            self._set_timeline_busy(False)
            self._save_state = "Chỉnh hiệu ứng thất bại"
            self.saveStateChanged.emit()
            pending_is_same_geometry = (
                pending is not None
                and pending.get("geometry") is True
                and str(pending.get("overlay_id", "")) == overlay_id
            )
            if geometry and overlay_id and not pending_is_same_geometry:
                self.overlayGeometrySettled.emit(overlay_id, False)
            if pending is not None:
                self._dispatch_pending_effect_command(pending)
            message = self._format_action_error(error)
            self._set_status(f"Chỉnh hiệu ứng không thành công: {message}")
            return
        if action in {
            "create_ocr_job",
            "create_ocr_rescan_job",
            "start_job",
            "pause_job",
            "resume_job",
            "cancel_job",
            "retry_job",
        }:
            job_kind = context.get("job_kind") if isinstance(context, dict) else ""
            if not job_kind and isinstance(context, dict):
                if context.get("resume_translation"):
                    job_kind = "translate"
                elif context.get("resume_ocr"):
                    job_kind = "ocr"
                elif context.get("resume_tts"):
                    job_kind = "tts"
                elif context.get("resume_dialogue_casting"):
                    job_kind = "dialogue_casting"
                elif context.get("resume_export"):
                    job_kind = "export"
            if (job_kind == "dialogue_casting"
                    and not self._dialogue_casting_callback_is_current(context)):
                return
            if action == "start_job" and job_kind == "translate":
                self._set_translation_busy(False)
                self._set_translation_job_state("failed")
            elif action == "cancel_job" and job_kind == "translate":
                self._translation_switch_pending = None
                self.runtime.request("list_jobs")
            elif action == "retry_job" and job_kind == "translate":
                # The immutable ledger is still selectable even when the
                # retry command itself is rejected (for example because a
                # worker is still shutting down). Release the busy gate and
                # leave an actionable cache-resume state instead of trapping
                # the UI in ``resume_requested``.
                self._set_translation_busy(False)
                self._set_translation_job_state("failed")
            elif action == "resume_job" and job_kind == "translate":
                self._set_translation_busy(False)
                self._set_translation_job_state("paused")
            elif action == "start_job" and job_kind == "tts":
                self._set_tts_busy(False)
            elif action in {"start_job", "retry_job"} and job_kind == "dialogue_casting":
                self._set_dialogue_casting_job_state("failed")
                if (
                    self._dialogue_casting_auto_tts_job_id
                    and self._dialogue_casting_auto_tts_job_id == self._dialogue_casting_job_id
                ):
                    self._finish_dialogue_casting_job(self._dialogue_casting_job_id, "failed")
            elif action == "pause_job" and job_kind == "dialogue_casting":
                self._set_dialogue_casting_job_state("running")
            elif action == "resume_job" and job_kind == "dialogue_casting":
                self._set_dialogue_casting_job_state("paused")
            elif action == "cancel_job" and job_kind == "dialogue_casting":
                self.runtime.request("list_jobs")
            elif action == "start_job" and job_kind == "export":
                self._set_export_busy(False)
            elif action in {"create_ocr_job", "start_job"}:
                self._set_ocr_busy(False)
                self._set_ocr_job_state("failed")
            message = self._format_action_error(error)
            self._set_status(
                f"Dịch thuật không thể tiếp tục: {message}"
                if job_kind == "translate"
                else (
                    f"Tạo giọng không thể tiếp tục: {message}"
                    if job_kind == "tts"
                    else (
                        f"Xuất video không thể tiếp tục: {message}"
                        if job_kind == "export"
                        else (
                            f"Phân vai thoại không thể tiếp tục: {message}"
                            if job_kind == "dialogue_casting"
                            else f"OCR không thể tiếp tục: {message}"
                        )
                    )
                )
            )
            if (job_kind == "dialogue_casting"
                    and action in {"start_job", "retry_job"}):
                self._resume_pending_casting_after_ocr()
            return
        if action not in {
            "create_project",
            "import_media",
            "get_project",
            "timeline_command",
            "subtitle_command",
            "delete_project",
        }:
            return
        self._set_import_busy(False)
        if (
            action == "timeline_command"
            and isinstance(context, dict)
            and context.get("timeline_cut") is True
        ):
            self._finish_timeline_cut(False, context)
        else:
            self._set_timeline_busy(False)
        if not self._scene_timing_context and not self._subtitle_command_context:
            self._set_subtitle_busy(False)
        if action == "timeline_command":
            self._dispatch_pending_effect_command()
        self._save_state = {
            "timeline_command": "Chỉnh timeline thất bại",
            "subtitle_command": "Chỉnh phụ đề thất bại",
            "effect_command": "Chỉnh hiệu ứng thất bại",
            "get_project": "Mở dự án thất bại",
        }.get(action, "Nhập media thất bại")
        self.saveStateChanged.emit()
        message = self._format_action_error(error)
        self._set_status(f"Tác vụ không thành công: {message}")

    def _refresh_edit_selection(self) -> None:
        """Notify inspectors after a general patch, including mixed-history undo."""
        if self.project_state.clip_info(self._selected_clip_id) is None:
            self._selected_clip_id = ""
        self.selectedClipChanged.emit()
        self._selected_cue_ids = [
            cue_id for cue_id in self._selected_cue_ids
            if self.project_state.subtitle_cues.find(cue_id)
        ]
        if not self.project_state.subtitle_cues.find(self._selected_cue_id):
            self._selected_cue_id = self._selected_cue_ids[-1] if self._selected_cue_ids else ""
        self.selectedCueChanged.emit()
        if self.project_state.track_info(self._selected_track_id) is None:
            self._selected_track_id = ""
        self._selected_track_ids.intersection_update(self.project_state._track_by_id)
        self.selectedTrackChanged.emit()

    def _apply_project(self, snapshot: dict) -> None:
        previous_project_id = self.project_state.projectId
        incoming_project_id = snapshot.get("id", "")
        if previous_project_id and incoming_project_id != previous_project_id:
            self._release_voice_preview()
            self._cancel_dependent_editor_changes()
            self._set_subtitle_busy(False)
            self._subtitle_save_timer.stop()
            self._subtitle_command_watchdog.stop()
            if self._scene_timing_context:
                self._finish_scene_timing_command(False)
            if self._subtitle_geometry_context:
                self._finish_subtitle_geometry(False)
            self._pending_manual_scene_text_commands.clear()
        self.project_state.replace(
            snapshot,
            preserve_live_translations=bool(
                previous_project_id and incoming_project_id == previous_project_id
            ),
            take_ownership=True,
        )
        self._observe_project_persistence({
            **snapshot,
            "_project_dirty": snapshot.get("_project_dirty", False),
            "_saved_revision": snapshot.get("_saved_revision", self.project_state.revision),
        })
        project_id = self.project_state.projectId
        conform_applied = self.project_state.voiceConformApplied
        conform_changed = conform_applied != self._voice_conform_applied
        self._voice_conform_applied = conform_applied
        if project_id != previous_project_id:
            self._last_export_source = ""
            self._last_export_qa_path = ""
            self.exportPreviewChanged.emit()
            self._dialogue_casting = {
                "project_id": project_id,
                "config": {
                    "enabled": False,
                    "male_voice": None,
                    "female_voice": None,
                    "fallback_voice": None,
                },
                "rows": [],
                "experimental": True,
            }
            self._dialogue_casting_loading = False
            self._dialogue_casting_saving = False
            self._dialogue_casting_job_id = ""
            self._dialogue_casting_auto_tts = False
            self._dialogue_casting_auto_tts_job_id = ""
            self._dialogue_casting_terminal_markers.clear()
            self._set_dialogue_casting_job_state("")
            self._pending_casting_after_ocr = None
            self._pending_tts_start = None
            self._set_tts_busy(False)
            self._set_voice_playback_enabled(True)
            self.dialogueCastingChanged.emit()
            if project_id:
                self.refreshDialogueCasting()
            self._voice_conform_job_id = ""
            self._voice_conform_parent_tts_job_id = ""
        if conform_applied:
            self._voice_conform_job_state = "succeeded"
        elif project_id != previous_project_id:
            self._voice_conform_job_state = ""
            self._voice_conform_estimate = {}
        if conform_changed or project_id != previous_project_id:
            self.voiceConformChanged.emit()
        if project_id != previous_project_id:
            self._selected_cue_id = ""
            self._selected_cue_ids = []
            self.selectedCueChanged.emit()
            cache_changed = bool(self._translation_caches or self._selected_translation_cache_id)
            self._translation_caches = []
            self._selected_translation_cache_id = ""
            self._translation_cache_selection_explicit = False
            self._translation_cache_refresh_pending = False
            self._translation_cache_refresh_marker = ("", "", "", "")
            self._translation_qa_event_sequences.clear()
            self._translation_qa_event_requests.clear()
            self._terminal_job_notification_markers.clear()
            self._jobs_snapshot_initialized = False
            self._set_translation_qa({"status": "NONE", "issues": []})
            self._voice_timing_review_requests.clear()
            self._voice_timing_repair_requests.clear()
            self._voice_timing_retry_requests.clear()
            self._voice_timing_override_requests.clear()
            self._set_voice_timing_qa({"status": "NONE", "issues": []})
            if cache_changed:
                self.translationCachesChanged.emit()
        for model in (self.jobs, self.subtitle_jobs, self.voice_jobs, self.export_jobs):
            model.setProjectFilter(project_id)
        if project_id != previous_project_id:
            self._voice_preview_job_id = ""
            self._voice_preview_target_job_id = None
            if self._voice_preview_source:
                self._voice_preview_source = ""
                self.voicePreviewChanged.emit()
        if self.project_state.clip_info(self._selected_clip_id) is None:
            self._selected_clip_id = ""
            self.selectedClipChanged.emit()
        elif self._selected_clip_id:
            self.selectedClipChanged.emit()
        if self.project_state.track_info(self._selected_track_id) is None:
            self._selected_track_id = ""
            self._selected_track_ids.clear()
            self.selectedTrackChanged.emit()
        elif self._selected_track_id:
            self._selected_track_ids.intersection_update(self.project_state._track_by_id)
            self.selectedTrackChanged.emit()
        if not self.project_state.subtitle_cues.find(self._selected_cue_id):
            self._selected_cue_ids = [
                cue_id
                for cue_id in self._selected_cue_ids
                if self.project_state.subtitle_cues.find(cue_id)
            ]
            self._selected_cue_id = self._selected_cue_ids[-1] if self._selected_cue_ids else ""
            self.selectedCueChanged.emit()
        elif self._selected_cue_id:
            if self._selected_cue_id not in self._selected_cue_ids:
                self._selected_cue_ids = [self._selected_cue_id]
            self.selectedCueChanged.emit()
        self._project_name = self.project_state.name or "Chưa mở dự án"
        self.projectNameChanged.emit()
        self.exportSettingsChanged.emit()
        if project_id and self.project_state.hasVoiceTrack and not self._voice_conform_busy:
            self.refreshVoiceConformEstimate()

    def _request_current_translation_preview(self) -> None:
        project_id = self.project_state.projectId
        if not project_id:
            return
        self._request_translation_cache_list(project_id)

    def _request_translation_cache_list(self, project_id: str) -> None:
        if not project_id or self._translation_cache_refresh_pending:
            return
        self._translation_cache_refresh_pending = True
        try:
            self.runtime.request(
                "list_translation_caches",
                {"project_id": project_id},
                context={"project_id": project_id},
            )
        except Exception:
            self._translation_cache_refresh_pending = False
            raise

    def _set_import_busy(self, value: bool) -> None:
        if value == self._import_busy:
            return
        self._import_busy = value
        self.importBusyChanged.emit()

    def _set_timeline_busy(self, value: bool) -> None:
        if value == self._timeline_busy:
            return
        self._timeline_busy = value
        self.timelineBusyChanged.emit()

    def _set_ocr_busy(self, value: bool) -> None:
        if value == self._ocr_busy:
            return
        self._ocr_busy = value
        self.ocrBusyChanged.emit()

    def _set_ocr_job_state(self, value: str) -> None:
        if value == self._ocr_job_state:
            return
        self._ocr_job_state = value
        self.ocrBusyChanged.emit()

    def _set_translation_busy(self, value: bool) -> None:
        if value == self._translation_busy:
            return
        self._translation_busy = value
        self.translationBusyChanged.emit()

    def _translation_preferences_saved(self, action: str, result: object, context: object) -> None:
        project_id = context.get("project_id", "") if isinstance(context, dict) else ""
        if project_id and project_id != self.project_state.projectId:
            self._translation_style_control_pending = None
            self._set_translation_preferences_busy(False)
            return
        try:
            if not project_id or not isinstance(result, dict) or result.get("id") != project_id:
                raise ValueError("Dữ liệu văn phong trả về không thuộc project hiện tại")
            self.project_state.apply_translation_preferences(result)
        except (TypeError, ValueError, KeyError) as exc:
            self._action_failed(action, {"code": "translation_preferences_invalid",
                                        "message": str(exc)}, context)
            return
        self._set_translation_preferences_busy(False)
        saved_revision = result.get("_saved_revision", result["revision"])
        if type(saved_revision) is int and saved_revision >= 0:
            # Record the durable watermark even while a cue ACK is pending;
            # its older metadata must not make a flushed head appear unsaved.
            self._last_saved_revision = max(self._last_saved_revision, saved_revision)
        # This ACK owns only preferences. Do not retire another operation's
        # busy gate or overwrite pending edits with an older clean-save state.
        if (result["revision"] >= self.project_state.revision
                and not self._subtitle_save_queue and not self._subtitle_save_in_flight
                and not self._subtitle_geometry_context and not self._scene_timing_context):
            self._observe_project_persistence({
                **result, "_project_dirty": result.get("_project_dirty", False),
                "_saved_revision": result.get("_saved_revision", result["revision"]),
            })
        self._set_status("Đã lưu văn phong mới cho project" if action == "save_translation_style"
                         else "Đã lưu văn phong dịch cho project")
        pending = self._translation_style_control_pending
        self._translation_style_control_pending = None
        if pending and pending["project_id"] == self.project_state.projectId:
            self.toggleTranslation(*pending["arguments"])

    def _set_translation_preferences_busy(self, value: bool) -> None:
        if value == self._translation_preferences_busy:
            return
        self._translation_preferences_busy = value
        self.translationPreferencesBusyChanged.emit()

    def _set_translation_job_state(self, value: str) -> None:
        if value == self._translation_job_state:
            return
        self._translation_job_state = value
        self.translationBusyChanged.emit()

    def _set_translation_qa(
        self, snapshot: object, *, reconcile_with_project: bool = True,
    ) -> None:
        if not isinstance(snapshot, dict):
            return
        status = str(snapshot.get("status", "NONE")).upper()
        if status not in {"NONE", "PENDING", "REVIEW", "PASS"}:
            status = "PENDING"
        issues = [dict(issue) for issue in snapshot.get("issues", []) if isinstance(issue, dict)]
        normalized: dict[str, object] = {
            "status": status,
            "completed_cues": int(snapshot.get("completed_cues", 0) or 0),
            "checked_cues": int(snapshot.get("checked_cues", 0) or 0),
            "cue_total": int(snapshot.get("cue_total", 0) or 0),
            "blocking_count": int(snapshot.get("blocking_count", len(issues)) or 0),
            "blocking_issue_count": int(snapshot.get("blocking_issue_count", len(issues)) or 0),
            "warning_count": int(snapshot.get("warning_count", 0) or 0),
            "issues": issues,
            "content_warnings": [
                dict(issue)
                for issue in snapshot.get("content_warnings", [])
                if isinstance(issue, dict)
            ],
        }
        # A job's QA counts belong to its immutable ledger. Visible project rows
        # may still contain another model/style's translations until new deltas
        # arrive; they must not inflate counts or add unrelated QA issues.
        if reconcile_with_project:
            normalized = self._reconcile_translation_qa_with_current_cues(normalized)
        if normalized == self._translation_qa:
            return
        self._translation_qa = normalized
        self.translationQaChanged.emit()
        self._refresh_subtitle_issue_roles()

    @staticmethod
    def _is_cjk_translation_issue(issue: dict[str, object]) -> bool:
        code = str(issue.get("code", "")).casefold()
        message = str(issue.get("message", "")).casefold()
        return "cjk" in code or "chữ hán" in message or "chữ trung" in message

    def _reconcile_translation_qa_with_current_cues(
        self,
        snapshot: dict[str, object],
    ) -> dict[str, object]:
        model = self.project_state.subtitle_cues
        rows = [model.get(row) for row in range(model.count)]
        translated_rows = [
            row
            for row in rows
            if str(row.get("translation_cue_id", "")) or str(row.get("translated_text", "")).strip()
        ]
        source_total = model.count
        if not translated_rows:
            if source_total > 0:
                return {**snapshot, "cue_total": source_total}
            previous_status = str(snapshot.get("status", "NONE"))
            return {
                **snapshot,
                "status": "NONE" if previous_status != "PENDING" else "PENDING",
                "completed_cues": 0,
                "checked_cues": 0,
                "cue_total": source_total,
                "blocking_count": 0,
                "blocking_issue_count": 0,
                "issues": [],
                "content_warnings": [],
            }

        by_cue_id = {
            str(row.get("cue_id", "")): row for row in translated_rows if str(row.get("cue_id", ""))
        }
        by_index = {
            int(row["index"]): row for row in translated_rows if type(row.get("index")) is int
        }
        reconciled: list[dict[str, object]] = []
        cjk_cue_ids: set[str] = set()
        raw_issues = snapshot.get("issues", [])
        issues = raw_issues if isinstance(raw_issues, list) else []
        for raw_issue in issues:
            if not isinstance(raw_issue, dict):
                continue
            cue_id = str(raw_issue.get("cue_id", ""))
            cue_index = raw_issue.get("id")
            row = by_cue_id.get(cue_id)
            if row is None and type(cue_index) is int:
                row = by_index.get(cue_index)
            if row is None:
                continue
            translated_text = str(row.get("translated_text", ""))
            if self._is_cjk_translation_issue(raw_issue):
                if not contains_cjk(translated_text):
                    continue
                cjk_cue_ids.add(str(row.get("cue_id", "")))
            reconciled.append(
                {
                    **raw_issue,
                    "id": int(row.get("index", 0) or 0),
                    "cue_id": str(row.get("cue_id", "")),
                    "translated_text": translated_text,
                }
            )

        for row in translated_rows:
            translated_text = str(row.get("translated_text", ""))
            cue_id = str(row.get("cue_id", ""))
            if not contains_cjk(translated_text) or cue_id in cjk_cue_ids:
                continue
            reconciled.append(
                {
                    "id": int(row.get("index", 0) or 0),
                    "cue_id": cue_id,
                    "code": "cjk_remaining",
                    "message": "Bản dịch còn chữ Hán/CJK.",
                    "translated_text": translated_text,
                    "original_severity": "blocker",
                }
            )

        reconciled.sort(key=lambda issue: int(issue.get("id", 0) or 0))
        previous_status = str(snapshot.get("status", "NONE"))
        full_translation = (
            source_total > 0
            and len(translated_rows) == source_total
            and all(str(row.get("translated_text", "")).strip() for row in translated_rows)
        )
        if reconciled:
            status = "REVIEW"
        elif previous_status == "NONE":
            status = "NONE"
        elif previous_status == "PENDING" or not full_translation:
            status = "PENDING"
        else:
            status = "PASS"
        cue_total = source_total
        return {
            **snapshot,
            "status": status,
            "completed_cues": min(
                cue_total,
                max(int(snapshot.get("completed_cues", 0) or 0), len(translated_rows)),
            ),
            "checked_cues": min(
                cue_total,
                max(int(snapshot.get("checked_cues", 0) or 0), len(translated_rows)),
            ),
            "cue_total": cue_total,
            "blocking_count": len(reconciled),
            "blocking_issue_count": len(reconciled),
            "issues": reconciled,
        }

    def _set_voice_timing_qa(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        raw_issues = snapshot.get("issues", [])
        issues = [dict(item) for item in raw_issues if isinstance(item, dict)]
        status = str(snapshot.get("status", "NONE")).upper()
        if status not in {"NONE", "REVIEW", "PASS"}:
            status = "REVIEW" if issues else "NONE"
        normalized: dict[str, object] = {
            "status": status,
            "job_id": str(snapshot.get("job_id", "")),
            "error_code": str(snapshot.get("error_code", "")),
            "issue_count": int(snapshot.get("issue_count", len(issues)) or 0),
            "issues": issues,
            "overflow_us": int(snapshot.get("overflow_us", 0) or 0),
            "maximum_delay_us": int(snapshot.get("maximum_delay_us", 0) or 0),
            "repair_cycle": int(snapshot.get("repair_cycle", 0) or 0),
            "max_repair_cycles": int(snapshot.get("max_repair_cycles", 3) or 3),
            "repairable": snapshot.get("repairable") is True,
            "timing_warning_count": int(snapshot.get("timing_warning_count", 0) or 0),
            "qa_non_blocking": snapshot.get("qa_non_blocking") is True,
            "voice_read_speed": float(snapshot.get("voice_read_speed", 1.0) or 1.0),
            "cue_max_total_speed": float(snapshot.get("cue_max_total_speed", 1.0) or 1.0),
            "adjusted_cue_count": int(snapshot.get("adjusted_cue_count", 0) or 0),
            "maximum_additional_factor": float(
                snapshot.get("maximum_additional_factor", 1.0) or 1.0
            ),
            "unresolved_timing_cue_count": int(
                snapshot.get("unresolved_timing_cue_count", len(issues)) or 0
            ),
            "cue_timeline": [
                dict(item) for item in snapshot.get("cue_timeline", []) if isinstance(item, dict)
            ],
            "message": str(snapshot.get("message", "")),
        }
        if normalized == self._voice_timing_qa:
            return
        self._voice_timing_qa = normalized
        self.project_state.replace_voice_timing_rows(normalized["cue_timeline"])
        self.voiceTimingQaChanged.emit()
        self._refresh_subtitle_issue_roles()

    def _refresh_subtitle_issue_roles(self) -> None:
        model = self.project_state.subtitle_cues
        combined: dict[int, dict[str, object]] = {}
        for snapshot in (self._translation_qa, self._voice_timing_qa):
            for raw in snapshot.get("issues", []):
                if not isinstance(raw, dict) or type(raw.get("id")) is not int:
                    continue
                cue_index = int(raw["id"])
                current = combined.get(cue_index)
                if current is None:
                    combined[cue_index] = dict(raw)
                    continue
                codes = [str(current.get("code", "")), str(raw.get("code", ""))]
                messages = [
                    str(current.get("message", "")),
                    str(raw.get("message", "")),
                ]
                current["code"] = ", ".join(value for value in codes if value)
                current["message"] = "\n".join(value for value in messages if value)
                if not current.get("cue_id") and raw.get("cue_id"):
                    current["cue_id"] = raw["cue_id"]
        model.set_qa_issues(list(combined.values()))

    def _set_tts_busy(self, value: bool) -> None:
        if value == self._tts_busy:
            return
        self._tts_busy = value
        self.ttsBusyChanged.emit()
        if not value:
            self._resume_pending_casting_after_ocr()

    def _set_voice_playback_enabled(self, value: bool) -> None:
        enabled = bool(value)
        if enabled == self._voice_playback_enabled:
            return
        self._voice_playback_enabled = enabled
        self.voicePlaybackEnabledChanged.emit()

    def _set_tts_job_state(self, value: str) -> None:
        if value == self._tts_job_state:
            return
        self._tts_job_state = value
        self.ttsBusyChanged.emit()

    def _set_tts_job_purpose(self, value: str) -> None:
        if value == self._tts_job_purpose:
            return
        self._tts_job_purpose = value
        self.ttsBusyChanged.emit()

    def _set_voice_timing_auto_repair_busy(self, value: bool) -> None:
        value = bool(value)
        if value == self._voice_timing_auto_repair_busy:
            return
        self._voice_timing_auto_repair_busy = value
        self.ttsBusyChanged.emit()

    def _set_export_busy(self, value: bool) -> None:
        if value == self._export_busy:
            return
        self._export_busy = value
        self.exportBusyChanged.emit()

    def _set_export_job_state(self, value: str) -> None:
        if value == self._export_job_state:
            return
        self._export_job_state = value
        self.exportBusyChanged.emit()

    def _set_subtitle_busy(self, value: bool) -> None:
        if not value and self._subtitle_command_context:
            self._subtitle_command_context = {}
            self._subtitle_command_watchdog.stop()
            if self._subtitle_save_queue:
                self._subtitle_save_timer.start()
        if value == self._subtitle_busy:
            return
        self._subtitle_busy = value
        self.subtitleBusyChanged.emit()

    def _set_subtitle_export_busy(self, value: bool) -> None:
        value = bool(value)
        if value == self._subtitle_export_busy:
            return
        self._subtitle_export_busy = value
        self.subtitleExportBusyChanged.emit()

    def _send_subtitle_command(
        self,
        command: str,
        *,
        cue_id: str | None = None,
        language: str = "source",
        values: dict[str, object] | None = None,
        needs_selection: bool = True,
        optimistic_changes: list[dict[str, object]] | None = None,
        status_context: dict[str, object] | None = None,
    ) -> int:
        changes = optimistic_changes or []

        def rollback_optimistic() -> None:
            self.project_state.subtitle_cues.rollback_text_changes(changes)
            if self._selected_cue_id:
                self.selectedCueChanged.emit()
            self._set_translation_qa(self._translation_qa)
            self._refresh_local_multi_translation_review()

        if self._timeline_busy:
            rollback_optimistic()
            self._set_status("Hãy chờ thao tác Timeline hiện tại hoàn tất trước khi sửa phụ đề")
            return 0
        background_save = command in {
            "update_text",
            "set_text_format",
            "set_scene_text_fade",
        }
        specialized_gesture = (command in {"move_scene_texts", "resize_scene_texts"}
                               or (status_context or {}).get("subtitle_geometry_generation"))
        if (not background_save and not specialized_gesture
                and (self._subtitle_save_in_flight or self._subtitle_save_queue)):
            self._defer_until_subtitle_saves_flushed(
                "subtitle-command-" + command,
                lambda: self._send_subtitle_command(
                    command, cue_id=cue_id, language=language, values=values,
                    needs_selection=needs_selection, optimistic_changes=changes,
                    status_context=status_context,
                ),
                "Đang lưu chỉnh sửa trước khi sửa phụ đề…",
            )
            return 1
        if (self._subtitle_busy and not background_save) or not self.project_state.hasProject:
            rollback_optimistic()
            self._set_status("Hãy chờ thay đổi phụ đề hiện tại được lưu trước khi chỉnh tiếp")
            return 0
        if needs_selection and command != "add" and not cue_id:
            rollback_optimistic()
            self._set_status("Hãy chọn cue trước")
            return 0
        command_values = dict(values or {})
        target_ids = command_values.get("cue_ids", [cue_id] if cue_id else [])
        if target_ids and all(self.project_state.title_cues.find(str(item)) for item in target_ids):
            command_values["title_mode"] = True
        params: dict[str, object] = {
            "project_id": self.project_state.projectId,
            "command": command,
            "language": language,
            "values": command_values,
            "compact": True,
        }
        if cue_id:
            params["cue_id"] = cue_id
        if changes:
            # The editor already owns the optimistic Vietnamese text. Mirror
            # its CJK count onto the Multi card before the disk/IPC round trip;
            # a failed save rolls both projections back together above.
            self._refresh_local_multi_translation_review()
        context: dict[str, object] = {
            "subtitle_command": command,
            "project_id": self.project_state.projectId,
            "optimistic_changes": changes,
            **(status_context or {}),
        }
        if background_save:
            context["background_subtitle_save"] = True
            self._queue_background_subtitle_save(params, context)
            return 1
        timing_command = command in {"move_scene_texts", "resize_scene_texts"}
        generation = 1
        if timing_command:
            self._scene_timing_generation += 1
            generation = self._scene_timing_generation
            context["scene_timing_generation"] = generation
            self._scene_timing_context = context
        self._subtitle_command_generation += 1
        context["subtitle_command_generation"] = self._subtitle_command_generation
        self._subtitle_command_context = context
        self._subtitle_command_watchdog.start()
        self._set_subtitle_busy(True)
        try:
            self.runtime.request("subtitle_command", params, context=context)
        except Exception as exc:
            self._action_failed("subtitle_command", {"message": str(exc)}, context)
            self._begin_timeline_reconcile(str(context.get("project_id", "")))
            return 0
        self._set_status(
            "Đã cập nhật trên màn hình; đang lưu project…" if changes else "Đang cập nhật phụ đề"
        )
        self._save_state = "Đang lưu thay đổi phụ đề"
        self.saveStateChanged.emit()
        return generation

    def _subtitle_command_response_is_current(self, context: object) -> bool:
        if not isinstance(context, dict) or not context.get("subtitle_command_generation"):
            return True
        return bool(self._subtitle_command_context
                    and context.get("subtitle_command_generation")
                    == self._subtitle_command_context.get("subtitle_command_generation")
                    and context.get("project_id") == self.project_state.projectId)

    @Slot()
    def _subtitle_command_timed_out(self) -> None:
        if not self._subtitle_command_context:
            return
        context = dict(self._subtitle_command_context)
        self._action_failed("subtitle_command", {
            "code": "save_ack_timeout",
            "message": "Chưa nhận xác nhận sau 120 giây; đang đọc lại dự án đã lưu",
        }, context)
        self._begin_timeline_reconcile(str(context.get("project_id", "")))

    def _scene_timing_response_is_current(self, context: object) -> bool:
        if not isinstance(context, dict) or not context.get("scene_timing_generation"):
            return True
        return bool(
            self._scene_timing_context
            and context.get("scene_timing_generation")
            == self._scene_timing_context.get("scene_timing_generation")
            and context.get("project_id") == self.project_state.projectId
        )

    def _finish_scene_timing_command(self, succeeded: bool) -> None:
        generation = int(self._scene_timing_context.get("scene_timing_generation", 0))
        self._scene_timing_context = {}
        self._set_subtitle_busy(False)
        if generation:
            self.sceneTimingSettled.emit(generation, succeeded)
        if succeeded:
            self._run_subtitle_flush_callbacks()
        else:
            self._subtitle_flush_callbacks.clear()

    def _queue_background_subtitle_save(
        self, params: dict[str, object], context: dict[str, object]
    ) -> None:
        values = params.get("values")
        cue_ids = (
            tuple(str(value) for value in values.get("cue_ids", []) if isinstance(value, str))
            if isinstance(values, dict)
            else ()
        )
        key = (
            str(params.get("command", "")),
            str(params.get("cue_id", "")),
            str(params.get("language", "")),
            cue_ids,
        )
        queued = {
            "key": key,
            "params": dict(params),
            "context": dict(context),
        }
        for index, item in enumerate(self._subtitle_save_queue):
            if item.get("key") != key:
                continue
            previous_context = item.get("context")
            if isinstance(previous_context, dict):
                queued["context"]["optimistic_changes"] = previous_context.get(
                    "optimistic_changes", []
                )
            self._subtitle_save_queue[index] = queued
            break
        else:
            self._subtitle_save_queue.append(queued)
        pending_count = len(self._subtitle_save_queue) + int(self._subtitle_save_in_flight)
        self._save_state = f"Đang đồng bộ {pending_count} thay đổi · Chưa lưu"
        self.saveStateChanged.emit()
        self._subtitle_save_timer.start()

    def _defer_until_subtitle_saves_flushed(
        self,
        key: str,
        callback: Callable[[], None],
        status: str,
        *,
        durable: bool = False,
    ) -> bool:
        after_sync = (lambda: self._save_before_action(key, callback)) if durable else callback
        if (not self._subtitle_save_in_flight and not self._subtitle_save_queue
                and not self._scene_timing_context and not self._subtitle_geometry_context
                and not self._subtitle_command_context and not self._timeline_busy
                and not self._pending_effect_commands and not self._timeline_reconcile_pending):
            if durable and (self._project_dirty or self._project_save_context):
                after_sync()
                return True
            return False
        self._subtitle_flush_callbacks[key] = after_sync
        self._subtitle_save_timer.stop()
        self._dispatch_background_subtitle_save()
        self._dispatch_pending_effect_command()
        self._set_status(status)
        return True

    @Property(bool, notify=saveStateChanged)
    def projectDirty(self) -> bool:
        return self._project_dirty

    @Property(bool, notify=saveStateChanged)
    def projectSaveBusy(self) -> bool:
        return bool(self._project_save_context)

    def _observe_project_persistence(self, result: object) -> None:
        if not isinstance(result, dict) or type(result.get("_project_dirty")) is not bool:
            return
        project_id = str(result.get("id", ""))
        if not project_id or project_id != self.project_state.projectId:
            return
        if self._draft_project_id != project_id:
            self._draft_project_id = project_id
            self._last_saved_revision = 0
        saved_revision = int(result.get("_saved_revision", 0))
        self._last_saved_revision = max(self._last_saved_revision, saved_revision)
        self._project_dirty = self.project_state.revision > self._last_saved_revision
        if self._project_dirty:
            if not self._project_save_context and not self._project_autosave_timer.isActive():
                self._project_autosave_timer.start()
            self._save_state = "Chưa lưu · Ctrl+S · Tự lưu sau 5 phút nếu còn thay đổi"
            if result.get("_project_dirty"):
                self._set_status("Đã cập nhật bản đang chỉnh · Ctrl+S để lưu")
        else:
            self._project_autosave_timer.stop()
            self._save_state = "Đã lưu"
        self.saveStateChanged.emit()

    @Slot()
    def _request_project_autosave(self) -> None:
        if (self.project_state.hasProject and self._project_dirty
                and not self._project_save_context):
            self.projectAutosaveRequested.emit()

    @Slot()
    def saveProject(self) -> None:
        if not self.project_state.hasProject:
            return
        if self._defer_until_subtitle_saves_flushed("manual-save", self.saveProject,
                                                   "Đang đồng bộ chỉnh sửa trước khi lưu…"):
            return
        if not self._project_dirty and not self._project_save_context:
            self._project_autosave_timer.stop()
            return
        self._start_project_save()

    def _save_before_action(self, key: str, callback: Callable[[], None]) -> None:
        if not self._project_dirty and not self._project_save_context:
            callback()
            return
        self._project_after_save_callbacks[key] = callback
        self._start_project_save()

    def _start_project_save(self) -> None:
        if self._project_save_context:
            return
        self._project_autosave_timer.stop()
        self._project_save_generation += 1
        context = {"project_id": self.project_state.projectId,
                   "save_generation": self._project_save_generation}
        self._project_save_context = context
        self._project_save_watchdog.start()
        self._save_state = "Đang lưu dự án…"
        self.saveStateChanged.emit()
        try:
            self.runtime.request("save_project_edits", {"project_id": context["project_id"]},
                                 context=context)
        except Exception as exc:
            self._project_save_failed({"message": str(exc)}, context)

    def _project_save_finished(self, result: object, context: object) -> None:
        if not self._project_save_context or context != self._project_save_context:
            return
        if (not isinstance(result, dict) or result.get("id") != self.project_state.projectId
                or type(result.get("_saved_revision")) is not int):
            self._project_save_failed({"message": "Xác nhận lưu không hợp lệ"}, context)
            return
        self._project_save_context = {}
        self._project_save_watchdog.stop()
        self._observe_project_persistence(result)
        callbacks = tuple(self._project_after_save_callbacks.values())
        self._project_after_save_callbacks.clear()
        for callback in callbacks:
            # Edits may have arrived while the previous snapshot was saving.
            # A dependent switch/job must save that newer draft too.
            QTimer.singleShot(0, lambda cb=callback: self._defer_after_project_save(cb))
        self._set_status("Đã lưu dự án" if not self._project_dirty else
                         "Đã lưu bản trước; còn chỉnh sửa mới chưa lưu")

    def _defer_after_project_save(self, callback: Callable[[], None]) -> None:
        if not self._defer_until_subtitle_saves_flushed(
            f"after-save:{id(callback)}", callback,
            "Đang đồng bộ chỉnh sửa mới trước khi tiếp tục…", durable=True,
        ):
            callback()

    def _project_save_failed(self, error: object, context: object) -> None:
        if not self._project_save_context or context != self._project_save_context:
            return
        self._project_save_context = {}
        self._project_save_watchdog.stop()
        self._project_after_save_callbacks.clear()
        self._save_state = "Chưa lưu · Lưu thất bại, Ctrl+S để thử lại"
        if self._project_dirty and not self._project_autosave_timer.isActive():
            self._project_autosave_timer.start()
        self.saveStateChanged.emit()
        self._set_status("Lưu chưa hoàn tất; giữ nguyên bản đang chỉnh. "
                         + self._format_action_error(error))

    @Slot()
    def _project_save_timed_out(self) -> None:
        self._project_save_failed({"message": "Chưa nhận xác nhận lưu sau 30 giây"},
                                  dict(self._project_save_context))

    @Slot()
    def _dispatch_background_subtitle_save(self) -> None:
        if (self._subtitle_save_in_flight or not self._subtitle_save_queue
                or self._subtitle_command_context or self._timeline_reconcile_pending):
            return
        pending = self._subtitle_save_queue.pop(0)
        params = pending.get("params")
        context = pending.get("context")
        if not isinstance(params, dict) or not isinstance(context, dict):
            self._dispatch_background_subtitle_save()
            return
        self._subtitle_save_in_flight = True
        self._subtitle_save_generation += 1
        context = {**context, "background_save_generation": self._subtitle_save_generation}
        self._subtitle_save_context = context
        self._subtitle_save_watchdog.start()
        try:
            self.runtime.request("subtitle_command", params, context=context)
        except Exception as exc:
            self._action_failed("subtitle_command", {"message": str(exc)}, context)
            self._begin_timeline_reconcile(str(context.get("project_id", "")))

    def _background_save_response_is_current(self, context: object) -> bool:
        if not isinstance(context, dict) or not context.get("background_save_generation"):
            return True
        return context.get("background_save_generation") == self._subtitle_save_context.get(
            "background_save_generation") and (
                context.get("project_id") == self.project_state.projectId)

    @Slot()
    def _background_subtitle_save_timed_out(self) -> None:
        if not self._subtitle_save_in_flight:
            return
        context = dict(self._subtitle_save_context)
        self._action_failed("subtitle_command", {
            "code": "save_ack_timeout",
            "message": ("Chưa nhận xác nhận lưu sau 120 giây; đã hủy thao tác xóa chờ "
                        "và đang đọc lại dự án"),
        }, context)
        self._begin_timeline_reconcile(str(context.get("project_id", "")))

    def _finish_background_subtitle_save(self, succeeded: bool = True) -> None:
        self._subtitle_save_in_flight = False
        self._subtitle_save_context = {}
        self._subtitle_save_watchdog.stop()
        if not succeeded:
            for pending in self._subtitle_save_queue:
                context = pending.get("context")
                if isinstance(context, dict):
                    self.project_state.subtitle_cues.rollback_text_changes(
                        context.get("optimistic_changes", [])
                    )
            self._subtitle_save_queue.clear()
            self._subtitle_flush_callbacks.clear()
            self.saveStateChanged.emit()
            return
        if self._subtitle_save_queue:
            if self._subtitle_flush_callbacks:
                QTimer.singleShot(0, self._dispatch_background_subtitle_save)
            else:
                self._subtitle_save_timer.start()
            self._save_state = f"Đang đồng bộ {len(self._subtitle_save_queue)} thay đổi · Chưa lưu"
        else:
            self._save_state = (
                "Đang lưu thay đổi phụ đề"
                if self._scene_timing_context or self._subtitle_geometry_context else (
                    "Chưa lưu · Ctrl+S · Tự lưu mỗi 5 phút" if self._project_dirty else "Đã lưu"
                )
            )
            self._run_subtitle_flush_callbacks()
        self.saveStateChanged.emit()

    def _run_subtitle_flush_callbacks(self) -> None:
        if (self._scene_timing_context or self._subtitle_geometry_context
                or self._subtitle_command_context
                or self._subtitle_save_in_flight
                or self._subtitle_save_queue or self._timeline_busy
                or self._pending_effect_commands or self._timeline_reconcile_pending):
            return
        callbacks = tuple(self._subtitle_flush_callbacks.values())
        self._subtitle_flush_callbacks.clear()
        for callback in callbacks:
            QTimer.singleShot(0, callback)

    def _cancel_dependent_editor_changes(self) -> None:
        """Do not replay uncertain dependent edits later or in another project."""
        self._subtitle_flush_callbacks.clear()
        if self._subtitle_save_queue or self._subtitle_save_in_flight:
            self._finish_background_subtitle_save(False)
        geometry_ids = set()
        for pending in reversed(self._pending_effect_commands):
            self.project_state.rollback_local_effect(pending.get("rollback", {}))
            if pending.get("geometry") and pending.get("overlay_id"):
                geometry_ids.add(str(pending["overlay_id"]))
        self._pending_effect_commands.clear()
        for overlay_id in geometry_ids:
            self.overlayGeometrySettled.emit(overlay_id, False)

    def _start_ocr_worker(self, job_id: str) -> None:
        self.runtime.request(
            "start_job",
            {
                "job_id": job_id,
                "program": str(runtime_program_path()),
                "arguments": [
                    "-m",
                    "ducai_studio.workers.ocr_worker",
                    job_id,
                ],
                "resume_stage_key": "ocr",
                "resume_checkpoint_key": "latest",
            },
            context={"job_kind": "ocr"},
        )

    def _start_translation_worker(self, job_id: str) -> None:
        self.runtime.request(
            "start_job",
            {
                "job_id": job_id,
                "program": str(runtime_program_path()),
                "arguments": [
                    "-m",
                    "ducai_studio.workers.translation_worker",
                    job_id,
                ],
                "resume_stage_key": "translation",
                "resume_checkpoint_key": "latest",
            },
            context={"job_kind": "translate"},
        )

    def _start_dialogue_casting_worker(self, job_id: str) -> None:
        self.runtime.request(
            "start_job",
            {
                "job_id": job_id,
                "program": str(runtime_program_path()),
                "arguments": [
                    "-m",
                    "ducai_studio.workers.dialogue_casting_worker",
                    job_id,
                ],
                "resume_stage_key": "dialogue_casting",
                "resume_checkpoint_key": "latest",
            },
            context=self._dialogue_casting_job_context(job_id),
        )

    def _start_tts_worker(self, job_id: str) -> None:
        self.runtime.request(
            "start_job",
            {
                "job_id": job_id,
                "program": str(runtime_program_path()),
                "arguments": [
                    "-m",
                    "ducai_studio.workers.tts_worker",
                    job_id,
                ],
                "resume_stage_key": "tts",
                "resume_checkpoint_key": "latest",
            },
            context={"job_kind": "tts"},
        )

    def _start_export_worker(self, job_id: str) -> None:
        self.runtime.request(
            "start_job",
            {
                "job_id": job_id,
                "program": str(runtime_program_path()),
                "arguments": [
                    "-m",
                    "ducai_studio.workers.export_worker",
                    job_id,
                ],
                "resume_stage_key": "export",
                "resume_checkpoint_key": "latest",
            },
            context={"job_kind": "export"},
        )

    def _send_timeline_command(
        self,
        command: str,
        *,
        value: str | int | float | dict | None = None,
        apply_linked: bool = True,
        use_playhead: bool = False,
        needs_selection: bool = True,
    ) -> None:
        if self._timeline_busy or not self.project_state.hasProject:
            return
        if needs_selection and not self._selected_clip_id:
            self._set_status("Hãy chọn clip trước")
            return
        params: dict[str, object] = {
            "project_id": self.project_state.projectId,
            "command": command,
            "apply_linked": apply_linked,
        }
        if needs_selection:
            params["clip_id"] = self._selected_clip_id
        if use_playhead:
            params["timeline_us"] = self._playhead_us
        if value is not None:
            params["value"] = value
        self._set_timeline_busy(True)
        self.runtime.request("timeline_command", params)
        self._set_status("Đang cập nhật timeline")

    def _send_track_command(self, command: str, value: bool) -> None:
        if self._timeline_busy or not self.project_state.hasProject or not self._selected_track_id:
            self._set_status("Hãy chọn track trước")
            return
        self._send_track_command_by_id(command, self._selected_track_id, value)

    def _send_track_command_by_id(self, command: str, track_id: str, value: bool | float) -> None:
        if self._timeline_busy or not self.project_state.hasProject:
            return
        if self.project_state.track_info(track_id) is None:
            self._set_status("Track cần chỉnh không tồn tại")
            return
        self._set_timeline_busy(True)
        self.runtime.request(
            "timeline_command",
            {
                "project_id": self.project_state.projectId,
                "command": command,
                "track_id": track_id,
                "value": value,
            },
        )
        self._set_status("Đang cập nhật track")
