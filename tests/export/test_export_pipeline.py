from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ducai_studio.application.effects_commands import (
    DEFAULT_BLUR_ID,
    DEFAULT_LOGO_ID,
    EffectsEditor,
    ensure_default_overlays,
)
from ducai_studio.application.export_pipeline import (
    SCENE_TEXT_FONT_FAMILIES,
    _scene_text_font_size,
    _scene_text_layout,
    build_export_snapshot,
)
from ducai_studio.domain.effects import (
    ExportSettings,
    ExportSnapshot,
    RenderClip,
    RenderOverlay,
    RenderSceneText,
    RenderSubtitleBlur,
    SubtitleStyle,
    VideoFilterSettings,
)
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    Overlay,
    OverlayKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import SpeedRatio, TimeRange
from ducai_studio.domain.timeline_edit import cut_state
from ducai_studio.providers.export.ffmpeg import (
    FfmpegCapabilities,
    _ass_color,
    _ink_splash_ass_drawing,
    _range_enable,
    _scene_label_background_pixels,
    _video_filter_chain,
    _write_ass,
    decode_export_snapshot,
    materialize_export_chunk,
)
from ducai_studio.providers.media_probe import MediaProbeResult


class Probe:
    def probe(self, _media_path):
        return MediaProbeResult(10_000_000, True, True, 1920, 1080, "mp4")


def test_scene_text_font_catalog_includes_vietnamese_brush_alternatives() -> None:
    assert SCENE_TEXT_FONT_FAMILIES["calligraphy_brush"] == "Comforter Brush"
    assert SCENE_TEXT_FONT_FAMILIES["calligraphy_water"] == "Water Brush"
    assert SCENE_TEXT_FONT_FAMILIES["calligraphy_splash"] == "Splash"
    assert SCENE_TEXT_FONT_FAMILIES["calligraphy_hurricane"] == "Hurricane"


def test_timed_effect_enable_uses_half_open_project_ranges() -> None:
    assert _range_enable(0, 100_000, 0, 1_000_000) == ("gte(t,0.000000)*lt(t,0.100000)")


def test_schema_two_export_snapshot_decodes_without_dynamic_blur_field() -> None:
    snapshot = ExportSnapshot(
        2,
        "legacy-export",
        1,
        0,
        1_000_000,
        1920,
        1080,
        RenderClip(
            "video",
            "V1",
            "video",
            "movie.mp4",
            0,
            1_000_000,
            0,
            1_000_000,
        ),
        (),
        (),
        (),
        ExportSettings(),
    )
    raw = json.loads(json.dumps(asdict(snapshot)))
    raw.pop("subtitle_blurs")
    raw.pop("video_segments")

    decoded = decode_export_snapshot(raw)

    assert decoded.schema_version == 2
    assert decoded.subtitle_blurs == ()
    assert decoded.video_segments == ()


def _project(tmp_path: Path) -> ProjectSnapshot:
    video = tmp_path / "movie.mp4"
    voice = tmp_path / "voice.wav"
    video.write_bytes(b"video")
    voice.write_bytes(b"voice")
    timeline = TimeRange(0, 10_000_000)
    media = (
        MediaAsset("video", str(video), MediaKind.VIDEO, timeline.end_us),
        MediaAsset("voice", str(voice), MediaKind.AUDIO, timeline.end_us),
    )
    return ProjectSnapshot(
        "project",
        "Movie",
        1,
        media=media,
        tracks=(
            TimelineTrack(
                "V1",
                TrackKind.VIDEO,
                "Video",
                (TimelineClip("v", "V1", timeline, timeline, "video", volume=0.0),),
            ),
            TimelineTrack(
                "D1",
                TrackKind.VOICE,
                "Voice",
                (TimelineClip("d", "D1", timeline, timeline, "voice", volume=0.9),),
            ),
        ),
        source_cues=(SubtitleCue("cue", 1, TimeRange(1_000_000, 2_000_000), "你好。", "zh"),),
        translated_cues=(
            SubtitleCue("cue", 1, TimeRange(1_000_000, 2_000_000), "Xin chào.", "vi"),
        ),
    )


@pytest.mark.parametrize("edited", [False, True])
def test_plain_video_export_needs_no_subtitles_or_ai_stages(tmp_path, edited):
    project = _project(tmp_path)
    video = replace(project.tracks[0].clips[0], volume=0.8, link_group_id="source")
    audio = replace(video, id="original", track_id="A1")
    project = replace(
        project,
        tracks=(
            replace(project.tracks[0], clips=(video,)),
            TimelineTrack("A1", TrackKind.ORIGINAL_AUDIO, "Original", (audio,)),
        ),
        source_cues=(),
        translated_cues=(),
    )
    if edited:
        project = replace(project, timeline_edit=cut_state(
            project, "remove_range", 2_000_000, 5_000_000))

    snapshot = build_export_snapshot(project, settings=ExportSettings(), media_probe=Probe())

    assert snapshot.timeline_end_us == (7_000_000 if edited else 10_000_000)
    assert snapshot.subtitles == snapshot.scene_texts == snapshot.subtitle_blurs == ()
    assert all(clip.track_kind == "original_audio" and clip.volume == 0.8
               for clip in snapshot.audio)
    assert len(snapshot.audio) == (2 if edited else 1)
    assert snapshot.settings.subtitle_style.enabled is True


def test_default_overlays_cover_full_video_and_logo_asset_can_be_replaced(
    tmp_path: Path,
) -> None:
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"logo")
    seeded = ensure_default_overlays(_project(tmp_path), logo_path=logo)

    assert [item.id for item in seeded.overlays] == [DEFAULT_BLUR_ID, DEFAULT_LOGO_ID]
    assert all(item.range == TimeRange(0, 10_000_000) for item in seeded.overlays)
    assert seeded.overlays[0].enabled is True
    assert seeded.overlays[1].enabled is True
    assert seeded.overlays[0].blur_radius == 46
    assert seeded.overlays[0].padding_x == pytest.approx(0.02)
    assert seeded.overlays[0].padding_y == pytest.approx(0.014)
    assert seeded.overlays[0].concealment_mode == "cover"

    updated = EffectsEditor(seeded).update_overlay(
        DEFAULT_BLUR_ID,
        x=0.1,
        width=0.8,
        blur_radius=30,
        enabled=False,
    )
    blur = next(item for item in updated.overlays if item.id == DEFAULT_BLUR_ID)
    assert (blur.x, blur.width, blur.blur_radius, blur.enabled) == (0.1, 0.8, 30, False)


