from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ducai_studio.domain.effects import RenderSceneText, RenderSubtitle, SubtitleStyle
from ducai_studio.providers.subtitle_artwork import (
    artwork_payload,
    concealment_rect,
    layout_batch,
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system font regression")
def test_headless_export_resolves_the_same_arial_as_windows_preview():
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    script = (
        "from ducai_studio.providers.subtitle_artwork import _ensure_fonts; "
        "from PySide6.QtGui import QFont,QFontInfo; _ensure_fonts(); "
        "assert QFontInfo(QFont('Arial')).family() == 'Arial'"
    )
    subprocess.run([sys.executable, "-c", script], check=True, env=env, timeout=15)


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("ffmpeg") is None,
                    reason="Native Windows font/export comparison")
def test_real_project_preview_matches_separate_native_export_worker(tmp_path):
    script = Path(__file__).parents[1] / "fixtures" / "subtitle-project-parity.py"
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    result = subprocess.run([sys.executable, str(script), str(tmp_path), shutil.which("ffmpeg")],
        env={**env, "QT_QUICK_BACKEND": "software"}, capture_output=True,
        text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "comparison.json").read_text())
    assert report["input_parity"] and report["iou"] > .9


@pytest.mark.parametrize("alignment", [1, 2, 3])
@pytest.mark.parametrize("text", ["Mẹ con ta không nơi nương tựa.", "Một câu dài\nVà dòng ngắn"])
def test_measured_dialogue_placement_preserves_font_and_position_on_first_move(alignment, text):
    from dataclasses import replace

    cue = RenderSubtitle("cue", 1, 0, 1_000_000, text, font_size=38)
    style = SubtitleStyle(alignment=alignment)
    original = layout_batch([artwork_payload(cue, 854, 480, style)])[0]
    x, y, w, h = original["placement_rect"]
    moved = replace(cue, panel_x=x / 854, panel_y=y / 480 - .1,
                    panel_width=w / 854, panel_height=h / 480)
    actual = layout_batch([artwork_payload(moved, 854, 480, style)])[0]
    assert actual["font_size"] == original["font_size"]
    ox, oy, ow, oh = original["text_rect"]
    assert actual["text_rect"] == pytest.approx([ox, oy - 48, ow, oh], abs=.01)


def test_lightweight_placement_matches_artwork_without_generating_paths():
    script = ("import json; from ducai_studio.providers.subtitle_artwork import layout_json; "
              "from ducai_studio.domain.effects import RenderSubtitle, SubtitleStyle; "
              "from ducai_studio.providers.subtitle_artwork import artwork_payload; "
              "p=json.dumps(artwork_payload(RenderSubtitle('c',1,0,1000000,'Hồng Ly tỷ tỷ'),"
              "854,480,SubtitleStyle())); a=layout_json(p); b=layout_json(p,False); "
              "assert a['placement_rect']==b['placement_rect']; "
              "assert a['font_size']==b['font_size']; assert 'text_path' not in b")
    subprocess.run([sys.executable, "-c", script], check=True, timeout=15,
                   env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})


def test_dialogue_outline_never_paints_over_the_foreground() -> None:
    script = Path(__file__).parents[1] / "fixtures" / "subtitle-readability.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    samples = json.loads(result.stdout)
    assert all(sample["fill_retained"] > 0.98 for sample in samples), samples


def test_dialogue_background_is_a_padded_rectangle_and_cover_contains_it() -> None:
    cue = RenderSubtitle(
        "cue",
        1,
        0,
        1_000_000,
        "Có nhiều Tiểu Bảo như vậy",
        font_size=38,
        text_color="#FFFFFF",
        background_color="#000000",
    )
    layout = layout_batch([artwork_payload(cue, 854, 480, SubtitleStyle())])[0]
    x, y, w, h = layout["text_rect"]
    bx, by, bw, bh = layout["background_rect"]
    assert bx <= x - 12 and bx + bw >= x + w + 12
    assert by <= y - 8 and by + bh >= y + h + 8
    assert "b " not in layout["background_path"]  # A flat rectangle, not an ink silhouette.
    left, top, width, height = concealment_rect(layout, 854, 480, 0.012, 0.008)
    assert left * 854 <= bx and (left + width) * 854 >= bx + bw
    assert top * 480 <= by and (top + height) * 480 >= by + bh


@pytest.mark.parametrize(
    ("foreground", "background", "expected"),
    [
        ("#000000", "#000000", "#FFFFFF"),
        ("#FFFFFF", "#FFFFFF", "#000000"),
        ("#FFFF00", "#000000", "#FFFF00"),
        ("#FFFFFF", "#000000", "#FFFFFF"),
    ],
)
def test_solid_background_keeps_foreground_readable(foreground, background, expected) -> None:
    cue = RenderSubtitle(
        "cue",
        1,
        0,
        1_000_000,
        "Hồng Ly tỷ tỷ",
        text_color=foreground,
        background_color=background,
    )
    layout = layout_batch([artwork_payload(cue, 854, 480, SubtitleStyle())])[0]
    assert layout["text_color"] == expected


