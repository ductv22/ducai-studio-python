"""Real QML group/geometry gestures, persistence and transport on isolated media."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QSettings, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QFontDatabase
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from ducai_studio.infrastructure.project_repository import decode_project_snapshot
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.subtitle_artwork import _ensure_fonts
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def descendants(item):
    yield item
    for child in item.childItems():
        yield from descendants(child)


def main():
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "source.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=854x480:rate=25", "-t", "10", "-c:v", "libx264",
                    "-threads", "1", "-pix_fmt", "yuv420p", str(source)], check=True)
    configure_qt_plugin_dll_search()
    app = QApplication([])
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    qml = Path(os.environ.get("DUCAI_TEST_QML_ROOT") or (
        Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml"))
    if os.environ.get("DUCAI_TEST_QML_ROOT"):
        for path in (qml.parent / "assets/fonts").glob("*.*"):
            if path.suffix in {".ttf", ".otf"}:
                assert QFontDatabase.addApplicationFont(str(path)) >= 0
    _ensure_fonts()
    model = ApplicationViewModel(app)
    model.settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
    model.runtime.start = lambda: None
    model.setExportPresetId("480p")
    cues = [{"id": f"c{i}", "index": i, "text": "Gốc", "language": "zh",
             "range": {"start_us": i * 2_000_000, "end_us": i * 2_000_000 + 1_500_000},
             "visual_box": [.45, .9, .05, .03], "visual_box_source": "ocr"}
            for i in range(1, 4)]
    project = {"id": "geometry", "name": "Kiểm thử kéo chữ — dữ liệu riêng", "revision": 1,
               "source_cues": cues,
               "translated_cues": [{**cue, "id": "vi" + cue["id"], "language": "vi",
                                     "text": text} for cue, text in zip(cues, (
                                         "Mẹ con ta không nơi nương tựa.", "Cha", "Giữ nguyên"),
                                         strict=True)],
               "media": [{"id": "m", "kind": "video", "path": str(source),
                          "duration_us": 10_000_000}],
               "tracks": [{"id": "V1", "kind": "video", "name": "Video", "clips": [{
                   "id": "video", "track_id": "V1", "media_id": "m",
                   "timeline_range": {"start_us": 0, "end_us": 10_000_000},
                   "source_range": {"start_us": 0, "end_us": 10_000_000}}]},
                   {"id": "S1", "kind": "subtitle", "name": "Thoại", "clips": [{
                       "id": "s" + cue["id"], "track_id": "S1", "cue_id": cue["id"],
                       "cue_index": cue["index"], "timeline_range": cue["range"]}
                       for cue in cues]}]}
    model.project_state.replace(project)
    service = JobManagerService(database_path=directory / "state.db",
                                artifact_root=directory / "artifacts")
    service.projects.save(project_path=directory / "fixture.ducai",
                          snapshot=decode_project_snapshot(project))
    requests = []
    model.runtime.request = lambda a, p=None, context=None: requests.append((a, p, context))
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.addImportPath(str(qml))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
    try:
        window = engine.rootObjects()[0]
        def close_fixture():
            window.setProperty("shutdownComplete", True)
            window.close()
            app.quit()
        model.quitReady.connect(close_fixture)
        window.setWidth(1180 if "--small" in sys.argv else 1514)
        window.setHeight(720 if "--small" in sys.argv else 932)
        window.show()
        model.navigate("timeline")
        model.setPlayheadMs(2500)
        QTest.qWait(250)
        preview = next(value for value in descendants(window.contentItem())
                       if value.objectName() == "timelinePreview" and value.isVisible())
        timeline = window.findChild(QObject, "timelineScreen")

        def item(name):
            return next(value for value in descendants(window.contentItem())
                        if value.objectName() == name and value.isVisible())

        def ack():
            action, payload, context = requests[-1]
            patch = service.subtitle_command(project_id=payload["project_id"],
                    command=payload["command"], cue_id=payload.get("cue_id"),
                    language=payload["language"], params=payload["values"], compact=True)
            model._action_succeeded(action, patch, context)
            app.processEvents()

        def click(value, modifiers=Qt.KeyboardModifier.NoModifier):
            point = value.mapToScene(QPointF(value.width() / 2, value.height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, modifiers, point)
            app.processEvents()

        click(item("timelineClipBody-sc1"))
        click(item("timelineClipBody-sc2"), Qt.KeyboardModifier.ControlModifier)
        assert set(model.selectedCueIds) == {"c1", "c2"}
        frame = item("dialogueTextSelectionFrame")
        assert frame.height() < 70, frame.height()
        before = model.subtitleArtwork(model.project_state.subtitle_cues.find("c1"), 854, 480)
        area = item("dialogueTextMoveArea")
        click(area)
        assert set(model.selectedCueIds) == {"c1", "c2"}, "preview click collapsed group"
        point = area.mapToScene(QPointF(area.width() / 2, area.height() / 2)).toPoint()
        count = len(requests)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.mouseMove(window, point + QPointF(0, -40).toPoint())
        app.processEvents()
        assert len(requests) == count, "drag must not send IPC"
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           point + QPointF(0, -55).toPoint())
        app.processEvents()
        assert len(requests) == count + 1
        assert requests[-1][1]["values"]["cue_ids"] == ["c1", "c2"]
        pending = preview.property("activeSubtitle")
        if hasattr(pending, "toVariant"):
            pending = pending.toVariant()
        assert preview.property("subtitleGeometryPending")
        ack()
        assert not preview.property("subtitleGeometryActive")
        assert set(model.selectedCueIds) == {"c1", "c2"}
        after = model.subtitleArtwork(model.project_state.subtitle_cues.find("c1"), 854, 480)
        assert before["font_size"] == after["font_size"], (before, after)
        assert abs(before["text_rect"][2] - after["text_rect"][2]) < .01
        assert after["text_rect"][1] < before["text_rect"][1] - 30
        stored = service.projects.load("geometry")
        assert stored.source_cues[0].panel_box and stored.source_cues[1].panel_box
        assert stored.source_cues[2].panel_box is None
        original_selection = set(model.selectedCueIds)
        model.setSelectedSubtitleTextFormat(18, "horizontal", "#000000", "#FFFFFF", "standard")
        assert set(model.selectedCueIds) == original_selection
        model._dispatch_background_subtitle_save()
        ack()
        enlarged = model.subtitleArtwork(model.project_state.subtitle_cues.find("c1"), 854, 480)
        assert enlarged["font_size"] > after["font_size"], (
            "font control stayed capped by drag panel")
        assert service.projects.load("geometry").source_cues[1].text_font_size == 18
        # Group timing reaches one atomic command and affects only the selection.
        timeline.beginSceneTiming("sc1", "c1", "move", 2_000_000, 3_500_000, 1, "S1", 400, 100)
        timeline.updateSceneTiming(410, 100)
        timeline.finishSceneTiming()
        ack()
        snapshot = service.projects.load("geometry")
        assert snapshot.source_cues[0].range.start_us > 2_000_000
        assert snapshot.source_cues[1].range.start_us > 4_000_000
        assert snapshot.source_cues[2].range.start_us == 6_000_000
        transport = item("previewTransport")
        video = item("previewVideoOutput")
        assert transport.y() >= video.y() + video.height() - .01
        window.grabWindow().save(str(directory / "preview.png"))
        # Reload from the real DB, keeping the cue selection, must retain geometry/style.
        model.project_state.replace(service.projects.load_json("geometry"))
        app.processEvents()
        reopened = model.project_state.subtitle_cues.find("c1")
        assert reopened["text_font_size"] == 18 and reopened["panel_box"]
        # The resize handle also works as a group gesture, with no per-move IPC.
        before_resize = service.projects.load("geometry")
        area = item("dialogueTextResizeArea")
        point = area.mapToScene(QPointF(area.width() / 2, area.height() / 2)).toPoint()
        count = len(requests)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.mouseMove(window, point + QPointF(-8, -4).toPoint())
        app.processEvents()
        assert len(requests) == count
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           point + QPointF(-12, -6).toPoint())
        app.processEvents()
        assert len(requests) == count + 1
        assert requests[-1][1]["values"]["scale_x"] < 1
        assert requests[-1][1]["values"]["scale_y"] < 1
        ack()
        resized = service.projects.load("geometry")
        for old, new in zip(before_resize.source_cues[:2], resized.source_cues[:2], strict=True):
            assert new.panel_box[2] < old.panel_box[2]
            assert new.panel_box[3] < old.panel_box[3]
            assert new.text_font_size == old.text_font_size == 18
        assert resized.source_cues[2] == before_resize.source_cues[2]
        assert set(model.selectedCueIds) == {"c1", "c2"}
        window.grabWindow().save(str(directory / "preview-resized.png"))
        report = {"status": "PASS", "requests": len(requests),
                  "first_move_font_size": after["font_size"],
                  "enlarged_font_size": enlarged["font_size"], "revision": resized.revision,
                  "group_resize_persisted": True,
                  "selection_frame_height": frame.height()}
        (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        if "--interactive" in sys.argv:
            window.setTitle("DUCAI — kiểm thử a130 (dữ liệu riêng)")
            model.setPlayheadMs(2600)

            def local_request(action, payload=None, context=None):
                if action != "subtitle_command":
                    raise ValueError("Cửa sổ kiểm thử chỉ lưu chỉnh sửa phụ đề")
                requests.append((action, payload, context))
                QTimer.singleShot(50, ack)
                return len(requests)

            model.runtime.request = local_request
            QTimer.singleShot(600_000, close_fixture)
            app.exec()
            (directory / "interactive-final.json").write_text(
                json.dumps(service.projects.load_json("geometry"), ensure_ascii=False, indent=2),
                encoding="utf-8")
        return 0
    finally:
        preview.pause()
        window.close()
        delete(engine)
        app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())