def test_blur_background_without_project_overlay_matches_preview_fallback_radius(tmp_path):
    snapshot = build_export_snapshot(_project(tmp_path), media_probe=Probe(),
        settings=ExportSettings(subtitle_style=SubtitleStyle(background_mode="blur")))
    assert len(snapshot.subtitle_blurs) == 1
    assert snapshot.subtitle_blurs[0].blur_radius == 46


def test_voice_track_crossing_a_video_layer_keeps_its_full_range(tmp_path):
    project = _project(tmp_path)
    second = replace(project.tracks[0].clips[0], id="layer", track_id="V2",
                     timeline_range=TimeRange(3_000_000, 4_000_000),
                     source_range=TimeRange(0, 1_000_000))
    project = replace(project, tracks=(*project.tracks,
        TimelineTrack("V2", TrackKind.VIDEO, "Layer", (second,))))
    snapshot = build_export_snapshot(project, settings=ExportSettings(), media_probe=Probe())
    voice = [item for item in snapshot.audio if item.track_kind == "voice"]
    assert len(voice) == 1
    assert (voice[0].timeline_start_us, voice[0].timeline_end_us) == (0, 10_000_000)
    decoded = decode_export_snapshot(json.loads(json.dumps(asdict(snapshot))))
    assert decoded == snapshot and [item.track_id for item in decoded.video_segments] == [
        "V1", "V2", "V1"]


def test_effects_editor_supports_multiple_blur_regions_and_deletes_added_region(
    tmp_path: Path,
) -> None:
    seeded = ensure_default_overlays(_project(tmp_path))
    editor = EffectsEditor(seeded)
    expanded = editor.add_blur("overlay-blur-extra")
    extra = next(item for item in expanded.overlays if item.id == "overlay-blur-extra")
    assert extra.kind.value == "blur"
    assert extra.range == TimeRange(0, 10_000_000)
    assert 0 < extra.x < 1 and 0 < extra.y < 1

    removed = editor.delete_overlay(extra.id)
    assert all(item.id != extra.id for item in removed.overlays)
    with pytest.raises(ValueError, match="disabled"):
        EffectsEditor(removed).delete_overlay(DEFAULT_BLUR_ID)


def test_ocr_anchor_is_identical_in_preview_export_and_snapshot_roundtrip(tmp_path):
    from PySide6.QtCore import QCoreApplication

    from ducai_studio.providers.subtitle_artwork import artwork_payload, editor_payload
    from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel

    project = _project(tmp_path)
    project = replace(project, translated_cues=(
        replace(project.translated_cues[0], text_anchor=(.5, .335)),))
    snapshot = build_export_snapshot(project, settings=ExportSettings(), media_probe=Probe())
    assert snapshot.subtitles[0].text_anchor == (.5, .335)
    assert decode_export_snapshot(json.loads(json.dumps(asdict(snapshot)))) == snapshot
    QCoreApplication.instance() or QCoreApplication([])
    model = ProjectStateModel()
    model.replace(json.loads(json.dumps(asdict(project))))
    cue = model.subtitle_cues.find("cue")
    assert cue["text_anchor"] == [.5, .335]
    assert editor_payload(cue, 1920, 1080, snapshot.settings.subtitle_style) == artwork_payload(
        snapshot.subtitles[0], 1920, 1080, snapshot.settings.subtitle_style)


def test_export_cannot_overwrite_a_secondary_video_layer(tmp_path):
    from ducai_studio.job_manager.service import JobManagerService

    project = _project(tmp_path)
    layer_path = tmp_path / "second.mp4"
    layer_path.write_bytes(b"preserve second source")
    interval = TimeRange(2_000_000, 3_000_000)
    layer = TimelineClip("second", "V2", interval, TimeRange(0, 1_000_000), "second")
    project = replace(project,
        media=(*project.media, MediaAsset("second", str(layer_path), MediaKind.VIDEO, 1_000_000)),
        tracks=(*project.tracks, TimelineTrack("V2", TrackKind.VIDEO, "Layer", (layer,))))
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts", media_probe=Probe())
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=project)
    with pytest.raises(ValueError, match="source"):
        service.create_export_job(project_id=project.id, output_path=str(layer_path),
                                  export_options={"overwrite": True})
    assert layer_path.read_bytes() == b"preserve second source"


def test_overlay_rejects_invalid_or_non_blur_concealment_mode() -> None:
    values = (
        "overlay",
        OverlayKind.BLUR,
        TimeRange(0, 1_000_000),
        0.1,
        0.8,
        0.8,
        0.1,
    )
    with pytest.raises(ValueError, match="concealment mode"):
        Overlay(*values, concealment_mode="remove")
    with pytest.raises(ValueError, match="Only blur overlays"):
        Overlay(
            "logo",
            OverlayKind.LOGO,
            TimeRange(0, 1_000_000),
            0.1,
            0.1,
            0.2,
            0.1,
            concealment_mode="cover",
        )


def test_export_snapshot_keeps_video_audio_subtitle_and_effect_contract(
    tmp_path: Path,
) -> None:
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"logo")
    project = ensure_default_overlays(_project(tmp_path), logo_path=logo)

    snapshot = build_export_snapshot(
        project,
        settings=ExportSettings(subtitle_style=SubtitleStyle(enabled=True)),
        media_probe=Probe(),
    )

    assert snapshot.video.path.endswith("movie.mp4")
    assert [item.track_kind for item in snapshot.audio] == ["voice"]
    assert {item.kind for item in snapshot.overlays} == {"logo"}
    assert len(snapshot.subtitle_blurs) == 1
    assert snapshot.subtitle_blurs[0].region_source == "estimated"
    assert snapshot.subtitle_blurs[0].concealment_mode == "cover"
    assert snapshot.subtitle_blurs[0].start_us == snapshot.subtitles[0].start_us
    assert snapshot.subtitle_blurs[0].end_us == snapshot.subtitles[0].end_us
    assert snapshot.subtitles[0].text == "Xin chào."


