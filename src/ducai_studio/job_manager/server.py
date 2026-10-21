from __future__ import annotations

import base64
import hashlib
import json
import zlib
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ducai_studio.application.job_commands import control_intent, retry_target
from ducai_studio.domain.jobs import (
    TERMINAL_JOB_STATES,
    JobKind,
    JobState,
    assert_safe_error_code,
)
from ducai_studio.infrastructure.credential_store import WindowsCredentialStore
from ducai_studio.infrastructure.database_optimizer import DatabaseOptimizationCancelled
from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
    encode_frame,
)
from ducai_studio.infrastructure.licensing_state import LicensingState
from ducai_studio.infrastructure.runtime_diagnostics import isolated_runtime_diagnostics
from ducai_studio.infrastructure.runtime_program import runtime_program_path
from ducai_studio.providers.translation.model_catalog import ModelCatalogError, discover_models
from ducai_studio.providers.tts.warm_host import WarmTtsHostConfig, WarmTtsHostPool
from ducai_studio.workers.protocol import WorkerMessage

from .multi_video_coordinator import MultiVideoCoordinator
from .service import (
    TRANSLATION_API_PROVIDER_IDS,
    JobManagerService,
    _engine_host_path,
    _file_sha256,
)
from .worker_supervisor import WorkerSupervisor


def _required_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _integer_param(params: dict[str, Any], key: str, *, default: int, minimum: int) -> int:
    value = params.get(key, default)
    if type(value) is not int or value < minimum:
        raise ValueError(f"{key} must be an integer greater than or equal to {minimum}")
    return value


_PROJECT_INLINE_COMPRESSION_THRESHOLD = 1_000_000
_PROJECT_TRANSPORT_MAX_BYTES = 256 * 1024 * 1024
_CONTROL_RETRY_INTERVAL_MS = 750
_START_CONTROL_FIRST_RETRY_MS = 750
_START_CONTROL_RETRY_INTERVAL_MS = 3_000
_START_CONTROL_SLOW_AFTER_SECONDS = 30.0
_START_CONTROL_SLOW_RETRY_INTERVAL_MS = 5_000
_TTS_REPLACEMENT_QUIET_MS = 500
_DATABASE_MAINTENANCE_READ_ACTIONS = frozenset(
    {
        "cancel_database_optimization",
        "get_database_optimization",
        "get_job",
        "get_project",
        "get_translation_preview",
        "get_update_install_eligibility",
        "inspect_media_usage",
        "list_credentials",
        "list_events",
        "list_jobs",
        "list_projects",
        "list_translation_caches",
        "multi_list",
        "multi_status",
        "ping",
        "runtime_diagnostics",
        "shutdown",
    }
)
_MANAGED_ENGINE_HOSTS = {
    JobKind.OCR: (
        "ocr_engine_host.py",
        "DUCAI_OCR_ENGINE_HOST_PATH",
        "DUCAI_OCR_ENGINE_HOST_SHA256",
    ),
    JobKind.TTS: (
        "tts_engine_host.py",
        "DUCAI_TTS_ENGINE_HOST_PATH",
        "DUCAI_TTS_ENGINE_HOST_SHA256",
    ),
}
_UPDATE_BLOCKED_ACTIONS = frozenset(
    {
        "create_job",
        "create_ocr_job",
        "create_ocr_rescan_job",
        "create_translation_job",
        "create_tts_job",
        "prepare_tts_rerun",
        "estimate_voice_conform",
        "create_voice_conform_job",
        "create_voice_timing_repair_job",
        "create_tts_timing_retry_job",
        "create_tts_timing_override_job",
        "create_export_job",
        "start_job",
        "retry_job",
        "resume_job",
        "multi_start",
        "multi_resume",
    }
)


def _managed_engine_host_contract(job: Any) -> tuple[str, str, str] | None:
    if (
        job.spec.kind is JobKind.OCR
        and job.spec.provider_config.get("extraction_method") == "whisper_audio"
    ):
        return (
            "whisper_engine_host.py",
            "DUCAI_OCR_ENGINE_HOST_PATH",
            "DUCAI_OCR_ENGINE_HOST_SHA256",
        )
    return _MANAGED_ENGINE_HOSTS.get(job.spec.kind)


def _is_managed_release_engine_host(path: Path | None, filename: str) -> bool:
    """Distinguish packaged engine hosts from user-owned custom hosts."""

    return (
        path is not None
        and path.stem.casefold() == Path(filename).stem.casefold()
        and path.suffix.casefold() in {".py", ".pyc"}
        and path.parent.name.casefold() == "engine_hosts"
        and path.parent.parent.name.casefold().startswith("ducai-studio-")
    )


def _project_transport_payload(
    snapshot: dict[str, Any], *, staging_root: Path
) -> dict[str, Any]:
    """Keep large project snapshots below the fixed IPC frame limit.

    Timeline projects can contain tens of thousands of cue clips.  Returning
    their expanded JSON directly either exceeds the 16 MiB frame limit or
    blocks the UI while it serializes.  A compressed envelope is cheap for
    normal projects; an artifact fallback handles deliberately uncompressible
    snapshots without changing the logical project mutation result.
    """

    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) <= _PROJECT_INLINE_COMPRESSION_THRESHOLD:
        return snapshot
    if len(raw) > _PROJECT_TRANSPORT_MAX_BYTES:
        raise ValueError("Project snapshot exceeds the safe transport size")
    digest = hashlib.sha256(raw).hexdigest()
    compressed = zlib.compress(raw, level=6)
    envelope = {
        "project_id": str(snapshot.get("id", "")),
        "revision": int(snapshot.get("revision", 0) or 0),
        "encoding": "zlib+base64+json",
        "uncompressed_bytes": len(raw),
        "compressed_bytes": len(compressed),
        "sha256": digest,
        "data": base64.b64encode(compressed).decode("ascii"),
    }
    # Base64 adds ~33%; use a conservative margin so the framed response is
    # still accepted after the surrounding IPC envelope is serialized.
    if len(json.dumps(envelope, separators=(",", ":")).encode("utf-8")) < 12 * 1024 * 1024:
        return {"__project_transport__": envelope}

    target_dir = (staging_root / "project-open").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid4().hex}.json.zlib"
    target.write_bytes(compressed)
    return {
        "__project_transport__": {
            "project_id": str(snapshot.get("id", "")),
            "revision": int(snapshot.get("revision", 0) or 0),
            "encoding": "file+zlib+json",
            "uncompressed_bytes": len(raw),
            "compressed_bytes": len(compressed),
            "sha256": digest,
            "path": str(target),
        }
    }


def _translation_preferences_transport_payload(
    snapshot: dict[str, Any], *, compact: bool, staging_root: Path,
) -> dict[str, Any]:
    if not compact:
        return _project_transport_payload(snapshot, staging_root=staging_root)
    return {
        "id": snapshot["id"], "revision": snapshot["revision"],
        "_translation_preferences_patch": True,
        "translation_preferences": snapshot["translation_preferences"],
        "_project_dirty": snapshot.get("_project_dirty", False),
        "_saved_revision": snapshot.get("_saved_revision", snapshot["revision"]),
    }


def _job_transport_payload(result: Any) -> Any:
    """GUI job acknowledgements need identity/status, never the worker's bulk input."""
    if isinstance(result, list):
        return [_job_transport_payload(item) for item in result]
    if not isinstance(result, dict) or not result.get("id"):
        return result
    spec = result.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("provider_config"), dict):
        return result
    config = spec["provider_config"]
    bulk = {"source_cues", "cues", "snapshot", "export_snapshot", "project_snapshot",
            "preserved_manual_translations", "source_cue_identities", "translated_cues"}
    compact = {key: value for key, value in config.items() if key not in bulk}
    return {**result, "spec": {**spec, "provider_config": compact}}


class _DatabaseOptimizerThread(QThread):
    progressReported = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, service: JobManagerService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._cancel_event = Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            result = self._service.optimize_database(
                cancel_event=self._cancel_event,
                progress_callback=self.progressReported.emit,
            )
        except DatabaseOptimizationCancelled as exc:
            self.completed.emit({"state": "cancelled", "message": str(exc)})
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit({"state": "completed", **result})


class _ModelCatalogThread(QThread):
    completed = Signal(object)

    def __init__(self, socket: QLocalSocket, request_id: str, params: dict, parent: QObject):
        super().__init__(parent)
        self.socket = socket
        self.request_id = request_id
        self.params = params

    def run(self) -> None:
        try:
            secret = WindowsCredentialStore().read(self.params["credential_reference"])
            result = discover_models(
                self.params["provider_id"], secret, base_url=self.params.get("base_url", ""),
                cancelled=self.isInterruptionRequested,
            )
        except ModelCatalogError as exc:
            result = {"models": [], "error": str(exc)}
        except Exception:
            result = {"models": [], "error": (
                "Không đọc được key hoặc danh sách model. Hãy nhập tên thủ công."
            )}
        self.completed.emit(result)


class _RuntimeDiagnosticsThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, socket: QLocalSocket, request_id: str, data_root: str | None,
                 parent: QObject):
        super().__init__(parent)
        self.socket = socket
        self.request_id = request_id
        self.data_root = data_root

    def run(self) -> None:
        try:
            result = isolated_runtime_diagnostics(
                data_root=self.data_root, cancelled=self.isInterruptionRequested)
        except Exception:
            self.failed.emit("Chẩn đoán chưa hoàn tất. Hãy thử lại khi máy rảnh.")
        else:
            if result is not None and not self.isInterruptionRequested():
                self.completed.emit(result)


