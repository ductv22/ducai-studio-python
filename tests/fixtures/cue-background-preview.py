"""Native GPU TimelinePreview versus real MP4 cue-background regression."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QByteArray, QPointF, QRect, QSettings, QSize, QStandardPaths, Qt, QUrl
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ducai_studio.application.export_pipeline import build_export_snapshot
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
from ducai_studio.providers.export.ffmpeg import (
    materialize_export_chunk,
    probe_ffmpeg_capabilities,
)
from ducai_studio.providers.media_probe import FfprobeProvider
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel

WIDTH = 640
HEIGHT = 360
def _run(
    command: list[str],
    *,
    timeout: int = 90,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        cwd=cwd,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n{result.stdout}\n{result.stderr}"
        )
    return result


def _make_textured_source(path: Path) -> None:
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    for y in range(0, HEIGHT, 8):
        for x in range(0, WIDTH, 8):
            light = ((x // 8) + (y // 8)) % 2 == 0
            painter.fillRect(x, y, 8, 8, QColor("#E8E8D8" if light else "#202838"))
    painter.fillRect(24, 24, 132, 72, QColor("#D13A3A"))
    painter.fillRect(484, 24, 132, 72, QColor("#2FBD67"))
    painter.fillRect(24, 264, 132, 72, QColor("#355ED1"))
    painter.fillRect(484, 264, 132, 72, QColor("#D6A928"))
    painter.setPen(QPen(QColor("#FFFFFF"), 2))
    for x in range(0, WIDTH, 32):
        painter.drawLine(x, 0, WIDTH - x - 1, HEIGHT - 1)
    painter.setPen(QPen(QColor("#050505"), 1))
    for x in range(0, WIDTH, 40):
        painter.drawLine(x, 0, x, HEIGHT - 1)
    for y in range(0, HEIGHT, 40):
        painter.drawLine(0, y, WIDTH - 1, y)
    painter.end()
    assert image.save(str(path))


def _project(source: Path, *, blur_text: str = "LÀM MỜ MẠNH") -> ProjectSnapshot:
    whole = TimeRange(0, 3_000_000)
    none_range = TimeRange(250_000, 1_000_000)
    blur_range = TimeRange(1_500_000, 2_500_000)
    source_none = SubtitleCue("source-none", 1, none_range, "无背景", "zh")
    source_blur = SubtitleCue("source-blur", 2, blur_range, "模糊", "zh")
    translated_none = SubtitleCue(
        "vi-none",
        1,
        none_range,
        "CHỈ CÓ CHỮ",
        "vi",
        background_style="none",
        text_opacity_percent=100,
        background_opacity_percent=100,
    )
    # Transparent glyphs isolate the actual blur kernel while retaining the
    # same measured translated-text geometry used by preview and export.
    translated_blur = SubtitleCue(
        "vi-blur",
        2,
        blur_range,
        blur_text,
        "vi",
        background_style="blur",
        text_opacity_percent=0,
        background_opacity_percent=100,
    )
    return ProjectSnapshot(
        "cue-background-preview",
        "Cue background preview",
        1,
        media=(MediaAsset("media", str(source), MediaKind.VIDEO, whole.duration_us),),
        tracks=(
            TimelineTrack(
                "V1",
                TrackKind.VIDEO,
                "Video",
                (TimelineClip("video", "V1", whole, whole, "media"),),
            ),
        ),
        source_cues=(source_none, source_blur),
        translated_cues=(translated_none, translated_blur),
    )


def _rect(raw: object, *, pad: int = 0) -> tuple[int, int, int, int]:
    values = list(raw) if isinstance(raw, (list, tuple)) else []
    assert len(values) == 4, raw
    x = max(0, int(round(float(values[0]))) - pad)
    y = max(0, int(round(float(values[1]))) - pad)
    right = min(WIDTH, int(round(float(values[0]) + float(values[2]))) + pad)
    bottom = min(HEIGHT, int(round(float(values[1]) + float(values[3]))) + pad)
    assert right > x and bottom > y, raw
    return x, y, right - x, bottom - y


def _rgba(image: QImage) -> tuple[QImage, memoryview]:
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    assert converted.size() == QSize(WIDTH, HEIGHT)
    return converted, memoryview(converted.bits()).cast("B")


def _selected_pixels(
    rect: tuple[int, int, int, int] | None,
    *,
    outside: bool = False,
):
    rx, ry, rw, rh = rect or (0, 0, WIDTH, HEIGHT)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            inside = rx <= x < rx + rw and ry <= y < ry + rh
            if (not outside and inside) or (outside and not inside):
                yield x, y


def _mean_difference(
    first: QImage,
    second: QImage,
    rect: tuple[int, int, int, int] | None = None,
    *,
    outside: bool = False,
) -> float:
    first_image, a = _rgba(first)
    second_image, b = _rgba(second)
    stride_a = first_image.bytesPerLine()
    stride_b = second_image.bytesPerLine()
    total = 0
    count = 0
    for x, y in _selected_pixels(rect, outside=outside):
        ia = y * stride_a + x * 4
        ib = y * stride_b + x * 4
        total += abs(a[ia] - b[ib]) + abs(a[ia + 1] - b[ib + 1]) + abs(a[ia + 2] - b[ib + 2])
        count += 3
    return total / max(1, count)


def _changed_fraction(
    first: QImage,
    second: QImage,
    rect: tuple[int, int, int, int],
    *,
    outside: bool = False,
    threshold: float = 10.0,
) -> float:
    first_image, a = _rgba(first)
    second_image, b = _rgba(second)
    stride_a = first_image.bytesPerLine()
    stride_b = second_image.bytesPerLine()
    changed = 0
    count = 0
    for x, y in _selected_pixels(rect, outside=outside):
        ia = y * stride_a + x * 4
        ib = y * stride_b + x * 4
        delta = (abs(a[ia] - b[ib]) + abs(a[ia + 1] - b[ib + 1])
                 + abs(a[ia + 2] - b[ib + 2])) / 3
        changed += delta >= threshold
        count += 1
    return changed / max(1, count)


def _edge_energy(image: QImage, rect: tuple[int, int, int, int]) -> float:
    converted, data = _rgba(image)
    stride = converted.bytesPerLine()
    rx, ry, rw, rh = rect

    def luma(x: int, y: int) -> int:
        offset = y * stride + x * 4
        return (3 * data[offset] + 6 * data[offset + 1] + data[offset + 2]) // 10

    total = 0
    count = 0
    for y in range(ry, ry + rh - 1):
        for x in range(rx, rx + rw - 1):
            value = luma(x, y)
            total += abs(value - luma(x + 1, y)) + abs(value - luma(x, y + 1))
            count += 2
    return total / max(1, count)


def _extract_frame(ffmpeg: str, source: Path, seconds: float, output: Path) -> QImage:
    _run([
        ffmpeg,
        "-v", "error",
        "-ss", f"{seconds:.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", f"scale={WIDTH}:{HEIGHT}",
        "-y", str(output),
    ])
    image = QImage(str(output))
    assert not image.isNull() and image.size() == QSize(WIDTH, HEIGHT)
    return image


def _wait_until(app: QApplication, predicate, timeout_ms: int, message: str) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(25)
    raise AssertionError(message)


def main() -> int:
    output_root = Path(sys.argv[1]).resolve()
    ffmpeg = str(Path(sys.argv[2]).resolve())
    ffprobe = str(Path(sys.argv[3]).resolve())
    output_root.mkdir(parents=True, exist_ok=True)
    preview_viewport_height = int(os.environ.get("DUCAI_TEST_PREVIEW_HEIGHT", "480"))
    blur_text = os.environ.get("DUCAI_TEST_BLUR_TEXT", "LÀM MỜ MẠNH")
    assert preview_viewport_height >= 180
    assert blur_text.strip()
    window_width = round(WIDTH * preview_viewport_height / HEIGHT)
    window_height = preview_viewport_height + 46

    configure_qt_plugin_dll_search()
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    pattern = output_root / "pattern.png"
    source = output_root / "source.mp4"
    exported = output_root / "export.mp4"
    _make_textured_source(pattern)
    _run([
        ffmpeg,
        "-v", "error",
        "-loop", "1",
        "-framerate", "24",
        "-i", str(pattern),
        "-t", "3",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "12",
        "-pix_fmt", "yuv420p",
        "-an",
        "-y", str(source),
    ])

    project = _project(source, blur_text=blur_text)
    style = SubtitleStyle(
        enabled=True,
        font_family="Arial",
        font_size=44,
        margin_bottom=52,
        primary_color="#FF00FF",
        outline_color="#00FFFF",
        outline_width=0,
        background_enabled=True,
        background_mode="blur",
    )
    settings = ExportSettings(
        backend="cpu",
        video_bitrate="8M",
        output_width=WIDTH,
        output_height=HEIGHT,
        fps=24,
        subtitle_style=style,
    )
    snapshot = build_export_snapshot(
        project,
        settings=settings,
        media_probe=FfprobeProvider(ffprobe),
    )
    explicit = [item for item in snapshot.subtitle_blurs if item.tracking_mode == "translated_text"]
    assert len(explicit) == 1
    assert explicit[0].blur_radius == 46 and explicit[0].opacity == 1.0
    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=3_000_000,
        work_dir=output_root / "export-work",
        output_path=exported,
        capabilities=probe_ffmpeg_capabilities(ffmpeg),
    )
    _run(list(plan.arguments), timeout=120, cwd=output_root / "export-work")
    graph = plan.filter_script_path.read_text(encoding="utf-8")
    boxblur_filters = graph.count("boxblur")
    boxblur_passes = graph.count("luma_power=2") * 2 + graph.count("luma_power=1")
    assert boxblur_filters >= 1 and boxblur_passes >= 2, graph

    source_frame = _extract_frame(ffmpeg, source, 1.25, output_root / "source-frame.png")
    export_none = _extract_frame(ffmpeg, exported, 0.60, output_root / "export-none.png")
    export_outside = _extract_frame(ffmpeg, exported, 1.25, output_root / "export-outside.png")
    export_blur = _extract_frame(ffmpeg, exported, 2.00, output_root / "export-blur.png")

    model = ApplicationViewModel(app)
    model.settings = QSettings(str(output_root / "settings.ini"), QSettings.Format.IniFormat)
    for key, value in {
        "fontFamily": style.font_family,
        "fontSize": style.font_size,
        "subtitlePrimaryColor": style.primary_color,
        "subtitleOutlineColor": style.outline_color,
        "subtitleOutlineWidth": style.outline_width,
        "subtitleMarginBottom": style.margin_bottom,
        "subtitleBackgroundEnabled": True,
        "subtitleBackgroundMode": "blur",
        "subtitleEnabled": True,
        "presetId": "source-quality",
    }.items():
        model.settings.setValue("export/" + key, value)
    model.runtime.start = lambda: None
    model.license_model._status = {"active": True, "message": "Fixture"}
    model.project_state.replace(json.loads(json.dumps(asdict(project))))

    qml_root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    qml_component_root = qml_root
    kernel_factor_text = os.environ.get("DUCAI_TEST_BLUR_KERNEL_FACTOR", "").strip()
    kernel_factor = 2**0.5
    if kernel_factor_text:
        kernel_factor = float(kernel_factor_text)
        assert 0 < kernel_factor <= 2
        qml_component_root = output_root / "qml-kernel-override"
        shutil.copytree(
            qml_root / "controls",
            qml_component_root / "controls",
            dirs_exist_ok=True,
        )
        timeline_path = qml_component_root / "controls" / "TimelinePreview.qml"
        timeline_source = timeline_path.read_text(encoding="utf-8")
        original = ") * root.subtitleScale() * Math.SQRT2"
        replacement = f") * root.subtitleScale() * {kernel_factor:.8f}"
        assert timeline_source.count(original) == 1
        timeline_path.write_text(
            timeline_source.replace(original, replacement),
            encoding="utf-8",
        )
    engine = QQmlEngine(model)
    engine.addImportPath(str(qml_root))
    engine.addImportPath(str(qml_component_root))
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.rootContext().setContextProperty("fixtureModel", model)
    component = QQmlComponent(engine)
    qml = f'''import QtQuick
import QtQuick.Controls
import "controls"
ApplicationWindow {{
    objectName: "cueBackgroundFixtureWindow"
    width: {window_width}; height: {window_height}
    visible: true; color: "black"
    TimelinePreview {{
        objectName: "cueBackgroundTimelinePreview"
        anchors.fill: parent
        appModel: fixtureModel
        showProjectOverlays: true
        controlsVisible: false
    }}
}}'''
    component.setData(
        QByteArray(qml.encode("utf-8")),
        QUrl.fromLocalFile(str(qml_component_root / "Fixture.qml")),
    )
    window = component.create()
    assert window is not None, [error.toString() for error in component.errors()]
    preview = window.findChild(object, "cueBackgroundTimelinePreview")
    video_output = window.findChild(object, "previewVideoOutput")
    assert preview is not None and video_output is not None
    window.show()
    _wait_until(app, window.isExposed, 8_000, "Native QML window never became exposed")
    # A paused MediaPlayer does not always submit its first D3D11 texture. Let
    # the real player advance briefly before making frame-accurate paused seeks.
    preview.play()
    _wait_until(
        app,
        lambda: video_output.property("sourceRect").width() > 0,
        10_000,
        "TimelinePreview never decoded the source video",
    )
    preview.pause()

    graphics_api = str(QQuickWindow.graphicsApi())
    assert "Software" not in graphics_api, graphics_api

    def capture(label: str, position_ms: int, expected_blurs: int) -> QImage:
        preview.seek(position_ms)
        _wait_until(
            app,
            lambda: abs(float(preview.property("sourcePositionMs")) - position_ms) <= 50,
            3_000,
            f"TimelinePreview did not seek to {position_ms} ms for {label}",
        )
        QTest.qWait(250)
        value = preview.property("cueBlurItems")
        items = value.toVariant() if hasattr(value, "toVariant") else value
        assert len(items) == expected_blurs, (label, items)
        grabbed = window.grabWindow()
        assert not grabbed.isNull()
        origin = video_output.mapToItem(window.contentItem(), QPointF(0, 0))
        crop = QRect(
            round(origin.x()),
            round(origin.y()),
            round(video_output.width()),
            round(video_output.height()),
        )
        frame = grabbed.copy(crop).scaled(
            WIDTH,
            HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        assert frame.save(str(output_root / f"preview-{label}.png"))
        return frame

    preview_outside = capture("outside", 1_250, 0)
    preview_none = capture("none", 600, 0)
    preview_blur = capture("blur", 2_000, 1)

    none_cue = model.project_state.sourceSubtitleRegionsAt(600_000)[0]
    blur_cue = model.project_state.sourceSubtitleRegionsAt(2_000_000)[0]
    none_art = model.subtitleArtwork(none_cue, WIDTH, HEIGHT)
    blur_art = model.subtitleArtwork(blur_cue, WIDTH, HEIGHT)
    none_rect = _rect(none_art.get("text_rect") or none_art.get("concealment_rect"), pad=5)
    blur_rect = _rect(blur_art.get("concealment_rect") or blur_art.get("text_rect"))

    preview_baseline_edge = _edge_energy(preview_outside, blur_rect)
    export_baseline_edge = _edge_energy(export_outside, blur_rect)
    preview_blur_edge = _edge_energy(preview_blur, blur_rect)
    export_blur_edge = _edge_energy(export_blur, blur_rect)
    preview_edge_retention = preview_blur_edge / max(0.001, preview_baseline_edge)
    export_edge_retention = export_blur_edge / max(0.001, export_baseline_edge)
    edge_retention_ratio = preview_edge_retention / max(0.001, export_edge_retention)
    metrics = {
        "graphics_api": graphics_api,
        "preview_viewport_height": preview_viewport_height,
        "blur_kernel_factor": kernel_factor,
        "blur_text": blur_text,
        "blur_rect": list(blur_rect),
        "none_text_rect": list(none_rect),
        "boxblur_filters": boxblur_filters,
        "boxblur_passes": boxblur_passes,
        "preview_blur_change_inside": _mean_difference(preview_blur, preview_outside, blur_rect),
        "preview_blur_change_outside": _mean_difference(
            preview_blur, preview_outside, blur_rect, outside=True),
        "export_blur_change_inside": _mean_difference(export_blur, export_outside, blur_rect),
        "export_blur_change_outside": _mean_difference(
            export_blur, export_outside, blur_rect, outside=True),
        "preview_blur_changed_fraction": _changed_fraction(
            preview_blur, preview_outside, blur_rect),
        "export_blur_changed_fraction": _changed_fraction(export_blur, export_outside, blur_rect),
        "preview_outside_changed_fraction": _changed_fraction(
            preview_blur, preview_outside, blur_rect, outside=True),
        "export_outside_changed_fraction": _changed_fraction(
            export_blur, export_outside, blur_rect, outside=True),
        "source_edge_energy": _edge_energy(source_frame, blur_rect),
        "preview_baseline_edge_energy": preview_baseline_edge,
        "export_baseline_edge_energy": export_baseline_edge,
        "preview_blur_edge_energy": preview_blur_edge,
        "export_blur_edge_energy": export_blur_edge,
        "preview_edge_retention": preview_edge_retention,
        "export_edge_retention": export_edge_retention,
        "preview_export_edge_retention_ratio": edge_retention_ratio,
        "preview_export_blur_mae": _mean_difference(preview_blur, export_blur, blur_rect),
        "preview_none_change_outside_text": _mean_difference(
            preview_none, preview_outside, none_rect, outside=True),
        "export_none_change_outside_text": _mean_difference(
            export_none, export_outside, none_rect, outside=True),
        "preview_none_change_inside_text": _mean_difference(
            preview_none, preview_outside, none_rect
        ),
        "export_none_change_inside_text": _mean_difference(export_none, export_outside, none_rect),
        "preview_outside_source_mae": _mean_difference(preview_outside, source_frame),
        "export_outside_source_mae": _mean_difference(export_outside, source_frame),
    }
    report_path = output_root / "comparison.json"
    report_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False), flush=True)

    # Strong, opaque local blur in both renderers.
    assert metrics["preview_edge_retention"] < 0.62, metrics
    assert metrics["export_edge_retention"] < 0.62, metrics
    assert metrics["preview_blur_changed_fraction"] > 0.45, metrics
    assert metrics["export_blur_changed_fraction"] > 0.45, metrics
    # No blur escapes the measured translated-text region, and no effect exists
    # before/after the cue. Compression/color-conversion noise stays bounded.
    assert metrics["preview_outside_changed_fraction"] < 0.01, metrics
    assert metrics["export_outside_changed_fraction"] < 0.04, metrics
    assert metrics["preview_none_change_outside_text"] < 2.0, metrics
    assert metrics["export_none_change_outside_text"] < 8.0, metrics
    assert metrics["preview_none_change_inside_text"] > 3.0, metrics
    assert metrics["export_none_change_inside_text"] > 3.0, metrics
    # Preview and export need not use an identical kernel, but their normalized
    # region must have comparable smoothing and appearance.
    both_near_zero = (
        metrics["preview_blur_edge_energy"] < 1.0
        and metrics["export_blur_edge_energy"] < 1.0
        and metrics["preview_edge_retention"] < 0.05
        and metrics["export_edge_retention"] < 0.05
    )
    comparable_retention = (
        0.55 <= metrics["preview_export_edge_retention_ratio"] <= 1.8
    )
    assert both_near_zero or comparable_retention, metrics
    assert metrics["preview_export_blur_mae"] < 10.0, metrics
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