def test_export_snapshot_splits_scene_text_from_dialogue_and_fits_vertical_panel(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    source_scene = SubtitleCue(
        "scene",
        2,
        TimeRange(3_000_000, 5_000_000),
        "海王兽",
        "zh",
        visual_box=(0.80, 0.25, 0.07, 0.28),
        visual_box_source="ocr",
        role="scene_text",
        orientation="vertical",
        style_hint="calligraphy",
        panel_box=(0.75, 0.19, 0.17, 0.40),
        background_color="#121212",
        background_style="ink_splash",
        scene_font_scale=0.75,
    )
    translated_scene = replace(
        source_scene,
        text="Hải Vương Thú",
        language="vi",
        scene_font_scale=2.0,
    )
    project = replace(
        project,
        source_cues=(*project.source_cues, source_scene),
        translated_cues=(*project.translated_cues, translated_scene),
    )

    snapshot = build_export_snapshot(
        project,
        settings=ExportSettings(),
        media_probe=Probe(),
    )

    assert snapshot.schema_version == 4
    assert [cue.cue_id for cue in snapshot.subtitles] == ["cue"]
    assert len(snapshot.scene_texts) == 1
    scene = snapshot.scene_texts[0]
    assert scene.text == "Hải\nVương\nThú"
    assert scene.orientation == "vertical"
    assert scene.font_family == "Playwrite VN"
    assert scene.background_color == "#121212"
    assert scene.font_size == _scene_text_font_size(
        "Hải\nVương\nThú",
        orientation="vertical",
        panel_width_px=round(0.17 * 1920),
        panel_height_px=round(0.40 * 1080),
        glyph_width_px=round(0.07 * 1920),
        glyph_height_px=round(0.28 * 1080),
        font_scale=2.0,  # Final cue formatting, not the source cue's old scale.
        style_hint="calligraphy",
    )
    assert (scene.panel_x, scene.panel_y, scene.panel_width, scene.panel_height) == (
        0.75,
        0.19,
        0.17,
        0.40,
    )


def test_scene_text_font_size_uses_source_glyph_and_manual_scale_monotonically() -> None:
    automatic = _scene_text_font_size(
        "Hàn Lập",
        orientation="horizontal",
        panel_width_px=700,
        panel_height_px=300,
        glyph_width_px=160,
        glyph_height_px=40,
    )
    smaller = _scene_text_font_size(
        "Hàn Lập",
        orientation="horizontal",
        panel_width_px=700,
        panel_height_px=300,
        glyph_width_px=160,
        glyph_height_px=40,
        font_scale=0.5,
    )
    larger = _scene_text_font_size(
        "Hàn Lập",
        orientation="horizontal",
        panel_width_px=700,
        panel_height_px=300,
        glyph_width_px=160,
        glyph_height_px=40,
        font_scale=2.0,
    )
    narrow = _scene_text_font_size(
        "Một danh xưng tiếng Việt rất dài",
        orientation="horizontal",
        panel_width_px=80,
        panel_height_px=30,
        glyph_width_px=70,
        glyph_height_px=26,
        font_scale=2.0,
    )

    assert automatic == 36
    assert smaller == 18
    assert larger > automatic
    assert narrow > 0
    assert smaller < automatic < larger


def test_scene_text_font_size_word_values_keep_growing_past_panel_fit() -> None:
    sizes = [
        _scene_text_font_size(
            "Hàn Lập",
            orientation="horizontal",
            panel_width_px=120,
            panel_height_px=60,
            glyph_width_px=100,
            glyph_height_px=48,
            font_scale=value / 12,
        )
        for value in (12, 36, 72)
    ]

    assert sizes[0] < sizes[1] < sizes[2]


def test_export_scene_text_uses_persisted_manual_scale() -> None:
    base = _scene_text_font_size(
        "Hàn Lập",
        orientation="horizontal",
        panel_width_px=700,
        panel_height_px=300,
        glyph_width_px=160,
        glyph_height_px=40,
    )
    enlarged = _scene_text_font_size(
        "Hàn Lập",
        orientation="horizontal",
        panel_width_px=700,
        panel_height_px=300,
        glyph_width_px=160,
        glyph_height_px=40,
        font_scale=1.25,
    )

    assert enlarged > base


def test_scene_text_layout_preserves_horizontal_line_breaks() -> None:
    assert _scene_text_layout("  Tên rất dài  \n  Hai dòng  ", "horizontal") == (
        "Tên rất dài\nHai dòng"
    )


def test_export_scene_text_sizing_uses_output_resolution(tmp_path: Path) -> None:
    project = _project(tmp_path)
    source_scene = SubtitleCue(
        "scene-output-size",
        2,
        TimeRange(3_000_000, 5_000_000),
        "天命",
        "zh",
        visual_box=(0.10, 0.20, 0.20, 0.10),
        visual_box_source="ocr",
        role="scene_text",
        panel_box=(0.08, 0.18, 0.30, 0.16),
        scene_font_scale=1.25,
    )
    translated_scene = replace(
        source_scene,
        text="Thiên\nMệnh",
        language="vi",
        scene_font_scale=1.25,
    )
    project = replace(
        project,
        source_cues=(*project.source_cues, source_scene),
        translated_cues=(*project.translated_cues, translated_scene),
    )

    snapshot = build_export_snapshot(
        project,
        settings=ExportSettings(output_width=854, output_height=480),
        media_probe=Probe(),
    )

    scene = snapshot.scene_texts[0]
    assert scene.text == "Thiên\nMệnh"
    assert scene.font_size == _scene_text_font_size(
        scene.text,
        orientation="horizontal",
        panel_width_px=round(0.30 * 854),
        panel_height_px=round(0.16 * 480),
        glyph_width_px=round(0.20 * 854),
        glyph_height_px=round(0.10 * 480),
        font_scale=1.25,
    )


def test_scene_text_ass_uses_panel_position_without_shrinking_or_clipping(tmp_path: Path) -> None:
    path = tmp_path / "scene-text.ass"
    cue = RenderSceneText(
        "scene-clip",
        1,
        0,
        1_000_000,
        "WWWWWW",
        0.25,
        0.25,
        0.20,
        0.20,
        0.25,
        0.25,
        0.50,
        0.50,
        font_size=42,
    )

    _write_ass(
        path,
        [],
        chunk_start_us=0,
        chunk_end_us=1_000_000,
        width=800,
        height=400,
        style=SubtitleStyle(),
        scene_texts=[cue],
    )

    ass = path.read_text(encoding="utf-8-sig")
    assert r"\pos(400,200)" in ass
    assert r"\clip(200,100,600,300)" not in ass


def test_calligraphy_scene_text_uses_libass_metrics_and_remains_readable() -> None:
    font_size = _scene_text_font_size(
        "W\nW\nW",
        orientation="vertical",
        panel_width_px=200,
        panel_height_px=500,
        glyph_width_px=180,
        glyph_height_px=450,
        font_scale=2.0,
        style_hint="calligraphy",
    )

    assert font_size > 0


def test_scene_text_export_preserves_font_and_background_padding(tmp_path: Path) -> None:
    project = _project(tmp_path)
    source_scene = SubtitleCue(
        "scene-style",
        2,
        TimeRange(3_000_000, 5_000_000),
        "仙人",
        "zh",
        visual_box=(0.20, 0.20, 0.20, 0.12),
        visual_box_source="ocr",
        role="scene_text",
        style_hint="calligraphy_soft",
        panel_box=(0.18, 0.18, 0.26, 0.18),
        background_color="#121212",
        background_padding_x=240,
        background_padding_y=120,
        background_style="ink_splash",
        letter_spacing_percent=180,
        font_bold=True,
        font_italic=True,
        text_opacity_percent=84,
        background_opacity_percent=93,
        fade_enabled=True,
        fade_duration_ms=450,
    )
    translated_scene = replace(source_scene, text="Tiên Nhân", language="vi")
    project = replace(
        project,
        source_cues=(*project.source_cues, source_scene),
        translated_cues=(*project.translated_cues, translated_scene),
    )

    snapshot = build_export_snapshot(project, settings=ExportSettings(), media_probe=Probe())
    scene = snapshot.scene_texts[0]

    assert scene.font_family == "Dancing Script"
    assert scene.background_style == "ink_splash"
    assert scene.background_padding_x == 240
    assert scene.background_padding_y == 120
    assert scene.letter_spacing_percent == 180
    assert scene.font_bold is True
    assert scene.font_italic is True
    assert scene.text_opacity_percent == 84
    assert scene.background_opacity_percent == 93
    assert scene.fade_enabled is True
    assert scene.fade_duration_ms == 450
    assert scene.source_blur_enabled is False


def test_scene_size_above_100_continues_scaling_in_shared_render_layout():
    params = dict(text="Tên", orientation="horizontal", panel_width_px=500,
                  panel_height_px=300, glyph_width_px=300, glyph_height_px=100)
    small = _scene_text_font_size(**params, font_scale=100 / 12)
    large = _scene_text_font_size(**params, font_scale=300 / 12)
    assert large > small


def test_manual_scene_text_keeps_label_but_skips_source_glyph_blur(tmp_path: Path) -> None:
    project = _project(tmp_path)
    source_scene = SubtitleCue(
        "manual-scene",
        2,
        TimeRange(3_000_000, 5_000_000),
        "Tên cảnh",
        "vi",
        source="manual_scene_text",
        visual_box=(0.20, 0.20, 0.24, 0.12),
        visual_box_source="estimated",
        role="scene_text",
        style_hint="calligraphy_brush",
        panel_box=(0.18, 0.18, 0.30, 0.18),
        background_color="#000000",
        text_color="#FFFFFF",
        background_style="ink_splash",
        background_opacity_percent=100,
        fade_enabled=True,
    )
    translated_scene = replace(source_scene, text="Tên cảnh", language="vi")
    snapshot = build_export_snapshot(
        replace(
            project,
            source_cues=(*project.source_cues, source_scene),
            translated_cues=(*project.translated_cues, translated_scene),
        ),
        settings=ExportSettings(),
        media_probe=Probe(),
    )

    scene = snapshot.scene_texts[0]
    assert scene.source_blur_enabled is False
    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=10_000_000,
        work_dir=tmp_path / "manual-scene-work",
        output_path=tmp_path / "manual-scene.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")
    ass = plan.ass_path.read_text(encoding="utf-8-sig") if plan.ass_path else ""

    assert "vscene0mask" not in graph
    assert r"\p1" in ass  # Shared measured glyph outlines, not a second font layout.


