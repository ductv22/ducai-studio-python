"""Native QML binding work must stop while a Preview workspace is hidden."""

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
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
    model = ApplicationViewModel(app)
    model.settings = QSettings(sys.argv[1], QSettings.Format.IniFormat)
    model.runtime.start = lambda: None
    model.runtime.request = lambda *_args, **_kwargs: None
    model.project_state.replace({"id": "hidden", "revision": 1, "name": "Preview",
        "source_cues": [{"id": "c", "index": 1, "text": "Current text",
            "range": {"start_us": 0, "end_us": 10_000_000}}],
        "tracks": [{"id": "V1", "kind": "video", "clips": [{"id": "v",
            "timeline_range": {"start_us": 0, "end_us": 10_000_000}}]}]})
    qml = Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml"
    engine = QQmlApplicationEngine()
    engine.addImportPath(str(qml))
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    window.resize(800, 600)
    window.show()
    model.navigate("settings")
    calls = []
    for cue_model in (model.project_state.subtitle_cues, model.project_state.title_cues):
        original = cue_model._items_at_time

        def count(position, original=original):
            calls.append(position)
            return original(position)

        cue_model._items_at_time = count
    app.processEvents()
    calls.clear()
    for milliseconds in range(100, 1100, 100):
        model.setPlayheadMs(milliseconds)
        app.processEvents()
    hidden_calls = len(calls)
    assert hidden_calls == 0, f"Hidden Preview made {hidden_calls} time queries"
    model.navigate("timeline")
    app.processEvents()
    def descendants(item):
        yield item
        for child in item.childItems():
            yield from descendants(child)

    preview = next(item for item in descendants(window.contentItem())
        if item.objectName() == "timelinePreview" and item.isVisible())
    active = preview.property("activeSubtitle")
    active = active.toVariant() if hasattr(active, "toVariant") else active
    assert active["source_text"] == "Current text", active
    assert len(calls) > hidden_calls
    delete(window)
    delete(engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
