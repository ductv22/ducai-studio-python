from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLockFile, QStandardPaths, QTimer, QUrl
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication
from shiboken6 import delete as delete_qt_object

from ducai_studio import __version__
from ducai_studio.providers.font_loading import load_application_font
from ducai_studio.ui.multi_thumbnail_provider import MultiThumbnailProvider
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def _ui_resource_root(
    *,
    module_path: Path | None = None,
    executable_path: Path | None = None,
) -> Path:
    source_root = (module_path or Path(__file__)).resolve().parent
    if (source_root / "qml" / "Main.qml").is_file():
        return source_root
    frozen_root = (executable_path or Path(sys.executable)).resolve().parent / "ui"
    if (frozen_root / "qml" / "Main.qml").is_file():
        return frozen_root
    raise FileNotFoundError("Không tìm thấy tài nguyên QML của DUCAI STUDIO")


def _qml_root() -> Path:
    return _ui_resource_root() / "qml"


def _application_data_root(location: str | None = None) -> Path:
    raw_location = (
        location
        or os.environ.get("DUCAI_APP_DATA_ROOT")
        or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    # QLockFile cannot reliably acquire a lock through a Windows junction.
    # Resolve redirected application data roots before creating the lock.
    return Path(raw_location).expanduser().resolve()


def _register_bundled_fonts() -> None:
    font_root = _ui_resource_root() / "assets" / "fonts"
    font_paths = sorted((*font_root.glob("*.ttf"), *font_root.glob("*.otf")))
    for font_path in font_paths:
        if load_application_font(font_path) < 0:
            raise RuntimeError(f"Cannot register bundled font: {font_path.name}")


def _seed_signed_update_smoke_candidate(app_model: ApplicationViewModel) -> None:
    """Let isolated frozen-GUI tests exercise the real install-button flow.

    This only supplies a genuinely signed, already extracted candidate. The
    normal reservation, discovery, verification, shutdown and helper still run.
    No installation is initiated here, and normal launches have no candidate.
    """

    raw_candidate = os.environ.get("DUCAI_SMOKE_UPDATE_CANDIDATE", "").strip()
    if not raw_candidate:
        return
    if not os.environ.get("DUCAI_APP_DATA_ROOT") or not os.environ.get("DUCAI_SMOKE_EXIT_MS"):
        raise ValueError("Signed update smoke requires isolated app data and a bounded exit")
    from ducai_studio.infrastructure.github_updates import (
        OWNER,
        REPOSITORY,
        GitHubRelease,
        ReleaseAsset,
        VerifiedRelease,
    )
    from ducai_studio.infrastructure.update_manifest import verify_manifest

    candidate = Path(raw_candidate).expanduser().resolve(strict=True)
    manifest_path = candidate / "update-manifest.json"
    signature_path = candidate / "update-manifest.sig"
    manifest_bytes = manifest_path.read_bytes()
    signature_bytes = signature_path.read_bytes()
    manifest = verify_manifest(manifest_bytes, signature_bytes)
    if not (candidate / "payload").is_dir():
        raise ValueError("Signed update smoke requires an extracted payload")
    tag = f"v{manifest.version}"
    base = f"https://github.com/{OWNER}/{REPOSITORY}/releases/download/{tag}"
    release = GitHubRelease(
        manifest.version, tag, manifest.channel == "testing", "",
        ReleaseAsset(manifest.archive.name, f"{base}/{manifest.archive.name}",
                     manifest.archive.size, manifest.archive.sha256),
        ReleaseAsset(manifest_path.name, f"{base}/{manifest_path.name}", len(manifest_bytes)),
        ReleaseAsset(signature_path.name, f"{base}/{signature_path.name}", len(signature_bytes)),
    )
    updater = app_model.updater_model
    updater._set_state("checking", "Signed update smoke")
    updater._checked(VerifiedRelease(release, manifest, manifest_bytes, signature_bytes), "")
    updater._set_state("downloading", "Signed update smoke")
    updater._downloaded((candidate / "payload", manifest_path, signature_path), "")


def run(argv: list[str] | None = None) -> int:
    """Run the QML desktop shell and own the backend lifecycle."""
    configure_qt_plugin_dll_search()
    os.environ.setdefault(
        "QT_LOGGING_RULES",
        "qt.multimedia.ffmpeg=false;qt.multimedia.ffmpeg.*=false",
    )
    QCoreApplication.setOrganizationName("DUCAI STUDIO")
    QCoreApplication.setOrganizationDomain("ducai.studio")
    QCoreApplication.setApplicationName("DUCAI STUDIO 2")
    QGuiApplication.setDesktopFileName("ducai-studio")
    QQuickStyle.setStyle("Basic")

    app = QApplication(argv if argv is not None else sys.argv)
    data_root = _application_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(data_root / "application.lock"))
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(0) and (
        not instance_lock.removeStaleLockFile() or not instance_lock.tryLock(0)
    ):
        return 3
    _register_bundled_fonts()
    QFont.insertSubstitution("Be Vietnam Pro", "Noto Sans CJK SC")
    qml_root = _qml_root()

    app_model = ApplicationViewModel(app)
    # Keep the QML engine below the view model in the QObject tree. During
    # application teardown the engine is then destroyed while appModel is
    # still valid, preventing every bound screen from briefly observing null.
    engine = QQmlApplicationEngine(app_model)
    engine.addImportPath(str(qml_root))
    thumbnail_provider = MultiThumbnailProvider(data_root / "preview_tiles" / "multi")
    engine.addImageProvider("multi-thumbnail", thumbnail_provider)
    from ducai_studio.ui.timeline_media_provider import TimelineMediaProvider
    engine.addImageProvider(
        "timeline-media", TimelineMediaProvider(data_root / "preview_tiles" / "timeline")
    )
    from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.setInitialProperties({"appModel": app_model})
    engine.load(QUrl.fromLocalFile(str(qml_root / "Main.qml")))
    if not engine.rootObjects():
        app_model.forceShutdown()
        return 1

    root_window = engine.rootObjects()[0]

    health_path = os.environ.get("DUCAI_UPDATE_HEALTH_PATH", "").strip()
    health_nonce = os.environ.get("DUCAI_UPDATE_HEALTH_NONCE", "").strip()
    health_version = os.environ.get("DUCAI_UPDATE_HEALTH_VERSION", "").strip()
    update_health_written = False

    def publish_update_health() -> None:
        nonlocal update_health_written
        if update_health_written or not health_path or not app_model.backendReady:
            return
        if not health_nonce or health_version != __version__:
            return
        target = Path(health_path).expanduser()
        if not target.is_absolute() or not target.parent.is_dir():
            return
        payload = {
            "schema": "ducai-update-health-v1",
            "pid": os.getpid(),
            "nonce": health_nonce,
            "version": __version__,
            "backend_ready": True,
            "install_root": str(Path(sys.executable).resolve().parent),
        }
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(target)
            update_health_written = True
        except OSError:
            temporary.unlink(missing_ok=True)

    app_model.backendReadyChanged.connect(publish_update_health)
    smoke_size = os.environ.get("DUCAI_SMOKE_SIZE", "")
    requested_size: tuple[int, int] | None = None
    if smoke_size:
        try:
            width, height = (int(value) for value in smoke_size.lower().split("x", 1))
        except (TypeError, ValueError):
            app_model.forceShutdown()
            raise ValueError("DUCAI_SMOKE_SIZE must use WIDTHxHEIGHT") from None
        requested_size = (max(1, width), max(1, height))
        root_window.setWidth(requested_size[0])
        root_window.setHeight(requested_size[1])

    screenshot_path = os.environ.get("DUCAI_SMOKE_SCREENSHOT")
    screenshot_workspace = os.environ.get("DUCAI_SMOKE_WORKSPACE", "")
    try:
        screenshot_delay_ms = max(
            0, int(os.environ.get("DUCAI_SMOKE_SCREENSHOT_DELAY_MS", "200"))
        )
    except ValueError:
        app_model.forceShutdown()
        raise ValueError("DUCAI_SMOKE_SCREENSHOT_DELAY_MS must be an integer") from None
    if screenshot_path:

        def capture_window() -> None:
            target = Path(screenshot_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            root_window.grabWindow().save(str(target))

        def prepare_capture() -> None:
            if screenshot_workspace:
                app_model.navigate(screenshot_workspace)
            if requested_size is not None:
                root_window.setWidth(requested_size[0])
                root_window.setHeight(requested_size[1])
            QTimer.singleShot(screenshot_delay_ms, capture_window)

    navigation_cycles = int(os.environ.get("DUCAI_SMOKE_NAVIGATION_CYCLES", "0"))
    if navigation_cycles > 0:
        workspaces = (
            "dashboard",
            "projects",
            "transcribe",
            "translate",
            "subtitles",
            "voice",
            "multi",
            "timeline",
            "effects",
            "mixer",
            "export",
            "queue",
            "settings",
        )
        steps = list(workspaces) * navigation_cycles

        def drive_navigation() -> None:
            if not steps:
                if screenshot_path:
                    prepare_capture()
                return
            workspace = steps.pop(0)
            app_model.navigate(workspace)
            if requested_size is None:
                if workspace in {"subtitles", "timeline"}:
                    root_window.setWidth(1180)
                    root_window.setHeight(720)
                else:
                    root_window.setWidth(1512)
                    root_window.setHeight(900)
            QTimer.singleShot(35, drive_navigation)

        QTimer.singleShot(200, drive_navigation)
    elif screenshot_path:
        QTimer.singleShot(300, prepare_capture)

    double_close_ms = os.environ.get("DUCAI_SMOKE_DOUBLE_CLOSE_MS")
    if double_close_ms:
        try:
            close_delay = max(0, int(double_close_ms))
        except ValueError:
            app_model.forceShutdown()
            raise ValueError("DUCAI_SMOKE_DOUBLE_CLOSE_MS must be an integer") from None

        def close_twice() -> None:
            root_window.close()
            root_window.close()

        QTimer.singleShot(close_delay, close_twice)

    def complete_quit() -> None:
        root_window.setProperty("shutdownComplete", True)
        app.quit()

    app_model.quitReady.connect(complete_quit)
    app.aboutToQuit.connect(app_model.forceShutdown)

    smoke_import_path = os.environ.get("DUCAI_SMOKE_IMPORT_PATH")
    smoke_import_paths = os.environ.get("DUCAI_SMOKE_IMPORT_PATHS_JSON")
    if smoke_import_paths:
        try:
            parsed_smoke_paths = json.loads(smoke_import_paths)
        except json.JSONDecodeError:
            app_model.forceShutdown()
            raise ValueError("DUCAI_SMOKE_IMPORT_PATHS_JSON must be a JSON array") from None
        if not isinstance(parsed_smoke_paths, list) or not all(
            isinstance(value, str) and value for value in parsed_smoke_paths
        ):
            app_model.forceShutdown()
            raise ValueError("DUCAI_SMOKE_IMPORT_PATHS_JSON must be a JSON array of paths")

        def start_multi_smoke_import() -> None:
            if app_model.backendReady:
                app_model.importPaths(parsed_smoke_paths)

        app_model.backendReadyChanged.connect(start_multi_smoke_import)
    elif smoke_import_path:

        def start_smoke_import() -> None:
            if app_model.backendReady:
                app_model.importPath(smoke_import_path)

        app_model.backendReadyChanged.connect(start_smoke_import)

    _seed_signed_update_smoke_candidate(app_model)
    app_model.start()
    publish_update_health()

    smoke_exit_ms = os.environ.get("DUCAI_SMOKE_EXIT_MS")
    if smoke_exit_ms:
        try:
            QTimer.singleShot(max(0, int(smoke_exit_ms)), app_model.requestQuit)
        except ValueError:
            app_model.forceShutdown()
            raise ValueError("DUCAI_SMOKE_EXIT_MS must be an integer") from None

    exit_code = app.exec()
    # QApplication owns both objects, but its child-destruction order is not a
    # public contract. Destroy the QML tree explicitly while appModel is still
    # alive so bound screens never evaluate against a null backend on exit.
    delete_qt_object(engine)
    instance_lock.unlock()
    return exit_code