def test_scene_export_uses_final_translation_style_not_source_style(tmp_path: Path) -> None:
    project = _project(tmp_path)
    source = SubtitleCue(
        "scene",
        2,
        TimeRange(3_000_000, 5_000_000),
        "名字",
        "zh",
        role="scene_text",
        visual_box=(0.8, 0.2, 0.1, 0.2),
        visual_box_source="ocr",
        panel_box=(0.7, 0.15, 0.2, 0.3),
        text_color="#FFFF00",
        background_style="solid",
        background_color="#123456",
    )
    final = replace(
        source,
        text="Tô Tiểu Ly",
        language="vi",
        text_color="#FFFFFF",
        style_hint="calligraphy_brush",
        background_style="ink_splash",
        panel_box=(0.75, 0.18, 0.15, 0.25),
        text_font_size=18,
        background_color="#000000",
        background_opacity_percent=100,
    )
    snapshot = build_export_snapshot(
        replace(
            project,
            source_cues=(*project.source_cues, source),
            translated_cues=(*project.translated_cues, final),
        ),
        settings=ExportSettings(),
        media_probe=Probe(),
    )
    scene = snapshot.scene_texts[0]
    assert scene.text_color == "#FFFFFF" and scene.background_style == "ink_splash"
    assert scene.font_family == "Comforter Brush"
    assert (scene.panel_x, scene.panel_y, scene.panel_width, scene.panel_height) == final.panel_box
    assert scene.background_color == "#000000" and scene.background_opacity_percent == 100


def test_translated_concealment_uses_final_timing_and_legacy_defaults(tmp_path: Path) -> None:
    project = ensure_default_overlays(_project(tmp_path))
    final = replace(project.translated_cues[0], range=TimeRange(2_500_000, 3_500_000))
    snapshot = build_export_snapshot(
        replace(project, translated_cues=(final,)), settings=ExportSettings(), media_probe=Probe()
    )
    blur = snapshot.subtitle_blurs[0]
    assert (blur.start_us, blur.end_us) == (final.range.start_us, final.range.end_us)
    assert blur.tracking_mode == "translated_text"
    raw = json.loads(json.dumps(asdict(snapshot)))
    for key in ("tracking_mode", "padding_x", "padding_y"):
        raw["subtitle_blurs"][0].pop(key)
    decoded = decode_export_snapshot(raw).subtitle_blurs[0]
    assert decoded.tracking_mode == "source_subtitle" and decoded.padding_x == 0.012


def test_legacy_scene_text_export_defaults_to_source_glyph_blur() -> None:
    scene = RenderSceneText(
        "legacy-scene",
        1,
        0,
        1_000_000,
        "Tên cảnh",
        0.2,
        0.2,
        0.2,
        0.1,
        0.18,
        0.18,
        0.26,
        0.16,
    )
    snapshot = ExportSnapshot(
        4,
        "legacy-project",
        1,
        0,
        1_000_000,
        1920,
        1080,
        RenderClip(
            "video",
            "V1",
            "video",
            "movie.mp4",
            0,
            1_000_000,
            0,
            1_000_000,
        ),
        (),
        (),
        (),
        ExportSettings(),
        (),
        (),
        (scene,),
    )
    raw = json.loads(json.dumps(asdict(snapshot)))
    raw["scene_texts"][0].pop("source_blur_enabled")

    decoded = decode_export_snapshot(raw)

    assert decoded.scene_texts[0].source_blur_enabled is True


