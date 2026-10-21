"""Offscreen QML gesture stress on a deterministic large synthetic timeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPointF,
    QSettings,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def main() -> int:
    configure_qt_plugin_dll_search()
    app = QApplication([])
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    report_path = Path(sys.argv[1]).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    settings = QSettings(str(report_path.with_suffix(".ini")), QSettings.Format.IniFormat)
    model = ApplicationViewModel(app)
    model.settings = settings
    model.runtime.start = lambda: None
    model.runtime.request = lambda *args, **kwargs: None
    count = 12_000
    cues = [
        {
            "id": f"c{i}",
            "index": i + 1,
            "text": f"Câu thoại số {i}",
            "range": {"start_us": i * 1_000_000, "end_us": i * 1_000_000 + 900_000},
        }
        for i in range(count)
    ]
    clips = [
        {"id": f"clip-{i}", "track_id": "S1", "cue_id": f"c{i}", "timeline_range": cue["range"]}
        for i, cue in enumerate(cues)
    ]
    scene_tracks = []
    for lane in range(2, 8):
        scene_cues = [
            {
                "id": f"s{lane}-{i}",
                "index": count + lane * 300 + i,
                "text": "Tên cảnh",
                "role": "scene_text",
                "subtitle_track_id": f"S{lane}",
                "visual_box": [0.1 * lane, 0.15, 0.08, 0.08],
                "visual_box_source": "ocr",
                "range": {"start_us": i * 30_000_000, "end_us": i * 30_000_000 + 2_000_000},
            }
            for i in range(300)
        ]
        cues.extend(scene_cues)
        scene_tracks.append(
            {
                "id": f"S{lane}",
                "kind": "subtitle",
                "name": "Chữ trong cảnh",
                "clips": [
                    {
                        "id": cue["id"],
                        "track_id": f"S{lane}",
                        "cue_id": cue["id"],
                        "timeline_range": cue["range"],
                    }
                    for cue in scene_cues
                ],
            }
        )
    snapshot = {
            "id": "stress",
            "name": "12k stress",
            "revision": 1,
            "source_cues": cues,
            "translated_cues": [{**cue, "language": "vi"} for cue in cues],
            "tracks": [
                {"id": "S1", "kind": "subtitle", "name": "Phụ đề thoại", "clips": clips},
                *scene_tracks,
                *[
                    {"id": f"S{i}", "kind": "subtitle", "name": "Chữ trong cảnh", "clips": []}
                    for i in range(8, 50)
                ],
            ],
        }
    s1 = next(track for track in snapshot["tracks"] if track["id"] == "S1")
    count = len(s1["clips"])
    first_cue_id, last_cue_id = s1["clips"][0]["cue_id"], s1["clips"][-1]["cue_id"]
    first_clip_id = s1["clips"][0]["id"]
    model.project_state.replace(snapshot, take_ownership=True)
    engine = QQmlApplicationEngine(model)
    engine.warnings.connect(
        lambda warnings: print("\n".join(error.toString() for error in warnings), file=sys.stderr)
    )
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    qml_root = Path(__file__).parents[2] / "src/ducai_studio/ui/qml"
    engine.addImportPath(str(qml_root))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str((qml_root / "Main.qml").resolve())))
    window = engine.rootObjects()[0]
    window.setWidth(1180)
    window.setHeight(720)
    window.show()
    model.navigate("timeline")
    timeline = window.findChild(QObject, "timelineScreen")
    model.runtime.request = lambda *args, **kwargs: None
    timers = []
    delays = []
    # An offscreen window has no display-driven frame cadence. Drive actual
    # rendering so QML's animation-synchronized viewport timers keep running
    # during the gesture burst, as they do in the visible application.
    # Rendering cost stays inside the heartbeat measurement.
    frame_pump = QTimer(app)
    frame_pump.setInterval(16)
    frame_pump.timeout.connect(window.grabWindow)
    frame_pump.start()
    heartbeat = QTimer(app)
    heartbeat.setInterval(10)
    previous = perf_counter()

    def tick():
        nonlocal previous
        current = perf_counter()
        delays.append((current - previous) * 1000)
        previous = current

    heartbeat.timeout.connect(tick)
    heartbeat.start()
    report = {}

    def select():
        nonlocal previous
        start = perf_counter()
        timeline.applyViewport(0, int(model.project_state.durationUs))
        timeline.syncTimelineViewportModels()
        report["fit_all_sync_ms"] = (perf_counter() - start) * 1000
        # A timer wait alone does not complete offscreen Qt Quick polish.
        # Realize the viewport before computing pointer positions, otherwise
        # the first press includes deferred layout/rendering instead of only
        # the selection gesture. Record that setup cost separately.
        settle_start = perf_counter()
        QTest.qWait(50)
        assert not window.grabWindow().isNull()
        # Rendering can arm the 24/40 ms viewport synchronizers. Let those
        # finish and render their resulting delegates before starting input.
        QTest.qWait(50)
        assert not window.grabWindow().isNull()
        app.processEvents()
        report["viewport_settle_ms"] = (perf_counter() - settle_start) * 1000
        delays.clear()
        previous = perf_counter()
        # Drag the actual blank lane MouseArea across S1 at fit-all zoom.
        start = perf_counter()
        area = timeline.findChild(QObject, "timelineMarqueeArea")
        assert area is not None
        lane = model.project_state.subtitleTrackIndexForCueId(first_cue_id)
        track_list = timeline.findChild(QObject, "timelineTrackList")
        assert track_list is not None
        # Real projects have V1/A1/title rows above S1 and a video Preview.
        # Scroll S1 into the actual track viewport before the gesture, never
        # send input to the off-screen lane coordinates.
        maximum_y = max(0, track_list.property("contentHeight") - track_list.height())
        track_list.setProperty("contentY", min(lane * 62, maximum_y))
        QTest.qWait(50)
        timeline.syncTimelineViewportModels()
        offset = track_list.property("contentY")
        top = max(1, lane * 62 - offset + 1)
        bottom = min(area.height() - 1, lane * 62 - offset + 58)
        assert 0 <= top < bottom < area.height(), (lane, top, bottom, area.height())
        first = area.mapToScene(QPointF(0, top)).toPoint()
        last = area.mapToScene(QPointF(area.width(), bottom)).toPoint()
        delays.clear()
        previous = perf_counter()
        start = perf_counter()
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, first)
        report["press_ms"] = (perf_counter() - start) * 1000
        QTest.mouseMove(window, last, 1)
        report["move_ms"] = (perf_counter() - start) * 1000
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, last)
        report["selection_ms"] = (perf_counter() - start) * 1000
        report["selected_count"] = model.selectedCueCount
        assert model.selectedCueCount == count, (
            model.selectedCueCount, count, lane, first, last, area.width(), area.height()
        )
        QTimer.singleShot(50, format_cues)

    def format_cues():
        start = perf_counter()
        model.setSelectedSubtitleTextFormat(16, "horizontal", "", "", "standard")
        report["bulk_style_ms"] = (perf_counter() - start) * 1000
        assert model.project_state.subtitle_cues.find(last_cue_id)["text_font_size"] == 16
        for i in range(100):
            timer = QTimer(app)
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda i=i: timeline.applyViewport(
                    i * 30_000_000, (60_000_000 if i % 2 else 120_000_000)
                )
            )
            timer.start(i * 15)
            timers.append(timer)
        QTimer.singleShot(1800, finish)

    def finish():
        report["max_heartbeat_gap_ms"] = max(delays)
        report["delegate_count"] = model.project_state.clips.count
        timeline.applyViewport(0, 60_000_000)
        timeline.syncTimelineViewportModels()
        QTest.qWait(50)
        # Repeater delegates are QObject-owned by the delegate model, not the
        # screen. Their visual parent tree is the interaction authority.
        pending = [timeline]
        body = None
        while pending:
            item = pending.pop()
            if item.objectName() == "timelineClipBody-" + first_clip_id:
                body = item
                break
            pending.extend(item.childItems())
        assert body is not None
        point = body.mapToScene(QPointF(body.width() / 2, body.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, point)
        assert timeline.property("openContextMenus") == 1
        previous_items = [item["id"] for item in model.project_state.clips._items]
        timeline.applyViewport(3_000_000_000, 60_000_000)
        timeline.syncTimelineViewportModels()
        assert [item["id"] for item in model.project_state.clips._items] == previous_items
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert timeline.property("openContextMenus") == 0
        report["context_menu_verified"] = True
        # Drag slowly in fixed window coordinates. A moving-thumb coordinate
        # delta used to alternate between forward and backward viewport jumps.
        timeline.applyViewport(0, 60_000_000)
        timeline.syncTimelineViewportModels()
        QTest.qWait(50)
        overview = timeline.findChild(QObject, "timelineOverview")
        drag = timeline.findChild(QObject, "timelineOverviewDrag")
        window.grabWindow().save(str(report_path.with_suffix(".before.png")))
        point = drag.mapToScene(QPointF(drag.width() / 2, drag.height() / 2)).toPoint()
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        assert drag.property("pressed"), "Overview must remain inside the interactive SplitView"
        positions = []
        for step in range(1, 21):
            QTest.mouseMove(window, point + QPointF(step, 0).toPoint(), 10)
            app.processEvents()
            positions.append(timeline.property("viewportStartUs"))
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           point + QPointF(20, 0).toPoint())
        assert all(a <= b for a, b in zip(positions, positions[1:], strict=False)), positions
        assert positions[-1] > 0, positions
        expected = 20 / overview.width() * timeline.property("projectDurationUs")
        assert abs(positions[-1] - expected) < 2, (positions, expected)
        report["overview_monotonic"] = True
        report["overview_positions_us"] = positions
        window.grabWindow().save(str(report_path.with_suffix(".png")))
        write_report()

    def write_report():
        report_path.write_text(json.dumps(report), encoding="utf-8")
        print(json.dumps(report), flush=True)
        assert report["selection_ms"] < 500
        assert report["bulk_style_ms"] < 500
        assert report["max_heartbeat_gap_ms"] < 750
        assert report["delegate_count"] <= 480
        app.quit()

    QTimer.singleShot(250, select)
    QTimer.singleShot(10_000, lambda: app.exit(9))
    result = app.exec()
    frame_pump.stop()
    window.close()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
