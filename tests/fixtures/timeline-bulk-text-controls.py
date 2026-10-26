"""Real QML track-header bulk typography and selected scene acceptance regression."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QSettings, QStandardPaths, Qt, QUrl
from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from ducai_studio.infrastructure.project_repository import decode_project_snapshot
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def descendants(item):
    yield item
    for child in item.childItems():
        yield from descendants(child)


def main() -> int:
    configure_qt_plugin_dll_search()
    app = QApplication([])
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)

    model = ApplicationViewModel(app)
    model.settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
    model.runtime.start = lambda: None
    model.license_model._status = {"active": True, "message": "Fixture"}
    ranges = {
        "dialogue-s1": {"start_us": 0, "end_us": 900_000},
        "scene-1": {"start_us": 1_000_000, "end_us": 1_900_000},
        "scene-2": {"start_us": 2_000_000, "end_us": 2_900_000},
        "dialogue-on-s2": {"start_us": 3_000_000, "end_us": 3_900_000},
        "locked-scene": {"start_us": 4_000_000, "end_us": 4_900_000},
        "title-1": {"start_us": 0, "end_us": 2_000_000},
        "title-2": {"start_us": 2_000_000, "end_us": 4_000_000},
    }
    source_cues = [
        {"id": "dialogue-s1", "index": 1, "text": "对白", "language": "zh",
         "role": "dialogue",
         "subtitle_track_id": "S1", "range": ranges["dialogue-s1"]},
        {"id": "scene-1", "index": 2, "text": "山河", "language": "zh",
         "role": "scene_text",
         "subtitle_track_id": "S2", "review_state": "review",
         "visual_box": [0.2, 0.2, 0.2, 0.1], "panel_box": [0.2, 0.2, 0.2, 0.1],
         "visual_box_source": "ocr",
         "range": ranges["scene-1"]},
        {"id": "scene-2", "index": 3, "text": "禁地", "language": "zh",
         "role": "scene_text",
         "subtitle_track_id": "S2", "review_state": "review",
         "visual_box": [0.5, 0.2, 0.2, 0.1], "panel_box": [0.5, 0.2, 0.2, 0.1],
         "visual_box_source": "ocr",
         "range": ranges["scene-2"]},
        # A dialogue cue may be moved to S2 without changing role. It is the
        # sentinel proving that acceptance filters by role rather than lane.
        {"id": "dialogue-on-s2", "index": 4, "text": "旁白", "language": "zh",
         "role": "dialogue",
         "subtitle_track_id": "S2", "review_state": "accepted",
         "range": ranges["dialogue-on-s2"]},
        {"id": "locked-scene", "index": 5, "text": "封印", "language": "zh",
         "role": "scene_text",
         "subtitle_track_id": "S3", "review_state": "review",
         "visual_box": [0.3, 0.5, 0.2, 0.1], "panel_box": [0.3, 0.5, 0.2, 0.1],
         "visual_box_source": "ocr",
         "range": ranges["locked-scene"]},
    ]
    title_cues = [
        {"id": cue_id, "index": index, "text": text, "language": "vi",
         "role": "title_text",
         "subtitle_track_id": "T1", "visual_box": [0.1, 0.1, 0.3, 0.1],
         "panel_box": [0.1, 0.1, 0.3, 0.1], "visual_box_source": "estimated",
         "range": ranges[cue_id]}
        for index, (cue_id, text) in enumerate(
            (("title-1", "Tên phim"), ("title-2", "Tên kênh")), 1
        )
    ]
    cue_index_by_id = {
        cue["id"]: cue["index"] for cue in [*source_cues, *title_cues]
    }

    def track(track_id, kind, cue_ids=(), *, locked=False):
        return {
            "id": track_id,
            "kind": kind,
            "name": track_id,
            "locked": locked,
            "clips": [
                {"id": f"clip-{cue_id}", "track_id": track_id, "cue_id": cue_id,
                 "cue_index": cue_index_by_id[cue_id], "timeline_range": ranges[cue_id]}
                for cue_id in cue_ids
            ],
        }

    def media_track(track_id, kind):
        return {
            "id": track_id,
            "kind": kind,
            "name": track_id,
            "clips": [{
                "id": "clip-" + track_id.lower(),
                "track_id": track_id,
                "media_id": "media-1",
                "link_group_id": "media-av",
                "timeline_range": {"start_us": 0, "end_us": 5_000_000},
                "source_range": {"start_us": 0, "end_us": 5_000_000},
            }],
        }

    project = {
        "id": "bulk-text-controls",
        "name": "Bulk text controls",
        "revision": 1,
        "source_cues": source_cues,
        "translated_cues": [
            {**cue, "id": "vi-" + cue["id"], "language": "vi", "text": "Bản dịch"}
            for cue in source_cues
        ],
        "title_cues": title_cues,
        "media": [{
            "id": "media-1",
            "kind": "video",
            "path": str(directory / "source.mp4"),
            "duration_us": 5_000_000,
        }],
        "tracks": [
            media_track("V1", "video"),
            media_track("A1", "original_audio"),
            track("T1", "text", ("title-1", "title-2")),
            track("S1", "subtitle", ("dialogue-s1",)),
            track("S2", "subtitle", ("scene-1", "scene-2", "dialogue-on-s2")),
            track("S3", "subtitle", ("locked-scene",), locked=True),
        ],
    }
    model.project_state.replace(project)
    service = JobManagerService(
        database_path=directory / "state.db", artifact_root=directory / "artifacts"
    )
    service.projects.save(
        project_path=directory / "fixture.ducai",
        snapshot=decode_project_snapshot(project),
    )
    requests: list[tuple[str, dict, object]] = []
    model.runtime.request = lambda action, payload=None, context=None: requests.append(
        (action, payload or {}, context)
    )

    qml_root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.addImportPath(str(qml_root))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml_root / "Main.qml")))
    try:
        window = engine.rootObjects()[0]
        window.setWidth(1992)
        window.setHeight(1284)
        window.show()
        model.navigate("timeline")
        QTest.qWait(250)
        timeline = window.findChild(QObject, "timelineScreen")
        assert timeline is not None
        track_list = window.findChild(QObject, "timelineTrackList")
        assert track_list is not None, "Timeline track list was not created"
        timeline.syncTimelineViewportModels()
        QTest.qWait(100)

        def visible(name: str):
            return next(
                item for item in descendants(window.contentItem())
                if item.objectName() == name and item.isVisible()
            )

        def header(track_id: str):
            headers = [
                item for item in descendants(window.contentItem())
                if item.objectName() == "timelineTrackHeaderMouseArea"
            ]
            match = next(
                (item for item in headers if item.property("trackId") == track_id), None
            )
            assert match is not None, (
                track_id,
                [item.property("trackId") for item in headers],
                timeline.property("visible"),
                timeline.property("width"),
                timeline.property("height"),
                track_list.property("count"),
                track_list.property("width"),
                track_list.property("height"),
            )
            return match

        def click(item, button=Qt.MouseButton.LeftButton,
                  modifiers=Qt.KeyboardModifier.NoModifier):
            point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
            QTest.mouseClick(window, button, modifiers, point)
            app.processEvents()

        def acknowledge_last_request():
            action, payload, context = requests[-1]
            assert action == "subtitle_command", (action, payload)
            patch = service.subtitle_command(
                project_id=payload["project_id"],
                command=payload["command"],
                cue_id=payload.get("cue_id"),
                language=payload["language"],
                params=payload["values"],
                compact=True,
            )
            model._action_succeeded(action, patch, context)
            app.processEvents()

        # Existing media header selection and Ctrl additive selection survive.
        click(header("V1"))
        click(header("A1"), modifiers=Qt.KeyboardModifier.ControlModifier)
        assert set(model.selectedTrackIds) == {"V1", "A1"}
        click(header("S2"))
        expected_s2 = ["scene-1", "scene-2", "dialogue-on-s2"]
        assert model.selectedCueIds == expected_s2, (
            model.selectedCueIds,
            model.project_state.track_info("S2"),
            [model.project_state.subtitle_cues.get(row)
             for row in range(model.project_state.subtitle_cues.count)],
        )
        assert set(model.selectedTrackIds) == {"V1", "A1"}
        preserved_media_tracks = sorted(model.selectedTrackIds)
        assert timeline.property("inspectorTab") == "text"
        toolbar = visible("subtitleTextToolbar")
        assert toolbar.property("hasCue") and not toolbar.property("selectionLocked")

        # Invoke the real QML toolbar path so orientation/background/padding and
        # size travel together through the same bulk persistence command.
        expression = QQmlExpression(
            engine.rootContext(),
            toolbar,
            'applyCurrent({"fontSize": 36, "orientation": "vertical", '
            '"backgroundColor": "#123456", "paddingX": 44, "paddingY": 55})',
        )
        expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        model._dispatch_background_subtitle_save()
        assert requests[-1][1]["command"] == "set_text_format"
        assert requests[-1][1]["values"]["cue_ids"] == expected_s2
        acknowledge_last_request()
        stored = service.projects.load("bulk-text-controls")
        by_id = {cue.id: cue for cue in stored.source_cues}
        for cue_id in expected_s2:
            cue = by_id[cue_id]
            assert (cue.text_font_size, cue.orientation, cue.background_color) == (
                36, "vertical", "#123456"
            )
            assert (cue.background_padding_x, cue.background_padding_y) == (44, 55)
        assert by_id["dialogue-s1"].text_font_size == 12
        assert by_id["locked-scene"].text_font_size == 12

        # The real selector and Remove Background action must persist explicit
        # choices for a mixed scene/dialogue selection without changing others.
        background_box = toolbar.findChild(QObject, "backgroundStyleBox")
        assert background_box.property("count") == 7
        for background in ("none", "blur"):
            expression = QQmlExpression(
                engine.rootContext(), toolbar, f'chooseBackground("{background}")',
            )
            expression.evaluate()
            assert not expression.hasError(), expression.error().toString()
            for cue_id in expected_s2:
                current = model.project_state.subtitle_cues.find(cue_id)
                assert current["background_style"] == background
            model._dispatch_background_subtitle_save()
            acknowledge_last_request()
            stored = service.projects.load("bulk-text-controls")
            for cue in (*stored.source_cues, *stored.translated_cues):
                if cue.index in {2, 3, 4}:
                    assert cue.background_style == background
            unselected = next(c for c in stored.source_cues if c.id == "dialogue-s1")
            assert unselected.background_style == "solid"
        remove = toolbar.findChild(QObject, "removeCueBackgroundButton")
        expression = QQmlExpression(engine.rootContext(), remove, "clicked()")
        expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        model._dispatch_background_subtitle_save()
        acknowledge_last_request()
        assert all(model.project_state.subtitle_cues.find(cid)["background_style"] == "none"
                   for cid in expected_s2)

        # Existing default background and outline controls now belong to Text.
        default_background = toolbar.findChild(QObject, "subtitleBackgroundMode")
        assert default_background is not None
        expression = QQmlExpression(engine.rootContext(), default_background,
                                   "currentIndex = 2; activated(2)")
        expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        assert model.exportSubtitleBackgroundEnabled
        assert model.exportSubtitleBackgroundMode == "blur"
        expression = QQmlExpression(engine.rootContext(), default_background,
                                   "currentIndex = 1; activated(1)")
        expression.evaluate()
        assert model.exportSubtitleBackgroundMode == "black"
        outline = toolbar.findChild(QObject, "textOutlineWidthSpin")
        expression = QQmlExpression(engine.rootContext(), outline, "value = 7; valueModified()")
        expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        assert model.exportSubtitleOutlineWidth == 7

        # Right-clicking an already selected cue must keep the whole selection;
        # the contextual accept command filters the dialogue sentinel out.
        requests.clear()
        click(visible("timelineClipBody-clip-scene-1"), Qt.MouseButton.RightButton)
        assert model.selectedCueIds == expected_s2
        accept = visible("acceptSelectedSceneTextButton")
        click(accept)
        assert requests, "Context action did not dispatch"
        accepted_ids = requests[-1][1]["values"]["cue_ids"]
        assert accepted_ids == ["scene-1", "scene-2"]
        acknowledge_last_request()
        stored = service.projects.load("bulk-text-controls")
        by_id = {cue.id: cue for cue in stored.source_cues}
        assert all(by_id[cue_id].review_state == "accepted" for cue_id in accepted_ids)
        assert by_id["dialogue-on-s2"].role == "dialogue"

        # Locked text can be inspected and selected, while every mutating text
        # control and the contextual acceptance action remains disabled.
        click(header("S3"))
        assert model.selectedCueIds == ["locked-scene"]
        assert toolbar.property("selectionLocked")
        assert not visible("sizeSpin").property("enabled")
        click(visible("timelineClipBody-clip-locked-scene"), Qt.MouseButton.RightButton)
        locked_accept = visible("acceptSelectedSceneTextButton")
        assert not locked_accept.property("enabled")
        QTest.keyClick(window, Qt.Key.Key_Escape)

        title_header = header("T1")
        title_caption = title_header.mapToScene(
            QPointF(title_header.width() / 2, 8)
        ).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, title_caption)
        app.processEvents()
        assert model.selectedCueIds == ["title-1", "title-2"]
        assert timeline.property("inspectorTab") == "text"
        assert toolbar.property("hasCue")

        report = {
            "status": "PASS",
            "header_selected": expected_s2,
            "styled": expected_s2,
            "accepted": accepted_ids,
            "media_tracks_preserved": preserved_media_tracks,
            "title_header_selected": list(model.selectedCueIds),
        }
        (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
        print(json.dumps(report), flush=True)
        return 0
    finally:
        window = engine.rootObjects()[0] if engine.rootObjects() else None
        if window is not None:
            window.setProperty("shutdownComplete", True)
            window.close()
        delete(engine)
        app.processEvents()
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