def test_s1_dialogue_export_preserves_per_cue_typography_and_position(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    source = replace(
        project.source_cues[0],
        text_font_size=36,
        orientation="vertical",
        style_hint="calligraphy_bold",
        background_color="#102030",
        text_color="#F0E0D0",
        panel_box=(0.22, 0.18, 0.40, 0.36),
        subtitle_track_id="S1",
    )
    translated = replace(source, text="Hồng Ly tỷ tỷ", language="vi")
    settings = ExportSettings()

    snapshot = build_export_snapshot(
        replace(project, source_cues=(source,), translated_cues=(translated,)),
        settings=settings,
        media_probe=Probe(),
    )
    subtitle = snapshot.subtitles[0]

    assert subtitle.font_size == settings.subtitle_style.font_size * 3
    assert subtitle.orientation == "vertical"
    assert subtitle.font_family == "Lobster"
    assert subtitle.background_color == "#102030"
    assert subtitle.text_color == "#F0E0D0"
    assert (
        subtitle.panel_x,
        subtitle.panel_y,
        subtitle.panel_width,
        subtitle.panel_height,
    ) == pytest.approx((0.22, 0.18, 0.40, 0.36))


def test_scene_text_background_padding_expands_tight_label_without_leaving_frame() -> None:
    cue = RenderSceneText(
        "scene-padding",
        1,
        0,
        1_000_000,
        "Hồng Ly tỷ tỷ",
        0.42,
        0.40,
        0.16,
        0.08,
        0.80,
        0.86,
        0.18,
        0.12,
        font_size=48,
        background_color="#101010",
    )

    default_box = _scene_label_background_pixels(cue, 1920, 1080)
    padded_box = _scene_label_background_pixels(
        replace(cue, background_padding_x=60, background_padding_y=40),
        1920,
        1080,
    )

    assert padded_box[2] > default_box[2]
    assert padded_box[3] > default_box[3]
    assert 0 <= padded_box[0] <= 1920 - padded_box[2]
    assert 0 <= padded_box[1] <= 1080 - padded_box[3]


def test_ink_scene_text_writes_vector_background_before_brush_text(tmp_path: Path) -> None:
    path = tmp_path / "ink-scene-text.ass"
    cue = RenderSceneText(
        "ink-scene",
        2,
        0,
        1_000_000,
        "Ma\nTôn",
        0.70,
        0.15,
        0.10,
        0.42,
        0.67,
        0.12,
        0.16,
        0.50,
        orientation="vertical",
        style_hint="calligraphy_ink",
        font_family="Agbalumo",
        font_size=58,
        background_color="#080808",
        background_style="ink_splash",
        background_padding_x=34,
        background_padding_y=26,
        fade_enabled=True,
        fade_duration_ms=300,
    )

    _write_ass(
        path,
        [],
        chunk_start_us=0,
        chunk_end_us=1_000_000,
        width=1920,
        height=1080,
        style=SubtitleStyle(),
        scene_texts=[cue],
    )

    ass = path.read_text(encoding="utf-8-sig")
    assert r"\p1" in ass
    assert "Agbalumo" in ass
    assert "Dialogue: 0" in ass
    assert "Dialogue: 1" in ass
    assert ass.count(r"\fad(300,300)") == 2
    assert ass.index(r"\p1") < ass.index("Agbalumo")


def test_scene_text_fade_is_clamped_and_does_not_restart_at_chunk_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "short-scene-text.ass"
    cue = RenderSceneText(
        "short-scene",
        1,
        0,
        400_000,
        "A Cẩu",
        0.1,
        0.1,
        0.2,
        0.1,
        0.08,
        0.08,
        0.3,
        0.14,
        fade_enabled=True,
        fade_duration_ms=300,
    )

    _write_ass(
        path,
        [],
        chunk_start_us=100_000,
        chunk_end_us=400_000,
        width=1920,
        height=1080,
        style=SubtitleStyle(),
        scene_texts=[cue],
    )

    ass = path.read_text(encoding="utf-8-sig")
    assert r"\fad(0,200)" in ass
    assert r"\fad(300,300)" not in ass


@pytest.mark.parametrize(
    "background_style",
    ("ink_splash", "ink_wash", "ink_cloud", "brush_stroke"),
)
@pytest.mark.parametrize(
    ("orientation", "text", "panel"),
    (
        ("vertical", "Tịch\nTà\nThần\nLôi", (0.70, 0.08, 0.16, 0.78)),
        ("horizontal", "Hồng Ly tỷ tỷ", (0.08, 0.66, 0.82, 0.18)),
    ),
)
def test_ink_variants_keep_vector_geometry_inside_frame_for_extreme_aspects(
    background_style: str,
    orientation: str,
    text: str,
    panel: tuple[float, float, float, float],
) -> None:
    cue = RenderSceneText(
        cue_id=f"{background_style}-{orientation}",
        index=3,
        start_us=0,
        end_us=1_000_000,
        text=text,
        glyph_x=panel[0],
        glyph_y=panel[1],
        glyph_width=panel[2],
        glyph_height=panel[3],
        panel_x=panel[0],
        panel_y=panel[1],
        panel_width=panel[2],
        panel_height=panel[3],
        orientation=orientation,
        font_size=72,
        background_color="#080808",
        background_style=background_style,
        background_padding_x=34,
        background_padding_y=26,
    )

    drawing = _ink_splash_ass_drawing(cue, 1920, 1080)
    coordinates = [int(value) for value in re.findall(r"-?\d+", drawing)]

    assert drawing.startswith("m ")
    assert " b " in drawing
    assert len(coordinates) % 2 == 0
    assert all(0 <= value <= 1920 for value in coordinates[0::2])
    assert all(0 <= value <= 1080 for value in coordinates[1::2])


def test_scene_text_font_size_scales_past_legacy_240_pixel_cap_for_4k() -> None:
    preview = _scene_text_font_size(
        "Hàn Lập",
        orientation="horizontal",
        panel_width_px=960,
        panel_height_px=300,
        glyph_width_px=500,
        glyph_height_px=210,
        font_scale=1.0,
    )
    export_4k = _scene_text_font_size(
        "Hàn Lập",
        orientation="horizontal",
        panel_width_px=3840,
        panel_height_px=1200,
        glyph_width_px=2000,
        glyph_height_px=840,
        font_scale=1.0,
    )

    assert preview > 0
    assert abs(export_4k - preview * 4) <= 3
    assert export_4k > 240


def test_scene_text_word_like_sizes_are_strictly_monotonic() -> None:
    def rendered_size(editor_size: int) -> int:
        return _scene_text_font_size(
            "Hồng Ly tỷ tỷ",
            orientation="horizontal",
            panel_width_px=640,
            panel_height_px=240,
            glyph_width_px=360,
            glyph_height_px=120,
            font_scale=editor_size / 12,
            style_hint="calligraphy_soft",
        )

    assert rendered_size(12) < rendered_size(36) < rendered_size(72)


def test_export_snapshot_renders_ocr_watermark_as_strong_dynamic_concealment(
    tmp_path: Path,
) -> None:
    watermark = Overlay(
        "overlay-ocr-watermark-1",
        OverlayKind.BLUR,
        TimeRange(0, 10_000_000),
        0.72,
        0.08,
        0.26,
        0.09,
        blur_radius=46,
        tracking_mode="ocr_watermark",
        concealment_mode="cover",
    )
    project = replace(_project(tmp_path), overlays=(watermark,))

    snapshot = build_export_snapshot(
        project,
        settings=ExportSettings(),
        media_probe=Probe(),
    )

    assert snapshot.overlays == ()
    assert len(snapshot.subtitle_blurs) == 1
    rendered = snapshot.subtitle_blurs[0]
    assert rendered.cue_id == watermark.id
    assert rendered.region_source == "ocr"
    assert rendered.concealment_mode == "cover"
    assert rendered.blur_radius == 46
    assert (rendered.x, rendered.y, rendered.width, rendered.height) == (
        0.72,
        0.08,
        0.26,
        0.09,
    )
    assert (rendered.start_us, rendered.end_us) == (0, 10_000_000)


def test_export_snapshot_preserves_cue_scoped_d1_clips(tmp_path: Path) -> None:
    project = _project(tmp_path)
    d1 = next(track for track in project.tracks if track.id == "D1")
    cue_ranges = (TimeRange(1_000_000, 1_900_000), TimeRange(3_000_000, 4_250_000))
    split_d1 = replace(
        d1,
        clips=tuple(
            TimelineClip(
                f"d{index}",
                "D1",
                cue_range,
                cue_range,
                "voice",
                volume=0.9,
            )
            for index, cue_range in enumerate(cue_ranges, 1)
        ),
    )
    split_project = replace(
        project,
        tracks=tuple(split_d1 if track.id == "D1" else track for track in project.tracks),
    )

    snapshot = build_export_snapshot(
        split_project,
        settings=ExportSettings(),
        media_probe=Probe(),
    )

    assert len(snapshot.audio) == 2
    assert [item.timeline_start_us for item in snapshot.audio] == [
        1_000_000,
        3_000_000,
    ]
    assert all(item.path.endswith("voice.wav") for item in snapshot.audio)


def test_chunk_plan_opens_cue_scoped_master_wav_once_on_windows(tmp_path: Path) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    voice = (tmp_path / "voice.wav").resolve()
    source.write_bytes(b"source")
    cue_duration_us = 600_000
    cue_count = 1_000
    duration_us = cue_duration_us * cue_count
    with wave.open(str(voice), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(8_000)
        writer.writeframes(b"\x81" * round(duration_us * 8_000 / 1_000_000))
    voice_clips = tuple(
        RenderClip(
            f"voice-{index}",
            "D1",
            "voice",
            str(voice),
            index * cue_duration_us,
            (index + 1) * cue_duration_us,
            index * cue_duration_us,
            (index + 1) * cue_duration_us,
        )
        for index in range(cue_count)
    )
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        RenderClip(
            "video",
            "V1",
            "video",
            str(source),
            0,
            duration_us,
            0,
            duration_us,
        ),
        voice_clips,
        (),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "work-many-cues",
        output_path=tmp_path / "many-cues.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")

    assert plan.arguments.count(str(voice)) == 0
    assert len(plan.temporary_paths) == 1
    assert plan.arguments.count(str(plan.temporary_paths[0])) == 1
    assert len(subprocess.list2cmdline(plan.arguments)) < 32_767
    assert graph.count("atrim=start=") == 1
    assert "amix=inputs=1:duration=longest:normalize=0" in graph
    assert graph.count("apad=whole_dur=") == 1
    assert ",apad," not in graph
    with wave.open(str(plan.temporary_paths[0]), "rb") as reader:
        assert reader.getnframes() == round(duration_us * 8_000 / 1_000_000)
        assert reader.readframes(1) == b"\x81"


def test_precomposed_pcm_voice_chunk_preserves_samples_and_timeline_gaps(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    voice = (tmp_path / "voice.wav").resolve()
    source.write_bytes(b"source")
    source_samples = bytes((index % 100) + 1 for index in range(400))
    with wave.open(str(voice), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(1_000)
        writer.writeframes(source_samples)
    clips = tuple(
        RenderClip(
            f"voice-{index}",
            "D1",
            "voice",
            str(voice),
            index * 100_000,
            index * 100_000 + 50_000,
            index * 50_000,
            index * 50_000 + 50_000,
        )
        for index in range(8)
    )
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        800_000,
        852,
        480,
        RenderClip("video", "V1", "video", str(source), 0, 800_000, 0, 800_000),
        clips,
        (),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=800_000,
        work_dir=tmp_path / "precomposed-voice-work",
        output_path=tmp_path / "precomposed-voice.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )

    assert len(plan.temporary_paths) == 1
    with wave.open(str(plan.temporary_paths[0]), "rb") as reader:
        rendered = reader.readframes(reader.getnframes())
    for index in range(8):
        destination = index * 100
        source_start = index * 50
        assert (
            rendered[destination : destination + 50]
            == source_samples[source_start : source_start + 50]
        )
        assert rendered[destination + 50 : destination + 100] == b"\x80" * 50


def test_overlapping_voice_cues_keep_the_compatible_ffmpeg_mix_path(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    voice = (tmp_path / "voice.wav").resolve()
    source.write_bytes(b"source")
    with wave.open(str(voice), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8_000)
        writer.writeframes(b"\0\0" * 8_000)
    clips = tuple(
        RenderClip(
            f"voice-{index}",
            "D1",
            "voice",
            str(voice),
            0,
            500_000,
            index * 50_000,
            index * 50_000 + 500_000,
        )
        for index in range(8)
    )
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        500_000,
        852,
        480,
        RenderClip("video", "V1", "video", str(source), 0, 500_000, 0, 500_000),
        clips,
        (),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=500_000,
        work_dir=tmp_path / "overlap-fallback-work",
        output_path=tmp_path / "overlap-fallback.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")

    assert plan.temporary_paths == ()
    assert plan.arguments.count(str(voice)) == 1
    assert graph.count("atrim=start=") == 8
    assert "amix=inputs=8:duration=longest:normalize=0" in graph


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
def test_reused_voice_master_keeps_a_late_cue_after_a_timeline_gap(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    source = (tmp_path / "source.mp4").resolve()
    voice = (tmp_path / "voice.wav").resolve()
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=12:d=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=4",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(voice),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    cue_ranges = (
        (100_000, 500_000, 0, 400_000),
        (600_000, 1_000_000, 400_000, 800_000),
        (1_100_000, 1_500_000, 800_000, 1_200_000),
        (3_000_000, 3_400_000, 2_000_000, 2_400_000),
    )
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        4_000_000,
        160,
        90,
        RenderClip("video", "V1", "video", str(source), 0, 4_000_000, 0, 4_000_000),
        tuple(
            RenderClip(
                f"voice-{index}",
                "D1",
                "voice",
                str(voice),
                timeline_start,
                timeline_end,
                source_start,
                source_end,
            )
            for index, (
                timeline_start,
                timeline_end,
                source_start,
                source_end,
            ) in enumerate(cue_ranges)
        ),
        (),
        (),
        ExportSettings(
            backend="cpu",
            video_bitrate="300k",
            audio_bitrate="96k",
            fps=12,
            subtitle_style=SubtitleStyle(enabled=False),
        ),
    )
    output = tmp_path / "late-cue.mp4"
    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=4_000_000,
        work_dir=tmp_path / "work-late-cue",
        output_path=output,
        capabilities=FfmpegCapabilities(ffmpeg, "ffmpeg test", False, True, True),
    )

    subprocess.run(
        plan.arguments,
        check=True,
        capture_output=True,
        timeout=30,
    )
    decoded = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            "3.1",
            "-t",
            "0.2",
            "-i",
            str(output),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    samples = [
        int.from_bytes(decoded.stdout[index : index + 2], "little", signed=True)
        for index in range(0, len(decoded.stdout) - 1, 2)
    ]

    assert samples
    assert max(abs(sample) for sample in samples) > 500


def test_subtitle_opacity_and_video_filter_compile_to_real_ffmpeg_values() -> None:
    style = SubtitleStyle(
        primary_color="#FFFFFF",
        outline_color="#102030",
        alignment=3,
        opacity_percent=60,
    )
    video_filter = VideoFilterSettings(
        preset_id="cinematic-warm",
        intensity_percent=50,
        brightness_percent=10,
        contrast_percent=110,
        saturation_percent=120,
    )

    filters = _video_filter_chain(video_filter)

    assert _ass_color(style.primary_color, style.opacity_percent) == "&H66FFFFFF"
    assert filters[0].startswith("colorbalance=")
    assert filters[1].startswith("eq=brightness=0.1000:")
    assert "contrast=1.1440" in filters[1]
    assert "saturation=1.2720" in filters[1]


def test_grayscale_filter_blends_with_user_saturation() -> None:
    filters = _video_filter_chain(
        VideoFilterSettings(
            preset_id="grayscale",
            intensity_percent=50,
            saturation_percent=120,
        )
    )

    assert filters == ["eq=brightness=0.0000:contrast=1.0000:saturation=0.6000"]


def test_export_snapshot_keeps_chronological_video_segments_with_offsets_and_speed(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    track = project.tracks[0]
    source_bytes = (tmp_path / "movie.mp4").read_bytes()
    second = TimelineClip(
        "v2",
        "V1",
        TimeRange(10_000_000, 14_000_000),
        TimeRange(2_000_000, 10_000_000),
        "video",
        speed=SpeedRatio(2, 1),
    )
    project = ProjectSnapshot(
        project.id,
        project.name,
        project.revision,
        media=project.media,
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (*track.clips, second)),),
    )

    snapshot = build_export_snapshot(
        project,
        settings=ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
        media_probe=Probe(),
    )

    assert snapshot.timeline_end_us == 14_000_000
    assert [item.clip_id for item in snapshot.video_segments] == ["v", "v2"]
    assert snapshot.video_segments[1].source_start_us == 2_000_000
    assert snapshot.video_segments[1].speed_numerator == 2
    assert (tmp_path / "movie.mp4").read_bytes() == source_bytes

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=8_000_000,
        end_us=12_000_000,
        work_dir=tmp_path / "split-work",
        output_path=tmp_path / "split.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")
    assert plan.arguments.count(str((tmp_path / "movie.mp4").resolve())) == 2
    assert "setpts=0.500000000000*PTS" in graph
    assert "concat=n=2:v=1:a=0[vsegments]" in graph


