"""Exercise casting audition and list preservation through the real QML screen."""
from __future__ import annotations

import copy
import json
import math
import os
import struct
import sys
import wave
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QStandardPaths, QUrl
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from ducai_studio.ui.app import _register_bundled_fonts
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.viewmodels import application_view_model as application_module


def _write_tone(path: Path, duration_seconds: float = 3.0) -> None:
    sample_rate = 16_000
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        stream.writeframes(
            b"".join(
                struct.pack(
                    "<h",
                    int(1000 * math.sin(2 * math.pi * 440 * index / sample_rate)),
                )
                for index in range(round(sample_rate * duration_seconds))
            )
        )


def _row(index: int, source: Path) -> dict:
    return {
        "id": f"cue-{index}",
        "index": index,
        "text": f"Câu nguồn {index}",
        "translated_text": f"Câu dịch {index}",
        "label": "male",
        "manual": False,
        "reason": "assigned",
        "parts": [
            {
                "media_path": str(source),
                "start_us": 200_000,
                "end_us": 600_000,
            },
            {
                "media_path": str(source),
                "start_us": 1_000_000,
                "end_us": 1_400_000,
            },
        ],
    }


def main() -> int:
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "nguồn thử.wav"
    _write_tone(source)

    configure_qt_plugin_dll_search()
    app = QApplication([])
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    _register_bundled_fonts()
    settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
    original_settings = application_module.QSettings
    application_module.QSettings = lambda *_args: settings
    try:
        model = application_module.ApplicationViewModel(app)
    finally:
        application_module.QSettings = original_settings

    model.project_state.replace(
        {"id": "casting-audition", "name": "Casting audition", "revision": 1}
    )
    raw_rows = [_row(index, source) for index in range(1, 15)]
    payload = {
        "project_id": "casting-audition",
        "config": {
            "enabled": False,
            "male_voice": None,
            "female_voice": None,
            "fallback_voice": None,
        },
        "rows": raw_rows,
    }
    model._replace_dialogue_casting(payload)
    requests: list[tuple[str, dict, object]] = []
    model.runtime.request = lambda action, values=None, context=None: requests.append(
        (action, values or {}, context)
    )

    qml_root = Path(os.environ.get("DUCAI_TEST_QML_ROOT") or (
        Path(__file__).parents[2] / "src/ducai_studio/ui/qml"))
    engine = QQmlEngine(model)
    engine.addImportPath(str(qml_root))
    component = QQmlComponent(
        engine,
        QUrl.fromLocalFile(str(qml_root / "screens/VoiceStudioScreen.qml")),
    )
    screen = component.createWithInitialProperties({"appModel": model})
    assert screen is not None, component.errors()
    window = QQuickWindow()
    window.resize(1180, 900)
    screen.setParentItem(window.contentItem())
    screen.setWidth(1180)
    screen.setHeight(900)
    screen.selectInspectorTab("casting")
    window.show()

    def locate(name: str, kind=QObject):
        item = screen.findChild(kind, name)
        assert item is not None, name
        return item

    def until(condition, label: str, timeout_ms: int = 6000) -> None:
        for _ in range(timeout_ms // 20):
            app.processEvents()
            if condition():
                return
            QTest.qWait(20)
        raise AssertionError(label)

    def settle(milliseconds: int = 120) -> None:
        for _ in range(max(1, milliseconds // 20)):
            app.processEvents()
            QTest.qWait(20)

    def assert_scroll_near(rows, expected: float, label: str) -> None:
        until(
            lambda: abs(float(rows.property("contentY")) - expected) < 6,
            label,
        )

    playing = QMediaPlayer.PlaybackState.PlayingState
    casting = locate("dialogueCastingSourcePlayer", QMediaPlayer)
    preview = locate("voicePreviewPlayer", QMediaPlayer)
    reference = locate("cloneReferencePlayer", QMediaPlayer)
    rows = locate("dialogueCastingRows")
    casting_error = locate("dialogueCastingAudioError")

    try:
        assert casting.audioOutput() is not None
        assert preview.audioOutput() is not None
        assert casting.audioOutput() is not preview.audioOutput()
        casting.audioOutput().setVolume(0)
        preview.audioOutput().setVolume(0)
        reference.audioOutput().setVolume(0)

        audition_row = model.dialogueCasting["rows"][0]
        for attempt in range(2):
            screen.playCastingSource(audition_row)
            until(
                lambda: casting.playbackState() == playing and casting.position() >= 260,
                f"Casting source did not start on attempt {attempt + 1}",
            )
            until(
                lambda: int(screen.property("castingPlaybackIndex")) == 1
                and casting.position() >= 1_040,
                f"Second same-file part did not start on attempt {attempt + 1}",
            )
            until(
                lambda: int(screen.property("castingPlaybackIndex")) == -1
                and casting.source().isEmpty(),
                f"Casting source did not stop at its bound on attempt {attempt + 1}",
            )

        missing = directory / "missing.wav"
        screen.playCastingSource(
            {
                "parts": [
                    {
                        "source_url": missing.as_uri(),
                        "start_us": 0,
                        "end_us": 300_000,
                    }
                ]
            }
        )
        until(
            lambda: bool(screen.property("castingAudioError"))
            and bool(casting_error.property("visible"))
            and bool(casting_error.property("text")),
            "Invalid casting media did not produce visible feedback",
        )

        model._voice_preview_source = source.as_uri()
        model.voicePreviewChanged.emit()
        until(
            lambda: preview.playbackState() == playing and preview.position() > 80,
            "TTS preview did not start",
        )
        preview.stop()
        model.voicePreviewChanged.emit()
        until(
            lambda: preview.playbackState() == playing and preview.position() > 80,
            "Same-source TTS preview did not replay after a fresh completion signal",
        )
        model._voice_preview_source = ""
        model.voicePreviewChanged.emit()
        until(
            lambda: preview.source().isEmpty()
            and preview.mediaStatus() == QMediaPlayer.MediaStatus.NoMedia,
            "Clearing the TTS preview source did not stop without autoplay",
        )
        cleared_position = preview.position()
        settle(160)
        assert preview.source().isEmpty()
        assert preview.mediaStatus() == QMediaPlayer.MediaStatus.NoMedia
        assert preview.position() == cleared_position

        until(lambda: int(rows.property("count")) == 14, "Casting rows did not load")
        until(
            lambda: float(rows.property("contentHeight"))
            > float(rows.property("height")) + 500,
            "Casting rows are not scrollable",
        )
        rows.setProperty("contentY", 520.0)
        settle()
        bulk_scroll = float(rows.property("contentY"))
        screen.applyCastingLabel(["cue-5", "cue-6"], "male")
        assert requests[-1][0] == "set_dialogue_casting_labels"
        assert requests[-1][1]["cue_ids"] == ["cue-5", "cue-6"]
        assert_scroll_near(rows, bulk_scroll, "Busy notification reset bulk-label scroll")
        bulk_payload = copy.deepcopy(payload)
        for row in bulk_payload["rows"]:
            if row["id"] in {"cue-5", "cue-6"}:
                row.update({"label": "male", "manual": True})
        model._action_succeeded(
            "set_dialogue_casting_labels",
            bulk_payload,
            {"project_id": "casting-audition"},
        )
        assert_scroll_near(rows, bulk_scroll, "Bulk-label ACK reset list scroll")

        casting_filter = locate("dialogueCastingFilter")
        casting_filter.setProperty("currentIndex", 2)
        until(lambda: int(rows.property("count")) == 14, "Male filter did not load")
        rows.setProperty("contentY", 520.0)
        settle()
        filtered_scroll = float(rows.property("contentY"))
        screen.applyCastingLabel(["cue-5"], "female")
        assert requests[-1][1]["cue_ids"] == ["cue-5"]
        assert_scroll_near(rows, filtered_scroll, "Busy notification reset filtered scroll")
        filtered_payload = copy.deepcopy(bulk_payload)
        for row in filtered_payload["rows"]:
            if row["id"] == "cue-5":
                row.update({"label": "female", "manual": True})
        model._action_succeeded(
            "set_dialogue_casting_labels",
            filtered_payload,
            {"project_id": "casting-audition"},
        )
        until(lambda: int(rows.property("count")) == 13, "Filtered row was not removed")
        assert_scroll_near(
            rows,
            filtered_scroll,
            "Filtered single-label ACK did not keep the nearest row in place",
        )

        report = {
            "status": "PASS",
            "distinct_audio_outputs": True,
            "source_sequence_played_twice": True,
            "same_file_parts_played": True,
            "source_bound_enforced": True,
            "source_error_visible": True,
            "same_tts_preview_replayed": True,
            "bulk_label_scroll_preserved": True,
            "filtered_label_scroll_preserved": True,
        }
        (directory / "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report))
        return 0
    finally:
        casting.stop()
        preview.stop()
        reference.stop()
        window.close()
        delete(window)
        delete(engine)
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
