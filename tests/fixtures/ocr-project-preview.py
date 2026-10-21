"""Render the reopened real OCR project and validate cue/video time alignment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QTimer, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication
from shiboken6 import delete as delete_qt_object

from ducai_studio.ui.app import _register_bundled_fonts
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def main() -> int:
    source = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["DUCAI_APP_DATA_ROOT"] = str(output / "app-data")
    configure_qt_plugin_dll_search()
    QCoreApplication.setOrganizationName("DUCAI OCR VERIFICATION")
    QCoreApplication.setApplicationName("ocr-project-preview")
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    _register_bundled_fonts()
    model = ApplicationViewModel(app)
    model._apply_project(json.loads(source.read_text(encoding="utf-8")))
    qml = Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml"
    if os.environ.get("DUCAI_TEST_QML_ROOT"):
        qml = Path(os.environ["DUCAI_TEST_QML_ROOT"])
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.addImportPath(str(qml))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    window.setWidth(1180)
    window.setHeight(720)
    window.show()
    model.navigate("timeline")
    timeline = window.findChild(QObject, "timelineScreen")
    assert timeline is not None
    preview = timeline.findChild(QObject, "timelinePreview")
    assert preview is not None
    checks = [
        (4200, "在去太南小会的路上"),
        (23300, "这里是散修们聚集的一处秘境"),
        (26100, "交换了不少有用之物"),
        (28500, "不过"),
    ]
    if len(sys.argv) > 3:
        checks = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    results = []
    failure = []

    def fail(exc):
        failure.append(str(exc))
        app.quit()

    def capture():
        try:
            timestamp, expected = checks[len(results)]
            rows = preview.property("activeSubtitleRegions")
            if hasattr(rows, "toVariant"):
                rows = rows.toVariant()
            text = preview.property("activeSubtitleText")
            source_ms = float(preview.property("sourcePositionMs"))
            descriptor = model.project_state.previewDescriptor(timestamp * 1000)
            assert len(rows) == 1, rows
            assert text == expected, (timestamp, text)
            expected_source_ms = float(descriptor["sourcePositionMs"])
            assert abs(source_ms - expected_source_ms) <= 100, (source_ms, descriptor)
            target = output / f"preview-{timestamp}ms.png"
            assert window.grabWindow().save(str(target))
            results.append(
                {
                    "timeline_ms": timestamp,
                    "source_ms": source_ms,
                    "text": text,
                    "visible_dialogue_count": len(rows),
                    "screenshot": str(target),
                }
            )
            if len(results) == len(checks):
                (output / "preview-report.json").write_text(
                    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(results, ensure_ascii=False), flush=True)
                app.quit()
            else:
                seek()
        except Exception as exc:
            fail(exc)

    def seek():
        preview.seek(checks[len(results)][0])
        QTimer.singleShot(700, capture)

    def loaded():
        if int(preview.property("mediaStatus")) not in (2, 5, 6):
            return
        timer.stop()
        preview.play()
        QTimer.singleShot(350, start_checks)

    def start_checks():
        preview.pause()
        seek()

    timer = QTimer(app)
    timer.setInterval(100)
    timer.timeout.connect(loaded)
    timer.start()
    QTimer.singleShot(
        20_000,
        lambda: fail(
            {
                "reason": "Preview video did not finish validation",
                "media_status": preview.property("mediaStatus"),
                "error": preview.property("errorText"),
                "descriptor": model.project_state.previewDescriptor(0),
                "project": model.project_state.projectId,
            }
        ),
    )
    app.exec()
    model.forceShutdown()
    delete_qt_object(engine)
    if failure:
        print("Preview validation failed:", failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