def test_export_snapshot_materializes_middle_cut_before_collecting_tracks(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project = replace(
        project,
        timeline_edit=cut_state(project, "remove_range", 2_000_000, 5_000_000),
    )

    snapshot = build_export_snapshot(
        project,
        settings=ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
        media_probe=Probe(),
    )

    assert snapshot.timeline_end_us == 7_000_000
    assert [
        (
            item.timeline_start_us,
            item.timeline_end_us,
            item.source_start_us,
            item.source_end_us,
        )
        for item in snapshot.video_segments
    ] == [
        (0, 2_000_000, 0, 2_000_000),
        (2_000_000, 7_000_000, 5_000_000, 10_000_000),
    ]
    assert [
        (
            item.timeline_start_us,
            item.timeline_end_us,
            item.source_start_us,
            item.source_end_us,
        )
        for item in snapshot.audio
    ] == [
        (0, 2_000_000, 0, 2_000_000),
        (2_000_000, 7_000_000, 5_000_000, 10_000_000),
    ]


def test_export_snapshot_rejects_video_timeline_gap(tmp_path: Path) -> None:
    project = _project(tmp_path)
    track = project.tracks[0]
    second = TimelineClip(
        "v2",
        "V1",
        TimeRange(11_000_000, 21_000_000),
        TimeRange(0, 10_000_000),
        "video",
    )
    project = replace(
        project,
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (*track.clips, second)),),
        translated_cues=(),
    )

    with pytest.raises(ValueError, match="gap"):
        build_export_snapshot(
            project,
            settings=ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
            media_probe=Probe(),
        )


