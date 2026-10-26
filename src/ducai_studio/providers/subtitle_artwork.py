"""One measured glyph/background layout for Preview and export.

Qt resolves the bundled fonts once. Export receives vector outlines, so libass
does not choose a second font, size, line spacing or background estimate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from functools import lru_cache

from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)

from ducai_studio.domain.effects import RenderSceneText, RenderSubtitle, SubtitleStyle
from ducai_studio.providers.font_loading import load_application_font

_app = None
_fonts_ready = False


def _ensure_fonts() -> None:
    global _app, _fonts_ready
    instance = QCoreApplication.instance()
    if instance is None:
        # Windows' offscreen plugin cannot enumerate installed system fonts.
        # A GUI-capable app needs no window, but must use the same font backend
        # as Preview rather than silently substituting a bundled display face.
        os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
        _app = QGuiApplication([])
    elif not isinstance(instance, QGuiApplication):
        raise RuntimeError("Font outlines require a GUI-capable Qt application")
    if _fonts_ready:
        return
    from ducai_studio.providers.export.ffmpeg import _bundled_font_directory

    directory = _bundled_font_directory()
    if directory is not None:
        for path in sorted((*directory.glob("*.ttf"), *directory.glob("*.otf"))):
            if load_application_font(path) < 0:
                raise RuntimeError(f"Cannot load subtitle font: {path.name}")
    _fonts_ready = True


def _drawing(path: QPainterPath) -> str:
    values = []
    index = 0
    while index < path.elementCount():
        element = path.elementAt(index)
        point = f"{element.x:.3f} {element.y:.3f}"
        if element.isMoveTo():
            values.append("m " + point)
        elif element.isLineTo():
            values.append("l " + point)
        elif element.type == QPainterPath.ElementType.CurveToElement:
            second, end = path.elementAt(index + 1), path.elementAt(index + 2)
            values.append(f"b {point} {second.x:.3f} {second.y:.3f} {end.x:.3f} {end.y:.3f}")
            index += 2
        index += 1
    return " ".join(values)


def path_from_drawing(drawing: str) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    tokens = drawing.split()
    index = 0
    while index < len(tokens):
        command = tokens[index]
        count = {"m": 2, "l": 2, "b": 6}[command]
        coordinates = [float(value) for value in tokens[index + 1 : index + 1 + count]]
        if command == "m":
            path.moveTo(*coordinates)
        elif command == "l":
            path.lineTo(*coordinates)
        else:
            path.cubicTo(*coordinates)
        index += count + 1
    return path


def _split_overlong_word(word: str, metrics: QFontMetricsF, available: float) -> list[str]:
    """Keep every glyph when one word is wider than the requested line."""

    chunks: list[str] = []
    current = ""
    for character in word:
        candidate = current + character
        if current and metrics.horizontalAdvance(candidate) > available + 1:
            chunks.append(current)
            current = character
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def _lines(text: str, metrics: QFontMetricsF, available: float) -> list[str]:
    """Word-wrap at the selected font size while preserving explicit newlines."""

    result: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            result.append("")
            continue
        current = ""
        for word in paragraph.split():
            pieces = _split_overlong_word(word, metrics, available)
            for index, piece in enumerate(pieces):
                candidate = (current + " " + piece).strip()
                if current and metrics.horizontalAdvance(candidate) > available + 1:
                    result.append(current)
                    current = piece
                else:
                    current = candidate
                if index < len(pieces) - 1:
                    result.append(current)
                    current = ""
        result.append(current)
    return result or [""]


def _columns(text: str, metrics: QFontMetricsF, available: float) -> list[list[str]]:
    """Flow vertical text into additional columns without changing font size."""

    rows = text.split("\n") or [text]
    rows_per_column = max(1, int(available // max(1.0, metrics.lineSpacing())))
    return [rows[index : index + rows_per_column] for index in range(0, len(rows), rows_per_column)]


def _readable_color(foreground: str, background: str, opacity: int) -> str:
    if not background or opacity < 50:
        return foreground

    def luminance(value: str) -> float:
        color = QColor(value)
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    fg, bg = luminance(foreground), luminance(background)
    if (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05) >= 3:
        return foreground
    return "#FFFFFF" if 1.05 / (bg + 0.05) >= (bg + 0.05) / 0.05 else "#000000"


@lru_cache(maxsize=256)
def layout_json(payload_json: str, include_paths: bool = True) -> dict:
    _ensure_fonts()
    payload = json.loads(payload_json)
    width, height = payload["width"], payload["height"]
    scene = payload["kind"] == "scene"
    cue = RenderSceneText(**payload["cue"]) if scene else RenderSubtitle(**payload["cue"])
    style = SubtitleStyle(**payload["style"])
    anchored = not scene and cue.panel_x < 0 and cue.text_anchor is not None
    if scene or cue.panel_x >= 0:
        panel = (
            cue.panel_x * width,
            cue.panel_y * height,
            cue.panel_width * width,
            cue.panel_height * height,
        )
    else:
        panel = (40, 0, max(1, width - 80),
                 max(1, height if anchored else height - style.margin_bottom))
    font = QFont(cue.font_family or style.font_family)
    font.setPixelSize(max(1, cue.font_size or style.font_size))
    font.setWeight(
        QFont.Weight.Bold
        if cue.font_bold
        else (QFont.Weight.Medium if scene else QFont.Weight.Normal)
    )
    font.setItalic(cue.font_italic)
    font.setLetterSpacing(
        QFont.SpacingType.AbsoluteSpacing, font.pixelSize() * cue.letter_spacing_percent / 100
    )
    outline = 1 if scene else style.outline_width
    metrics = QFontMetricsF(font)
    pad_x = font.pixelSize() * cue.background_padding_x / 100
    pad_y = font.pixelSize() * cue.background_padding_y / 100
    if not scene and style.background_mode == "legacy":
        pad_x = max(pad_x, width * 0.012, font.pixelSize() * 0.4)
        pad_y = max(pad_y, height * 0.008, font.pixelSize() * 0.25)
    available_width = max(1, panel[2] - 2 * (outline + pad_x))
    available_height = max(1, panel[3] - 2 * (outline + pad_y))
    columns = _columns(cue.text, metrics, available_height) if cue.orientation == "vertical" else []
    lines = _lines(cue.text, metrics, available_width) if not columns else []
    column_gap = max(1.0, font.pixelSize() * 0.2)
    column_widths = [
        max((metrics.horizontalAdvance(line) for line in column), default=0.0)
        for column in columns
    ]
    if columns:
        panel_flow_width = sum(column_widths) + column_gap * max(0, len(columns) - 1)
        frame_height = max(1, height - 2 * (outline + pad_y))
        if panel_flow_width + 2 * (outline + pad_x) > width and frame_height > available_height:
            columns = _columns(cue.text, metrics, frame_height)
            column_widths = [
                max((metrics.horizontalAdvance(line) for line in column), default=0.0)
                for column in columns
            ]
    else:
        frame_width = max(1, width - 2 * (outline + pad_x))
        if (
            metrics.lineSpacing() * len(lines) + 2 * (outline + pad_y) > height
            and frame_width > available_width
        ):
            lines = _lines(cue.text, metrics, frame_width)
    text_width = (
        sum(column_widths) + column_gap * max(0, len(columns) - 1)
        if columns
        else max(metrics.horizontalAdvance(line) for line in lines)
    )
    text_height = (
        max((metrics.lineSpacing() * len(column) for column in columns), default=0.0)
        if columns
        else metrics.lineSpacing() * len(lines)
    )
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    centered = scene or cue.panel_x >= 0 or anchored
    top = (
        panel[1] + (panel[3] - text_height) / 2
        if centered
        else panel[1] + panel[3] - text_height - outline - pad_y
    )
    if text_height + 2 * (outline + pad_y) <= height:
        top = max(
            outline + pad_y,
            min(height - text_height - outline - pad_y, top),
        )
    line_bounds = QRectF()
    if columns:
        left = panel[0] + (panel[2] - text_width) / 2
        if text_width + 2 * (outline + pad_x) <= width:
            left = max(outline + pad_x, min(width - text_width - outline - pad_x, left))
        for column, column_width in zip(columns, column_widths, strict=True):
            column_top = panel[1] + (panel[3] - metrics.lineSpacing() * len(column)) / 2
            if metrics.lineSpacing() * len(column) + 2 * (outline + pad_y) <= height:
                column_top = max(
                    outline + pad_y,
                    min(
                        height - metrics.lineSpacing() * len(column) - outline - pad_y,
                        column_top,
                    ),
                )
            for index, line in enumerate(column):
                line_width = metrics.horizontalAdvance(line)
                x = left + (column_width - line_width) / 2
                y = column_top + index * metrics.lineSpacing()
                line_bounds = line_bounds.united(
                    QRectF(x, y, line_width, metrics.lineSpacing())
                )
                if include_paths:
                    path.addText(QPointF(x, y + metrics.ascent()), font, line)
            left += column_width + column_gap
    else:
        for index, line in enumerate(lines):
            line_width = metrics.horizontalAdvance(line)
            alignment = 2 if scene else style.alignment
            x = panel[0] + (panel[2] - line_width) / 2
            if alignment == 1:
                x = panel[0] + outline + pad_x
            elif alignment == 3:
                x = panel[0] + panel[2] - line_width - outline - pad_x
            if line_width + 2 * (outline + pad_x) <= width:
                x = max(outline + pad_x, min(width - line_width - outline - pad_x, x))
            line_bounds = line_bounds.united(
                QRectF(x, top + index * metrics.lineSpacing(), line_width, metrics.lineSpacing())
            )
            if include_paths:
                path.addText(
                    QPointF(x, top + metrics.ascent() + index * metrics.lineSpacing()), font, line
                )
    placement = line_bounds.adjusted(
        -outline - pad_x, -outline - pad_y, outline + pad_x, outline + pad_y
    )
    if anchored:
        # The OCR rectangle supplies a centre, never a font/line-width limit.
        # Only move off an edge when the finished label would leave the frame.
        dx = cue.text_anchor[0] * width - placement.center().x()
        dy = cue.text_anchor[1] * height - placement.center().y()
        dx = max(-placement.left(), min(width - placement.right(), dx))
        dy = max(-placement.top(), min(height - placement.bottom(), dy))
        placement.translate(dx, dy)
        line_bounds.translate(dx, dy)
        path.translate(dx, dy)
    if not include_paths:
        return {
            "placement_rect": [placement.x(), placement.y(), placement.width(), placement.height()],
            "font_size": font.pixelSize(),
        }
    bounds = path.boundingRect().adjusted(-outline, -outline, outline, outline)
    background_bounds = (
        bounds if scene or style.background_mode != "legacy" else bounds.united(line_bounds)
    )
    background_rect = background_bounds.adjusted(-pad_x, -pad_y, pad_x, pad_y)
    if not scene:
        background_rect = background_rect.intersected(QRectF(0, 0, width, height))
    background = QPainterPath()
    background_color = cue.background_color
    background_opacity = cue.background_opacity_percent
    if cue.background_style in {"none", "blur"}:
        background_color = ""
    elif not scene:
        if not style.background_enabled or style.background_mode == "blur":
            background_color = ""
        elif style.background_mode == "black":
            background_color = cue.background_color or "#000000"
            if not cue.background_color:
                background_opacity = 100
    if scene and background_color:
        from ducai_studio.providers.export.ffmpeg import _INK_SHAPES, _ink_splash_ass_drawing

        if cue.background_style in _INK_SHAPES:
            extra_x = max(3, font.pixelSize() * (0.34 if cue.orientation == "vertical" else 0.22))
            extra_y = max(3, font.pixelSize() * (0.22 if cue.orientation == "vertical" else 0.34))
            rect = background_rect.adjusted(-extra_x, -extra_y, extra_x, extra_y)
            background = path_from_drawing(
                _ink_splash_ass_drawing(
                    cue,
                    width,
                    height,
                    bounds=(
                        round(rect.x()),
                        round(rect.y()),
                        round(rect.width()),
                        round(rect.height()),
                    ),
                )
            )
        else:
            background.addRoundedRect(background_rect, 2, 2)
    elif background_color:
        background.addRect(background_rect)
    text_color = _readable_color(
        cue.text_color or ("#FFFFFF" if scene else style.primary_color),
        background_color,
        background_opacity,
    )
    background_coordinates = [
        background_rect.x(),
        background_rect.y(),
        background_rect.width(),
        background_rect.height(),
    ]
    # This panel reproduces line baselines/alignment exactly when a globally
    # placed dialogue is moved for the first time. OCR bounds are evidence,
    # never the generated Vietnamese text's placement.
    selection = background.boundingRect().united(bounds)
    return {
        "text_path": _drawing(path),
        "background_path": _drawing(background),
        "text_color": text_color,
        "text_opacity": cue.text_opacity_percent * (1 if scene else style.opacity_percent / 100),
        "background_color": background_color,
        "background_opacity": background_opacity,
        "outline_color": "#000000" if scene else style.outline_color,
        "outline_width": outline,
        "text_rect": [bounds.x(), bounds.y(), bounds.width(), bounds.height()],
        "background_rect": background_coordinates,
        "concealment_rect": (
            background_coordinates
            if not scene or cue.background_style == "blur"
            else [bounds.x(), bounds.y(), bounds.width(), bounds.height()]
        ),
        "font_size": font.pixelSize(),
        "placement_rect": [placement.x(), placement.y(), placement.width(), placement.height()],
        "selection_rect": [selection.x(), selection.y(), selection.width(), selection.height()],
    }


def artwork_payload(cue, width: int, height: int, style: SubtitleStyle) -> dict:
    return {
        "kind": "scene" if isinstance(cue, RenderSceneText) else "dialogue",
        "width": width,
        "height": height,
        "cue": asdict(cue),
        "style": asdict(style),
    }


def editor_payload(cue: dict, width: int, height: int, style: SubtitleStyle) -> dict:
    from ducai_studio.application.export_pipeline import _render_scene_text, _scene_text_layout
    from ducai_studio.domain.project import SubtitleCue
    from ducai_studio.domain.timebase import TimeRange

    text = str(cue.get("translated_text") or cue.get("source_text") or "")
    scene = cue.get("cue_role") in {"scene_text", "title_text"}
    values = {
        field: cue[field]
        for field in (
            "visual_box",
            "visual_box_source",
            "orientation",
            "style_hint",
            "panel_box",
            "text_anchor",
            "background_color",
            "text_color",
            "review_state",
            "scene_font_scale",
            "text_font_size",
            "background_padding_x",
            "background_padding_y",
            "background_style",
            "letter_spacing_percent",
            "font_bold",
            "font_italic",
            "text_opacity_percent",
            "background_opacity_percent",
            "fade_enabled",
            "fade_duration_ms",
        )
        if field in cue
    }
    domain_cue = SubtitleCue(
        str(cue["cue_id"]),
        int(cue["index"]),
        TimeRange(int(cue["start_us"]), int(cue["end_us"])),
        text,
        "vi",
        role="scene_text" if scene else "dialogue",
        **values,
    )
    if scene:
        rendered = _render_scene_text(
            domain_cue,
            geometry=domain_cue,
            render_width=width,
            render_height=height,
            clip_start_us=domain_cue.range.start_us,
            clip_end_us=domain_cue.range.end_us,
            conform_plan=None,
        )
    else:
        from ducai_studio.application.export_pipeline import _cue_font_family

        rendered = RenderSubtitle(
            domain_cue.id,
            domain_cue.index,
            domain_cue.range.start_us,
            domain_cue.range.end_us,
            _scene_text_layout(domain_cue.text, domain_cue.orientation),
            max(1, round(style.font_size * domain_cue.text_font_size / 12)),
            domain_cue.orientation,
            domain_cue.background_color,
            domain_cue.text_color,
            *(domain_cue.panel_box or (-1.0, -1.0, 0.0, 0.0)),
            _cue_font_family(domain_cue.style_hint),
            domain_cue.letter_spacing_percent,
            domain_cue.font_bold,
            domain_cue.font_italic,
            domain_cue.text_opacity_percent,
            domain_cue.background_opacity_percent,
            domain_cue.background_padding_x,
            domain_cue.background_padding_y,
            text_anchor=domain_cue.text_anchor,
            background_style=domain_cue.background_style,
        )
    return artwork_payload(rendered, width, height, style)


def concealment_rect(layout: dict, width: int, height: int, pad_x: float, pad_y: float) -> list:
    x, y, w, h = layout.get("concealment_rect", layout["text_rect"])
    left, top = max(0, x / width - pad_x), max(0, y / height - pad_y)
    right, bottom = min(1, (x + w) / width + pad_x), min(1, (y + h) / height + pad_y)
    return [left, top, max(1 / width, right - left), max(1 / height, bottom - top)]


def layout_batch(payloads: list[dict]) -> list[dict]:
    instance = QCoreApplication.instance()
    if instance is not None and not isinstance(instance, QGuiApplication):
        # Headless contract tests may already own QCoreApplication. Production
        # export workers have no Qt application and resolve fonts directly.
        result = subprocess.run(
            [sys.executable, "-m", __name__],
            input=json.dumps(payloads),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return json.loads(result.stdout)
    return [layout_json(json.dumps(payload, sort_keys=True)) for payload in payloads]


def paint_artwork(layout: dict, width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if layout["background_path"]:
        color = QColor(layout["background_color"])
        color.setAlphaF(layout["background_opacity"] / 100)
        painter.fillPath(path_from_drawing(layout["background_path"]), color)
    color = QColor(layout["text_color"])
    outline = QColor(layout["outline_color"])
    path = path_from_drawing(layout["text_path"])
    # Draw a centred outline behind an opaque fill, then composite text alpha
    # once. Painting the stroke last consumes thin glyphs; overlapping two
    # translucent passes also darkens their interiors. A tight temporary layer
    # avoids slow boolean path subtraction on complex calligraphy contours.
    rect = (
        path.boundingRect()
        .adjusted(
            -layout["outline_width"] - 1,
            -layout["outline_width"] - 1,
            layout["outline_width"] + 1,
            layout["outline_width"] + 1,
        )
        .intersected(QRectF(0, 0, width, height))
        .toAlignedRect()
    )
    if not rect.isEmpty():
        text_image = QImage(rect.size(), QImage.Format.Format_ARGB32_Premultiplied)
        text_image.fill(Qt.GlobalColor.transparent)
        text_painter = QPainter(text_image)
        text_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text_painter.translate(-rect.x(), -rect.y())
        if layout["outline_width"] > 0:
            pen = QPen(outline, layout["outline_width"] * 2)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            text_painter.strokePath(path, pen)
        text_painter.fillPath(path, color)
        text_painter.end()
        painter.setOpacity(layout["text_opacity"] / 100)
        painter.drawImage(rect.topLeft(), text_image)
    painter.end()
    return image


if __name__ == "__main__":
    print(json.dumps(layout_batch(json.load(sys.stdin))))
