"""Exercise real cue timing QML without touching user projects or runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QSettings, QStandardPaths, Qt, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from ducai_studio.infrastructure.project_repository import decode_project_snapshot
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def items(item):
    yield item
    for child in item.childItems():
        yield from items(child)


def main() -> int:
    configure_qt_plugin_dll_search()
    app = QApplication([])
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    model = ApplicationViewModel(app)
    model.settings = QSettings(sys.argv[1], QSettings.Format.IniFormat)
    cue = {"id": "cue", "index": 1, "text": "若是有缘", "language": "zh",
           "range": {"start_us": 2_000_000, "end_us": 3_000_000}}
    project = {
        "id": "gesture", "name": "Gesture", "revision": 1,
        "source_cues": [cue],
        "translated_cues": [{**cue, "text": "Nếu có duyên", "language": "vi"}],
        "tracks": [
            {"id": "V1", "name": "Video", "kind": "video", "clips": [
                {"id": "video", "track_id": "V1", "timeline_range": {
                    "start_us": 0, "end_us": 10_000_000}}]},
            {"id": "S1", "name": "Sub", "kind": "subtitle", "clips": [
                {"id": "sub", "track_id": "S1", "cue_id": "cue",
                 "timeline_range": cue["range"]}]},
        ],
    }
    model.project_state.replace(project)
    directory = Path(sys.argv[1]).resolve().parent
    service = JobManagerService(database_path=directory / "state.db",
                                artifact_root=directory / "artifacts")
    service.projects.save(project_path=directory / "gesture.ducai",
                          snapshot=decode_project_snapshot(project))
    requests = []
    model.runtime.request = lambda action, payload=None, context=None: requests.append(
        (action, payload, context))
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    qml = Path(os.environ.get("DUCAI_TEST_QML_ROOT") or (
        Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml"))
    engine.addImportPath(str(qml))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
    try:
        window = engine.rootObjects()[0]
        window.setWidth(1992)
        window.setHeight(1284)
        window.show()
        model.navigate("timeline")
        app.processEvents()
        timeline = window.findChild(QObject, "timelineScreen")
        timeline.setProperty("viewportStartUs", 0)
        timeline.setProperty("viewportEndUs", 5_890_516)
        app.processEvents()
        body = next(i for i in items(window.contentItem())
                    if i.objectName() == "timelineClipBody-sub")
        width = body.parentItem().parentItem().width()
        clip = body.parentItem()

        def acknowledge(request):
            action, payload, context = request
            patch = service.subtitle_command(
                project_id=payload["project_id"], command=payload["command"],
                cue_id=payload.get("cue_id"), language=payload["language"],
                params=payload["values"], compact=True,
            )
            model._action_succeeded(action, patch, context)
            app.processEvents()

        model.updateSubtitleText("cue", "Có duyên ắt gặp", "translated")
        model._dispatch_background_subtitle_save()
        assert len(requests) == 1
        model.setPlayheadMs(2000)
        timeline.beginSceneTiming("sub", "cue", "move", 2_000_000, 3_000_000,
                                  1, "S1", 400, 100)
        timeline.updateSceneTiming(410, 100)
        expected = round(10 * 5_890_516 / width)
        assert round(timeline.property("sceneTimingPreviewDeltaUs")) == expected, (
            "fine drag was pulled back to the playhead", expected,
            timeline.property("sceneTimingPreviewDeltaUs"))
        assert len(requests) == 1, "pointer movement must not send IPC"
        timeline.finishSceneTiming()
        assert len(requests) == 2 and requests[-1][1]["values"]["delta_us"] == expected
        acknowledge(requests[0])
        assert timeline.property("sceneTimingPending") and model.subtitleBusy
        assert round(clip.property("effectiveStart")) == 2_000_000 + expected
        assert model.saveState != "Đã lưu"
        acknowledge(requests[1])
        assert not timeline.property("sceneTimingPending") and not model.subtitleBusy
        assert round(clip.property("effectiveStart")) == 2_000_000 + expected

        # Both handles can leave an already aligned playhead by only five pixels.
        for mode in ("left", "right", "move"):
            current = model.project_state.subtitle_cues.find("cue")
            start, end = current["start_us"], current["end_us"]
            model.setPlayheadMs((end if mode == "right" else start) / 1000)
            timeline.beginSceneTiming("sub", "cue", mode, start, end, 1, "S1", 400, 100)
            timeline.updateSceneTiming(405, 100)
            assert round(timeline.property("sceneTimingPreviewDeltaUs")) == round(
                5 * 5_890_516 / width)
            count = len(requests)
            timeline.finishSceneTiming()
            assert len(requests) == count + 1
            acknowledge(requests[-1])

        # Real MouseAreas must include the final release, not just the last move.
        for name in ("Body", "LeftHandle", "RightHandle"):
            area = next(i for i in items(window.contentItem())
                        if i.objectName() == f"timelineClip{name}-sub")
            current = model.project_state.subtitle_cues.find("cue")
            model.setPlayheadMs((current["end_us"] if name == "RightHandle"
                                 else current["start_us"]) / 1000)
            origin = area.mapToScene(QPointF(area.width() / 2, area.height() / 2)).toPoint()
            count = len(requests)
            QTest.mousePress(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, origin)
            QTest.mouseMove(window, origin + QPointF(5, 0).toPoint(), 1)
            assert len(requests) == count
            QTest.mouseRelease(window, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier,
                               origin + QPointF(11, 0).toPoint())
            assert len(requests) == count + 1, name
            assert abs(requests[-1][1]["values"]["delta_us"]
                       - round(11 * 5_890_516 / width)) <= round(5_890_516 / width)
            acknowledge(requests[-1])

        for _ in range(20):
            current = model.project_state.subtitle_cues.find("cue")
            start, end = current["start_us"], current["end_us"]
            model.setPlayheadMs(start / 1000)
            timeline.beginSceneTiming("sub", "cue", "move", start, end, 1, "S1", 400, 100)
            timeline.updateSceneTiming(405, 100)
            timeline.finishSceneTiming()
            acknowledge(requests[-1])
            assert not timeline.property("sceneTimingPending")

        # Moving down/up a subtitle lane saves both identity and role changes.
        for lane_delta in (1, -1):
            current = model.project_state.subtitle_cues.find("cue")
            track_id = current["subtitle_track_id"]
            timeline.beginSceneTiming("sub", "cue", "move", current["start_us"],
                                      current["end_us"], 1, track_id, 400, 100)
            timeline.updateSceneTiming(400, 100 + 62 * lane_delta)
            assert timeline.property("sceneTimingLaneDelta") == lane_delta
            timeline.finishSceneTiming()
            acknowledge(requests[-1])
        assert model.project_state.subtitle_cues.find("cue")["subtitle_track_id"] == "S1"

        # A rejected submission does not leave preview/busy stuck.
        current = model.project_state.subtitle_cues.find("cue")
        timeline.beginSceneTiming("sub", "cue", "move", current["start_us"],
                                  current["end_us"], 1, "S1", 400, 100)
        timeline.updateSceneTiming(415, 100)
        model._set_timeline_busy(True)
        count = len(requests)
        timeline.finishSceneTiming()
        assert len(requests) == count and not timeline.property("sceneTimingPending")
        model._set_timeline_busy(False)

        current = model.project_state.subtitle_cues.find("cue")
        start, end = current["start_us"], current["end_us"]
        model.setPlayheadMs((start + 300_000) / 1000)
        timeline.beginSceneTiming("sub", "cue", "move", start, end, 1, "S1", 400, 100)
        timeline.updateSceneTiming(400 + 300_000 * width / 5_890_516 - 4, 100)
        assert timeline.property("sceneTimingSnapActive")
        assert round(timeline.property("sceneTimingPreviewDeltaUs")) == 300_000
        timeline.finishSceneTiming()
        acknowledge(requests[-1])

        current = model.project_state.subtitle_cues.find("cue")
        timeline.beginSceneTiming("sub", "cue", "move", current["start_us"],
                                  current["end_us"], 1, "S1", 400, 100)
        timeline.updateSceneTiming(420, 100)
        timeline.finishSceneTiming()
        model._action_failed("subtitle_command", {"message": "disk full"}, requests[-1][2])
        app.processEvents()
        assert not timeline.property("sceneTimingPending") and not model.subtitleBusy
        assert round(clip.property("effectiveStart")) == current["start_us"]
        assert model.saveState == "Chỉnh phụ đề thất bại"

        reopened = JobManagerService(database_path=directory / "state.db",
                                     artifact_root=directory / "artifacts")
        saved = reopened.projects.load("gesture")
        current = model.project_state.subtitle_cues.find("cue")
        assert saved.source_cues[0].range.start_us == current["start_us"]
        assert saved.source_cues[0].range.end_us == current["end_us"]
        assert saved.translated_cues[0].text == "Có duyên ắt gặp"
        undo = service.subtitle_command(project_id="gesture", command="undo", compact=True)
        model._action_succeeded("subtitle_command", undo, {})
        redo = service.subtitle_command(project_id="gesture", command="redo", compact=True)
        model._action_succeeded("subtitle_command", redo, {})
        assert service.projects.load("gesture").source_cues[0].range == saved.source_cues[0].range
        print("PASS: fine move/resize, background ACK, release coordinates, 20 edits, reopen/undo")
        return 0
    finally:
        delete(engine)
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
