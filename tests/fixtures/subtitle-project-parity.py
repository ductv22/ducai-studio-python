"""Compare real project->UI artwork with a separate native export worker."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from dataclasses import asdict, replace
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QFont, QFontInfo, QGuiApplication, QImage
from PySide6.QtQml import QQmlComponent, QQmlEngine

from ducai_studio.application.export_pipeline import build_export_snapshot
from ducai_studio.application.title_text_commands import edit_title_text
from ducai_studio.domain.effects import ExportSettings, SubtitleStyle
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.providers.font_loading import load_application_font
from ducai_studio.providers.media_probe import FfprobeProvider
from ducai_studio.providers.subtitle_artwork import artwork_payload, editor_payload
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which(sys.argv[2]) or sys.argv[2]
    runtime = sys.argv[3] if len(sys.argv) > 3 else sys.executable
    media, output = root / "source.mp4", root / "export.mp4"
    width, height = 854, 480
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                    f"color=c=0x808080:s={width}x{height}:r=24", "-t", "2",
                    "-c:v", "libx264", "-y", str(media)], check=True, capture_output=True)
    app = QGuiApplication([])
    if sys.platform == "win32" and app.platformName() == "offscreen":
        # The offscreen backend does not enumerate Windows' system fonts.
        # Load the same installed faces that the native export worker uses.
        for name in ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"):
            assert load_application_font(Path(os.environ["WINDIR"]) / "Fonts" / name) >= 0
    QStandardPaths.setTestModeEnabled(True)
    model = ApplicationViewModel(app)
    model.settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
    for key, value in {"fontFamily": "Arial", "fontSize": 37,
                       "subtitlePrimaryColor": "#EFDB08", "subtitleOutlineWidth": 2,
                       "subtitleMarginBottom": 45, "subtitleBackgroundEnabled": False}.items():
        model.settings.setValue("export/" + key, value)
    timing = TimeRange(0, 2_000_000)
    source = SubtitleCue("cue", 1, timing, "你我都年轻过", "zh")
    translated = SubtitleCue("vi", 1, timing, "Cả hai chúng ta đều từng trẻ tuổi", "vi")
    project = ProjectSnapshot("parity", "Parity", 1,
        media=(MediaAsset("m", str(media), MediaKind.VIDEO, timing.duration_us),),
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (
            TimelineClip("v", "V1", timing, timing, "m"),)),),
        source_cues=(source,), translated_cues=(translated,))
    title_mode = os.environ.get("DUCAI_TEST_TITLE_MODE") == "1"
    if title_mode:
        project = edit_title_text(replace(project, source_cues=(), translated_cues=()),
            "add_title_text", None, {
                "text": "DUCAI Studio · Tên phim", "start_us": 0, "end_us": 2_000_000,
                "visual_box": [0.06, 0.08, 0.65, 0.22], "font_size": 40,
                "style_hint": "calligraphy_brush", "text_color": "#FFFFFF",
                "background_color": "#000000", "background_style": "ink_splash",
                "font_bold": True, "letter_spacing_percent": 5,
            })
        model.settings.setValue("export/subtitleEnabled", False)
    raw = json.loads(json.dumps(asdict(project)))
    model.project_state.replace(raw)
    style = SubtitleStyle(font_family="Arial", font_size=37, primary_color="#EFDB08",
                          outline_width=2, margin_bottom=45, background_enabled=False,
                          background_mode="black", enabled=not title_mode)
    assert model._subtitle_artwork_style() == style
    snapshot = build_export_snapshot(project, settings=ExportSettings(subtitle_style=style, fps=24),
                                     media_probe=FfprobeProvider())
    ui_cue = (model.project_state.title_cues if title_mode
              else model.project_state.subtitle_cues).get(0)
    payload = editor_payload(ui_cue, width, height, style)
    rendered = artwork_payload((snapshot.title_texts if title_mode
                                else snapshot.subtitles)[0], width, height, style)
    if title_mode:
        assert not snapshot.subtitles and not snapshot.scene_texts
        assert not snapshot.settings.subtitle_style.enabled
        assert not model.project_state.subtitle_cues.count
    # Translation and source have different IDs but must have identical glyph inputs.
    payload["cue"]["cue_id"] = rendered["cue"]["cue_id"]
    assert payload == rendered, (payload, rendered)
    artwork = model.subtitleArtwork(ui_cue, width, height)
    assert QFontInfo(QFont("Arial")).family() == "Arial"
    command = WorkerMessage(PROTOCOL_VERSION, "font-parity", "start", "control", {
        "action": "start", "command_id": "start", "job": {"kind": "export",
        "provider_config": {"snapshot": asdict(snapshot), "output_path": str(output),
        "ffmpeg_path": ffmpeg, "ffprobe_path": str(Path(ffmpeg).with_name("ffprobe.exe")),
        "cache_root": str(root / "cache")}}})
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    env["DUCAI_ARTIFACT_STAGING_ROOT"] = str(root / "staging")
    result = subprocess.run([runtime, "-m", "ducai_studio.workers.export_worker", "font-parity"],
        input=command.to_json_line().decode(), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=60)
    (root / "worker-events.jsonl").write_text(result.stdout, encoding="utf-8")
    assert result.returncode == 0 and '"kind":"succeeded"' in result.stdout.replace(" ", ""), (
        result.stdout + result.stderr)
    subprocess.run([ffmpeg, "-v", "error", "-ss", "0.5", "-i", str(output),
                    "-frames:v", "1", "-y", str(root / "export.png")],
                   check=True, capture_output=True)
    engine = QQmlEngine()
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    component = QQmlComponent(engine)
    visible = "false" if os.environ.get("DUCAI_TEST_HIDE_WINDOW") == "1" else "true"
    component.setData(QByteArray((f'import QtQuick\nWindow {{ width: {width}; height: {height}; '
        f'visible: {visible}; color: "#808080"; Image {{ anchors.fill: parent; '
        f'source: {json.dumps(artwork["source"])} }} }}').encode()), QUrl())
    window = component.create()
    assert window is not None, component.errors()

    def compare():
        preview = window.grabWindow().scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
        preview.save(str(root / "preview.png"))
        exported = QImage(str(root / "export.png"))
        masks = []
        for image in (preview, exported):
            masks.append({(x, y) for y in range(height) for x in range(width)
                          if (c := image.pixelColor(x, y))
                          and (abs(c.red() - 128) + abs(c.green() - 128)
                               + abs(c.blue() - 128) > 120)})
        bounds = [(min(x for x, _ in m), min(y for _, y in m),
                   max(x for x, _ in m), max(y for _, y in m)) for m in masks]
        assert all(abs(a - b) <= 1 for a, b in zip(*bounds, strict=True)), bounds
        iou = len(masks[0] & masks[1]) / max(1, len(masks[0] | masks[1]))
        report = {"iou": iou, "font": "Arial", "input_parity": True,
                  "independent_title": title_mode,
                  "bounds": bounds,
                  "preview_pixels": len(masks[0]), "export_pixels": len(masks[1])}
        (root / "comparison.json").write_text(json.dumps(report), encoding="utf-8")
        print(json.dumps(report))
        app.exit(0 if iou > .9 and min(map(len, masks)) > 500 else 1)

    def verify():
        try:
            compare()
        except Exception:
            traceback.print_exc()
            app.exit(1)

    QTimer.singleShot(300, verify)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