class JobManagerServer(QObject):
    def __init__(
        self,
        *,
        name: str,
        service: JobManagerService,
        workers: WorkerSupervisor | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self.service = service
        self.licensing = LicensingState()
        self.workers = workers or WorkerSupervisor(self)
        self.server = QLocalServer(self)
        self._decoders: dict[QLocalSocket, FrameDecoder] = {}
        self._catalog_threads: set[_ModelCatalogThread] = set()
        self._diagnostic_threads: set[_RuntimeDiagnosticsThread] = set()
        self._control_retry_attempts: dict[str, int] = {}
        self._control_retry_started_at: dict[str, float] = {}
        self._slow_start_controls_reported: set[str] = set()
        self._pending_history_deletions: set[str] = set()
        self._pending_tts_replacements: set[str] = set()
        self._pending_tts_replacement_projects: dict[str, str] = {}
        self._pending_tts_replacement_deadlines: dict[str, float] = {}
        self._database_optimizer_thread: _DatabaseOptimizerThread | None = None
        self._database_maintenance: dict[str, Any] = {"state": "idle"}
        self._update_install_reservation = ""
        self._shutting_down = False
        self._shutdown_deadline = 0.0
        self._warm_tts = WarmTtsHostPool(idle_timeout_seconds=120)
        self.multi = MultiVideoCoordinator(self)
        self.server.newConnection.connect(self._accept_connections)
        self.workers.eventReceived.connect(self._worker_event)
        self.workers.workerLog.connect(self._worker_log)
        self.workers.workerFailed.connect(self._worker_failed)
        self.workers.workerFinished.connect(self._worker_finished)

    def listen(self) -> None:
        if not self.server.listen(self.name):
            raise RuntimeError(f"Cannot listen on {self.name}: {self.server.errorString()}")

    def close(self) -> None:
        for thread in tuple(self._diagnostic_threads):
            thread.requestInterruption()
            thread.wait(10_000)
        for thread in tuple(self._catalog_threads):
            thread.requestInterruption()
            thread.wait(10_000)
        optimizer = self._database_optimizer_thread
        if optimizer is not None and optimizer.isRunning():
            optimizer.cancel()
            optimizer.wait(10_000)
        self.workers.force_shutdown()
        self._warm_tts.close()
        for socket in tuple(self._decoders):
            socket.abort()
        self.server.close()

    @Slot()
    def _accept_connections(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                return
            self._decoders[socket] = FrameDecoder()
            socket.readyRead.connect(lambda current=socket: self._read_socket(current))
            socket.disconnected.connect(lambda current=socket: self._drop_socket(current))

    def _drop_socket(self, socket: QLocalSocket) -> None:
        self._decoders.pop(socket, None)
        for thread in self._diagnostic_threads:
            if thread.socket is socket:
                thread.requestInterruption()
        socket.deleteLater()

    def _broadcast_multi_status(self) -> None:
        self._broadcast_multi_result(
            self.service.list_multi_video_status(), partial=True
        )

    def _broadcast_multi_catalog(self) -> None:
        """Refresh media/artifact descriptors once after a terminal publish."""

        # The payload is rich, but it is still a patch while card membership and
        # order are unchanged. The client merge path will reset only if IDs differ.
        self._broadcast_multi_result(self.service.list_multi_videos(), partial=True)

    def _broadcast_multi_result(
        self, result: dict[str, Any], *, partial: bool
    ) -> None:
        message = IpcMessage(
            protocol_version=IPC_PROTOCOL_VERSION,
            message_id=str(uuid4()),
            kind="event",
            payload={
                "event": "multi_status",
                "result": result,
                "partial": partial,
            },
        )
        frame = encode_frame(message)
        for socket in tuple(self._decoders):
            if socket.state() is QLocalSocket.LocalSocketState.ConnectedState:
                socket.write(frame)

    def _read_socket(self, socket: QLocalSocket) -> None:
        decoder = self._decoders.get(socket)
        if decoder is None:
            return
        try:
            messages = decoder.feed(bytes(socket.readAll()))
            for message in messages:
                self._handle_message(socket, message)
        except Exception as exc:
            self._send_error(socket, request_id="unknown", exc=exc)

    def _handle_message(self, socket: QLocalSocket, message: IpcMessage) -> None:
        if message.kind != "request":
            self._send_error(
                socket,
                request_id=message.message_id,
                exc=ValueError("Expected request"),
            )
            return
        action = message.payload.get("action")
        if not isinstance(action, str) or not action.strip():
            self._send_error(
                socket,
                request_id=message.message_id,
                exc=ValueError("action must be a non-empty string"),
            )
            return
        params = message.payload.get("params", {})
        if not isinstance(params, dict):
            self._send_error(
                socket, request_id=message.message_id, exc=ValueError("params must be an object")
            )
            return
        try:
            self.licensing.require_action(action)
            if action == "list_translation_models":
                self._start_model_catalog(socket, message.message_id, params)
                return
            if action == "runtime_diagnostics":
                self._start_runtime_diagnostics(socket, message.message_id, params)
                return
            result = _job_transport_payload(self._dispatch(action, params))
            response = IpcMessage(
                protocol_version=IPC_PROTOCOL_VERSION,
                message_id=str(uuid4()),
                kind="response",
                payload={"request_id": message.message_id, "ok": True, "result": result},
            )
            frame = encode_frame(response)
        except Exception as exc:
            self._send_error(socket, request_id=message.message_id, exc=exc)
            return
        socket.write(frame)
        socket.flush()
        if action == "shutdown":
            QTimer.singleShot(0, self._begin_shutdown)

    def _start_model_catalog(self, socket: QLocalSocket, request_id: str, params: dict) -> None:
        self.licensing.require_active()
        provider = _required_string(params, "provider_id")
        reference = _required_string(params, "credential_reference")
        if provider not in TRANSLATION_API_PROVIDER_IDS:
            raise ValueError("Nguồn dịch không hỗ trợ danh sách model API")
        if self._shutting_down or len(self._catalog_threads) >= 2:
            raise ValueError("Đang tải danh sách model. Hãy chờ rồi thử lại.")
        thread = _ModelCatalogThread(socket, request_id, {
            "provider_id": provider, "credential_reference": reference,
            "base_url": str(params.get("base_url", "")),
        }, self)
        self._catalog_threads.add(thread)
        thread.completed.connect(self._model_catalog_completed)
        thread.finished.connect(self._model_catalog_finished)
        thread.start()

    def _start_runtime_diagnostics(self, socket: QLocalSocket, request_id: str,
                                   params: dict) -> None:
        data_root = params.get("data_root")
        if data_root is not None and (not isinstance(data_root, str) or not data_root.strip()):
            raise ValueError("data_root must be a non-empty string")
        if self._shutting_down or self._diagnostic_threads:
            raise ValueError("Đang chẩn đoán. Hãy chờ rồi thử lại.")
        thread = _RuntimeDiagnosticsThread(socket, request_id, data_root, self)
        self._diagnostic_threads.add(thread)
        thread.completed.connect(self._runtime_diagnostics_completed)
        thread.failed.connect(self._runtime_diagnostics_failed)
        thread.finished.connect(self._runtime_diagnostics_finished)
        thread.start()

    @Slot(object)
    def _runtime_diagnostics_completed(self, result: object) -> None:
        thread = self.sender()
        if (not isinstance(thread, _RuntimeDiagnosticsThread) or self._shutting_down
                or thread.socket not in self._decoders):
            return
        thread.socket.write(encode_frame(IpcMessage(
            protocol_version=IPC_PROTOCOL_VERSION, message_id=str(uuid4()), kind="response",
            payload={"request_id": thread.request_id, "ok": True, "result": result},
        )))
        thread.socket.flush()

    @Slot(str)
    def _runtime_diagnostics_failed(self, message: str) -> None:
        thread = self.sender()
        if (isinstance(thread, _RuntimeDiagnosticsThread) and not self._shutting_down
                and thread.socket in self._decoders):
            self._send_error(thread.socket, request_id=thread.request_id, exc=RuntimeError(message))

    @Slot()
    def _runtime_diagnostics_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, _RuntimeDiagnosticsThread):
            self._diagnostic_threads.discard(thread)
            thread.deleteLater()

    @Slot(object)
    def _model_catalog_completed(self, result: object) -> None:
        thread = self.sender()
        if not isinstance(thread, _ModelCatalogThread) or thread.socket not in self._decoders:
            return
        thread.socket.write(encode_frame(IpcMessage(
            protocol_version=IPC_PROTOCOL_VERSION, message_id=str(uuid4()), kind="response",
            payload={"request_id": thread.request_id, "ok": True, "result": result},
        )))
        thread.socket.flush()

    @Slot()
    def _model_catalog_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, _ModelCatalogThread):
            self._catalog_threads.discard(thread)
            thread.deleteLater()

    def _dispatch(self, action: str, params: dict[str, Any]) -> Any:
        self.licensing.require_action(action)
        if action == "get_license_status":
            return self.licensing.status(refresh=True)
        if action == "activate_license":
            return self.licensing.activate(_required_string(params, "token"))
        if action == "save_project_edits":
            return self.service.save_project_edits(_required_string(params, "project_id"))
        deferred = params.get("deferred", False)
        if (action in {"subtitle_command", "timeline_command", "effect_command"}
                and type(deferred) is not bool):
            raise ValueError("Chế độ lưu bản nháp phải là bật/tắt")
        if (
            self._database_maintenance.get("state") in {"running", "cancelling"}
            and action not in _DATABASE_MAINTENANCE_READ_ACTIONS
        ):
            raise ValueError(
                "Database đang được tối ưu; hãy chờ hoàn tất hoặc bấm Hủy trước khi sửa project."
            )
        if action == "ping":
            return {"status": "ready", "license": self.licensing.status()}
        if action == "get_update_install_eligibility":
            return self._update_install_eligibility()
        if action == "reserve_update_install":
            return self._reserve_update_install()
        if action == "release_update_install":
            token = _required_string(params, "token")
            if not self._update_install_reservation or token != self._update_install_reservation:
                raise ValueError("Phiên cài đặt cập nhật không còn hợp lệ")
            self._update_install_reservation = ""
            return {"released": True}
        if action in _UPDATE_BLOCKED_ACTIONS:
            self._ensure_update_admission_open()
        if action == "create_job":
            return self.service.create_job(params)
        if action == "store_credential":
            return self.service.store_credential(
                reference=_required_string(params, "reference"),
                secret=_required_string(params, "secret"),
            )
        if action == "store_credentials":
            return self.service.store_credentials(credentials=params.get("credentials"))
        if action == "list_credentials":
            return self.service.list_credentials()
        if action == "delete_credential":
            return self.service.delete_credential(reference=_required_string(params, "reference"))
        if action == "create_ocr_job":
            sample_interval_us = params.get("sample_interval_us")
            if type(sample_interval_us) is not int:
                raise ValueError("sample_interval_us must be an integer")
            return self.service.create_ocr_job(
                project_id=_required_string(params, "project_id"),
                sample_interval_us=sample_interval_us,
                engine_python=_required_string(params, "engine_python"),
                device=_required_string(params, "device"),
                reuse_completed=bool(params.get("reuse_completed", False)),
                extraction_method=str(params.get("extraction_method", "frame_ocr")),
                scene_text_enabled=params.get("scene_text_enabled", True),
                language=str(params.get("language", "zh")),
                model_id=str(params.get("model_id", "large-v3")),
                model_path=(
                    str(params["model_path"])
                    if isinstance(params.get("model_path"), str)
                    else None
                ),
            )
        if action == "create_ocr_rescan_job":
            sample_interval_us = params.get("sample_interval_us")
            if type(sample_interval_us) is not int:
                raise ValueError("sample_interval_us must be an integer")
            return self.service.create_ocr_job(
                project_id=_required_string(params, "project_id"),
                sample_interval_us=sample_interval_us,
                engine_python=_required_string(params, "engine_python"),
                device=_required_string(params, "device"),
                rescan_cue_id=_required_string(params, "cue_id"),
                reuse_completed=bool(params.get("reuse_completed", False)),
            )
        if action == "create_translation_job":
            provider_options = params.get("provider_options", {})
            if not isinstance(provider_options, dict):
                raise ValueError("provider_options must be an object")
            return self.service.create_translation_job(
                project_id=_required_string(params, "project_id"),
                provider_id=_required_string(params, "provider_id"),
                model_id=_required_string(params, "model_id"),
                provider_options=provider_options,
                reuse_completed=bool(params.get("reuse_completed", False)),
            )
        if action == "create_tts_job":
            provider_options = params.get("provider_options", {})
            if not isinstance(provider_options, dict):
                raise ValueError("provider_options must be an object")
            return self.service.create_tts_job(
                project_id=_required_string(params, "project_id"),
                provider_id=_required_string(params, "provider_id"),
                voice_id=_required_string(params, "voice_id"),
                provider_options=provider_options,
                reuse_completed=bool(params.get("reuse_completed", False)),
            )
        if action == "prepare_tts_rerun":
            return _project_transport_payload(
                self.service.prepare_tts_rerun(
                    project_id=_required_string(params, "project_id")
                ),
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "create_project_with_media":
            options = params.get("overlay_preset")
            if options is not None and not isinstance(options, dict):
                raise ValueError("overlay_preset must be an object")
            default_logo = params.get("default_logo_path")
            if default_logo is not None and not isinstance(default_logo, str):
                raise ValueError("default_logo_path must be a string")
            excluded_project_ids = params.get("excluded_project_ids", [])
            if not isinstance(excluded_project_ids, list) or not all(
                isinstance(value, str) for value in excluded_project_ids
            ):
                raise ValueError("excluded_project_ids must be an array of strings")
            return self.service.create_project_with_media(
                project_path=_required_string(params, "project_path"),
                name=_required_string(params, "name"),
                media_path=_required_string(params, "media_path"),
                default_logo_path=default_logo,
                overlay_preset=options,
                excluded_project_ids=excluded_project_ids,
            )
        if action in {"estimate_voice_conform", "create_voice_conform_job"}:
            max_audio_speed = params.get("max_audio_speed", 1.25)
            min_video_speed = params.get("min_video_speed", 0.5)
            allow_duration_growth = params.get("allow_duration_growth", True)
            if (
                isinstance(max_audio_speed, bool)
                or not isinstance(max_audio_speed, (int, float))
                or isinstance(min_video_speed, bool)
                or not isinstance(min_video_speed, (int, float))
                or type(allow_duration_growth) is not bool
            ):
                raise ValueError("Voice conform speed/settings values are invalid")
            kwargs = {
                "project_id": _required_string(params, "project_id"),
                "parent_tts_job_id": str(params.get("parent_tts_job_id", "")),
                "mode": _required_string(params, "mode"),
                "max_audio_speed": float(max_audio_speed),
                "min_video_speed": float(min_video_speed),
                "allow_duration_growth": allow_duration_growth,
            }
            if action == "estimate_voice_conform":
                return self.service.estimate_voice_conform(
                    **kwargs,
                    include_plan=False,
                    verify_cache=False,
                )
            created = self.service.create_voice_conform_job(**kwargs)
            # The GUI only needs the new cache-job identity and state.  The
            # full conform plan remains durable in Job Manager, but returning
            # its 5+ MB provider config here would stall QML on every apply.
            return {
                "id": created.get("id", ""),
                "state": created.get("state", "queued"),
                "kind": "tts",
                "project_id": kwargs["project_id"],
                "provider_id": "voice-conform",
            }
        if action == "reset_voice_conform":
            return self.service.reset_voice_conform(_required_string(params, "project_id"))
        if action == "get_voice_timing_review":
            return self.service.get_voice_timing_review(_required_string(params, "job_id"))
        if action == "create_voice_timing_repair_job":
            translation_job_id = params.get("translation_job_id")
            if translation_job_id is not None and not isinstance(translation_job_id, str):
                raise ValueError("translation_job_id must be a string")
            return self.service.create_voice_timing_repair_job(
                project_id=_required_string(params, "project_id"),
                tts_job_id=_required_string(params, "tts_job_id"),
                translation_job_id=translation_job_id,
            )
        if action == "create_tts_timing_retry_job":
            return self.service.create_tts_timing_retry_job(
                project_id=_required_string(params, "project_id"),
                parent_tts_job_id=_required_string(params, "parent_tts_job_id"),
                repair_job_id=_required_string(params, "repair_job_id"),
            )
        if action == "create_tts_timing_override_job":
            return self.service.create_tts_timing_override_job(
                project_id=_required_string(params, "project_id"),
                parent_tts_job_id=_required_string(params, "parent_tts_job_id"),
            )
        if action == "create_export_job":
            export_options = params.get("export_options", {})
            if not isinstance(export_options, dict):
                raise ValueError("export_options must be an object")
            return self.service.create_export_job(
                project_id=_required_string(params, "project_id"),
                output_path=_required_string(params, "output_path"),
                export_options=export_options,
            )
        if action == "get_job":
            return self.service.get_job(_required_string(params, "job_id"))
        if action == "list_jobs":
            return self.service.list_jobs(_integer_param(params, "limit", default=100, minimum=1))
        if action == "delete_job_history":
            return self._delete_job_history(_required_string(params, "job_id"))
        if action == "clear_project_job_history":
            raw_kinds = params.get("kinds", ["ocr", "translate"])
            if not isinstance(raw_kinds, list) or not all(
                isinstance(value, str) for value in raw_kinds
            ):
                raise ValueError("kinds must be a list of strings")
            return self._clear_project_job_history(
                _required_string(params, "project_id"), tuple(raw_kinds)
            )
        if action == "list_translation_caches":
            return self.service.list_translation_caches(_required_string(params, "project_id"))
        if action == "multi_list":
            return self.service.list_multi_videos()
        if action == "multi_add":
            raw_ids = params.get("project_ids", [])
            if not isinstance(raw_ids, list) or not all(
                isinstance(value, str) for value in raw_ids
            ):
                raise ValueError("project_ids must be an array of strings")
            return self.service.add_multi_projects(raw_ids)
        if action == "multi_remove":
            return self.multi.remove_project(_required_string(params, "project_id"))
        if action == "multi_restore_hidden":
            return self.service.restore_multi_projects()
        if action == "multi_reorder":
            raw_ids = params.get("project_ids", [])
            if not isinstance(raw_ids, list) or not all(
                isinstance(value, str) for value in raw_ids
            ):
                raise ValueError("project_ids must be an array of strings")
            return self.service.reorder_multi_projects(raw_ids)
        if action == "multi_select":
            selected = params.get("selected")
            if type(selected) is not bool:
                raise ValueError("selected must be a boolean")
            return self.service.select_multi_project(
                _required_string(params, "project_id"), selected
            )
        if action == "multi_select_all":
            selected = params.get("selected")
            if type(selected) is not bool:
                raise ValueError("selected must be a boolean")
            return self.service.select_all_multi_projects(selected)
        if action == "multi_start":
            settings = params.get("settings", {})
            if not isinstance(settings, dict):
                raise ValueError("settings must be an object")
            return self.multi.start(settings)
        if action == "multi_pause":
            return self.multi.pause()
        if action == "multi_resume":
            return self.multi.resume()
        if action == "multi_status":
            result = self.service.list_multi_video_status()
            return {**result, "partial": True}
        if action == "multi_preview_descriptor":
            return self.service.get_multi_preview_descriptor(
                _required_string(params, "project_id")
            )
        if action == "list_projects":
            return self.service.list_projects(
                _integer_param(params, "limit", default=100, minimum=1)
            )
        if action == "create_project":
            return self.service.create_project(
                project_path=_required_string(params, "project_path"),
                name=_required_string(params, "name"),
            )
        if action == "delete_project":
            return self.service.delete_project(_required_string(params, "project_id"))
        if action == "inspect_legacy_project":
            return self.service.inspect_legacy_project(
                source_path=_required_string(params, "source_path")
            )
        if action == "import_legacy_project":
            name = params.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValueError("name must be a non-empty string")
            return self.service.import_legacy_project(
                source_path=_required_string(params, "source_path"),
                project_path=_required_string(params, "project_path"),
                name=name,
            )
        if action == "runtime_diagnostics":
            data_root = params.get("data_root")
            if data_root is not None and (not isinstance(data_root, str) or not data_root.strip()):
                raise ValueError("data_root must be a non-empty string")
            return self.service.runtime_diagnostics(data_root=data_root)
        if action == "optimize_project_database":
            return self._start_database_optimization()
        if action == "get_database_optimization":
            return dict(self._database_maintenance)
        if action == "cancel_database_optimization":
            return self._cancel_database_optimization()
        if action == "export_subtitle_srt":
            return self.service.export_subtitle_srt(
                project_id=_required_string(params, "project_id"),
                language=_required_string(params, "language"),
                output_path=_required_string(params, "output_path"),
            )
        if action == "get_project":
            snapshot = self.service.get_project(_required_string(params, "project_id"))
            return _project_transport_payload(
                snapshot,
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "inspect_media_usage":
            return self.service.inspect_media_usage(
                media_path=_required_string(params, "media_path")
            )
        if action == "import_media":
            start_value = params.get("timeline_start_us")
            if start_value is not None and (type(start_value) is not int or start_value < 0):
                raise ValueError("timeline_start_us must be a non-negative integer")
            default_logo_path = params.get("default_logo_path")
            if default_logo_path is not None and not isinstance(default_logo_path, str):
                raise ValueError("default_logo_path must be a string")
            overlay_preset = params.get("overlay_preset")
            if overlay_preset is not None and not isinstance(overlay_preset, dict):
                raise ValueError("overlay_preset must be an object")
            return _project_transport_payload(
                self.service.import_media(
                    project_id=_required_string(params, "project_id"),
                    media_path=_required_string(params, "media_path"),
                    timeline_start_us=start_value,
                    default_logo_path=default_logo_path,
                    overlay_preset=overlay_preset,
                ),
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "import_translation_srt":
            return _project_transport_payload(
                self.service.import_translation_srt(
                    project_id=_required_string(params, "project_id"),
                    subtitle_path=_required_string(params, "subtitle_path"),
                ),
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "copy_video_clip":
            return self.service.copy_video_clip(_required_string(params, "project_id"),
                                                _required_string(params, "clip_id"))
        if action == "timeline_command":
            command = _required_string(params, "command")
            clip_id = params.get("clip_id")
            if clip_id is not None and (not isinstance(clip_id, str) or not clip_id.strip()):
                raise ValueError("clip_id must be a non-empty string")
            track_id = params.get("track_id")
            if track_id is not None and (not isinstance(track_id, str) or not track_id.strip()):
                raise ValueError("track_id must be a non-empty string")
            timeline_us = params.get("timeline_us")
            if timeline_us is not None and (type(timeline_us) is not int or timeline_us < 0):
                raise ValueError("timeline_us must be a non-negative integer")
            timeline_start_us = params.get("timeline_start_us")
            if timeline_start_us is not None and (
                type(timeline_start_us) is not int or timeline_start_us < 0
            ):
                raise ValueError("timeline_start_us must be a non-negative integer")
            timeline_end_us = params.get("timeline_end_us")
            if timeline_end_us is not None and (
                type(timeline_end_us) is not int or timeline_end_us <= 0
            ):
                raise ValueError("timeline_end_us must be a positive integer")
            track_ids = params.get("track_ids", [])
            if not isinstance(track_ids, list) or any(
                not isinstance(value, str) or not value.strip() for value in track_ids
            ):
                raise ValueError("track_ids must be an array of non-empty strings")
            apply_linked = params.get("apply_linked", True)
            if type(apply_linked) is not bool:
                raise ValueError("apply_linked must be a boolean")
            track_commands = {"track_mute", "track_lock", "track_volume"}
            if command in track_commands and track_id is None:
                raise ValueError(f"{command} requires track_id")
            if command == "auto_fit_voice_timing" and track_id != "D1":
                raise ValueError("auto_fit_voice_timing requires the D1 track")
            if command == "delete_tracks" and not track_ids:
                raise ValueError("delete_tracks requires track_ids")
            if command == "set_markers":
                marker_value = params.get("value")
                markers = marker_value.get("markers") if isinstance(marker_value, dict) else None
                if not isinstance(markers, list) or any(
                    type(value) is not int or value < 0 for value in markers
                ):
                    raise ValueError("set_markers requires non-negative integer markers")
            if (
                command
                not in {
                    "undo",
                    "redo",
                    *track_commands,
                    "auto_fit_voice_timing",
                    "delete_tracks",
                    "cut",
                    "split_video",
                    "paste_video",
                     "set_markers",
                }
                and clip_id is None
            ):
                raise ValueError(f"{command} requires clip_id")
            if (command in {"split", "split_video", "trim_head", "trim_tail", "paste_video"}
                    and timeline_us is None):
                raise ValueError(f"{command} requires timeline_us")
            if command == "resize" and (timeline_start_us is None or timeline_end_us is None):
                raise ValueError("resize requires timeline_start_us and timeline_end_us")
            for key in ("range_start_us", "range_end_us", "base_revision"):
                if key in params and (type(params[key]) is not int or params[key] < 0):
                    raise ValueError(f"{key} phải là số nguyên không âm")
            if params.get("request_id") is not None and (
                not isinstance(params["request_id"], str) or not params["request_id"].strip()
            ):
                raise ValueError("Mã yêu cầu cắt không hợp lệ")
            if command == "cut" and params.get("operation") not in {
                "keep_from", "keep_until", "keep_range", "remove_range",
            }:
                raise ValueError("Thao tác cắt video không hợp lệ")
            return _project_transport_payload(
                self.service.timeline_command(
                    project_id=_required_string(params, "project_id"),
                    command=command,
                    clip_id=clip_id,
                    track_id=track_id,
                    timeline_us=timeline_us,
                    timeline_start_us=timeline_start_us,
                    timeline_end_us=timeline_end_us,
                    track_ids=tuple(track_ids),
                    value=params.get("value"),
                    apply_linked=apply_linked,
                    operation=params.get("operation"),
                    range_start_us=params.get("range_start_us"),
                    range_end_us=params.get("range_end_us"),
                    base_revision=params.get("base_revision"),
                    request_id=params.get("request_id"),
                    deferred=deferred,
                ),
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "effect_command":
            values = params.get("values", {})
            if not isinstance(values, dict):
                raise ValueError("Effect values must be an object")
            overlay_id = params.get("overlay_id")
            if overlay_id is not None and (
                not isinstance(overlay_id, str) or not overlay_id.strip()
            ):
                raise ValueError("overlay_id must be a non-empty string")
            return _project_transport_payload(
                self.service.effect_command(
                    project_id=_required_string(params, "project_id"),
                    command=_required_string(params, "command"),
                    overlay_id=overlay_id,
                    values=values,
                    deferred=deferred,
                ),
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "set_translation_preferences":
            preset = _required_string(params, "preset")
            custom_rules = params.get("custom_rules", "")
            if not isinstance(custom_rules, str):
                raise ValueError("Translation custom rules must be a string")
            active_style_name = params.get("active_style_name", "")
            if not isinstance(active_style_name, str):
                raise ValueError("Translation style name must be a string")
            compact = params.get("compact", False)
            if type(compact) is not bool:
                raise ValueError("Translation compact response flag must be boolean")
            return _translation_preferences_transport_payload(
                self.service.set_translation_preferences(
                    project_id=_required_string(params, "project_id"),
                    preset=preset,
                    custom_rules=custom_rules,
                    active_style_name=active_style_name,
                ),
                compact=compact,
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "save_translation_style":
            preset = _required_string(params, "preset")
            name = _required_string(params, "name")
            custom_rules = params.get("custom_rules", "")
            if not isinstance(custom_rules, str):
                raise ValueError("Translation custom rules must be a string")
            compact = params.get("compact", False)
            if type(compact) is not bool:
                raise ValueError("Translation compact response flag must be boolean")
            return _translation_preferences_transport_payload(
                self.service.save_translation_style(
                    project_id=_required_string(params, "project_id"),
                    name=name,
                    preset=preset,
                    custom_rules=custom_rules,
                ),
                compact=compact,
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "subtitle_command":
            language = params.get("language", "source")
            if language not in {"source", "translated"}:
                raise ValueError("Subtitle language must be source or translated")
            cue_id = params.get("cue_id")
            if cue_id is not None and (not isinstance(cue_id, str) or not cue_id.strip()):
                raise ValueError("cue_id must be a non-empty string")
            values = params.get("values", {})
            if not isinstance(values, dict):
                raise ValueError("Subtitle command values must be an object")
            compact = params.get("compact", False)
            if type(compact) is not bool:
                raise ValueError("Subtitle compact response flag must be boolean")
            project_id = _required_string(params, "project_id")
            result = self.service.subtitle_command(
                    project_id=project_id,
                    command=_required_string(params, "command"),
                    cue_id=cue_id,
                    language=language,
                    params=values,
                    compact=compact,
                    deferred=deferred,
                )
            self._request_tts_replacement_after_edit(project_id)
            return _project_transport_payload(
                result,
                staging_root=self.service.checkpoints.staging_root,
            )
        if action == "start_job":
            return self._start_job(params)
        if action == "retry_job":
            return self._retry_job(
                _required_string(params, "job_id"),
                credential_references=params.get("credential_references"),
                provider_options=params.get("provider_options"),
            )
        if action == "pause_job":
            return self._pause_job(_required_string(params, "job_id"))
        if action == "resume_job":
            return self._resume_job(_required_string(params, "job_id"))
        if action == "cancel_job":
            return self._cancel_job(_required_string(params, "job_id"))
        if action == "list_events":
            kind = params.get("kind")
            if kind is not None and (not isinstance(kind, str) or not kind.strip()):
                raise ValueError("kind must be a non-empty string")
            return self.service.list_events(
                _required_string(params, "job_id"),
                _integer_param(params, "after_sequence", default=0, minimum=0),
                kind=kind,
            )
        if action == "get_translation_preview":
            return self.service.get_translation_preview(
                _required_string(params, "project_id"),
                str(params.get("job_id", "")) or None,
            )
        if action == "shutdown":
            return {"status": "stopping"}
        raise LookupError(f"Unknown Job Manager action: {action}")

    def _update_install_eligibility(self) -> dict[str, Any]:
        active_jobs = self.service.jobs.active_jobs()
        batch = self.service.multi_videos.get_batch()
        batch_state = str(batch.get("state", "idle"))
        multi_active = batch_state in {"running", "pause_requested"}
        eligible = not active_jobs and not multi_active and not self._update_install_reservation
        return {
            "eligible": eligible,
            "active_job_count": len(active_jobs),
            "active_job_ids": [job_id for job_id, _state in active_jobs],
            "multi_state": batch_state,
            "reserved": bool(self._update_install_reservation),
        }

    def _reserve_update_install(self) -> dict[str, Any]:
        eligibility = self._update_install_eligibility()
        if not eligibility["eligible"]:
            return eligibility
        token = str(uuid4())
        self._update_install_reservation = token
        return {**eligibility, "eligible": True, "reserved": True, "token": token}

    def _ensure_update_admission_open(self) -> None:
        if self._update_install_reservation:
            raise ValueError(
                "DUCAI STUDIO đang chuẩn bị cài bản cập nhật; không thể chạy tác vụ mới"
            )

    def _start_job(self, params: dict[str, Any]) -> dict[str, Any]:
        self.licensing.require_active()
        self._ensure_update_admission_open()
        job_id = _required_string(params, "job_id")
        job = self.service.jobs.get(job_id)
        if (
            job.spec.kind is JobKind.TTS
            and job.spec.provider_id == "vieneu-3.3"
        ):
            raise ValueError(
                "Tác vụ VieNeu 3.3 cũ không thể tiếp tục bằng engine 3.8.1. "
                "Hãy tạo tác vụ voice mới; profile clone và cache cũ không bị xóa."
            )
        self._ensure_no_other_active_stage(job)
        intent = control_intent("start", job.state)
        program = Path(_required_string(params, "program")).expanduser().resolve(strict=True)
        if not program.is_file():
            raise ValueError("Worker program must be a file")
        raw_arguments = params.get("arguments", [])
        raw_environment = params.get("environment")
        if not isinstance(raw_arguments, list) or not all(
            isinstance(value, str) for value in raw_arguments
        ):
            raise ValueError("Worker arguments must be a list of strings")
        if raw_environment is not None and not isinstance(raw_environment, dict):
            raise ValueError("Worker environment must be an object")
        if raw_environment is not None and not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_environment.items()
        ):
            raise ValueError("Worker environment keys and values must be strings")
        resume_stage_key = params.get("resume_stage_key", "runtime")
        resume_checkpoint_key = params.get("resume_checkpoint_key", "latest")
        if not isinstance(resume_stage_key, str) or not resume_stage_key.strip():
            raise ValueError("resume_stage_key must be a non-empty string")
        if not isinstance(resume_checkpoint_key, str) or not resume_checkpoint_key.strip():
            raise ValueError("resume_checkpoint_key must be a non-empty string")
        if job.spec.kind in {JobKind.OCR, JobKind.TTS}:
            self.service.checkpoints.ensure_worker_capacity(
                stage_label=(
                    "trích xuất SRT" if job.spec.kind is JobKind.OCR else "lồng tiếng"
                )
            )
        command_id, _record = self.service.jobs.request_control(
            job_id,
            action=intent.action,
            target=intent.target,
        )
        worker_environment = dict(raw_environment) if raw_environment is not None else {}
        worker_environment["DUCAI_ARTIFACT_STAGING_ROOT"] = str(
            self.service.checkpoints.staging_root
        )
        # Only the manager grants a session. Ignore stale ambient/IPC settings.
        for key in ("PROTOCOL", "ENDPOINT", "TOKEN"):
            worker_environment[f"DUCAI_TTS_WARM_{key}"] = ""
        if job.spec.kind is JobKind.TTS:
            # A stale system environment must never override a custom host.
            worker_environment["DUCAI_TTS_ENGINE_HOST_MANAGED"] = "0"
        host_contract = _managed_engine_host_contract(job)
        if host_contract is not None:
            filename, path_variable, hash_variable = host_contract
            configured_host = job.spec.provider_config.get("engine_host_path")
            configured_path = (
                Path(configured_host).expanduser().resolve()
                if isinstance(configured_host, str)
                else None
            )
            if _is_managed_release_engine_host(configured_path, filename):
                runtime_host = _engine_host_path(filename)
                runtime_hash = _file_sha256(runtime_host)
                worker_environment[path_variable] = str(runtime_host)
                worker_environment[hash_variable] = runtime_hash
                if job.spec.kind is JobKind.TTS:
                    worker_environment["DUCAI_TTS_ENGINE_HOST_MANAGED"] = "1"
                configured_hash = str(
                    job.spec.provider_config.get("engine_host_sha256", "")
                )
                if configured_path != runtime_host or configured_hash != runtime_hash:
                    self.service.jobs.append_event(
                        event_id=str(uuid4()),
                        job_id=job_id,
                        kind=f"{job.spec.kind.value}_engine_host_runtime_refreshed",
                        payload={
                            "configured_sha256": configured_hash,
                            "runtime_sha256": runtime_hash,
                            "checkpoint_compatible": True,
                        },
                    )
        try:
            if job.spec.kind is JobKind.OCR:
                self._warm_tts.discard_idle()
            elif (
                job.spec.kind is JobKind.TTS
                and job.spec.provider_id == "vieneu-3.8"
                and job.spec.provider_config.get("job_mode") != "voice_conform"
            ):
                config = job.spec.provider_config
                current_host = _engine_host_path("tts_engine_host.py").resolve()
                selected_host = Path(worker_environment.get("DUCAI_TTS_ENGINE_HOST_PATH")
                                     or str(config.get("engine_host_path", ""))).resolve()
                if selected_host == current_host and config.get("runtime_python"):
                    try:
                        warm_config = WarmTtsHostConfig(
                            runtime_python=Path(str(config["runtime_python"])),
                            host_script=current_host,
                            data_root=Path(str(config["data_root"])),
                            timeout_seconds=float(config.get("provider_timeout_seconds", 1800)),
                            batch_size=int(config.get("batch_size", 32)),
                        )
                        worker_environment.update(self._warm_tts.register(job_id, warm_config))
                    except (OSError, ValueError, KeyError):
                        # Warm reuse is optional; standalone keeps existing runtime
                        # validation/error messages for old or unavailable installs.
                        self._warm_tts.release(job_id, healthy=False)
            self.workers.start(
                job_id=job_id,
                program=str(program),
                arguments=raw_arguments,
                environment=worker_environment,
            )
        except Exception as exc:
            self._warm_tts.release(job_id, healthy=False)
            self.service.jobs.append_worker_event(
                event_id=str(uuid4()),
                job_id=job_id,
                kind="worker_start_failed",
                payload={"message": str(exc)},
                transitions={JobState.START_REQUESTED: JobState.FAILED},
                error_code="worker_start_failed",
                error_payload={"message": str(exc)},
            )
            raise
        checkpoint = self.service.load_worker_checkpoint(
            job_id,
            stage_key=resume_stage_key,
            checkpoint_key=resume_checkpoint_key,
        )
        start_payload: dict[str, Any] = {"job": self.service.get_job(job_id)["spec"]}
        if checkpoint is not None:
            if self.service.jobs.get(job_id).spec.kind.value == "translate":
                checkpoint = dict(checkpoint)
                checkpoint["translation_qa"] = self.service.get_translation_qa(job_id)
            start_payload["checkpoint"] = checkpoint
        self._deliver_control(
            job_id,
            command_id,
            "start",
            JobState.START_REQUESTED,
            extra_payload=start_payload,
        )
        return self.service.get_job(job_id)

    def _retry_job(
        self,
        job_id: str,
        *,
        credential_references: object = None,
        provider_options: object = None,
    ) -> dict[str, Any]:
        self.licensing.require_active()
        self._ensure_update_admission_open()
        job = self.service.jobs.get(job_id)
        retry_state = retry_target(job.state)
        if (
            job.spec.kind is JobKind.TTS
            and job.spec.provider_id == "vieneu-3.3"
        ):
            raise ValueError(
                "Tác vụ VieNeu 3.3 cũ không thể tiếp tục bằng engine 3.8.1. "
                "Hãy tạo tác vụ voice mới; trạng thái và cache cũ vẫn được giữ nguyên."
            )
        if job.spec.kind is JobKind.TTS:
            replacement = self.service.rebase_tts_retry(job_id)
            if replacement is not None:
                return replacement
        self._ensure_no_other_active_stage(job)
        if job.state is JobState.CANCELLED and job.spec.kind.value != "translate":
            raise ValueError("Chỉ tác vụ dịch có checkpoint mới được tiếp tục sau khi hủy")
        if (
            job.spec.kind.value == "translate"
            and job.spec.provider_id in TRANSLATION_API_PROVIDER_IDS
        ):
            if provider_options is not None:
                raise ValueError("API translation retry keeps the original provider options")
            if credential_references is None:
                configured = job.spec.provider_config.get("credential_references", ())
                credential_references = (
                    list(configured) if isinstance(configured, (list, tuple)) else []
                )
            self.service.refresh_translation_credential_references(
                job_id=job_id,
                credential_references=credential_references,
            )
        elif job.spec.kind.value == "translate" and job.spec.provider_id == "gemini-web":
            if credential_references is not None:
                raise ValueError("Only API translation jobs accept credential references")
            if provider_options is not None:
                self.service.refresh_translation_web_options(
                    job_id=job_id,
                    provider_options=provider_options,
                )
        elif credential_references is not None or provider_options is not None:
            raise ValueError(
                "Only API translation jobs or Gemini Web accept translation retry options"
            )
        return self.service.transition_job(job_id, retry_state.value)

    def _pause_job(self, job_id: str) -> dict[str, Any]:
        job = self.service.jobs.get(job_id)
        intent = control_intent("pause", job.state)
        if not self.workers.has_worker(job_id):
            raise LookupError(f"Unknown worker: {job_id}")
        command_id, _record = self.service.jobs.request_control(
            job_id,
            action=intent.action,
            target=intent.target,
        )
        self._deliver_control(job_id, command_id, "pause", JobState.PAUSE_REQUESTED)
        return self.service.get_job(job_id)

    def _resume_job(self, job_id: str) -> dict[str, Any]:
        self.licensing.require_active()
        self._ensure_update_admission_open()
        job = self.service.jobs.get(job_id)
        self._ensure_no_other_active_stage(job)
        intent = control_intent("resume", job.state)
        if not self.workers.has_worker(job_id):
            raise LookupError(f"Unknown worker: {job_id}")
        command_id, _record = self.service.jobs.request_control(
            job_id,
            action=intent.action,
            target=intent.target,
        )
        self._deliver_control(job_id, command_id, "resume", JobState.RESUME_REQUESTED)
        return self.service.get_job(job_id)

    def _ensure_no_other_active_stage(self, job: Any) -> None:
        if job.spec.kind not in {JobKind.OCR, JobKind.TRANSLATE, JobKind.TTS}:
            return
        other_job_id = self.service.jobs.active_id(
            project_id=job.spec.project_id,
            kind=job.spec.kind,
            exclude_job_id=job.id,
        )
        if other_job_id:
            label = {
                JobKind.OCR: "trích xuất SRT",
                JobKind.TRANSLATE: "dịch",
                JobKind.TTS: "lồng tiếng",
            }[job.spec.kind]
            raise ValueError(
                f"Dự án đã có một tác vụ {label} khác đang hoạt động; hãy tạm dừng, "
                "hủy hoặc xử lý tác vụ đó trước."
            )

    def _cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.service.jobs.get(job_id)
        if self.workers.has_worker(job_id):
            intent = control_intent("cancel", job.state)
            command_id, _record = self.service.jobs.request_control(
                job_id,
                action=intent.action,
                target=intent.target,
            )
            self._deliver_control(
                job_id,
                command_id,
                "cancel",
                JobState.CANCEL_REQUESTED,
                request_stop=True,
            )
            return self.service.get_job(job_id)

        if job.state in {
            JobState.RUNNING,
            JobState.START_REQUESTED,
            JobState.PAUSE_REQUESTED,
            JobState.RESUME_REQUESTED,
        }:
            _sequence, job = self.service.jobs.append_worker_event(
                event_id=str(uuid4()),
                job_id=job_id,
                kind="manager_reconciled_missing_worker",
                payload={"from": job.state.value},
                transitions={job.state: JobState.INTERRUPTED},
            )
        intent = control_intent("cancel", job.state)
        self.service.transition_job(job_id, intent.target.value)
        return self.service.transition_job(job_id, JobState.CANCELLED.value)

    def _request_tts_replacement_after_edit(self, project_id: str) -> None:
        """Stop one stale production worker and defer one rebased replacement.

        Replacement creation is intentionally delayed until the old worker has
        acknowledged its checkpoint-safe stop. More edits during that window
        only update the project head, so they are coalesced into the same new
        job instead of spawning parallel TTS workers.
        """

        quiet_deadline = monotonic() + (_TTS_REPLACEMENT_QUIET_MS / 1_000)
        for pending_id, pending_project_id in tuple(
            self._pending_tts_replacement_projects.items()
        ):
            if pending_project_id == project_id:
                self._pending_tts_replacement_deadlines[pending_id] = quiet_deadline

        job_id = self.service.jobs.active_id(project_id=project_id, kind=JobKind.TTS)
        if not job_id or job_id in self._pending_tts_replacements:
            return
        job = self.service.jobs.get(job_id)
        if not self.workers.has_worker(job_id):
            return
        if not self.service.tts_job_needs_rebase(job_id):
            return
        self._pending_tts_replacements.add(job_id)
        self._pending_tts_replacement_projects[job_id] = project_id
        self._pending_tts_replacement_deadlines[job_id] = quiet_deadline
        self.service.jobs.append_event(
            event_id=str(uuid4()),
            job_id=job_id,
            kind="replacement_requested",
            payload={
                "reason": "dialogue_input_changed",
                "latest_revision": self.service.projects.head_revision(project_id),
            },
        )
        if job.state is not JobState.CANCEL_REQUESTED:
            self._cancel_job(job_id)

    def _start_pending_tts_replacement(self, replaced_job_id: str) -> None:
        if replaced_job_id not in self._pending_tts_replacements:
            return
        remaining_ms = round(
            max(
                0.0,
                self._pending_tts_replacement_deadlines.get(replaced_job_id, 0.0)
                - monotonic(),
            )
            * 1_000
        )
        if remaining_ms > 0:
            QTimer.singleShot(
                max(1, remaining_ms),
                lambda: self._start_pending_tts_replacement(replaced_job_id),
            )
            return
        self._pending_tts_replacements.discard(replaced_job_id)
        self._pending_tts_replacement_projects.pop(replaced_job_id, None)
        self._pending_tts_replacement_deadlines.pop(replaced_job_id, None)
        if self._shutting_down:
            return
        try:
            replacement = self.service.rebase_tts_retry(replaced_job_id)
            if replacement is None:
                return
            replacement_id = str(replacement.get("id", ""))
            if not replacement_id:
                raise RuntimeError("Tác vụ voice thay thế không có mã job")
            self._start_job(
                {
                    "job_id": replacement_id,
                    "program": str(runtime_program_path()),
                    "arguments": [
                        "-m",
                        "ducai_studio.workers.tts_worker",
                        replacement_id,
                    ],
                    "resume_stage_key": "tts",
                    "resume_checkpoint_key": "latest",
                }
            )
        except Exception as exc:
            self.service.jobs.append_event(
                event_id=str(uuid4()),
                job_id=replaced_job_id,
                kind="replacement_start_failed",
                payload={"message": str(exc)[:300]},
            )

    def _start_database_optimization(self) -> dict[str, Any]:
        thread = self._database_optimizer_thread
        if thread is not None and thread.isRunning():
            return dict(self._database_maintenance)
        self.service.ensure_database_optimization_allowed()
        thread = _DatabaseOptimizerThread(self.service, self)
        thread.progressReported.connect(self._database_optimization_progress)
        thread.completed.connect(self._database_optimization_completed)
        thread.failed.connect(self._database_optimization_failed)
        thread.finished.connect(self._database_optimization_thread_finished)
        self._database_optimizer_thread = thread
        self._database_maintenance = {"state": "running", "stage": "starting"}
        thread.start()
        return dict(self._database_maintenance)

    def _cancel_database_optimization(self) -> dict[str, Any]:
        thread = self._database_optimizer_thread
        if thread is None or not thread.isRunning():
            return dict(self._database_maintenance)
        thread.cancel()
        self._database_maintenance = {
            **self._database_maintenance,
            "state": "cancelling",
        }
        return dict(self._database_maintenance)

    @Slot(object)
    def _database_optimization_progress(self, progress: object) -> None:
        if not isinstance(progress, dict):
            return
        state = str(self._database_maintenance.get("state", "running"))
        self._database_maintenance = {
            **self._database_maintenance,
            **progress,
            "state": "cancelling" if state == "cancelling" else "running",
        }

    @Slot(object)
    def _database_optimization_completed(self, result: object) -> None:
        if isinstance(result, dict):
            self._database_maintenance = dict(result)
        else:
            self._database_maintenance = {"state": "completed"}

    @Slot(str)
    def _database_optimization_failed(self, message: str) -> None:
        self._database_maintenance = {
            "state": "failed",
            "message": message[:500],
        }

    @Slot()
    def _database_optimization_thread_finished(self) -> None:
        thread = self._database_optimizer_thread
        if thread is None:
            return
        self._database_optimizer_thread = None
        thread.deleteLater()

    def _delete_job_history(self, job_id: str) -> dict[str, Any]:
        job = self.service.jobs.get(job_id)
        if job.state in TERMINAL_JOB_STATES:
            result = self.service.delete_job_history(job_id)
            return {**result, "pending": False}
        if self.workers.has_worker(job_id):
            self._pending_history_deletions.add(job_id)
            try:
                self._cancel_job(job_id)
            except Exception:
                self._pending_history_deletions.discard(job_id)
                raise
            return {
                "job_id": job_id,
                "project_id": job.spec.project_id,
                "kind": job.spec.kind.value,
                "pending": True,
            }
        self._cancel_job(job_id)
        result = self.service.delete_job_history(job_id)
        return {**result, "pending": False}

    def _clear_project_job_history(
        self,
        project_id: str,
        kinds: tuple[str, ...],
    ) -> dict[str, Any]:
        job_ids = self.service.list_project_job_history_ids(project_id, kinds)
        deleted: list[str] = []
        pending: list[str] = []
        for job_id in job_ids:
            result = self._delete_job_history(job_id)
            (pending if result.get("pending") is True else deleted).append(job_id)
        return {
            "project_id": project_id,
            "kinds": list(kinds),
            "deleted_jobs": len(deleted),
            "pending_jobs": len(pending),
            "job_ids": deleted,
            "pending_job_ids": pending,
        }

    @Slot(str, object)
    def _worker_event(self, job_id: str, message: WorkerMessage) -> None:
        try:
            self._persist_worker_event(job_id, message)
            managed_by_multi = self.multi.on_worker_event(
                job_id, message.kind, dict(message.payload)
            )
            if managed_by_multi and message.kind == "progress":
                return
            # Manual OCR/translation/TTS/export jobs also drive catalog card
            # status, but only when this project is actually in Multi. Avoid
            # emitting unsolicited IPC events for unrelated standalone jobs.
            try:
                project_id = self.service.jobs.project_id_for(job_id)
                is_member = any(
                    str(item.get("project_id", "")) == project_id
                    for item in self.service.multi_videos.list_items()
                )
                if is_member and message.kind == "succeeded":
                    self._broadcast_multi_catalog()
                elif is_member:
                    self.multi._publish_status()
            except LookupError:
                pass
        except Exception as exc:
            detail = str(exc).strip()
            if detail.startswith("Không thể nhập D1"):
                user_message = detail
            else:
                user_message = "Không thể xử lý phản hồi của tác vụ" + (
                    f": {detail}" if detail else ". Hãy thử tiếp tục từ cache."
                )
            self._worker_failed(job_id, user_message)

    def _persist_worker_event(self, job_id: str, message: WorkerMessage) -> None:
        if message.kind == "progress":
            self.service.jobs.append_progress_event(
                event_id=message.event_id,
                job_id=job_id,
                payload=dict(message.payload),
            )
            return
        if self.service.jobs.event_exists(message.event_id, job_id=job_id):
            return
        if message.kind == "checkpoint":
            self.service.commit_worker_checkpoint(
                job_id,
                dict(message.payload),
                event_id=message.event_id,
            )
            return

        current_job = self.service.jobs.get(job_id)
        if message.kind == "succeeded" and current_job.state is not JobState.RUNNING:
            # A cancel/pause request can win after the worker's last progress
            # callback. Persist the late result for diagnosis, but never publish
            # project media or artifacts from a job that no longer owns RUNNING.
            self.service.jobs.append_worker_event(
                event_id=message.event_id,
                job_id=job_id,
                kind="late_success_ignored",
                payload={
                    "worker_kind": message.kind,
                    "job_state": current_job.state.value,
                    "worker_payload": dict(message.payload),
                },
            )
            return

        if message.kind == "failed" and message.payload.get("code") == "ocr_review_required":
            checkpoint = self.service.load_worker_checkpoint(
                job_id, stage_key="ocr", checkpoint_key="review"
            )
            qa_checkpoint = self.service.load_worker_checkpoint(
                job_id, stage_key="ocr", checkpoint_key="review-qa"
            )
            self.service.import_review_ocr_srt(job_id, checkpoint, qa_checkpoint)

        if (
            message.kind == "failed"
            and message.payload.get("code") == "translation_review_required"
        ):
            checkpoint = self.service.load_worker_checkpoint(
                job_id, stage_key="translation", checkpoint_key="review"
            )
            self.service.import_review_translation_srt(job_id, checkpoint)

        if message.kind == "succeeded" and self.service.jobs.get(job_id).spec.kind.value == "ocr":
            status = message.payload.get("status")
            if status != "PASS":
                raise ValueError("OCR success must carry a PASS status")
            job = self.service.jobs.get(job_id)
            if isinstance(job.spec.provider_config.get("rescan_cue_id"), str):
                self.service.apply_ocr_rescan(job_id, message.payload.get("rescan"))
            else:
                output_path = message.payload.get("output_path")
                if not isinstance(output_path, str):
                    raise ValueError("OCR success must carry a guarded PASS output")
                self.service.import_final_ocr_srt(job_id, output_path)

        if (
            message.kind == "succeeded"
            and self.service.jobs.get(job_id).spec.kind.value == "translate"
        ):
            if message.payload.get("status") != "PASS":
                raise ValueError("Translation success must carry a PASS status")
            output_path = message.payload.get("output_path")
            if not isinstance(output_path, str):
                raise ValueError("Translation success must carry a guarded PASS output")
            self.service.import_final_translation_srt(job_id, output_path)

        if message.kind == "succeeded" and self.service.jobs.get(job_id).spec.kind.value == "tts":
            if message.payload.get("status") != "PASS":
                raise ValueError("TTS success must carry a PASS status")
            output_path = message.payload.get("output_path")
            if not isinstance(output_path, str):
                raise ValueError("TTS success must carry a guarded PASS output")
            job = self.service.jobs.get(job_id)
            if job.spec.provider_config.get("job_mode") == "voice_conform":
                self.service.import_final_voice_conform(job_id, output_path)
            elif job.spec.provider_config.get("purpose") == "production":
                self.service.import_final_voice_wav(job_id, output_path)

        if (
            message.kind == "succeeded"
            and self.service.jobs.get(job_id).spec.kind.value == "export"
        ):
            if message.payload.get("status") != "PASS":
                raise ValueError("Export success must carry a PASS status")
            output_path = message.payload.get("output_path")
            if not isinstance(output_path, str):
                raise ValueError("Export success must carry a finalized output path")
            self.service.validate_final_export(job_id, output_path)

        transitions: dict[JobState, JobState] = {}
        error_code: str | None = None
        error_payload: dict[str, Any] | None = None
        command_id: str | None = None
        expected_action: str | None = None
        if message.kind == "paused":
            transitions = {JobState.PAUSE_REQUESTED: JobState.PAUSED}
            command_id = self._required_command_id(message)
            expected_action = "pause"
        elif message.kind == "resumed":
            transitions = {JobState.RESUME_REQUESTED: JobState.RUNNING}
            command_id = self._required_command_id(message)
            expected_action = "resume"
        elif message.kind == "started":
            transitions = {JobState.START_REQUESTED: JobState.RUNNING}
            command_id = self._required_command_id(message)
            expected_action = "start"
        elif message.kind == "succeeded":
            transitions = {JobState.RUNNING: JobState.SUCCEEDED}
        elif message.kind == "failed":
            transitions = {
                state: JobState.FAILED
                for state in (
                    JobState.START_REQUESTED,
                    JobState.RUNNING,
                    JobState.PAUSE_REQUESTED,
                    JobState.PAUSED,
                    JobState.RESUME_REQUESTED,
                    JobState.CANCEL_REQUESTED,
                )
            }
            raw_error_code = message.payload.get("code", "worker_failed")
            if not isinstance(raw_error_code, str) or not raw_error_code.strip():
                raise ValueError("Worker failure code must be a non-empty string")
            assert_safe_error_code(raw_error_code)
            error_code = raw_error_code
            error_payload = dict(message.payload)
        elif message.kind == "stopped":
            state = self.service.jobs.get(job_id).state
            if state is JobState.CANCEL_REQUESTED:
                command_id = self._required_command_id(message)
                expected_action = "cancel"
            transitions = {
                JobState.CANCEL_REQUESTED: JobState.CANCELLED,
                JobState.START_REQUESTED: JobState.INTERRUPTED,
                JobState.RUNNING: JobState.INTERRUPTED,
                JobState.PAUSE_REQUESTED: JobState.INTERRUPTED,
                JobState.PAUSED: JobState.INTERRUPTED,
                JobState.RESUME_REQUESTED: JobState.INTERRUPTED,
            }
        self.service.jobs.append_worker_event(
            event_id=message.event_id,
            job_id=job_id,
            kind=message.kind,
            payload=dict(message.payload),
            transitions=transitions,
            error_code=error_code,
            error_payload=error_payload,
            acknowledges_command_id=command_id,
            expected_control_action=expected_action,
        )
        if command_id is not None:
            self._clear_pending_control(command_id)

    def _deliver_control(
        self,
        job_id: str,
        command_id: str,
        action: str,
        requested_state: JobState,
        *,
        request_stop: bool = False,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        self._control_retry_attempts[command_id] = 1
        self._control_retry_started_at[command_id] = monotonic()
        self._slow_start_controls_reported.discard(command_id)
        try:
            if request_stop:
                self.workers.request_stop(
                    job_id,
                    action=action,
                    command_id=command_id,
                )
            else:
                payload = {"action": action, "command_id": command_id}
                payload.update(extra_payload or {})
                self.workers.send(
                    job_id,
                    kind="control",
                    payload=payload,
                )
        except Exception as exc:
            self.service.jobs.append_event(
                event_id=str(uuid4()),
                job_id=job_id,
                kind="control_dispatch_failed",
                payload={"command_id": command_id, "action": action, "message": str(exc)},
            )
        QTimer.singleShot(
            (
                _START_CONTROL_FIRST_RETRY_MS
                if action == "start"
                else _CONTROL_RETRY_INTERVAL_MS
            ),
            lambda: self._retry_control(job_id, command_id, action, requested_state, extra_payload),
        )

    def _retry_control(
        self,
        job_id: str,
        command_id: str,
        action: str,
        requested_state: JobState,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        attempts = self._control_retry_attempts.get(command_id)
        if attempts is None:
            return
        try:
            job = self.service.jobs.get(job_id)
        except LookupError:
            self._clear_pending_control(command_id)
            return
        if job.state is not requested_state:
            self._clear_pending_control(command_id)
            return
        if not self.workers.has_worker(job_id):
            self.service.jobs.append_worker_event(
                event_id=str(uuid4()),
                job_id=job_id,
                kind="manager_reconciled_missing_worker",
                payload={"from": requested_state.value, "command_id": command_id},
                transitions={requested_state: JobState.INTERRUPTED},
            )
            self._clear_pending_control(command_id)
            return
        if action == "start":
            started_at = self._control_retry_started_at.get(command_id, monotonic())
            elapsed_seconds = max(0.0, monotonic() - started_at)
            if (
                elapsed_seconds >= _START_CONTROL_SLOW_AFTER_SECONDS
                and command_id not in self._slow_start_controls_reported
            ):
                self._slow_start_controls_reported.add(command_id)
                self.service.jobs.append_event(
                    event_id=str(uuid4()),
                    job_id=job_id,
                    kind="worker_start_ack_delayed",
                    payload={
                        "command_id": command_id,
                        "action": action,
                        "elapsed_ms": round(elapsed_seconds * 1_000),
                        "message": (
                            "Worker vẫn đang chạy và cần thêm thời gian nạp bộ máy; "
                            "Job Manager tiếp tục chờ thay vì tự dừng tác vụ."
                        ),
                    },
                )
        elif attempts >= 3 and action not in {"pause", "resume"}:
            self.workers.request_stop(job_id)
            self._clear_pending_control(command_id)
            return
        try:
            payload = {"action": action, "command_id": command_id}
            payload.update(extra_payload or {})
            self.workers.send(
                job_id,
                kind="control",
                payload=payload,
            )
        except Exception as exc:
            self.service.jobs.append_event(
                event_id=str(uuid4()),
                job_id=job_id,
                kind="control_dispatch_failed",
                payload={
                    "command_id": command_id,
                    "action": action,
                    "attempt": attempts + 1,
                    "message": str(exc),
                },
            )
        self._control_retry_attempts[command_id] = attempts + 1
        if action == "start":
            elapsed_seconds = max(
                0.0,
                monotonic() - self._control_retry_started_at.get(command_id, monotonic()),
            )
            retry_delay_ms = (
                _START_CONTROL_SLOW_RETRY_INTERVAL_MS
                if elapsed_seconds >= _START_CONTROL_SLOW_AFTER_SECONDS
                else _START_CONTROL_RETRY_INTERVAL_MS
            )
        else:
            retry_delay_ms = 750 if attempts < 3 else 2_500
        QTimer.singleShot(
            retry_delay_ms,
            lambda: self._retry_control(job_id, command_id, action, requested_state, extra_payload),
        )

    def _clear_pending_control(self, command_id: str) -> None:
        self._control_retry_attempts.pop(command_id, None)
        self._control_retry_started_at.pop(command_id, None)
        self._slow_start_controls_reported.discard(command_id)

    @staticmethod
    def _required_command_id(message: WorkerMessage) -> str:
        command_id = message.payload.get("command_id")
        if not isinstance(command_id, str) or not command_id.strip():
            raise RuntimeError("Worker control acknowledgement is missing command_id")
        return command_id

    @Slot(str, str)
    def _worker_log(self, job_id: str, message: str) -> None:
        self.service.jobs.append_event(
            event_id=str(uuid4()),
            job_id=job_id,
            kind="worker_stderr",
            payload={
                "message": "Worker wrote to stderr; raw text was not persisted.",
                "characters": len(message),
                "lines": message.count("\n") + 1,
            },
        )

    @Slot(str, str)
    def _worker_failed(self, job_id: str, error: str) -> None:
        self.service.jobs.append_worker_event(
            event_id=str(uuid4()),
            job_id=job_id,
            kind="worker_error",
            payload={"message": error},
            transitions={
                state: JobState.FAILED
                for state in (
                    JobState.RUNNING,
                    JobState.START_REQUESTED,
                    JobState.PAUSE_REQUESTED,
                    JobState.PAUSED,
                    JobState.RESUME_REQUESTED,
                    JobState.CANCEL_REQUESTED,
                )
            },
            error_code="worker_error",
            error_payload={"message": error},
        )
        if self.workers.has_worker(job_id):
            self.workers.request_stop(job_id)

    @Slot(str, int)
    def _worker_finished(self, job_id: str, exit_code: int) -> None:
        state = self.service.jobs.get(job_id).state
        self._warm_tts.release(job_id, healthy=(state is JobState.SUCCEEDED and exit_code == 0))
        active_states = {
            JobState.RUNNING,
            JobState.START_REQUESTED,
            JobState.PAUSE_REQUESTED,
            JobState.PAUSED,
            JobState.RESUME_REQUESTED,
            JobState.CANCEL_REQUESTED,
        }
        if state in active_states:
            cancelled = state is JobState.CANCEL_REQUESTED
            target = (
                JobState.CANCELLED
                if cancelled
                else JobState.INTERRUPTED
                if self._shutting_down
                else JobState.FAILED
            )
            self.service.jobs.append_worker_event(
                event_id=str(uuid4()),
                job_id=job_id,
                kind="worker_exited",
                payload={"exit_code": exit_code},
                transitions={current: target for current in active_states},
                error_code=(
                    None if cancelled or self._shutting_down else "worker_exit_without_ack"
                ),
                error_payload=(
                    {} if cancelled or self._shutting_down else {"exit_code": exit_code}
                ),
            )
        if job_id in self._pending_history_deletions:
            self._pending_history_deletions.discard(job_id)
            try:
                self.service.delete_job_history(job_id)
            except Exception as exc:
                self.service.jobs.append_event(
                    event_id=str(uuid4()),
                    job_id=job_id,
                    kind="history_delete_failed",
                    payload={"message": str(exc)[:300]},
                )
        if self._shutting_down:
            self._poll_shutdown()
        self.multi.on_worker_finished(job_id)
        self._start_pending_tts_replacement(job_id)

    def _begin_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._shutdown_deadline = monotonic() + 10
        for thread in self._diagnostic_threads:
            thread.requestInterruption()
        for thread in self._catalog_threads:
            thread.requestInterruption()
        self.server.close()
        optimizer = self._database_optimizer_thread
        if optimizer is not None and optimizer.isRunning():
            optimizer.cancel()
        self.workers.shutdown()
        self._poll_shutdown()

    def _poll_shutdown(self) -> None:
        optimizer = self._database_optimizer_thread
        optimizer_running = optimizer is not None and optimizer.isRunning()
        catalog_running = any(thread.isRunning() for thread in self._catalog_threads)
        diagnostic_running = any(thread.isRunning() for thread in self._diagnostic_threads)
        if (not self.workers.active_job_ids() and not optimizer_running
                and not catalog_running and not diagnostic_running):
            self._quit_application()
            return
        if monotonic() >= self._shutdown_deadline:
            self.workers.force_shutdown()
            if optimizer_running:
                optimizer.cancel()
                optimizer.wait(1_000)
            self._quit_application()
            return
        QTimer.singleShot(50, self._poll_shutdown)

    def _send_error(self, socket: QLocalSocket, *, request_id: str, exc: Exception) -> None:
        response = IpcMessage(
            protocol_version=IPC_PROTOCOL_VERSION,
            message_id=str(uuid4()),
            kind="response",
            payload={
                "request_id": request_id,
                "ok": False,
                "error": {"code": type(exc).__name__, "message": str(exc)},
            },
        )
        socket.write(encode_frame(response))
        socket.flush()

    @staticmethod
    def _quit_application() -> None:
        from PySide6.QtCore import QCoreApplication

        application = QCoreApplication.instance()
        if application is not None:
            application.quit()
