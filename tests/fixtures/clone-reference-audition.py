"""Exercise reference audition through real QML controls and audio decoders."""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QSettings, QStandardPaths, Qt, QUrl
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


def main() -> int:
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    reference = directory / "Mẫu giọng.wav"
    with wave.open(str(reference), "wb") as stream:
        stream.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        stream.writeframes(b"".join(
            struct.pack("<h", int(1000 * math.sin(2 * math.pi * 440 * i / 16_000)))
            for i in range(16_000 * 8)
        ))
    mp3 = directory / "Mẫu giọng 2.mp3"
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", str(reference),
                    "-y", str(mp3)], check=True, capture_output=True, **kwargs)
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
    clone = next(item for item in model.voice_catalog._items if item["is_clone"])
    qml = Path(os.environ.get("DUCAI_TEST_QML_ROOT") or (
        Path(__file__).parents[2] / "src/ducai_studio/ui/qml"))
    engine = QQmlEngine(model)
    engine.addImportPath(str(qml))
    component = QQmlComponent(
        engine, QUrl.fromLocalFile(str(qml / "screens/VoiceStudioScreen.qml")))
    screen = component.createWithInitialProperties({"appModel": model})
    assert screen is not None, component.errors()
    window = QQuickWindow()
    window.resize(1180, 900)
    screen.setParentItem(window.contentItem())
    screen.setWidth(1180)
    screen.setHeight(900)
    window.show()

    def locate(name):
        item = screen.findChild(QObject, name)
        assert item is not None, name
        return item

    def until(condition, label, timeout_ms=5000):
        for _ in range(timeout_ms // 20):
            app.processEvents()
            if condition():
                return
            QTest.qWait(20)
        raise AssertionError(label)

    def click(item, fraction=.5):
        assert item.isVisible() and item.isEnabled(), item.objectName()
        point = item.mapToScene(QPointF(item.width() * fraction, item.height() / 2))
        assert 0 <= point.x() < window.width() and 0 <= point.y() < window.height(), point
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point.toPoint())
        app.processEvents()

    playing = QMediaPlayer.PlaybackState.PlayingState
    paused = QMediaPlayer.PlaybackState.PausedState
    stopped = QMediaPlayer.PlaybackState.StoppedState
    player = locate("cloneReferencePlayer")
    preview = locate("voicePreviewPlayer")
    # Exercise decoding and playback clocks without sounding a test tone.
    player.audioOutput().setVolume(0)
    preview.audioOutput().setVolume(0)
    play, stop = locate("cloneReferencePlayButton"), locate("cloneReferenceStopButton")
    slider = locate("cloneReferenceSeekSlider")
    try:
        assert not model.currentProject.hasProject
        assert not play.isEnabled()
        model.setVieNeuReferenceAudio(reference.as_uri())
        until(lambda: player.duration() > 7000, "Reference WAV did not load")
        assert player.playbackState() == stopped, "Selection must not autoplay"
        click(play)
        until(lambda: player.position() > 120, "Reference WAV did not advance")
        click(play)
        assert player.playbackState() == paused
        position = player.position()
        QTest.qWait(100)
        assert player.position() == position
        assert slider.width() > 180, "Reference seek control is cramped"
        click(slider, .65)
        until(lambda: player.position() > 4000, "Reference seek did not apply")
        click(play)
        until(lambda: player.playbackState() == playing, "Reference did not resume")
        click(stop)
        until(lambda: player.playbackState() == stopped and player.position() == 0,
              "Reference did not stop/reset")

        model.setVieNeuReferenceAudio(str(mp3))
        until(lambda: player.duration() > 7000 and Path(player.source().toLocalFile()) == mp3,
              "Reference MP3 did not load")
        click(play)
        until(lambda: player.position() > 100, "Reference MP3 did not advance")
        model._voice_catalog_preview_source = reference.as_uri()
        model.voicePreviewChanged.emit()
        until(lambda: preview.playbackState() == playing, "Catalog preview did not start")
        assert player.playbackState() != playing, "Two voice players overlap"
        click(play)
        until(lambda: player.playbackState() == playing, "Reference did not take playback")
        assert preview.playbackState() != playing
        screen.setVisible(False)
        until(lambda: player.playbackState() == stopped, "Leaving screen did not stop audio")
        screen.setVisible(True)

        model.setVieNeuReferenceAudio(str(reference))
        until(lambda: Path(player.source().toLocalFile()) == reference, "Source did not change")
        assert player.playbackState() == stopped and player.position() == 0
        invalid = directory / "invalid.wav"
        invalid.write_bytes(b"Not an audio stream")
        model.setVieNeuReferenceAudio(str(invalid))
        until(lambda: bool(screen.property("referenceAudioError")), "Invalid audio has no feedback")
        assert player.playbackState() == stopped
        model.setVieNeuReferenceAudio(str(reference))
        until(lambda: player.duration() > 7000, "Reference did not recover after a decode error")
        assert not screen.property("referenceAudioError")
        click(play)
        until(lambda: player.playbackState() == playing, "Reference did not restart")
        model._selected_voice_id = clone["voice_id"]
        model.voiceSettingsChanged.emit()
        model._selected_voice_id = ""
        model.voiceSettingsChanged.emit()
        assert player.playbackState() == stopped, "Changing away from clone leaves audio running"
        model._selected_voice_id = clone["voice_id"]
        model.voiceSettingsChanged.emit()
        QTest.qWait(100)
        assert window.grabWindow().save(str(directory / "clone-reference.png"))
        model.setVieNeuReferenceAudio("")
        assert player.source().isEmpty() and not play.isEnabled()
        assert not model.ttsBusy and not model.currentProject.hasProject
        report = {"status": "PASS", "wav": True, "mp3": True, "play_pause_stop_seek": True,
                  "no_autoplay": True, "source_change_stops": True,
                  "mutually_exclusive_players": True, "decode_error_and_recovery": True,
                  "no_project_or_synthesis_required": True}
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0
    finally:
        player.stop()
        preview.stop()
        delete(window)
        delete(engine)
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
