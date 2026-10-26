import pytest

from ducai_studio.domain.effects import RenderSubtitle, RenderSubtitleBlur
from ducai_studio.domain.project import (
    CUE_BACKGROUND_STYLES,
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
    TranslationPreferences,
    TranslationStylePreset,
)
from ducai_studio.domain.timebase import TimeRange


def test_translation_style_presets_validate_names_and_remain_unique() -> None:
    saved = TranslationStylePreset("  Cổ trang  ", "xianxia", "Dùng ta/ngươi.")
    assert saved.normalized_name == "Cổ trang"
    preferences = TranslationPreferences(saved_styles=(saved,))
    assert preferences.saved_styles == (saved,)

    with pytest.raises(ValueError, match="name is required"):
        TranslationStylePreset("   ")
    with pytest.raises(ValueError, match="unique"):
        TranslationPreferences(
            saved_styles=(
                TranslationStylePreset("Phong cách"),
                TranslationStylePreset("phong cách"),
            )
        )


def test_project_rejects_duplicate_media_ids() -> None:
    first = MediaAsset("media-1", "A.mp4", MediaKind.VIDEO, 10_000_000)
    second = MediaAsset("media-1", "B.mp4", MediaKind.VIDEO, 20_000_000)
    with pytest.raises(ValueError, match="unique"):
        ProjectSnapshot("project", "Demo", 1, media=(first, second))


@pytest.mark.parametrize("anchor", [[], [0.5], [0.5, 0.5, 0.1], [True, .5],
                                    [float("nan"), .5], [.5, float("inf")],
                                    [-.1, .5], [.5, 1.1], "0.5,0.5"])
def test_cue_rejects_invalid_text_anchor(anchor):
    with pytest.raises(ValueError, match="Text anchor"):
        SubtitleCue("c", 1, TimeRange(0, 1_000_000), "你好", "zh", text_anchor=anchor)


def test_track_rejects_clip_owned_by_another_track() -> None:
    clip = TimelineClip("clip", "wrong-track", TimeRange(0, 1_000_000))
    with pytest.raises(ValueError, match="owning track"):
        TimelineTrack("video-track", TrackKind.VIDEO, "Video", clips=(clip,))


def test_subtitle_cue_requires_text_and_valid_confidence() -> None:
    with pytest.raises(ValueError, match="empty"):
        SubtitleCue("cue", 1, TimeRange(0, 1_000_000), "", "zh")
    with pytest.raises(ValueError, match="confidence"):
        SubtitleCue("cue", 1, TimeRange(0, 1_000_000), "你好", "zh", confidence=1.1)

    with pytest.raises(ValueError, match="số nguyên dương"):
        SubtitleCue(
            "cue",
            1,
            TimeRange(0, 1_000_000),
            "你好",
            "zh",
            text_font_size=0,
        )

    cue = SubtitleCue(
        "cue",
        1,
        TimeRange(0, 1_000_000),
        "Xin chào",
        "vi",
        style_hint="calligraphy_bold",
        background_style="ink_splash",
        background_padding_x=24,
        background_padding_y=12,
    )
    assert cue.style_hint == "calligraphy_bold"
    assert cue.background_style == "ink_splash"
    assert cue.background_padding_x == 24
    assert cue.background_padding_y == 12

    assert {
        "solid",
        "none",
        "blur",
        "ink_splash",
        "ink_wash",
        "ink_cloud",
        "brush_stroke",
    } == CUE_BACKGROUND_STYLES
    for style in CUE_BACKGROUND_STYLES:
        styled = SubtitleCue(
            f"cue-{style}",
            1,
            TimeRange(0, 1_000_000),
            "Hồng Ly tỷ tỷ",
            "vi",
            background_style=style,
        )
        assert styled.background_style == style

    with pytest.raises(ValueError, match="Background padding"):
        SubtitleCue(
            "cue",
            1,
            TimeRange(0, 1_000_000),
            "Xin chào",
            "vi",
            background_padding_x=-1,
        )

    with pytest.raises(ValueError, match="Background style"):
        SubtitleCue(
            "cue",
            1,
            TimeRange(0, 1_000_000),
            "Xin chào",
            "vi",
            background_style="photocopy",
        )


def test_render_subtitle_background_style_supports_none_and_blur() -> None:
    for background in ("none", "blur"):
        rendered = RenderSubtitle("cue", 1, 0, 1_000_000, "Xin chào", background_style=background)
        assert rendered.background_style == background

    with pytest.raises(ValueError, match="background style"):
        RenderSubtitle("cue", 1, 0, 1_000_000, "Xin chào", background_style="legacy")


@pytest.mark.parametrize("duration_ms", [49, 2_001])
def test_render_subtitle_blur_validates_fade_duration(duration_ms: int) -> None:
    with pytest.raises(ValueError, match="fade duration"):
        RenderSubtitleBlur("cue", 1, 0, 1_000_000, 0.1, 0.1, 0.8, 0.2,
                           fade_duration_ms=duration_ms)

    with pytest.raises(TypeError, match="fade flag"):
        RenderSubtitleBlur("cue", 1, 0, 1_000_000, 0.1, 0.1, 0.8, 0.2,
                           fade_enabled=1)

