from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QPointF,
    QSettings,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def main() -> int:
    media_path = Path(os.environ["DUCAI_PREVIEW_FIXTURE"]).resolve()
    QStandardPaths.setTestModeEnabled(True)
    QCoreApplication.setOrganizationName("DUCAI STUDIO TEST")
    QCoreApplication.setApplicationName("preview-smoke")
    QGuiApplication.setDesktopFileName("ducai-preview-smoke")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QQuickStyle.setStyle("Basic")

    app = QApplication(sys.argv)
    project_root = Path(__file__).parents[2]
    qml_root = project_root / "src" / "ducai_studio" / "ui" / "qml"
    app_model = ApplicationViewModel(app)
    app_model.project_state.replace(
        {
            "id": "preview-smoke",
            "name": "Preview smoke",
            "revision": 1,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_200_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
            "media": [
                {"id": "media", "kind": "video", "path": str(media_path)},
                {"id": "voice-media", "kind": "audio", "path": str(media_path)},
            ],
            "tracks": [
                {
                    "id": "V1",
                    "kind": "video",
                    "name": "Video 1",
                    "clips": [
                        {
                            "id": "clip",
                            "track_id": "V1",
                            "media_id": "media",
                            "timeline_range": {"start_us": 0, "end_us": 6_000_000},
                            "source_range": {"start_us": 0, "end_us": 3_000_000},
                            "speed": {"numerator": 1, "denominator": 2},
                            "volume": 0.0,
                        }
                    ],
                },
                {
                    "id": "S1",
                    "kind": "subtitle",
                    "name": "Subtitle",
                    "clips": [
                        {
                            "id": "cue",
                            "track_id": "S1",
                            "timeline_range": {"start_us": 179_000_000, "end_us": 180_000_000},
                            "speed": {"numerator": 1, "denominator": 1},
                            "volume": 1.0,
                        }
                    ],
                },
                {
                    "id": "D1",
                    "kind": "voice",
                    "name": "Voice",
                    "clips": [
                        {
                            "id": "voice-000001-cue-1",
                            "track_id": "D1",
                            "media_id": "voice-media",
                            "timeline_range": {"start_us": 0, "end_us": 1_200_000},
                            "source_range": {"start_us": 0, "end_us": 3_000_000},
                            "speed": {"numerator": 5, "denominator": 2},
                            "volume": 0.75,
                        }
                    ],
                },
            ],
        }
    )

    engine = QQmlApplicationEngine(app)
    engine.addImportPath(str(qml_root))
    engine.setInitialProperties({"appModel": app_model})
    engine.load(QUrl.fromLocalFile(str(qml_root / "Main.qml")))
    if not engine.rootObjects():
        return 10
    window = engine.rootObjects()[0]
    window.setWidth(1180)
    window.setHeight(720)
    window.show()
    app_model.navigate("timeline")
    preview = window.findChild(QObject, "timelinePreview")
    if preview is None:
        return 11
    timeline = window.findChild(QObject, "timelineScreen")
    if timeline is None:
        return 17
    timeline.showProjectStart()
    timeline.zoomBy(2 / 3)
    viewport_duration = float(timeline.property("viewportDurationUs"))
    app_model.project_state.replace(
        {
            "id": "preview-smoke",
            "name": "Preview smoke",
            "revision": 2,
            "source_cues": [
                {
                    "id": "cue-1",
                    "index": 1,
                    "range": {"start_us": 0, "end_us": 1_200_000},
                    "text": "第一句",
                    "source": "paddle",
                    "revision": 1,
                }
            ],
            "media": [
                {"id": "media", "kind": "video", "path": str(media_path)},
                {"id": "voice-media", "kind": "audio", "path": str(media_path)},
            ],
            "tracks": [
                {
                    "id": "V1",
                    "kind": "video",
                    "name": "Video 1",
                    "clips": [
                        {
                            "id": "clip",
                            "track_id": "V1",
                            "media_id": "media",
                            "timeline_range": {"start_us": 0, "end_us": 6_000_000},
                            "source_range": {"start_us": 0, "end_us": 3_000_000},
                            "speed": {"numerator": 1, "denominator": 2},
                            "volume": 0.5,
                        }
                    ],
                },
                {
                    "id": "S1",
                    "kind": "subtitle",
                    "name": "Subtitle",
                    "clips": [
                        {
                            "id": "cue",
                            "track_id": "S1",
                            "timeline_range": {"start_us": 179_000_000, "end_us": 180_000_000},
                            "speed": {"numerator": 1, "denominator": 1},
                            "volume": 1.0,
                        }
                    ],
                },
                {
                    "id": "D1",
                    "kind": "voice",
                    "name": "Voice",
                    "clips": [
                        {
                            "id": "voice-000001-cue-1",
                            "track_id": "D1",
                            "media_id": "voice-media",
                            "timeline_range": {"start_us": 0, "end_us": 1_200_000},
                            "source_range": {"start_us": 0, "end_us": 3_000_000},
                            "speed": {"numerator": 5, "denominator": 2},
                            "volume": 0.75,
                        }
                    ],
                },
            ],
        }
    )
    if abs(float(timeline.property("viewportDurationUs")) - viewport_duration) > 1:
        return 18

    state: dict[str, float | int] = {"attempts": 0}

    def fail(code: int) -> None:
        app.exit(code)

    def wait_until_loaded() -> None:
        state["attempts"] = int(state["attempts"]) + 1
        if float(preview.property("durationMs") or 0) > 0:
            loader.stop()
            state["before_play"] = float(preview.property("positionMs") or 0)
            state["play_attempts"] = 0
            preview.play()
            QTimer.singleShot(200, verify_playing)
            return
        if int(state["attempts"]) >= 60:
            fail(12)

    def verify_playing() -> None:
        state["play_attempts"] = int(state["play_attempts"]) + 1
        position = float(preview.property("positionMs") or 0)
        source_position = float(preview.property("sourcePositionMs") or 0)
        before = float(state["before_play"])
        if (
            position <= before + 150
            or app_model.playheadUs <= 150_000
            or source_position <= 50
            or float(preview.property("voicePositionMs") or 0) <= 50
            or position < source_position * 1.7
        ):
            if int(state["play_attempts"]) < 20:
                QTimer.singleShot(150, verify_playing)
                return
            print(
                "preview did not advance",
                {
                    "before": before,
                    "position": position,
                    "source_position": source_position,
                    "playhead_us": app_model.playheadUs,
                    "playback_state": preview.property("playbackState"),
                    "media_status": preview.property("mediaStatus"),
                    "duration": preview.property("durationMs"),
                },
                file=sys.stderr,
            )
            fail(13)
            return
        state["before_navigation"] = position
        app_model.navigate("voice")
        QTimer.singleShot(450, verify_navigation)

    def verify_navigation() -> None:
        position = float(preview.property("positionMs") or 0)
        if position <= float(state["before_navigation"]) + 100:
            fail(14)
            return
        app_model.navigate("timeline")
        preview.seek(1_200)
        QTimer.singleShot(120, pause_preview)

    def pause_preview() -> None:
        preview.pause()
        state["paused_at"] = float(preview.property("positionMs") or 0)
        state["voice_paused_at"] = float(preview.property("voicePositionMs") or 0)
        preview.toggleMute()
        if preview.property("muted") is not True:
            fail(19)
            return
        QTimer.singleShot(350, verify_paused)

    def verify_paused() -> None:
        position = float(preview.property("positionMs") or 0)
        voice_position = float(preview.property("voicePositionMs") or 0)
        if (
            abs(position - float(state["paused_at"])) > 120
            or abs(voice_position - float(state["voice_paused_at"])) > 120
        ):
            fail(15)
            return
        preview.toggleMute()
        if preview.property("muted") is not False:
            fail(20)
            return
        def visual_descendants(item: QObject) -> list[QObject]:
            children = list(item.childItems()) if hasattr(item, "childItems") else []
            return children + [
                descendant
                for child in children
                for descendant in visual_descendants(child)
            ]

        track_list = window.findChild(QObject, "timelineTrackList")
        if track_list is None:
            fail(23)
            return
        maximum_content_y = max(
            0.0,
            float(track_list.property("contentHeight") or 0)
            - float(track_list.property("height") or 0),
        )
        track_list.setProperty("contentY", maximum_content_y)
        QCoreApplication.processEvents()
        # ListView may recycle delegates while a vertical scroll is applied.
        # Resolve the click target after scrolling instead of keeping a stale
        # QObject whose old scene coordinates now belong to the blank layer.
        visual_items = visual_descendants(window.contentItem())
        body = next(
            (
                item
                for item in visual_items
                if item.objectName() == "timelineClipBody-voice-000001-cue-1"
            ),
            None,
        )
        if body is None:
            print(
                "timeline clip bodies",
                [
                    item.objectName()
                    for item in visual_items
                    if str(item.objectName()).startswith("timelineClip")
                ],
                file=sys.stderr,
            )
            fail(21)
            return
        centre = body.mapToScene(QPointF(body.width() / 2, body.height() / 2)).toPoint()
        QTest.mouseDClick(
            window,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            centre,
            20,
        )
        QTimer.singleShot(200, verify_voice_double_click)

    def verify_voice_double_click() -> None:
        if app_model.currentWorkspace != "translate" or app_model.selectedCueId != "cue-1":
            print(
                "voice double click state "
                + ascii(
                    {
                    "workspace": app_model.currentWorkspace,
                    "selected_cue": app_model.selectedCueId,
                    "playhead_us": app_model.playheadUs,
                    "status": app_model.statusMessage,
                    }
                ),
                file=sys.stderr,
            )
            fail(22)
            return
        app.quit()

    loader = QTimer(app)
    loader.setInterval(100)
    loader.timeout.connect(wait_until_loaded)
    loader.start()
    QTimer.singleShot(8_000, lambda: fail(16))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
