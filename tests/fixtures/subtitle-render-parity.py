"""Render the same measured scene/dialogue with QML Preview and real FFmpeg."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QFontDatabase, QGuiApplication, QImage
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow

from ducai_studio.application.voice_pipeline import file_sha256
from ducai_studio.domain.effects import (
    ExportSettings,
    ExportSnapshot,
    RenderClip,
    RenderSceneText,
    RenderSubtitle,
    RenderSubtitleBlur,
    SubtitleStyle,
)
from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.providers.export.ffmpeg import (
    _bundled_font_directory,
    materialize_export_chunk,
    probe_ffmpeg_capabilities,
)
from ducai_studio.providers.subtitle_artwork import artwork_payload, concealment_rect, layout_batch
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def main() -> int:
    app = QGuiApplication([])
    if os.environ.get("DUCAI_TEST_QML_ROOT"):
        fonts = Path(os.environ["DUCAI_TEST_QML_ROOT"]).parent / "assets/fonts"
        for path in fonts.glob("*.*"):
            if path.suffix in {".ttf", ".otf"}:
                assert QFontDatabase.addApplicationFont(str(path)) >= 0
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ffmpeg = sys.argv[2]
    width, height = 854, 480
    media = root / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x808080:s={width}x{height}:r=24",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-y",
            str(media),
        ],
        check=True,
        capture_output=True,
    )
    style = SubtitleStyle(primary_color="#FFFF00", font_family="Be Vietnam Pro", font_size=38)
    scene = RenderSceneText(
        "scene",
        2,
        0,
        2_000_000,
        "Tô\nTiểu\nLy",
        0.80,
        0.18,
        0.06,
        0.25,
        0.78,
        0.16,
        0.10,
        0.30,
        orientation="vertical",
        style_hint="calligraphy_brush",
        font_family="Comforter Brush",
        font_size=18,
        text_color="#FFFFFF",
        background_color="#000000",
        background_style="ink_splash",
        background_opacity_percent=100,
        source_blur_enabled=False,
    )
    subtitle = RenderSubtitle("cue", 1, 0, 2_000_000, "Trần Trường An rất xuất sắc,", font_size=38)
    white_subtitle = RenderSubtitle(
        "white-cue",
        3,
        0,
        2_000_000,
        "Có nhiều Tiểu Bảo như vậy",
        font_size=30,
        text_color="#FFFFFF",
        background_color="#000000",
        background_opacity_percent=100,
        panel_x=0.1,
        panel_y=0.55,
        panel_width=0.65,
        panel_height=0.16,
    )
    snapshot = ExportSnapshot(
        4,
        "parity",
        1,
        0,
        2_000_000,
        width,
        height,
        RenderClip("v", "V1", "video", str(media), 0, 2_000_000, 0, 2_000_000),
        (),
        (),
        (subtitle, white_subtitle),
        ExportSettings(subtitle_style=style, fps=24),
        (),
        (
            RenderSubtitleBlur(
                "cue",
                1,
                0,
                2_000_000,
                0.05,
                0.05,
                0.1,
                0.1,
                blur_radius=46,
                concealment_mode="cover",
                tracking_mode="translated_text",
            ),
        ),
        (scene,),
    )
    # A flat video makes blur kernels irrelevant: the real 90% cover boundary,
    # measured glyphs and ink must match even though the legacy OCR box differs.
    output = root / "export.mp4"
    if len(sys.argv) > 3:
        command = WorkerMessage(
            PROTOCOL_VERSION,
            "parity-export",
            "start",
            "control",
            {
                "action": "start",
                "command_id": "start",
                "job": {
                    "kind": "export",
                    "provider_config": {
                        "snapshot": asdict(snapshot),
                        "output_path": str(output),
                        "ffmpeg_path": ffmpeg,
                        "ffprobe_path": str(Path(ffmpeg).with_name("ffprobe.exe")),
                        "cache_root": str(root / "cache"),
                    },
                },
            },
        )
        result = subprocess.run(
            [sys.argv[3], "-m", "ducai_studio.workers.export_worker", "parity-export"],
            input=command.to_json_line().decode("utf-8"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            env={**os.environ, "DUCAI_ARTIFACT_STAGING_ROOT": str(root / "staging")},
        )
        (root / "worker-events.jsonl").write_text(result.stdout, encoding="utf-8")
        (root / "worker-stderr.log").write_text(result.stderr, encoding="utf-8")
        events = [json.loads(line) for line in result.stdout.splitlines()]
        assert any(event["kind"] == "succeeded" for event in events), result.stdout + result.stderr
        ledger = json.loads((root / "staging/parity-export/export-ledger-working.json").read_text())
        worker_fonts = Path(sys.argv[3]).resolve().parent / "ui/assets/fonts"
        directory = worker_fonts if worker_fonts.is_dir() else _bundled_font_directory()
        font_assets = (
            {
                path.name: file_sha256(path)
                for path in sorted(directory.iterdir())
                if path.suffix.casefold() in {".ttf", ".otf"}
            }
            if directory
            else {}
        )
        assert ledger["signature"] == stable_fingerprint(
            {
                "schema": "ducai-export-ledger-v1",
                "snapshot": asdict(snapshot),
                "ffmpeg": probe_ffmpeg_capabilities(ffmpeg).version_line,
                "render_contract": "subtitle-system-fonts-v4",
                "font_assets": font_assets,
            }
        )
    else:
        plan = materialize_export_chunk(
            snapshot,
            index=0,
            start_us=0,
            end_us=2_000_000,
            work_dir=root / "work",
            output_path=output,
            capabilities=probe_ffmpeg_capabilities(ffmpeg),
        )
        result = subprocess.run(
            plan.arguments,
            cwd=root / "work",
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    if result.returncode:
        raise RuntimeError(result.stderr)
    exported = root / "export.png"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            "1",
            "-i",
            str(output),
            "-frames:v",
            "1",
            "-y",
            str(exported),
        ],
        check=True,
        capture_output=True,
    )
    engine = QQmlEngine()
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    component = QQmlComponent(engine)
    sources = [
        "image://subtitle-artwork/"
        + quote(json.dumps(artwork_payload(cue, width, height, style), sort_keys=True), safe="")
        for cue in (scene, subtitle, white_subtitle)
    ]
    layout = layout_batch([artwork_payload(subtitle, width, height, style)])[0]
    rect = concealment_rect(layout, width, height, 0.012, 0.008)
    # The ASS mask uses integer output coordinates; preview paints subpixels.
    x, y, w, h = [
        round(value * scale)
        for value, scale in zip(rect, (width, height, width, height), strict=True)
    ]
    cover_pixels = [x, y, w, h]
    qml = f"""import QtQuick
