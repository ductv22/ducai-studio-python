"""Native isolated title/real-media interaction and save/reopen verification."""

import faulthandler
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QPointF, QSettings, QStandardPaths, Qt, QTimer
from PySide6.QtQml import QQmlApplicationEngine, QQmlProperty, qmlContext
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


def item_data(item):
    value = item.property("modelData")
    return value.toVariant() if hasattr(value, "toVariant") else value


def main():
    faulthandler.dump_traceback_later(20, repeat=True)
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "source.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=854x480:rate=25",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.3*sin(2*PI*440*t)*(0.5+0.5*sin(2*PI*0.8*t)):s=48000",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        capture_output=True,
        check=True,
    )
    configure_qt_plugin_dll_search()
    app = QApplication([])

    def wait(duration_ms):
        # Native rendering may await Python image-provider callbacks. Unlike
        # QTest.qWait in these bindings, exec releases the interpreter lock.
        loop = QEventLoop()
        QTimer.singleShot(duration_ms, loop.quit)
        loop.exec()

    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    _ensure_fonts()
    model = ApplicationViewModel(app)
    model.settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
    model.runtime.start = lambda: None
    project = {
        "id": "title-fixture",
        "name": "Independent Text / real media",
        "revision": 1,
        "media": [{"id": "m", "kind": "video", "path": str(source), "duration_us": 6_000_000}],
        "tracks": [
            {
                "id": track,
                "kind": kind,
                "name": track,
                "clips": [
                    {
                        "id": clip,
                        "track_id": track,
                        "media_id": "m",
                        "link_group_id": "av",
                        "timeline_range": {"start_us": 0, "end_us": 6_000_000},
                        "source_range": {"start_us": 0, "end_us": 6_000_000},
                    }
                ],
            }
            for track, kind, clip in (("V1", "video", "video"), ("A1", "original_audio", "audio"))
        ],
    }
    model.project_state.replace(project)
    service = JobManagerService(
        database_path=directory / "state.db", artifact_root=directory / "artifacts"
    )
    service.projects.save(
        project_path=directory / "fixture.ducai", snapshot=decode_project_snapshot(project)
    )
    requests = []
    failures = []

    def request(action, payload=None, context=None):
        requests.append((action, payload, context))

        def acknowledge():
            try:
                if action == "subtitle_command":
                    result = service.subtitle_command(
                        project_id=payload["project_id"],
                        command=payload["command"],
                        cue_id=payload.get("cue_id"),
                        language=payload["language"],
                        params=payload["values"],
                        compact=True,
                        deferred=True,
                    )
                elif action == "timeline_command":
                    result = service.timeline_command(**payload, deferred=True)
                elif action == "save_project_edits":
                    result = service.save_project_edits(payload["project_id"])
                else:
                    return
                model._action_succeeded(action, result, context)
            except Exception as exc:
                failures.append(str(exc))
                model._action_failed(action, {"message": str(exc)}, context)

        QTimer.singleShot(0, acknowledge)

    model.runtime.request = request
    qml = Path(
        os.environ.get("DUCAI_TEST_QML_ROOT")
        or (Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml")
    )
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
    model.setPlayheadMs(1000)
    wait(500)

    def locate(name):
        return next(
            i for i in descendants(window.contentItem()) if i.objectName() == name and i.isVisible()
        )

    def point(item):
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()

    def click(item, modifier=Qt.NoModifier):
        QTest.mouseClick(window, Qt.LeftButton, modifier, point(item))
        app.processEvents()

    def type_text(value):
        for char in value:
            QTest.keyClick(
                window,
                Qt.Key(ord(char.upper())),
                Qt.ShiftModifier if char.isupper() else Qt.NoModifier,
            )

    preview = locate("timelinePreview")
    timeline = locate("timelineScreen")
    try:
        click(locate("addTimelineTitleButton"))
        dialog = next(
            d
            for d in window.findChildren(QObject, "manualSubtitleCueDialog")
            if d.property("visible")
        )
        assert dialog.property("visible") and dialog.property("titleMode")
        text_input = locate("manualCueTextInput")
        click(text_input)
        type_text("DUCAI Studio")
        duration = locate("titleDurationField")
        click(duration)
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        type_text("1.250")
        QTest.keyClick(window, Qt.Key_Tab)
        click(locate("confirmNewCueButton"))
        wait(350)
        assert not failures, failures
        first = service.projects.load(project["id"]).title_cues[0]
        assert first.range.start_us == 1_000_000 and first.range.end_us == 2_250_000
        assert not service.projects.load(project["id"]).source_cues
        assert model.selectedCueId == first.id and not model.subtitleBusy
        assert model.project_state.tracks._items[0]["id"] == "T1"

        # A second independent title covers the complete video.
        click(locate("addTimelineTitleButton"))
        click(locate("manualCueTextInput"))
        type_text("Channel label")
        click(locate("titleFullVideoButton"))
        click(locate("confirmNewCueButton"))
        wait(350)
        second = service.projects.load(project["id"]).title_cues[0]
        assert second.range.start_us == 0 and second.range.end_us == 6_000_000
        assert model.project_state.title_cues.count == 2
        model.selectTimelineCue(first.id)
        model.selectAllSubtitleCues()
        assert model.selectedCueCount == 2
        model.setSelectedSubtitleTextFormat(64, "horizontal", "#000000", "#FFFFFF", "display")
        wait(400)
        assert all(c.text_font_size == 64 for c in service.projects.load(project["id"]).title_cues)
        model.selectTimelineCue(first.id)
        assert model.selectedCueCount == 1
        before = model.playheadUs
        clip_id = next(
            c["id"] for c in model.project_state._clip_by_id.values() if c.get("cue_id") == first.id
        )
        body = locate("timelineClipBody-" + clip_id)
        tooltip = QQmlProperty(body.parentItem(), "ToolTip.text", qmlContext(body.parentItem()))
        assert tooltip.isValid()
        tooltip_text = tooltip.read()
        assert tooltip_text.startswith("Text #") and first.text in tooltip_text
        assert "SRT" not in tooltip_text
        click(body)
        assert model.playheadUs == before and model.selectedCueId == first.id, (
            before,
            model.playheadUs,
            first.id,
            model.selectedCueId,
        )

        # Timeline duration and position gestures commit once at release.
        def drag_timing(item, dx):
            initial_point = point(item)
            count_before = len([r for r in requests if r[0] == "subtitle_command"])
            QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, initial_point)
            QTest.mouseMove(window, initial_point + QPointF(dx, 0).toPoint())
            assert len([r for r in requests if r[0] == "subtitle_command"]) == count_before
            QTest.mouseRelease(
                window, Qt.LeftButton, Qt.NoModifier, initial_point + QPointF(dx, 0).toPoint()
            )
            wait(300)
            assert len([r for r in requests if r[0] == "subtitle_command"]) == count_before + 1
            assert not failures, failures

        drag_timing(locate("timelineClipRightHandle-" + clip_id), 24)
        resized = next(
            c for c in service.projects.load(project["id"]).title_cues if c.id == first.id
        )
        assert resized.range.start_us == first.range.start_us
        assert resized.range.end_us > first.range.end_us
        drag_timing(locate("timelineClipBody-" + clip_id), -24)
        timed = next(c for c in service.projects.load(project["id"]).title_cues if c.id == first.id)
        assert timed.range.start_us < resized.range.start_us
        assert timed.range.duration_us == resized.range.duration_us
        assert model.playheadUs == before and model.selectedCueId == first.id
        # The actual Preview drag sends one command, preserves text size, and saves geometry.
        frame = next(
            i
            for i in descendants(window.contentItem())
            if i.isVisible()
            and i.metaObject().className().startswith("QQuickMouseArea")
            and i.z() == 220
            and i.parentItem()
            and item_data(i.parentItem()).get("cue_id") == first.id
        )
        initial = point(frame)
        count = len([r for r in requests if r[0] == "subtitle_command"])
        QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, initial)
        QTest.mouseMove(window, initial + QPointF(15, 10).toPoint())
        assert len([r for r in requests if r[0] == "subtitle_command"]) == count
        QTest.mouseRelease(
            window, Qt.LeftButton, Qt.NoModifier, initial + QPointF(15, 10).toPoint()
        )
        wait(400)
        assert not failures, failures
        moved = next(c for c in service.projects.load(project["id"]).title_cues if c.id == first.id)
        assert moved.panel_box != first.panel_box and moved.text_font_size == 64
        # Ctrl+S flushes actual manager state; a separate service can reopen it.
        timeline.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_S, Qt.ControlModifier)
        wait(250)
        reopened = JobManagerService(
            database_path=directory / "state.db", artifact_root=directory / "artifacts"
        ).projects.load(project["id"])
        assert reopened.title_cues == service.projects.load(project["id"]).title_cues
        model.project_state.replace(
            json.loads(json.dumps(__import__("dataclasses").asdict(reopened)))
        )
        model.selectTimelineCue(first.id)
        wait(100)
        click(locate("timelineClipBody-" + clip_id))
        QTest.keyClick(window, Qt.Key_Delete)
        wait(250)
        assert model.project_state.title_cues.count == 1 and not model.subtitleBusy
        QTest.keyClick(window, Qt.Key_Z, Qt.ControlModifier)
        wait(250)
        assert model.project_state.title_cues.count == 2
        QTest.keyClick(window, Qt.Key_Y, Qt.ControlModifier)
        wait(250)
        assert model.project_state.title_cues.count == 1
        assert not failures, failures
        wait(350)
        assert list((directory / "tiles/thumbnail").rglob("*.png")), "No real thumbnail"
        assert list((directory / "tiles/waveform").rglob("*.png")), "No real waveform"
        window.grabWindow().save(str(directory / "timeline.png"))
        # Scroll real tracks into view and verify the Canvas actually paints
        # waveform pixels; a disk cache alone is not proof of visible UI.
        track_list = locate("timelineTrackList")
        track_list.setProperty("contentY", 62)
        wait(500)
        audio_body = locate("timelineClipBody-audio")
        top_left = audio_body.mapToScene(QPointF(0, 0))
        capture = window.grabWindow()
        scale = capture.width() / window.width()
        greens = 0
        for y in range(
            int(top_left.y() * scale), int((top_left.y() + audio_body.height()) * scale)
        ):
            for x in range(
                int(top_left.x() * scale), int((top_left.x() + audio_body.width()) * scale)
            ):
                color = capture.pixelColor(x, y)
                greens += (
                    color.green() > 100
                    and color.green() > color.red() + 25
                    and color.green() > color.blue() + 10
                )
        assert greens > 100, f"Waveform Canvas did not paint: {greens}"
        capture.save(str(directory / "timeline-media.png"))
        report = {
            "status": "PASS",
            "track": "T1",
            "precise_duration_us": 1_250_000,
            "native_add_full_video": True,
            "bulk_style": True,
            "seek_free_selection": True,
            "independent_title_tooltip": True,
            "timeline_move_resize": True,
            "preview_drag_saved": True,
            "manual_save_reopen": True,
            "delete_undo_redo": True,
            "actual_media_tiles": True,
            "visible_waveform_pixels": greens,
            "subtitle_job_inputs_empty": True,
        }
        (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
        print(json.dumps(report))
        return 0
    finally:
        preview.pause()
        window.setProperty("shutdownComplete", True)
        window.close()
        delete(engine)
        app.processEvents()
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
