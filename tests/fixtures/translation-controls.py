"""Native QML interaction of catalog/manual models, named keys and scene OCR."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPointF,
    QSettings,
    QStandardPaths,
    Qt,
    QUrl,
)
from PySide6.QtGui import QKeyEvent
from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def main():
    configure_qt_plugin_dll_search()
    app = QApplication([])
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    model = ApplicationViewModel(app)
    model.settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
    requests = []
    model.runtime.start = lambda: None
    model.runtime.request = lambda action, params=None, context=None: requests.append(
        (action, params, context))
    model._set_backend_ready(True)
    qml = Path(os.environ.get("DUCAI_TEST_QML_ROOT", str(
        Path(__file__).parents[2] / "src/ducai_studio/ui/qml")))
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.addImportPath(str(qml))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
    window = engine.rootObjects()[0]
    window.setWidth(1180)
    window.setHeight(720)
    window.show()

    def locate(name):
        result = window.findChild(QObject, name)
        assert result is not None, name
        return result

    def click(item):
        p = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
        app.processEvents()

    def enter(item, value):
        click(item)
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        for character in value:
            QCoreApplication.sendEvent(window, QKeyEvent(
                QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier, character))
        app.processEvents()

    try:
        model.navigate("translate")
        QTest.qWait(150)
        provider, chooser = locate("translationProviderChooser"), locate("translationModelChooser")
        provider.setProperty("currentIndex", 1)
        QTest.qWait(50)
        enter(chooser, "my-manual-model")
        assert chooser.property("editText") == "my-manual-model", (
            chooser.property("editText"), chooser.property("activeFocus"))
        model.refreshTranslationModels("gemini-api", "fixture-key", "")
        context = requests[-1][2]
        model._action_succeeded("list_translation_models", {"models": ["a-model", "b-model"]},
                               context)
        QTest.qWait(50)
        assert chooser.property("editText") == "my-manual-model", chooser.property("editText")
        # Select a real dropdown item via keyboard, then keep manual fallback.
        click(chooser)
        QTest.keyClick(window, Qt.Key.Key_F4)
        QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.keyClick(window, Qt.Key.Key_Return)
        app.processEvents()
        assert chooser.property("editText") in {"a-model", "b-model"}
        enter(chooser, "custom-v2")
        click(provider)
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert model.uiStringValue("translation/model/gemini-api", "") == "custom-v2"
        provider.setProperty("currentIndex", 5)
        QTest.qWait(50)
        assert chooser.property("editText") == "qwen3.7-flash", chooser.property("editText")
        provider.setProperty("currentIndex", 1)
        QTest.qWait(50)
        assert chooser.property("editText") == "custom-v2", chooser.property("editText")
        model.settings.setValue("translation/openai-compatible/credentialReferences",
                                ["fixture-a", "fixture-b"])
        model.setUiStringValue("translation/selectedCredential/openai-compatible", "fixture-b")
        model.translationSettingsChanged.emit()
        provider.setProperty("currentIndex", 7)
        QTest.qWait(50)
        saved_key = locate("translationSavedKeyChooser")
        assert saved_key.property("currentText") == "fixture-b", saved_key.property("currentText")
        provider.setProperty("currentIndex", 1)
        QTest.qWait(50)
        provider.setProperty("currentIndex", 7)
        QTest.qWait(50)
        assert saved_key.property("currentText") == "fixture-b", saved_key.property("currentText")
        name, secret = locate("translationKeyName"), locate("translationKeySecret")
        model.project_state.replace({"id": "project-1", "name": "Phim", "revision": 1})
        model._translation_job_id = "current-job"
        model._selected_translation_cache_id = "current-job"
        model._set_translation_job_state("running")
        model._set_translation_busy(True)
        model._action_succeeded("list_translation_caches", [{
            "job_id": "old-job", "state": "cancelled", "provider_id": "gemini-api",
            "model_id": "old-model", "completed_cues": 12, "label": "Cache cũ 12 cue",
        }], {"project_id": "project-1"})
        QTest.qWait(50)
        cache = locate("translationCacheChooser")
        assert model.selectedTranslationCacheId == "current-job"
        assert cache.property("displayText") == "Cache của lượt dịch hiện tại", (
            cache.property("displayText"), cache.property("currentIndex"))
        model._action_succeeded("list_translation_caches", [
            {"job_id": "old-job", "label": "Cache cũ 12 cue"},
            {"job_id": "current-job", "label": "Cache hiện tại 77 cue"},
        ], {"project_id": "project-1"})
        QTest.qWait(50)
        assert cache.property("displayText") == "Cache hiện tại 77 cue"
        model._action_succeeded("list_translation_caches", [
            {"job_id": "old-job", "label": "Cache cũ 12 cue"},
            {"job_id": "current-job", "label": "77/200 cue · Đang chạy",
             "provider_id": "gemini-api", "model_id": "current-model",
             "completed_cues": 77, "cue_total": 200, "state": "running"},
        ], {"project_id": "project-1"})
        model._set_translation_qa({"status": "PENDING", "completed_cues": 102,
                                   "checked_cues": 77, "cue_total": 200, "issues": []},
                                  reconcile_with_project=False)
        model._set_translation_busy(False)
        model._set_translation_job_state("paused")
        QTest.qWait(50)
        assert cache.property("displayText") == (
            "102/200 cue · Đã tạm dừng · gemini-api / current-model"
        ), cache.property("displayText")
        # An explicit historical row must not borrow the current job's progress.
        model._selected_translation_cache_id = "old-job"
        model.translationCachesChanged.emit()
        QTest.qWait(50)
        assert cache.property("displayText") == "Cache cũ 12 cue"
        model._selected_translation_cache_id = "current-job"
        model.translationCachesChanged.emit()
        model._set_translation_job_state("cancelled")
        window.grabWindow().save(str(root / "before-key.png"))
        enter(name, "Main key")
        assert name.property("text") == "Main key", (
            name.property("text"), name.property("activeFocus"), name.mapToScene(QPointF(0, 0)))
        enter(secret, "fixture-secret-not-a-real-key")
        for field, expected_mode in ((secret, 2), (name, 0)):
            expression = QQmlExpression(engine.rootContext(), field, "Number(echoMode)")
            assert expression.evaluate()[0] == expected_mode
        assert name.property("text") == "Main key"
        window.grabWindow().save(str(root / "translation.png"))
        model.navigate("transcribe")
        QTest.qWait(50)
        toggle = locate("sceneOcrToggle")
        assert toggle.property("visible") and toggle.property("checked")
        click(toggle)
        assert not model.ocrSceneTextEnabled
        click(toggle)
        assert model.ocrSceneTextEnabled
        assert locate("subtitleTextToolbar") is not None  # Only Timeline owns it.
        window.grabWindow().save(str(root / "extraction.png"))
        (root / "report.json").write_text(json.dumps({"status": "PASS", "manual_model": True,
            "provider_restore": True, "named_masked_key": True, "scene_switch": True,
            "saved_key_restore": True, "current_cache_label": True}),
            encoding="utf-8")
        return 0
    finally:
        delete(engine)
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
