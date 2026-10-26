"""Exercise D1 entry transitions through the real TimelinePreview players."""
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


def _write_tone(path: Path, frequency: int, duration_seconds: float = 5.0) -> None:
    sample_rate = 16_000
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        stream.writeframes(
            b"".join(
                struct.pack(
                    "<h",
                    int(1000 * math.sin(2 * math.pi * frequency * index / sample_rate)),
                )
                for index in range(round(sample_rate * duration_seconds))
            )
        )


def _snapshot(video: Path, voice: Path, revision: int, ranges: list[tuple[int, int, int]]) -> dict:
    voice_clips = []
    for index, (start_ms, end_ms, source_start_ms) in enumerate(ranges, start=1):
        voice_clips.append(
            {
                "id": f"voice-{revision}-{index}",
                "track_id": "D1",
                "media_id": "voice-media",
                "timeline_range": {
                    "start_us": start_ms * 1000,
                    "end_us": end_ms * 1000,
                },
                "source_range": {
                    "start_us": source_start_ms * 1000,
                    "end_us": (source_start_ms + end_ms - start_ms) * 1000,
                },
                "speed": {"numerator": 1, "denominator": 1},
                "volume": 1.0,
            }
        )
    return {
        "id": "timeline-voice-transition",
        "name": "Timeline voice transition",
        "revision": revision,
        "media": [
            {"id": "video-media", "kind": "video", "path": str(video)},
            {"id": "voice-media", "kind": "audio", "path": str(voice)},
        ],
        "tracks": [
            {
                "id": "V1",
                "kind": "video",
                "name": "Video",
                "clips": [
                    {
                        "id": "video-1",
                        "track_id": "V1",
                        "media_id": "video-media",
                        "timeline_range": {"start_us": 0, "end_us": 5_000_000},
                        "source_range": {"start_us": 0, "end_us": 5_000_000},
                        "speed": {"numerator": 1, "denominator": 1},
                        "volume": 0.0,
                    }
                ],
            },
            {"id": "D1", "kind": "voice", "name": "Voice", "clips": voice_clips},
        ],
    }


def main() -> int:
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / "timeline-source.wav"
    voice = directory / "timeline-voice.wav"
    _write_tone(video, 220)
    _write_tone(voice, 880)

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
    model.project_state.replace(
        _snapshot(video, voice, 1, [(800, 1500, 100), (2300, 3000, 1000)])
    )

    project_root = Path(__file__).parents[2]
    qml_root = project_root / "src" / "ducai_studio" / "ui" / "qml"
    engine = QQmlEngine(model)
    engine.addImportPath(str(qml_root))
    component = QQmlComponent(
        engine,
        QUrl.fromLocalFile(str(qml_root / "controls/TimelinePreview.qml")),
    )
    preview = component.createWithInitialProperties({"appModel": model})
    assert preview is not None, component.errors()
    window = QQuickWindow()
    window.resize(900, 600)
    preview.setParentItem(window.contentItem())
    preview.setWidth(900)
    preview.setHeight(600)
    window.show()

    playing = int(QMediaPlayer.PlaybackState.PlayingState.value)

    def voice_playing() -> bool:
        return int(preview.property("voicePlaybackState")) == playing

    def until(condition, label: str, timeout_ms: int = 5000) -> None:
        for _ in range(timeout_ms // 20):
            app.processEvents()
            if condition():
                return
            QTest.qWait(20)
        raise AssertionError(
            f"{label}: playhead={model.playheadUs}, "
            f"voice_state={preview.property('voicePlaybackState')}, "
            f"voice_position={preview.property('voicePositionMs')}"
        )

    try:
        preview.seek(0)
        preview.play()
        until(
            lambda: model.playheadUs >= 950_000 and voice_playing(),
            "D1 did not start when normal playback entered the first cue",
            3000,
        )
        until(
            lambda: model.playheadUs >= 1700_000 and not voice_playing(),
            "D1 did not pause after leaving the first cue",
            2500,
        )

        # Relative seek while playing must re-anchor D1 immediately.
        preview.seekRelative(-600)
        until(
            lambda: 1_000_000 <= model.playheadUs <= 1_550_000 and voice_playing(),
            "D1 did not resume after a relative seek into a cue",
            1500,
        )

        # A paused absolute seek before the next cue must still allow the natural
        # playback transition to start D1 without a second/manual scrub.
        preview.pause()
        preview.seek(2050)
        preview.play()
        until(
            lambda: model.playheadUs >= 2450_000 and voice_playing(),
            "D1 did not start after absolute seek crossed the second cue",
            2200,
        )

        # Simulate a timeline edit that replaces D1 while the preview is paused.
        preview.pause()
        preview.seek(3300)
        model.project_state.replace(
            _snapshot(video, voice, 2, [(3800, 4550, 1800)])
        )
        preview.play()
        until(
            lambda: model.playheadUs >= 3950_000 and voice_playing(),
            "D1 did not start after an edited cue entered normal playback",
            2200,
        )

        report = {
            "status": "PASS",
            "normal_entry_started": True,
            "relative_seek_resynced": True,
            "absolute_seek_then_entry_started": True,
            "edited_timeline_entry_started": True,
        }
        (directory / "timeline-voice-transition-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report))
        return 0
    finally:
        preview.pause()
        window.close()
        delete(window)
        delete(engine)
        model.forceShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
