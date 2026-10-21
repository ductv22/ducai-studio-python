"""Exercise Timeline initialization as an empty project receives imported media."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QStandardPaths, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def main() -> int:
    configure_qt_plugin_dll_search()
    app = QApplication([])
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    model = ApplicationViewModel(app)
    model.settings = QSettings(sys.argv[1], QSettings.Format.IniFormat)
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    qml_root = Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml"
    engine.addImportPath(str(qml_root))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml_root / "Main.qml")))
    try:
        assert engine.rootObjects()
        window = engine.rootObjects()[0]
        window.setWidth(1180)
        window.setHeight(720)
        window.show()
        model.navigate("timeline")
        timeline = window.findChild(QObject, "timelineScreen")
        assert timeline is not None
        model.project_state.replace({"id": "imported", "name": "New video", "revision": 0})
        app.processEvents()
        project = {
            "id": "imported",
            "name": "New video",
            "revision": 1,
            "tracks": [{
                "id": "V1", "kind": "video", "name": "Video",
                "clips": [{
                    "id": "video", "track_id": "V1",
                    "timeline_range": {"start_us": 0, "end_us": 8_000_000},
                }],
            }],
        }
        model.project_state.replace(project)
        app.processEvents()
        assert timeline.property("fittedProjectDurationUs") == 8_000_000
        assert timeline.property("cutMarkerAUs") is None
        assert timeline.property("cutMarkerBUs") is None
        assert timeline.property("viewportDurationUs") == 8_000_000
        assert window.findChild(QObject, "timelineCutPanel") is None
        timeline.zoomBy(2)
        viewport = timeline.property("viewportDurationUs")
        model.project_state.replace({**project, "revision": 2})
        model.selectClip("video")
        app.processEvents()
        assert timeline.property("fittedProjectDurationUs") == 8_000_000
        assert timeline.property("viewportDurationUs") == viewport
        print("PASS: import initializes viewport once; edits preserve zoom; no retention controls")
        return 0
    finally:
        delete(engine)
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