Window {{ width: {width}; height: {height}; visible: true; color: "#808080"
    Rectangle {{ x: {x}; y: {y}; width: {w}; height: {h}; color: "black"; opacity: 0.90 }}
    Image {{ anchors.fill: parent; source: {json.dumps(sources[0])} }}
    Image {{ anchors.fill: parent; source: {json.dumps(sources[1])} }}
    Image {{ anchors.fill: parent; source: {json.dumps(sources[2])} }}
}}"""
    component.setData(QByteArray(qml.encode()), QUrl())
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errors()

    def verify():
        preview = window.grabWindow()
        captured_size = [preview.width(), preview.height()]
        # Window grabs use physical display pixels; compare in video pixels,
        # including when Windows/Qt renders this fixture at 125% DPI.
        preview = preview.scaled(
            width,
            height,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        preview.save(str(root / "preview.png"))
        actual = QImage(str(exported))
        intersections = unions = 0
        expected_bounds = [width, height, 0, 0]
        actual_bounds = [width, height, 0, 0]
        for y in range(height):
            for x in range(width):
                colors = [preview.pixelColor(x, y), actual.pixelColor(x, y)]
                masks = [
                    abs(c.red() - 128) + abs(c.green() - 128) + abs(c.blue() - 128) > 120
                    for c in colors
                ]
                intersections += all(masks)
                unions += any(masks)
                for mask, bounds in zip(masks, [expected_bounds, actual_bounds], strict=True):
                    if mask:
                        bounds[:] = [
                            min(bounds[0], x),
                            min(bounds[1], y),
                            max(bounds[2], x),
                            max(bounds[3], y),
                        ]
        report = {
            "iou": intersections / max(1, unions),
            "captured_preview_size": captured_size,
            "preview_bounds": expected_bounds,
            "export_bounds": actual_bounds,
            "cover_rect": cover_pixels,
        }
        color_counts = {}
        for cue, name in (
            (subtitle, "yellow_dialogue"),
            (white_subtitle, "white_dialogue"),
            (scene, "white_scene"),
        ):
            cue_layout = layout_batch([artwork_payload(cue, width, height, style)])[0]
            bx, by, bw, bh = cue_layout["text_rect"]
            counts = []
            for rendered in (preview, actual):
                count = 0
                for cy in range(max(0, int(by)), min(height, int(by + bh + 1))):
                    for cx in range(max(0, int(bx)), min(width, int(bx + bw + 1))):
                        color = rendered.pixelColor(cx, cy)
                        count += (
                            color.red() > 180
                            and color.green() > 180
                            and (
                                color.blue() < 100
                                if name == "yellow_dialogue"
                                else color.blue() > 180
                            )
                        )
                counts.append(count)
            color_counts[name] = counts
        report["foreground_pixels"] = color_counts
        (root / "comparison.json").write_text(json.dumps(report), encoding="utf-8")
        print(json.dumps(report), flush=True)
        app.exit(
            0
            if report["iou"] > 0.90
            and all(abs(a - b) <= 2 for a, b in zip(expected_bounds, actual_bounds, strict=True))
            and all(
                min(counts) > 40 and min(counts) / max(counts) > 0.65
                for counts in color_counts.values()
            )
            else 1
        )

    QTimer.singleShot(300, verify)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