def test_chunk_plan_reuses_aligned_source_audio_and_skips_same_size_scale(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    voice = (tmp_path / "voice.wav").resolve()
    source.write_bytes(b"source")
    voice.write_bytes(b"voice")
    duration_us = 10_000_000
    video = RenderClip(
        "video",
        "V1",
        "video",
        str(source),
        0,
        duration_us,
        0,
        duration_us,
    )
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        video,
        (
            RenderClip(
                "original",
                "A1",
                "original_audio",
                str(source),
                0,
                duration_us,
                0,
                duration_us,
                volume=0.2,
            ),
            RenderClip(
                "voice",
                "D1",
                "voice",
                str(voice),
                1_234_567,
                duration_us,
                1_234_567,
                duration_us,
            ),
        ),
        (),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
    )
    capabilities = FfmpegCapabilities("ffmpeg", "ffmpeg test", True, True, True)

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "work",
        output_path=tmp_path / "chunk.mp4",
        capabilities=capabilities,
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")

    assert plan.arguments.count(str(source)) == 1
    assert plan.arguments.count(str(voice)) == 1
    assert "[0:a]" in graph
    assert "[1:a]" in graph
    assert "amix=inputs=2:duration=longest:normalize=0" in graph
    assert "alimiter=limit=0.84139514:level=0:latency=1" in graph
    assert "aresample=48000" in graph
    assert "adelay=59259S:all=1" in graph
    assert "scale=852:480:flags=lanczos" not in graph

    misaligned = replace(
        snapshot,
        audio=(
            replace(snapshot.audio[0], timeline_start_us=1_000_000),
            snapshot.audio[1],
        ),
    )
    separate_plan = materialize_export_chunk(
        misaligned,
        index=1,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "separate-work",
        output_path=tmp_path / "separate-chunk.mp4",
        capabilities=capabilities,
    )

    assert separate_plan.arguments.count(str(source)) == 2


