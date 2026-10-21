"""Exercise real QML clicks against the project style command dispatcher."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from ducai_studio.infrastructure.project_repository import ProjectRepository
from ducai_studio.job_manager.server import JobManagerServer
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.ui.app import _register_bundled_fonts
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels import application_view_model as application_module


def main() -> int:
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    configure_qt_plugin_dll_search()
    app = QApplication([])
    _register_bundled_fonts()
    QFont.insertSubstitution("Be Vietnam Pro", "Noto Sans CJK SC")
    QQuickStyle.setStyle("Basic")
    settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
    original_settings = application_module.QSettings
    application_module.QSettings = lambda *_args: settings
    try:
        model = application_module.ApplicationViewModel(app)
    finally:
        application_module.QSettings = original_settings
    service = JobManagerService(
        database_path=directory / "state.db", artifact_root=directory / "artifacts"
    )
    server = JobManagerServer(name=f"style-fixture-{os.getpid()}", service=service)
    project = service.create_project(project_path=str(directory / "test.ducai"), name="Styles")
    project_id = project["id"]
    errors = []
    calls = []

    def request(action, params=None, context=None):
        calls.append(action)

        def respond():
            try:
                result = server._dispatch(action, params or {})
            except (ValueError, RuntimeError) as error:
                errors.append(str(error))
                model._action_failed(action, {"message": str(error)}, context)
            else:
                model._action_succeeded(action, result, context)

        QTimer.singleShot(0, respond)
        return str(len(calls))

    model.runtime.start = lambda: None
    model.runtime.request = request
    model._set_backend_ready(True)
    model._apply_project(project)
    qml = Path(os.environ.get("DUCAI_TEST_QML_ROOT", str(
        Path(__file__).parents[2] / "src/ducai_studio/ui/qml")))
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.addImportPath(str(qml))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
    assert engine.rootObjects(), "The QML shell did not load"
    window = engine.rootObjects()[0]
    window.setWidth(1180)
    window.setHeight(720)

    def locate(name):
        item = window.findChild(QObject, name)
        assert item is not None, name
        return item

    def click(item):
        position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         position)
        app.processEvents()

    def enter(item, value):
        click(item)
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        for character in value:
            QCoreApplication.sendEvent(window, QKeyEvent(
                QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier, character))
        app.processEvents()
        assert item.property("text") == value

    def wait_saved():
        for _ in range(100):
            app.processEvents()
            if not model.translationPreferencesBusy:
                return
            QTest.qWait(10)
        raise AssertionError("Style save did not release its busy gate")

    try:
        model.navigate("translate")
        QTest.qWait(150)
        name = locate("translationStyleName")
        rules = locate("translationStyleRules")
        save = locate("saveTranslationStyle")
        saved = locate("savedTranslationStyle")
        assert not save.property("enabled"), "Empty names must not be saved"
        text = "Dùng anh/em. Không thêm chú thích vào phụ đề."
        enter(rules, text)
        enter(name, "Bản riêng")
        click(locate("translationBuiltinStyle"))
        QTest.keyClick(window, Qt.Key.Key_Escape)
        model.project_state.changed.emit()
        app.processEvents()
        assert name.property("text") == "Bản riêng"
        assert rules.property("text") == text
        click(save)
        wait_saved()
        assert len(service.projects.load(project_id).translation_preferences.saved_styles) == 1
        assert model.project_state.translationActiveStyleName == "Bản riêng"

        enter(name, "Bản riêng 2")
        click(save)
        wait_saved()
        assert len(service.projects.load(project_id).translation_preferences.saved_styles) == 2
        assert saved.property("currentText") == "Bản riêng 2"
        # Keyboard activation of the previous saved entry must retain its name,
        # even though these two separate presets deliberately have equal rules.
        click(saved)
        QTest.keyClick(window, Qt.Key.Key_Escape)
        QTest.keyClick(window, Qt.Key.Key_Up)
        wait_saved()
        assert model.project_state.translationActiveStyleName == "Bản riêng"
        assert saved.property("currentText") == "Bản riêng"
        assert rules.property("text") == text

        enter(name, "bản riêng")
        revision = service.projects.load(project_id).revision
        click(save)
        wait_saved()
        assert len(errors) == 1 and "đã tồn tại" in errors[0], errors
        assert service.projects.load(project_id).revision == revision

        # Read from a fresh repository rather than the service's editor memory.
        repository = ProjectRepository(service.database)
        reloaded = repository.load(project_id)
        assert reloaded.translation_preferences.active_style_name == "Bản riêng"
        model._apply_project(server._dispatch("get_project", {"project_id": project_id}))
        app.processEvents()
        assert saved.property("currentText") == "Bản riêng"
        assert rules.property("text") == text
        screenshot = directory / "named-styles.png"
        assert window.grabWindow().save(str(screenshot))
        report = {"status": "PASS", "save_click": True, "load_selection": True,
                  "same_rules_distinct_names": True, "duplicate_rejected": True,
                  "drafts_survive_unrelated_project_change": True,
                  "database_reload": True, "screenshot": str(screenshot)}
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0
    finally:
        delete(engine)
        model.forceShutdown()
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
