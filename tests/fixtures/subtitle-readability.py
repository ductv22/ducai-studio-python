"""Probe filled glyphs independently of outline/background pixels."""

from __future__ import annotations

import json
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter

from ducai_studio.domain.effects import RenderSubtitle, SubtitleStyle
from ducai_studio.providers.subtitle_artwork import (
    artwork_payload,
    layout_batch,
    paint_artwork,
    path_from_drawing,
)


def main() -> None:
    app = QGuiApplication([])
    results = []
    for size, outline in ((20, 2), (38, 2), (20, 12), (38, 0)):
        cue = RenderSubtitle(
            "cue",
            1,
            0,
            1_000_000,
            "Chân Giới đại hội sẽ gặp phải chuyện sau đó",
            font_size=size,
            text_color="#FFFFFF",
            background_color="#000000",
        )
        layout = layout_batch(
            [
                artwork_payload(
                    cue,
                    854,
                    480,
                    SubtitleStyle(
                        outline_width=outline,
                    ),
                )
            ]
        )[0]
        actual = paint_artwork(layout, 854, 480)
        reference = QImage(854, 480, QImage.Format.Format_ARGB32_Premultiplied)
        reference.fill(Qt.GlobalColor.transparent)
        painter = QPainter(reference)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(path_from_drawing(layout["text_path"]), QColor("white"))
        painter.end()
        expected = visible = 0
        for y in range(480):
            for x in range(854):
                if reference.pixelColor(x, y).alpha() < 240:
                    continue
                expected += 1
                color = actual.pixelColor(x, y)
                visible += min(color.red(), color.green(), color.blue()) > 220
        results.append({"size": size, "outline": outline, "fill_retained": visible / expected})
        transparent = replace(cue, background_color="", text_opacity_percent=50)
        alpha_layout = layout_batch(
            [artwork_payload(transparent, 854, 480, SubtitleStyle(outline_width=outline))]
        )[0]
        alpha_image = paint_artwork(alpha_layout, 854, 480)
        alpha_max = max(
            alpha_image.pixelColor(x, y).alpha() for y in range(480) for x in range(854)
        )
        assert 126 <= alpha_max <= 128, alpha_max
    print(json.dumps(results))
    app.quit()


if __name__ == "__main__":
    main()