def test_dialogue_padding_and_concealment_grow_with_final_vietnamese_length() -> None:
    from dataclasses import replace

    cue = RenderSubtitle("cue", 1, 0, 1_000_000, "Cha", background_color="#000000")
    layouts = layout_batch(
        [
            artwork_payload(value, 854, 480, SubtitleStyle())
            for value in (
                cue,
                replace(cue, text="Có nhiều Tiểu Bảo như vậy"),
                replace(cue, background_padding_x=80, background_padding_y=60),
            )
        ]
    )
    assert layouts[1]["background_rect"][2] > layouts[0]["background_rect"][2]
    assert layouts[2]["background_rect"][2] > layouts[0]["background_rect"][2]
    assert layouts[2]["background_rect"][3] > layouts[0]["background_rect"][3]


def test_scene_font_and_background_values_above_100_reach_shared_artwork():
    from ducai_studio.domain.effects import RenderSceneText

    scene = RenderSceneText(
        "scene", 1, 0, 1_000_000, "Việt", .1, .1, .2, .2, .1, .1, .2, .2,
        font_size=240, font_family="Be Vietnam Pro", background_color="#000000",
        background_padding_x=300, background_padding_y=150, letter_spacing_percent=120,
    )
    layout = layout_batch([artwork_payload(scene, 1920, 1080, SubtitleStyle())])[0]
    assert layout["font_size"] == 240
    assert layout["background_rect"][2] > layout["text_rect"][2] + 1400
    assert layout["background_rect"][3] > layout["text_rect"][3] + 700
    dialogue = RenderSubtitle("dialogue", 1, 0, 1_000_000, "Việt", font_size=200,
                              background_padding_x=300, background_padding_y=150,
                              letter_spacing_percent=120, background_color="#000000")
    fitted = layout_batch([artwork_payload(dialogue, 1920, 1080, SubtitleStyle())])[0]
    assert fitted["font_size"] > 0 and fitted["background_rect"][2] > 0


def test_dialogue_fits_short_sentence_without_wrapping_and_cover_contains_it() -> None:
    cue = RenderSubtitle(
        "cue",
        1,
        0,
        1_000_000,
        "Trần Trường An rất xuất sắc,",
        font_size=160,
        panel_x=0.1,
        panel_y=0.8,
        panel_width=0.6,
        panel_height=0.12,
    )
    layout = layout_batch([artwork_payload(cue, 854, 480, SubtitleStyle())])[0]
    assert layout["font_size"] < 160
    x, y, w, h = layout["text_rect"]
    assert x >= 0.1 * 854 and x + w <= 0.7 * 854 + 1
    left, top, width, height = concealment_rect(layout, 854, 480, 0.012, 0.008)
    assert left * 854 <= x and (left + width) * 854 >= x + w
    assert top * 480 <= y and (top + height) * 480 >= y + h
    assert h < 0.12 * 480  # A short sentence stays on one line and shrinks to fit.


@pytest.mark.parametrize("font", ["Comforter Brush", "Playwrite VN", "Water Brush"])
@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_scene_artwork_keeps_ink_tight_to_measured_glyphs(font, orientation) -> None:
    cue = RenderSceneText(
        "scene",
        1,
        0,
        1_000_000,
        "Hồng Ly" if orientation == "horizontal" else "Hồng\nLy",
        0.1,
        0.1,
        0.2,
        0.2,
        0.1,
        0.1,
        0.3,
        0.3,
        font_family=font,
        font_size=30,
        orientation=orientation,
        background_color="#000000",
        background_style="ink_splash",
    )
    layout = layout_batch([artwork_payload(cue, 854, 480, SubtitleStyle())])[0]
    assert layout["text_color"] == "#FFFFFF"
    assert layout["text_path"] and layout["background_path"]
    # The exported background is a separate vector path, never ASS BorderStyle=3
    # inherited from the dialogue's color or opaque rectangle.
    assert layout["outline_width"] == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Real render requires FFmpeg")
def test_qml_and_real_export_preserve_glyph_ink_color_and_bounds(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures" / "subtitle-render-parity.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), shutil.which("ffmpeg")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "comparison.json").read_text())
    assert report["preview_bounds"] == report["export_bounds"]
    assert report["iou"] > 0.90
    assert all(min(counts) > 40 for counts in report["foreground_pixels"].values())
    x, y, w, h = report["cover_rect"]
    assert x >= 0 and y >= 0 and x + w <= 854 and y + h <= 480
