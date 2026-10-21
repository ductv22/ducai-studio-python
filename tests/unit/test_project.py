import pytest

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

