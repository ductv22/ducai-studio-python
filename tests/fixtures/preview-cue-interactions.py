"""Native scene selection/drag/cancel/playback regression on isolated real media."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QSettings, QStandardPaths, Qt
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
from ducai_studio.ui.timeline_media_provider import TimelineMediaProvider
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
    _ensure_fonts()
    model = ApplicationViewModel(app)
    model.settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
    model.runtime.start = lambda: None
    cues = [{"id": f"c{i}", "index": i, "text": text, "language": "zh",
             "range": {"start_us": 2_000_000, "end_us": 5_000_000},
             "role": "scene_text", "subtitle_track_id": f"S{i+1}",
             "visual_box": [x, .25, .12, .20], "panel_box": [x, .25, .12, .20],
             "visual_box_source": "ocr", "text_font_size": 40,
             "review_state": "accepted"}
            for i, (text, x) in enumerate((("山脉", .25), ("禁地", .65)), 1)]
    project = {"id": "preview-interaction", "name": "Isolated Preview gestures", "revision": 1,
               "source_cues": cues, "translated_cues": [
                   {**c, "id": "vi" + c["id"], "language": "vi", "text": t}
                   for c, t in zip(cues, ("Sơn Mạch", "Cấm Địa"), strict=True)],
               "media": [{"id": "m", "kind": "video", "path": str(source),
                          "duration_us": 10_000_000}], "tracks": [
                   {"id": "V1", "kind": "video", "name": "Video", "clips": [{
                       "id": "video", "track_id": "V1", "media_id": "m",
                       "timeline_range": {"start_us": 0, "end_us": 10_000_000},
                       "source_range": {"start_us": 0, "end_us": 10_000_000}}]},
                   *[{"id": f"S{i+1}", "kind": "subtitle", "name": "Scene", "clips": [{
                       "id": "s" + c["id"], "track_id": f"S{i+1}", "cue_id": c["id"],
                       "cue_index": i, "timeline_range": c["range"]}]}
                     for i, c in enumerate(cues, 1)]]}
    model.project_state.replace(project)
    service = JobManagerService(database_path=directory / "state.db",
                                artifact_root=directory / "artifacts")
    service.projects.save(project_path=directory / "fixture.ducai",
                          snapshot=decode_project_snapshot(project))
    requests = []
    model.runtime.request = lambda a, p=None, context=None: requests.append((a, p, context))
    qml = Path(os.environ.get("DUCAI_TEST_QML_ROOT") or (
        Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml"))
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.addImageProvider("timeline-media", TimelineMediaProvider(directory / "tiles"))
    engine.addImportPath(str(qml))
    engine.setInitialProperties({"appModel": model})
    engine.load(qml.joinpath("Main.qml").as_uri())
    window = engine.rootObjects()[0]
    window.setWidth(1180)
    window.setHeight(720)
    window.show()
    model.navigate("timeline")
    model.setPlayheadMs(3500)
    QTest.qWait(700)
    def visible_item(name):
        return next(i for i in descendants(window.contentItem())
                    if i.objectName() == name and i.isVisible())

    preview = visible_item("timelinePreview")

    def area_for(cue_id):
        for item in descendants(window.contentItem()):
            parent = item.parentItem()
            if (item.isVisible() and item.metaObject().className().startswith("QQuickMouseArea")
                    and parent
                    and parent.property("modelData") is not None):
                data = parent.property("modelData")
                data = data.toVariant() if hasattr(data, "toVariant") else data
                if isinstance(data, dict) and data.get("cue_id") == cue_id and item.z() == 220:
                    return item
        raise AssertionError("Scene move area not found: " + cue_id)

    def point(item):
        return item.mapToScene(QPointF(item.width()/2, item.height()/2)).toPoint()

    def acknowledge(request):
        action, payload, context = request
        patch = service.subtitle_command(project_id=payload["project_id"],
            command=payload["command"], cue_id=payload.get("cue_id"),
            language=payload["language"], params=payload["values"], compact=True)
        model._action_succeeded(action, patch, context)
        app.processEvents()

    try:
        model.selectTimelineCue("c1")
        time = model.playheadUs
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point(area_for("c2")))
        app.processEvents()
        assert model.playheadUs == time, ("Preview click sought", time, model.playheadUs)
        assert model.selectedCueIds == ["c2"]
        assert not preview.property("subtitleGeometryActive"), "click left stale drag"
        area = area_for("c2")
        start = point(area)
        count = len(requests)
        QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(window, start + QPointF(-25, -10).toPoint())
        app.processEvents()
        assert len(requests) == count
        QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier,
                           start + QPointF(-35, -15).toPoint())
        app.processEvents()
        assert len(requests) == count + 1
        acknowledge(requests[-1])
        assert not preview.property("subtitleGeometryActive")
        assert service.projects.load("preview-interaction").source_cues[1].panel_box[0] < .65
        QTest.mouseClick(window, Qt.LeftButton, Qt.ControlModifier, point(area_for("c1")))
        app.processEvents()
        assert set(model.selectedCueIds) == {"c1", "c2"} and model.playheadUs == time
        before = service.projects.load("preview-interaction")
        start = point(area_for("c1"))
        QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(window, start + QPointF(12, 8).toPoint())
        app.processEvents()
        QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier,
                           start + QPointF(20, 10).toPoint())
        app.processEvents()
        action, _, context = requests[-1]
        assert set(requests[-1][1]["values"]["cue_ids"]) == {"c1", "c2"}
        assert preview.property("subtitleGeometryPending")
        model._finish_background_subtitle_save()
        app.processEvents()
        assert preview.property("subtitleGeometryPending"), "unrelated ACK ended geometry"
        stale = {**context, "subtitle_geometry_generation": -1}
        model._action_succeeded(action, {}, stale)
        app.processEvents()
        assert preview.property("subtitleGeometryPending"), "stale ACK ended geometry"
        acknowledge(requests[-1])
        after = service.projects.load("preview-interaction")
        assert all(new.panel_box[0] > old.panel_box[0]
                   for old, new in zip(before.source_cues, after.source_cues, strict=True))
        assert not preview.property("subtitleGeometryPending")
        # A rejected released movement restores confirmed artwork and remains usable.
        before = service.projects.load("preview-interaction")
        start = point(area_for("c1"))
        QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier,
                           start + QPointF(10, 5).toPoint())
        app.processEvents()
        model._action_failed("subtitle_command", {"message": "disk full"}, requests[-1][2])
        app.processEvents()
        assert not preview.property("subtitleGeometryActive") and not model.subtitleBusy
        assert service.projects.load("preview-interaction") == before
        # A seek which destroys the grabbed scene delegate must cancel, not commit,
        # and leave the blank-video playback area enabled.
        start = point(area_for("c2"))
        count = len(requests)
        QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(window, start + QPointF(-10, -5).toPoint())
        model.setPlayheadMs(6000)
        app.processEvents()
        QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, start)
        app.processEvents()
        assert len(requests) == count and not preview.property("subtitleGeometryActive")
        toggle = visible_item("videoPlaybackToggleArea")
        assert toggle is not None and toggle.property("enabled")
        for _ in range(3):
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point(toggle))
            QTest.qWait(100)
            assert preview.property("playing")
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point(toggle))
            QTest.qWait(60)
            assert not preview.property("playing")
        QTest.keyClick(window, Qt.Key_Space)
        QTest.qWait(80)
        assert preview.property("playing")
        QTest.keyClick(window, Qt.Key_Space)
        QTest.qWait(60)
        assert not preview.property("playing")
        report = {"status": "PASS", "seek_free_selection": True, "drag_persisted": True,
                  "group_drag_persisted": True, "stale_unrelated_ack_ignored": True,
                  "rejection_recovered": True, "cancel_unblocks_playback": True,
                  "click_space_cycles": 3}
        (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
        print(json.dumps(report))
        return 0
    finally:
        preview.pause()
        window.setProperty("shutdownComplete", True)
        window.close()
        delete(engine)
        app.processEvents()
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
