"""Verify that restored voice previews stay silent until an explicit audition."""
from __future__ import annotations

import json
import math
import struct
import sys
import wave
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, QUrl
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


def _write_tone(path: Path, duration_seconds: float = 2.0) -> None:
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


def main() -> int:
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "restored-preview.wav"
    _write_tone(source)

    configure_qt_plugin_dll_search()
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    _register_bundled_fonts()
    settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
    original_settings = application_module.QSettings
    application_module.QSettings = lambda *_args: settings
    try:
        model = application_module.ApplicationViewModel(app)
    finally:
        application_module.QSettings = original_settings

    # This is the state restored from a completed preview job during startup.
    # Loading the screen must not interpret restored state as user intent to play.
    model._voice_preview_source = source.as_uri()

    project_root = Path(__file__).parents[2]
    qml_root = project_root / "src" / "ducai_studio" / "ui" / "qml"
    engine = QQmlEngine(model)
    engine.addImportPath(str(qml_root))
    component = QQmlComponent(
        engine,
        QUrl.fromLocalFile(str(qml_root / "screens/VoiceStudioScreen.qml")),
    )
    screen = component.createWithInitialProperties({"appModel": model})
    assert screen is not None, component.errors()
    window = QQuickWindow()
    window.resize(1180, 820)
    screen.setParentItem(window.contentItem())
    screen.setWidth(1180)
    screen.setHeight(820)
    window.show()

    preview = screen.findChild(QMediaPlayer, "voicePreviewPlayer")
    assert preview is not None
    assert preview.audioOutput() is not None
    preview.audioOutput().setVolume(0)
    playing = QMediaPlayer.PlaybackState.PlayingState

    def settle(milliseconds: int) -> None:
        for _ in range(max(1, milliseconds // 20)):
            app.processEvents()
            QTest.qWait(20)

    def until(condition, label: str, timeout_ms: int = 5000) -> None:
        for _ in range(timeout_ms // 20):
            app.processEvents()
            if condition():
                return
            QTest.qWait(20)
        raise AssertionError(label)

    try:
        settle(600)
        assert preview.playbackState() != playing, "Restored preview autoplayed on screen load"
        assert preview.position() == 0, "Restored preview advanced without an audition"

        for attempt in range(2):
            screen.armVoicePreviewPlayback()
            model.voicePreviewChanged.emit()
            until(
                lambda: preview.playbackState() == playing and preview.position() > 80,
                f"Explicit same-source audition did not play on attempt {attempt + 1}",
            )
            preview.stop()

        report = {
            "status": "PASS",
            "restored_preview_silent": True,
            "explicit_same_source_replayed": True,
        }
        (directory / "voice-audition-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report))
        return 0
    finally:
        preview.stop()
        window.close()
        delete(window)
        delete(engine)
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