def test_chunk_plan_decodes_static_logo_once_and_avoids_chunk_faststart(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    logo = (tmp_path / "logo.png").resolve()
    source.write_bytes(b"source")
    logo.write_bytes(b"logo")
    duration_us = 10_000_000
    snapshot = ExportSnapshot(
        1,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        RenderClip(
            "video",
            "V1",
            "video",
            str(source),
            0,
            duration_us,
            0,
            duration_us,
        ),
        (),
        (
            RenderOverlay(
                "logo",
                "logo",
                0,
                duration_us,
                0.8,
                0.05,
                0.15,
                0.1,
                str(logo),
            ),
        ),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "work",
        output_path=tmp_path / "chunk.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", True, True, True),
    )

    assert plan.arguments.count(str(logo)) == 1
    assert "-loop" not in plan.arguments
    assert "-framerate" not in plan.arguments
    assert "-movflags" not in plan.arguments
    assert "eof_action=repeat" in plan.filter_script_path.read_text(encoding="utf-8")


def test_dynamic_subtitle_blur_groups_one_full_frame_source_per_radius(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    source.write_bytes(b"source")
    duration_us = 100_000_000
    subtitle_blurs = tuple(
        RenderSubtitleBlur(
            f"cue-{index}",
            index + 1,
            index * 1_000_000,
            (index + 1) * 1_000_000,
            0.2,
            0.8,
            0.6,
            0.12,
            22 if index < 70 else 30,
            1.0,
            "ocr",
        )
        for index in range(100)
    )
    snapshot = ExportSnapshot(
        3,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        RenderClip(
            "video",
            "V1",
            "video",
            str(source),
            0,
            duration_us,
            0,
            duration_us,
        ),
        (),
        (),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
        (),
        subtitle_blurs,
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "dynamic-blur-work",
        output_path=tmp_path / "dynamic-blur.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")

    assert graph.count("boxblur=luma_radius=") == 2
    assert graph.count("luma_power=2") == 2
    assert "luma_power=1" not in graph
    assert graph.count("drawbox=") == 0
    assert graph.count("alphamerge") == 2
    assert graph.count("overlay=shortest=1") == 2
    assert graph.index("alphamerge") < graph.index("null[vout]")
    mask_files = sorted((tmp_path / "dynamic-blur-work").glob("subtitle-mask-*.ass"))
    assert len(mask_files) == 2
    assert (
        sum(path.read_text(encoding="utf-8-sig").count("Dialogue: 0,") for path in mask_files)
        == 100
    )


def test_dynamic_subtitle_cover_uses_strong_blur_and_dark_veil(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    source.write_bytes(b"source")
    duration_us = 2_000_000
    snapshot = ExportSnapshot(
        3,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        RenderClip(
            "video",
            "V1",
            "video",
            str(source),
            0,
            duration_us,
            0,
            duration_us,
        ),
        (),
        (),
        (),
        ExportSettings(subtitle_style=SubtitleStyle(enabled=False)),
        (),
        (
            RenderSubtitleBlur(
                "cue-1",
                1,
                0,
                duration_us,
                0.2,
                0.8,
                0.6,
                0.12,
                46,
                1.0,
                "ocr",
                "cover",
            ),
        ),
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "covered-blur-work",
        output_path=tmp_path / "covered-blur.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")

    assert "luma_power=2" in graph
    assert "chroma_power=2" in graph
    cover_path = next((tmp_path / "covered-blur-work").glob("subtitle-cover-*.ass"))
    cover_ass = cover_path.read_text(encoding="utf-8-sig")
    assert "\\c&H000000&" in cover_ass
    assert "\\1a&H1A&" in cover_ass
    assert graph.count("boxblur=luma_radius=") == 1


def test_scene_text_conceals_glyph_only_and_writes_positioned_calligraphy_ass(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "movie.mp4").resolve()
    source.write_bytes(b"source")
    duration_us = 2_000_000
    scene = RenderSceneText(
        "scene",
        1,
        0,
        duration_us,
        "Hải\nVương\nThú",
        0.80,
        0.25,
        0.07,
        0.28,
        0.75,
        0.19,
        0.17,
        0.40,
        "vertical",
        "calligraphy",
        "Playwrite VN",
        38,
        "#121212",
    )
    snapshot = ExportSnapshot(
        4,
        "project",
        1,
        0,
        duration_us,
        852,
        480,
        RenderClip("video", "V1", "video", str(source), 0, duration_us, 0, duration_us),
        (),
        (),
        (),
        ExportSettings(),
        (),
        (),
        (scene,),
    )

    plan = materialize_export_chunk(
        snapshot,
        index=0,
        start_us=0,
        end_us=duration_us,
        work_dir=tmp_path / "scene-work",
        output_path=tmp_path / "scene.mp4",
        capabilities=FfmpegCapabilities("ffmpeg", "ffmpeg test", False, True, True),
    )
    graph = plan.filter_script_path.read_text(encoding="utf-8")
    ass = plan.ass_path.read_text(encoding="utf-8-sig") if plan.ass_path else ""
    assert "vscene0mask" in graph
    assert not list((tmp_path / "scene-work").glob("scene-fill-*.ass"))
    assert "\\c&H121212&" in ass
    assert "\\alpha&H1A&" in ass
    assert r"\p1" in ass
    assert "\\pos(" in ass
