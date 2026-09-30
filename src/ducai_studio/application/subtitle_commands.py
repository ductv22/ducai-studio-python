from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from uuid import uuid4

from ducai_studio.domain.project import (
    CUE_BACKGROUND_STYLES,
    CUE_ROLES,
    CUE_STYLE_HINTS,
    SCENE_TEXT_DEFAULT_BACKGROUND_COLOR,
    SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT,
    SCENE_TEXT_DEFAULT_BACKGROUND_STYLE,
    SCENE_TEXT_DEFAULT_STYLE_HINT,
    SCENE_TEXT_DEFAULT_TEXT_COLOR,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
    subtitle_ranges_overlap,
    subtitle_track_number,
)
from ducai_studio.domain.project import (
    allocate_scene_text_lanes as allocate_scene_text_lanes,
)
from ducai_studio.domain.subtitles import VIETNAMESE_SUBTITLE_WRAP_CHARS
from ducai_studio.domain.timebase import TimeRange


class SubtitleEditError(ValueError):
    pass


SCENE_TEXT_MIN_DURATION_US = 100_000


ranges_overlap = subtitle_ranges_overlap


def auto_break_text(text: str, *, max_chars: int) -> str:
    if max_chars < 8:
        raise ValueError("Subtitle line length must be at least 8 characters")
    value = " ".join(text.replace("\n", " ").split())
    if len(value) <= max_chars:
        return value
    lines: list[str] = []
    remaining = value
    break_characters = " ，。！？；：、,.!?;:"
    while len(remaining) > max_chars:
        lower = max(1, int(max_chars * 0.55))
        positions = [
            index + 1
            for index, character in enumerate(remaining[: max_chars + 1])
            if index + 1 >= lower and character in break_characters
        ]
        split_at = positions[-1] if positions else max_chars
        lines.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        lines.append(remaining)
    return "\n".join(line for line in lines if line)


class SubtitleEditor:
    def __init__(
        self,
        snapshot: ProjectSnapshot,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self._undo: list[ProjectSnapshot] = []
        self._redo: list[ProjectSnapshot] = []
        self._id_factory = id_factory or (lambda: uuid4().hex)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def _new_id(self) -> str:
        value = self._id_factory().strip()
        if not value:
            raise RuntimeError("Subtitle id factory returned a blank value")
        return f"cue-{value}"

    def _cues(self, language: str) -> tuple[SubtitleCue, ...]:
        return self.snapshot.source_cues if language == "source" else self.snapshot.translated_cues

    def _replace_cues(
        self, language: str, cues: Iterable[SubtitleCue]
    ) -> ProjectSnapshot:
        normalized = self._normalize_cues(cues)
        if language == "source":
            return replace(self.snapshot, source_cues=normalized)
        if language == "translated":
            return replace(self.snapshot, translated_cues=normalized)
        raise SubtitleEditError("Subtitle language must be source or translated")

    @staticmethod
    def _normalize_cues(cues: Iterable[SubtitleCue]) -> tuple[SubtitleCue, ...]:
        return tuple(
            replace(cue, index=index)
            for index, cue in enumerate(
                sorted(cues, key=lambda item: (item.range.start_us, item.range.end_us, item.id)),
                start=1,
            )
        )

    def _commit(self, candidate: ProjectSnapshot) -> ProjectSnapshot:
        if candidate.id != self.snapshot.id:
            raise SubtitleEditError("Subtitle command cannot change project identity")
        previous = self.snapshot
        self.snapshot = replace(candidate, revision=previous.revision + 1)
        self._undo.append(previous)
        self._redo.clear()
        return self.snapshot

    def _rebuild_subtitle_tracks(
        self,
        source: tuple[SubtitleCue, ...],
        translated: tuple[SubtitleCue, ...],
    ) -> tuple[TimelineTrack, ...]:
        translated_by_index = {cue.index: cue for cue in translated}
        clips_by_cue: dict[str, TimelineClip] = {}
        clips_by_index: dict[int, TimelineClip] = {}
        for track in self.snapshot.tracks:
            if track.kind is not TrackKind.SUBTITLE:
                continue
            for clip in track.clips:
                if clip.cue_id:
                    clips_by_cue[clip.cue_id] = clip
                if clip.cue_index is not None:
                    clips_by_index[clip.cue_index] = clip
        projected: dict[str, list[TimelineClip]] = {}
        roles_by_track: dict[str, set[str]] = {}
        for cue in source:
            track_id = cue.subtitle_track_id or ("S1" if cue.is_dialogue else "S2")
            roles_by_track.setdefault(track_id, set()).add(cue.role)
            clip = clips_by_cue.get(cue.id) or clips_by_index.get(cue.index)
            translated_cue = translated_by_index.get(cue.index)
            cue_text = translated_cue.text if translated_cue is not None else cue.text
            if clip is None:
                clip = TimelineClip(
                    id=f"subtitle-{cue.id}",
                    track_id=track_id,
                    timeline_range=cue.range,
                    cue_id=cue.id,
                    cue_index=cue.index,
                    cue_text=cue_text,
                )
            else:
                clip = replace(
                    clip,
                    track_id=track_id,
                    timeline_range=cue.range,
                    # Once a subtitle cue becomes editor-authored, its timing
                    # is not constrained by the imported SRT media duration.
                    source_range=None,
                    cue_id=cue.id,
                    cue_index=cue.index,
                    cue_text=cue_text,
                    payload_revision=clip.payload_revision + 1,
                )
            projected.setdefault(track_id, []).append(clip)
        non_subtitle = tuple(
            track for track in self.snapshot.tracks if track.kind is not TrackKind.SUBTITLE
        )
        subtitle_tracks = tuple(
            TimelineTrack(
                track_id,
                TrackKind.SUBTITLE,
                (
                    "Phụ đề thoại"
                    if roles_by_track.get(track_id) == {"dialogue"}
                    else (
                        "Chữ trong cảnh"
                        if roles_by_track.get(track_id) == {"scene_text"}
                        else "Phụ đề / Chữ trong cảnh"
                    )
                ),
                tuple(
                    sorted(
                        clips,
                        key=lambda item: (
                            item.timeline_range.start_us,
                            item.timeline_range.end_us,
                            item.id,
                        ),
                    )
                ),
            )
            for track_id, clips in sorted(
                projected.items(),
                key=lambda item: subtitle_track_number(item[0], fallback=10_000),
            )
        )
        return non_subtitle + subtitle_tracks

    def _index(self, cues: tuple[SubtitleCue, ...], cue_id: str) -> int:
        for index, cue in enumerate(cues):
            if cue.id == cue_id:
                return index
        raise LookupError(f"Unknown subtitle cue: {cue_id}")

    @staticmethod
    def _assert_timeline(cues: Iterable[SubtitleCue]) -> None:
        previous_by_lane: dict[str, SubtitleCue] = {}
        for cue in sorted(cues, key=lambda item: item.range.start_us):
            lane = cue.subtitle_track_id or ("S1" if cue.is_dialogue else "S2")
            previous = previous_by_lane.get(lane)
            if previous is not None and previous.range.end_us > cue.range.start_us:
                raise SubtitleEditError("Subtitle cues cannot overlap")
            previous_by_lane[lane] = cue

    def add(
        self,
        *,
        language: str = "source",
        after_cue_id: str | None = None,
        text: str = "Cue mới",
    ) -> ProjectSnapshot:
        cues = self._cues(language)
        if after_cue_id:
            index = self._index(cues, after_cue_id)
            start_us = cues[index].range.end_us
            next_start = (
                cues[index + 1].range.start_us
                if index + 1 < len(cues)
                else start_us + 2_000_000
            )
            end_us = min(start_us + 2_000_000, next_start)
            if end_us <= start_us:
                raise SubtitleEditError("No timeline room after the selected cue")
        elif cues:
            start_us = cues[-1].range.end_us
            end_us = start_us + 2_000_000
        else:
            start_us, end_us = 0, 2_000_000
        language_code = "zh" if language == "source" else "vi"
        result = (
            *cues,
            SubtitleCue(
                self._new_id(), 1, TimeRange(start_us, end_us), text, language_code
            ),
        )
        self._assert_timeline(result)
        return self._commit(self._replace_cues(language, result))

    def update_text(self, cue_id: str, text: str, *, language: str = "source") -> ProjectSnapshot:
        if not text.strip():
            raise SubtitleEditError("Subtitle text cannot be empty")
        cues = list(self._cues(language))
        index = self._index(tuple(cues), cue_id)
        updated_cue = replace(
            cues[index],
            text=text.strip(),
            revision=cues[index].revision + 1,
        )
        cues[index] = updated_cue
        candidate = self._replace_cues(language, cues)
        tracks: list[TimelineTrack] = []
        for track in candidate.tracks:
            if track.kind is not TrackKind.SUBTITLE:
                tracks.append(track)
                continue
            clips = tuple(
                replace(
                    clip,
                    cue_text=updated_cue.text,
                    payload_revision=clip.payload_revision + 1,
                )
                if (
                    clip.cue_id == updated_cue.id
                    or (
                        clip.cue_id is None
                        and clip.cue_index == updated_cue.index
                    )
                )
                else clip
                for clip in track.clips
            )
            tracks.append(replace(track, clips=clips))
        return self._commit(replace(candidate, tracks=tuple(tracks)))

    def accept_scene_text(self, cue_id: str) -> ProjectSnapshot:
        source = list(self.snapshot.source_cues)
        source_index = self._index(tuple(source), cue_id)
        cue = source[source_index]
        if not cue.is_scene_text:
            raise SubtitleEditError("Only scene-text cues can be accepted")
        source[source_index] = replace(
            cue,
            review_state="accepted",
            revision=cue.revision + 1,
        )
        translated = list(self.snapshot.translated_cues)
        for index, translated_cue in enumerate(translated):
            if translated_cue.id == cue_id:
                translated[index] = replace(
                    translated_cue,
                    review_state="accepted",
                    revision=translated_cue.revision + 1,
                )
                break
        return self._commit(
            replace(
                self.snapshot,
                source_cues=tuple(source),
                translated_cues=tuple(translated),
            )
        )

    def set_scene_text_font_scale(self, cue_id: str, scale: float) -> ProjectSnapshot:
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            raise SubtitleEditError("Kích thước chữ trong cảnh phải là một số")
        normalized = round(float(scale), 2)
        if not math.isfinite(normalized) or not 0.5 <= normalized <= 2.0:
            raise SubtitleEditError(
                "Kích thước chữ trong cảnh phải nằm trong khoảng 50% đến 200%"
            )

        source = list(self.snapshot.source_cues)
        source_index = self._index(tuple(source), cue_id)
        cue = source[source_index]
        if not cue.is_scene_text:
            raise SubtitleEditError(
                "Chỉ cue chữ trong cảnh mới có thể chỉnh kích thước kiểu cũ"
            )
        source[source_index] = replace(
            cue,
            scene_font_scale=normalized,
            revision=cue.revision + 1,
        )
        translated = list(self.snapshot.translated_cues)
        for index, translated_cue in enumerate(translated):
            if translated_cue.id != cue.id and translated_cue.index != cue.index:
                continue
            translated[index] = replace(
                translated_cue,
                scene_font_scale=normalized,
                revision=translated_cue.revision + 1,
            )
        return self._commit(
            replace(
                self.snapshot,
                source_cues=tuple(source),
                translated_cues=tuple(translated),
            )
        )

    def set_text_format(
        self,
        cue_id: str,
        *,
        font_size: int,
        orientation: str,
        background_color: str,
    ) -> ProjectSnapshot:
        """Format one subtitle cue and its translated/source counterpart."""

        if type(font_size) is not int or not 1 <= font_size <= 100:
            raise SubtitleEditError("Kích thước chữ phải nằm trong khoảng 1 đến 100")
        if orientation not in {"horizontal", "vertical"}:
            raise SubtitleEditError("Hướng chữ phải là ngang hoặc dọc")
        color = background_color.strip().upper()
        if color:
            if len(color) != 7 or not color.startswith("#"):
                raise SubtitleEditError("Màu nền phải dùng định dạng #RRGGBB")
            try:
                int(color[1:], 16)
            except ValueError as exc:
                raise SubtitleEditError("Màu nền phải dùng định dạng #RRGGBB") from exc

        source = list(self.snapshot.source_cues)
        translated = list(self.snapshot.translated_cues)
        source_index = next(
            (index for index, cue in enumerate(source) if cue.id == cue_id),
            None,
        )
        translated_index = next(
            (index for index, cue in enumerate(translated) if cue.id == cue_id),
            None,
        )
        if source_index is None and translated_index is None:
            raise LookupError(f"Unknown subtitle cue: {cue_id}")
        matched = source[source_index] if source_index is not None else translated[translated_index]
        assert matched is not None

        def formatted(cue: SubtitleCue) -> SubtitleCue:
            return replace(
                cue,
                text_font_size=font_size,
                scene_font_scale=1.0,
                orientation=orientation,
                background_color=color,
                revision=cue.revision + 1,
            )

        if source_index is not None:
            source[source_index] = formatted(source[source_index])
        if translated_index is not None:
            translated[translated_index] = formatted(translated[translated_index])
        else:
            pair_index = next(
                (
                    index
                    for index, cue in enumerate(translated)
                    if cue.index == matched.index
                ),
                None,
            )
            if pair_index is not None:
                translated[pair_index] = formatted(translated[pair_index])
        if source_index is None:
            pair_index = next(
                (index for index, cue in enumerate(source) if cue.index == matched.index),
                None,
            )
            if pair_index is not None:
                source[pair_index] = formatted(source[pair_index])
        return self._commit(
            replace(
                self.snapshot,
                source_cues=tuple(source),
                translated_cues=tuple(translated),
            )
        )

    def set_text_format_many(
        self,
        cue_ids: tuple[str, ...],
        *,
        font_size: int,
        orientation: str,
        background_color: str,
        text_color: str = "",
        style_hint: str = "standard",
        background_padding_x: int = 20,
        background_padding_y: int = 10,
        background_style: str = "solid",
        letter_spacing_percent: int = 0,
        font_bold: bool = False,
        font_italic: bool = False,
        text_opacity_percent: int = 100,
        background_opacity_percent: int = 90,
    ) -> ProjectSnapshot:
        """Format selected cue identities in one undoable revision."""

        selected = tuple(dict.fromkeys(value.strip() for value in cue_ids if value.strip()))
        if not selected:
            raise SubtitleEditError("Hãy chọn ít nhất một cue chữ")
        if type(font_size) is not int or not 1 <= font_size <= 100:
            raise SubtitleEditError("Kích thước chữ phải nằm trong khoảng 1 đến 100")
        if orientation not in {"horizontal", "vertical"}:
            raise SubtitleEditError("Hướng chữ phải là ngang hoặc dọc")
        if style_hint not in {
            "standard",
            "display",
            "calligraphy",
            "calligraphy_soft",
            "calligraphy_bold",
            "calligraphy_ink",
            "calligraphy_brush",
            "calligraphy_water",
            "calligraphy_splash",
            "calligraphy_hurricane",
        }:
            raise SubtitleEditError("Kiểu chữ không hợp lệ")
        if background_style not in CUE_BACKGROUND_STYLES:
            raise SubtitleEditError("Kiểu nền không hợp lệ")
        for value, label in (
            (background_padding_x, "Nền ngang"),
            (background_padding_y, "Nền dọc"),
        ):
            if type(value) is not int or not 0 <= value <= 100:
                raise SubtitleEditError(f"{label} phải nằm trong khoảng 0 đến 100")
        for value, label in (
            (letter_spacing_percent, "Khoảng cách chữ"),
            (text_opacity_percent, "Độ đục chữ"),
            (background_opacity_percent, "Độ đục nền"),
        ):
            if type(value) is not int or not 0 <= value <= 100:
                raise SubtitleEditError(f"{label} phải nằm trong khoảng 0 đến 100")
        if type(font_bold) is not bool or type(font_italic) is not bool:
            raise SubtitleEditError("Đậm và nghiêng phải là giá trị bật/tắt")

        def normalized_color(value: str, label: str) -> str:
            color = value.strip().upper()
            if not color:
                return ""
            if len(color) != 7 or not color.startswith("#"):
                raise SubtitleEditError(f"{label} phải dùng định dạng #RRGGBB")
            try:
                int(color[1:], 16)
            except ValueError as exc:
                raise SubtitleEditError(f"{label} phải dùng định dạng #RRGGBB") from exc
            return color

        background = normalized_color(background_color, "Màu nền")
        foreground = normalized_color(text_color, "Màu chữ")
        source = list(self.snapshot.source_cues)
        translated = list(self.snapshot.translated_cues)
        target_indexes: set[int] = set()
        found_ids: set[str] = set()
        for cue in (*source, *translated):
            if cue.id in selected:
                target_indexes.add(cue.index)
                found_ids.add(cue.id)
        missing = set(selected) - found_ids
        if missing:
            raise LookupError(f"Unknown subtitle cue: {sorted(missing)[0]}")

        def formatted(cue: SubtitleCue) -> SubtitleCue:
            if cue.index not in target_indexes:
                return cue
            return replace(
                cue,
                text_font_size=font_size,
                scene_font_scale=1.0,
                orientation=orientation,
                style_hint=style_hint,
                background_color=background,
                text_color=foreground,
                background_padding_x=background_padding_x,
                background_padding_y=background_padding_y,
                background_style=background_style,
                letter_spacing_percent=letter_spacing_percent,
                font_bold=font_bold,
                font_italic=font_italic,
                text_opacity_percent=text_opacity_percent,
                background_opacity_percent=background_opacity_percent,
                revision=cue.revision + 1,
            )

        next_source = tuple(formatted(cue) for cue in source)
        next_translated = tuple(formatted(cue) for cue in translated)
        if (
            next_source == self.snapshot.source_cues
            and next_translated == self.snapshot.translated_cues
        ):
            return self.snapshot
        return self._commit(
            replace(
                self.snapshot,
                source_cues=next_source,
                translated_cues=next_translated,
            )
        )

    def set_scene_fade_many(
        self,
        cue_ids: tuple[str, ...],
        *,
        enabled: bool,
        duration_ms: int,
    ) -> ProjectSnapshot:
        """Apply one fade-in/out setting to selected scene-text cue pairs."""

        selected = tuple(dict.fromkeys(value.strip() for value in cue_ids if value.strip()))
        if not selected:
            raise SubtitleEditError("Hãy chọn ít nhất một cue chữ trong cảnh")
        if type(enabled) is not bool:
            raise SubtitleEditError("Chế độ mờ dần phải là bật hoặc tắt")
        if type(duration_ms) is not int or not 50 <= duration_ms <= 2_000:
            raise SubtitleEditError("Thời lượng mờ dần phải từ 50 đến 2.000 ms")
        source = list(self.snapshot.source_cues)
        translated = list(self.snapshot.translated_cues)
        target_indexes = {
            cue.index
            for cue in (*source, *translated)
            if cue.id in selected and cue.is_scene_text
        }
        found_ids = {
            cue.id
            for cue in (*source, *translated)
            if cue.id in selected and cue.is_scene_text
        }
        missing = set(selected) - found_ids
        if missing:
            raise SubtitleEditError("Chế độ mờ dần chỉ áp dụng cho chữ trong cảnh")

        def faded(cue: SubtitleCue) -> SubtitleCue:
            if cue.index not in target_indexes:
                return cue
            return replace(
                cue,
                fade_enabled=enabled,
                fade_duration_ms=duration_ms,
                revision=cue.revision + 1,
            )

        next_source = tuple(faded(cue) for cue in source)
        next_translated = tuple(faded(cue) for cue in translated)
        if (
            next_source == self.snapshot.source_cues
            and next_translated == self.snapshot.translated_cues
        ):
            return self.snapshot
        return self._commit(
            replace(
                self.snapshot,
                source_cues=next_source,
                translated_cues=next_translated,
            )
        )

    def add_scene_text(
        self,
        *,
        text: str,
        start_us: int,
        end_us: int,
        visual_box: tuple[float, float, float, float],
        font_size: int = 12,
        orientation: str = "horizontal",
        background_color: str = SCENE_TEXT_DEFAULT_BACKGROUND_COLOR,
        text_color: str = SCENE_TEXT_DEFAULT_TEXT_COLOR,
        style_hint: str = SCENE_TEXT_DEFAULT_STYLE_HINT,
        track_id: str = "",
    ) -> ProjectSnapshot:
        """Compatibility wrapper for a user-authored non-spoken scene label."""

        return self.add_subtitle_cue(
            text=text,
            start_us=start_us,
            end_us=end_us,
            visual_box=visual_box,
            cue_role="scene_text",
            font_size=font_size,
            orientation=orientation,
            background_color=background_color,
            text_color=text_color,
            style_hint=style_hint,
            track_id=track_id,
            background_style=SCENE_TEXT_DEFAULT_BACKGROUND_STYLE,
            background_opacity_percent=(
                SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT
            ),
        )

    def add_subtitle_cue(
        self,
        *,
        text: str,
        start_us: int,
        end_us: int,
        visual_box: tuple[float, float, float, float] | None = None,
        cue_role: str = "scene_text",
        font_size: int | None = None,
        orientation: str | None = None,
        background_color: str | None = None,
        text_color: str | None = None,
        style_hint: str | None = None,
        track_id: str = "",
        background_padding_x: int | None = None,
        background_padding_y: int | None = None,
        background_style: str | None = None,
        letter_spacing_percent: int | None = None,
        font_bold: bool | None = None,
        font_italic: bool | None = None,
        text_opacity_percent: int | None = None,
        background_opacity_percent: int | None = None,
        fade_enabled: bool | None = None,
        fade_duration_ms: int | None = None,
    ) -> ProjectSnapshot:
        """Add one explicit dialogue or scene-text cue as real subtitle data.

        Role is deliberately independent from the display lane.  A dialogue
        cue may live on S2+ and still belongs to the spoken/TTS stream.
        """

        value = text.strip()
        if not value:
            raise SubtitleEditError("Nội dung chữ thủ công không được để trống")
        if type(start_us) is not int or type(end_us) is not int:
            raise SubtitleEditError("Thời gian chữ thủ công phải dùng micro-giây nguyên")
        if start_us < 0 or end_us - start_us < SCENE_TEXT_MIN_DURATION_US:
            raise SubtitleEditError("Thời lượng chữ thủ công phải từ 100 ms trở lên")
        if cue_role not in CUE_ROLES:
            raise SubtitleEditError("Loại cue phải là thoại chính hoặc chữ trong cảnh")

        cue_range = TimeRange(start_us, end_us)

        def cue_lane(cue: SubtitleCue) -> int:
            fallback = 1 if cue.is_dialogue else 2
            return max(1, subtitle_track_number(cue.subtitle_track_id, fallback=fallback))

        def lane_is_free(number: int) -> bool:
            return not any(
                cue_lane(cue) == number and ranges_overlap(cue_range, cue.range)
                for cue in self.snapshot.source_cues
            )

        if track_id:
            number = subtitle_track_number(track_id, fallback=0)
            if number < 1 or track_id != f"S{number}":
                raise SubtitleEditError("Track phụ đề phải có dạng S1, S2, S3…")
            if cue_role == "scene_text" and number == 1:
                raise SubtitleEditError("Chữ trong cảnh phải nằm từ S2 trở xuống")
            if not lane_is_free(number):
                raise SubtitleEditError(f"{track_id} đã có cue trong khoảng thời gian này")
        elif cue_role == "dialogue":
            number = 1
            while not lane_is_free(number):
                number += 1
            track_id = f"S{number}"
        else:
            highest = max(
                [
                    subtitle_track_number(track.id, fallback=0)
                    for track in self.snapshot.tracks
                    if track.kind is TrackKind.SUBTITLE
                ]
                + [
                    subtitle_track_number(cue.subtitle_track_id, fallback=0)
                    for cue in (
                        *self.snapshot.source_cues,
                        *self.snapshot.translated_cues,
                    )
                    if cue.subtitle_track_id
                ],
                default=1,
            )
            track_id = f"S{max(2, highest + 1)}"

        dialogue_template = min(
            (cue for cue in self.snapshot.source_cues if cue.is_dialogue),
            key=lambda cue: (
                min(abs(start_us - cue.range.start_us), abs(start_us - cue.range.end_us)),
                cue.index,
            ),
            default=None,
        )
        inherited_box = (
            (dialogue_template.panel_box or dialogue_template.visual_box)
            if dialogue_template is not None
            else None
        )
        resolved_box = visual_box or inherited_box or (
            (0.10, 0.74, 0.80, 0.16)
            if cue_role == "dialogue"
            else None
        )
        if resolved_box is None or len(resolved_box) != 4:
            raise SubtitleEditError("Vùng chữ thủ công cần đủ x, y, rộng và cao")
        try:
            x, y, width, height = (float(item) for item in resolved_box)
        except (TypeError, ValueError) as exc:
            raise SubtitleEditError("Vùng chữ thủ công không hợp lệ") from exc
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > 1.000001
            or y + height > 1.000001
        ):
            raise SubtitleEditError("Vùng chữ thủ công phải nằm trong khung hình")
        # Reuse the formatter's validation without producing an intermediate
        # revision by constructing a cue first.
        def inherited(name: str, fallback: object) -> object:
            if dialogue_template is None or cue_role != "dialogue":
                return fallback
            return getattr(dialogue_template, name)

        resolved_font_size = int(
            inherited("text_font_size", 12) if font_size is None else font_size
        )
        resolved_orientation = str(
            inherited("orientation", "horizontal")
            if orientation is None
            else orientation
        )
        resolved_background_color = str(
            inherited(
                "background_color",
                SCENE_TEXT_DEFAULT_BACKGROUND_COLOR
                if cue_role == "scene_text"
                else "",
            )
            if background_color is None
            else background_color
        )
        resolved_text_color = str(
            inherited(
                "text_color",
                SCENE_TEXT_DEFAULT_TEXT_COLOR if cue_role == "scene_text" else "",
            )
            if text_color is None
            else text_color
        )
        resolved_style_hint = str(
            inherited(
                "style_hint",
                SCENE_TEXT_DEFAULT_STYLE_HINT
                if cue_role == "scene_text"
                else "standard",
            )
            if style_hint is None
            else style_hint
        )
        resolved_padding_x = int(
            inherited("background_padding_x", 20)
            if background_padding_x is None
            else background_padding_x
        )
        resolved_padding_y = int(
            inherited("background_padding_y", 10)
            if background_padding_y is None
            else background_padding_y
        )
        resolved_background_style = str(
            inherited(
                "background_style",
                SCENE_TEXT_DEFAULT_BACKGROUND_STYLE
                if cue_role == "scene_text"
                else "solid",
            )
            if background_style is None
            else background_style
        )
        resolved_letter_spacing = int(
            inherited("letter_spacing_percent", 0)
            if letter_spacing_percent is None
            else letter_spacing_percent
        )
        resolved_font_bold = bool(
            inherited("font_bold", False) if font_bold is None else font_bold
        )
        resolved_font_italic = bool(
            inherited("font_italic", False) if font_italic is None else font_italic
        )
        resolved_text_opacity = int(
            inherited("text_opacity_percent", 100)
            if text_opacity_percent is None
            else text_opacity_percent
        )
        resolved_background_opacity = int(
            inherited(
                "background_opacity_percent",
                SCENE_TEXT_DEFAULT_BACKGROUND_OPACITY_PERCENT
                if cue_role == "scene_text"
                else 90,
            )
            if background_opacity_percent is None
            else background_opacity_percent
        )
        resolved_fade_enabled = bool(
            inherited("fade_enabled", False)
            if fade_enabled is None
            else fade_enabled
        )
        resolved_fade_duration = int(
            inherited("fade_duration_ms", 300)
            if fade_duration_ms is None
            else fade_duration_ms
        )

        color = resolved_background_color.strip().upper()
        if color:
            if len(color) != 7 or not color.startswith("#"):
                raise SubtitleEditError("Màu nền phải dùng định dạng #RRGGBB")
            try:
                int(color[1:], 16)
            except ValueError as exc:
                raise SubtitleEditError("Màu nền phải dùng định dạng #RRGGBB") from exc
        if not 1 <= resolved_font_size <= 100:
            raise SubtitleEditError("Kích thước chữ phải nằm trong khoảng 1 đến 100")
        if resolved_orientation not in {"horizontal", "vertical"}:
            raise SubtitleEditError("Hướng chữ phải là ngang hoặc dọc")
        foreground = resolved_text_color.strip().upper()
        if foreground:
            if len(foreground) != 7 or not foreground.startswith("#"):
                raise SubtitleEditError("Màu chữ phải dùng định dạng #RRGGBB")
            try:
                int(foreground[1:], 16)
            except ValueError as exc:
                raise SubtitleEditError("Màu chữ phải dùng định dạng #RRGGBB") from exc
        if resolved_style_hint not in CUE_STYLE_HINTS:
            raise SubtitleEditError("Kiểu chữ không hợp lệ")
        if resolved_background_style not in CUE_BACKGROUND_STYLES:
            raise SubtitleEditError("Kiểu nền chữ không hợp lệ")
        if not 0 <= resolved_padding_x <= 100 or not 0 <= resolved_padding_y <= 100:
            raise SubtitleEditError("Khoảng nền chữ phải nằm trong khoảng 0 đến 100")
        if not 0 <= resolved_letter_spacing <= 100:
            raise SubtitleEditError("Khoảng cách chữ phải nằm trong khoảng 0 đến 100")
        if not 0 <= resolved_text_opacity <= 100 or not 0 <= resolved_background_opacity <= 100:
            raise SubtitleEditError("Độ đục chữ và nền phải nằm trong khoảng 0 đến 100")
        if not 50 <= resolved_fade_duration <= 2_000:
            raise SubtitleEditError("Thời lượng mờ dần phải từ 50 đến 2.000 ms")

        video_end_us = max(
            (
                clip.timeline_range.end_us
                for track in self.snapshot.tracks
                if track.kind is TrackKind.VIDEO
                for clip in track.clips
            ),
            default=0,
        )
        if video_end_us > 0 and end_us > video_end_us:
            raise SubtitleEditError("Chữ thủ công không được vượt quá cuối video")

        cue_id = self._new_id()
        cue_index = max(
            (cue.index for cue in (*self.snapshot.source_cues, *self.snapshot.translated_cues)),
            default=0,
        ) + 1
        common = {
            "id": cue_id,
            "index": cue_index,
            "range": cue_range,
            "text": value,
            "source": "manual_dialogue" if cue_role == "dialogue" else "manual_scene_text",
            "visual_box": (x, y, width, height),
            "visual_box_source": "estimated",
            "role": cue_role,
            "orientation": resolved_orientation,
            "style_hint": resolved_style_hint,
            "panel_box": (x, y, width, height),
            "background_color": color,
            "text_color": foreground,
            "review_state": "accepted",
            "text_font_size": resolved_font_size,
            "subtitle_track_id": track_id,
            "background_padding_x": resolved_padding_x,
            "background_padding_y": resolved_padding_y,
            "background_style": resolved_background_style,
            "letter_spacing_percent": resolved_letter_spacing,
            "font_bold": resolved_font_bold,
            "font_italic": resolved_font_italic,
            "text_opacity_percent": resolved_text_opacity,
            "background_opacity_percent": resolved_background_opacity,
            "fade_enabled": resolved_fade_enabled if cue_role == "scene_text" else False,
            "fade_duration_ms": resolved_fade_duration,
        }
        source_cue = SubtitleCue(language="vi", **common)
        translated_cue = SubtitleCue(language="vi", **common)
        source = self._normalize_cues((*self.snapshot.source_cues, source_cue))
        translated = self._normalize_cues((*self.snapshot.translated_cues, translated_cue))
        normalized_source = next(cue for cue in source if cue.id == cue_id)

        tracks = list(self.snapshot.tracks)
        track_index = next(
            (
                index
                for index, track in enumerate(tracks)
                if track.kind is TrackKind.SUBTITLE and track.id == track_id
            ),
            None,
        )
        clip = TimelineClip(
            id=f"subtitle-manual-{cue_id}",
            track_id=track_id,
            timeline_range=cue_range,
            source_range=None,
            cue_id=cue_id,
            cue_index=normalized_source.index,
            cue_text=value,
        )
        if track_index is None:
            tracks.append(
                TimelineTrack(
                    track_id,
                    TrackKind.SUBTITLE,
                    "Phụ đề thoại" if cue_role == "dialogue" else "Chữ trong cảnh",
                    (clip,),
                )
            )
        else:
            track = tracks[track_index]
            tracks[track_index] = replace(
                track,
                clips=tuple(
                    sorted(
                        (*track.clips, clip),
                        key=lambda item: (
                            item.timeline_range.start_us,
                            item.timeline_range.end_us,
                            item.id,
                        ),
                    )
                ),
            )
        return self._commit(
            replace(
                self.snapshot,
                source_cues=source,
                translated_cues=translated,
                tracks=tuple(tracks),
            )
        )

    @staticmethod
    def _transform_box(
        box: tuple[float, float, float, float],
        *,
        delta_x: float,
        delta_y: float,
        scale_x: float,
        scale_y: float,
    ) -> tuple[float, float, float, float]:
        x, y, width, height = box
        width = max(0.01, min(1.0, width * scale_x))
        height = max(0.01, min(1.0, height * scale_y))
        x = max(0.0, min(1.0 - width, x + delta_x))
        y = max(0.0, min(1.0 - height, y + delta_y))
        return (x, y, width, height)

    def transform_cue_geometry(
        self,
        cue_ids: tuple[str, ...],
        *,
        delta_x: float,
        delta_y: float,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
    ) -> ProjectSnapshot:
        """Move/resize generated subtitle geometry as one atomic edit."""

        selected = {value.strip() for value in cue_ids if value.strip()}
        if not selected:
            raise SubtitleEditError("Hãy chọn ít nhất một cue chữ")
        values = (delta_x, delta_y, scale_x, scale_y)
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
            raise SubtitleEditError("Tọa độ chữ không hợp lệ")
        if scale_x <= 0 or scale_y <= 0:
            raise SubtitleEditError("Kích thước chữ phải lớn hơn 0")

        source = list(self.snapshot.source_cues)
        translated = list(self.snapshot.translated_cues)
        selected_indexes = {
            cue.index for cue in (*source, *translated) if cue.id in selected
        }
        if not selected_indexes:
            raise LookupError(f"Unknown subtitle cue: {sorted(selected)[0]}")

        def transformed(cue: SubtitleCue) -> SubtitleCue:
            if cue.index not in selected_indexes:
                return cue
            fallback = (
                (0.10, 0.74, 0.80, 0.16)
                if cue.is_dialogue
                else (0.35, 0.35, 0.30, 0.14)
            )
            panel = cue.panel_box or cue.visual_box or fallback
            next_panel = self._transform_box(
                panel,
                delta_x=float(delta_x),
                delta_y=float(delta_y),
                scale_x=float(scale_x),
                scale_y=float(scale_y),
            )
            visual = cue.visual_box
            if cue.source == "manual_scene_text" or cue.is_dialogue:
                visual = self._transform_box(
                    visual or panel,
                    delta_x=float(delta_x),
                    delta_y=float(delta_y),
                    scale_x=float(scale_x),
                    scale_y=float(scale_y),
                )
            return replace(
                cue,
                visual_box=visual,
                visual_box_source=(cue.visual_box_source or "estimated")
                if visual is not None
                else "",
                panel_box=next_panel,
                revision=cue.revision + 1,
            )

        return self._commit(
            replace(
                self.snapshot,
                source_cues=tuple(transformed(cue) for cue in source),
                translated_cues=tuple(transformed(cue) for cue in translated),
            )
        )

    def move_scene_texts(
        self,
        cue_ids: tuple[str, ...],
        *,
        delta_us: int,
        lane_delta: int = 0,
    ) -> ProjectSnapshot:
        """Shift selected subtitle cues in time and between display lanes.

        Moving scene text to S1 is the explicit user action that promotes it
        into spoken dialogue. Moving dialogue away from S1 never demotes it.
        """

        selected = {value.strip() for value in cue_ids if value.strip()}
        if not selected:
            raise SubtitleEditError("Hãy chọn ít nhất một cue chữ")
        if type(delta_us) is not int or type(lane_delta) is not int:
            raise SubtitleEditError("Dịch chuyển cue phải dùng số nguyên")
        source = list(self.snapshot.source_cues)
        selected_source = [cue for cue in source if cue.id in selected]
        if len(selected_source) != len(selected):
            missing = selected - {cue.id for cue in selected_source}
            raise LookupError(f"Unknown subtitle cue: {sorted(missing)[0]}")
        video_end_us = max(
            (
                clip.timeline_range.end_us
                for track in self.snapshot.tracks
                if track.kind is TrackKind.VIDEO
                for clip in track.clips
            ),
            default=max((cue.range.end_us for cue in source), default=0),
        )
        minimum_delta = -min(cue.range.start_us for cue in selected_source)
        maximum_delta = video_end_us - max(cue.range.end_us for cue in selected_source)
        applied_delta = max(minimum_delta, min(maximum_delta, delta_us))

        occupied: dict[int, list[TimeRange]] = {}
        for cue in source:
            if cue.id in selected:
                continue
            lane = max(
                1,
                subtitle_track_number(
                    cue.subtitle_track_id,
                    fallback=1 if cue.is_dialogue else 2,
                ),
            )
            occupied.setdefault(lane, []).append(cue.range)

        ordered_selected = sorted(
            selected_source,
            key=lambda item: (
                subtitle_track_number(item.subtitle_track_id),
                item.range.start_us,
                item.id,
            ),
        )
        minimum_lane = min(
            max(1, subtitle_track_number(cue.subtitle_track_id))
            for cue in ordered_selected
        )
        applied_lane_delta = max(lane_delta, 1 - minimum_lane)
        projected: list[tuple[SubtitleCue, TimeRange, int]] = []
        for cue in ordered_selected:
            next_range = TimeRange(
                cue.range.start_us + applied_delta,
                cue.range.end_us + applied_delta,
            )
            lane = max(
                1,
                subtitle_track_number(cue.subtitle_track_id) + applied_lane_delta,
            )
            projected.append((cue, next_range, lane))

        if any(
            lane == 1
            and any(ranges_overlap(next_range, item) for item in occupied.get(1, ()))
            for _cue, next_range, lane in projected
        ):
            raise SubtitleEditError(
                "Không thể kéo chữ lên S1 vì khoảng thời gian này đang có cue thoại"
            )

        # Resolve collisions with one shared offset. Independent lane bumps can
        # collapse two selected cues onto the same track and destroy their
        # relative layout; a shared bump preserves the selected group's lane
        # spacing while still finding the lowest free destination.
        collision_offset = 0
        while any(
            any(
                ranges_overlap(next_range, item)
                for item in occupied.get(lane + collision_offset, ())
            )
            for _cue, next_range, lane in projected
        ):
            collision_offset += 1

        assignments: dict[str, tuple[TimeRange, str]] = {}
        for cue, next_range, lane in projected:
            lane += collision_offset
            occupied.setdefault(lane, []).append(next_range)
            assignments[cue.id] = (next_range, f"S{lane}")

        index_assignment = {
            cue.index: assignments[cue.id]
            for cue in selected_source
        }

        def moved(cue: SubtitleCue) -> SubtitleCue:
            assignment = index_assignment.get(cue.index)
            if assignment is None:
                return cue
            cue_range, track_id = assignment
            next_role = "dialogue" if track_id == "S1" else cue.role
            return replace(
                cue,
                range=cue_range,
                subtitle_track_id=track_id,
                role=next_role,
                revision=cue.revision + 1,
            )

        next_source = tuple(moved(cue) for cue in source)
        next_translated = tuple(moved(cue) for cue in self.snapshot.translated_cues)
        return self._commit(
            replace(
                self.snapshot,
                source_cues=next_source,
                translated_cues=next_translated,
                tracks=self._rebuild_subtitle_tracks(next_source, next_translated),
            )
        )

    def set_ocr_regions(
        self,
        *,
        dialogue_region: tuple[float, float, float, float] | None,
        exclusion_regions: tuple[tuple[float, float, float, float], ...],
    ) -> ProjectSnapshot:
        """Persist normalized OCR routing geometry in one undoable revision."""

        try:
            candidate = replace(
                self.snapshot,
                ocr_dialogue_region=dialogue_region,
                ocr_exclusion_regions=exclusion_regions,
            )
        except (TypeError, ValueError) as exc:
            raise SubtitleEditError(f"Vùng OCR không hợp lệ: {exc}") from exc
        if candidate == self.snapshot:
            return self.snapshot
        return self._commit(candidate)

    def set_scene_text_timing(
        self,
        cue_id: str,
        *,
        start_us: int,
        end_us: int,
    ) -> ProjectSnapshot:
        """Retime one subtitle cue and its projected timeline clip atomically."""

        if type(start_us) is not int or type(end_us) is not int:
            raise SubtitleEditError("Thời gian cue phải dùng micro-giây nguyên")
        if start_us < 0 or end_us <= start_us:
            raise SubtitleEditError("Khoảng thời gian cue không hợp lệ")
        if end_us - start_us < SCENE_TEXT_MIN_DURATION_US:
            raise SubtitleEditError(
                "Thời lượng hiển thị cue phải từ 100 ms trở lên"
            )

        source = list(self.snapshot.source_cues)
        source_index = self._index(tuple(source), cue_id)
        cue = source[source_index]
        video_end_us = max(
            (
                clip.timeline_range.end_us
                for track in self.snapshot.tracks
                if track.kind.value == "video"
                for clip in track.clips
            ),
            default=0,
        )
        if video_end_us > 0 and end_us > video_end_us:
            raise SubtitleEditError(
                "Thời gian cue không được vượt quá cuối video"
            )

        next_range = TimeRange(start_us, end_us)
        if next_range == cue.range:
            return self.snapshot
        lane = max(
            1,
            subtitle_track_number(
                cue.subtitle_track_id,
                fallback=1 if cue.is_dialogue else 2,
            ),
        )
        if lane == 1 and any(
            other.id != cue.id
            and max(
                1,
                subtitle_track_number(
                    other.subtitle_track_id,
                    fallback=1 if other.is_dialogue else 2,
                ),
            )
            == 1
            and ranges_overlap(next_range, other.range)
            for other in source
        ):
            raise SubtitleEditError(
                "Không thể đặt chữ trên S1 vì khoảng thời gian này đang có cue thoại"
            )
        while any(
            other.id != cue.id
            and max(
                1,
                subtitle_track_number(
                    other.subtitle_track_id,
                    fallback=1 if other.is_dialogue else 2,
                ),
            )
            == lane
            and ranges_overlap(next_range, other.range)
            for other in source
        ):
            lane += 1
        next_track_id = f"S{lane}"
        source[source_index] = replace(
            cue,
            range=next_range,
            subtitle_track_id=next_track_id,
            revision=cue.revision + 1,
        )

        translated = list(self.snapshot.translated_cues)
        translated_match: SubtitleCue | None = None
        for index, translated_cue in enumerate(translated):
            if translated_cue.id != cue.id and translated_cue.index != cue.index:
                continue
            translated_match = replace(
                translated_cue,
                range=next_range,
                subtitle_track_id=next_track_id,
                revision=translated_cue.revision + 1,
            )
            translated[index] = translated_match
            break

        source_tuple = tuple(source)
        translated_tuple = tuple(translated)
        return self._commit(
            replace(
                self.snapshot,
                source_cues=source_tuple,
                translated_cues=translated_tuple,
                tracks=self._rebuild_subtitle_tracks(source_tuple, translated_tuple),
            )
        )

    def split(
        self,
        cue_id: str,
        *,
        split_us: int | None = None,
        language: str = "source",
    ) -> ProjectSnapshot:
        cues = list(self._cues(language))
        index = self._index(tuple(cues), cue_id)
        cue = cues[index]
        target_us = split_us if split_us is not None else (
            cue.range.start_us + cue.range.end_us
        ) // 2
        if type(target_us) is not int or not cue.range.start_us < target_us < cue.range.end_us:
            raise SubtitleEditError("Subtitle split point must be inside the cue")
        text = cue.text.replace("\n", " ").strip()
        midpoint = max(1, len(text) // 2)
        punctuation = [
            match.end()
            for match in re.finditer(r"[，。！？；：、,.!?;:]|\s+", text)
            if 1 <= match.end() < len(text)
        ]
        split_at = (
            min(punctuation, key=lambda value: abs(value - midpoint))
            if punctuation
            else midpoint
        )
        left_text = text[:split_at].strip()
        right_text = text[split_at:].strip()
        if not left_text or not right_text:
            raise SubtitleEditError("Subtitle text cannot be split into two non-empty cues")
        left = replace(
            cue,
            range=TimeRange(cue.range.start_us, target_us),
            text=left_text,
            revision=cue.revision + 1,
        )
        right = replace(
            cue,
            id=self._new_id(),
            range=TimeRange(target_us, cue.range.end_us),
            text=right_text,
            revision=1,
        )
        cues[index : index + 1] = [left, right]
        return self._commit(self._replace_cues(language, cues))

    def merge_with_next(self, cue_id: str, *, language: str = "source") -> ProjectSnapshot:
        cues = list(self._cues(language))
        index = self._index(tuple(cues), cue_id)
        if index + 1 >= len(cues):
            raise SubtitleEditError("Selected cue has no following cue")
        left, right = cues[index : index + 2]
        separator = "" if left.language.startswith("zh") else " "
        merged = replace(
            left,
            range=TimeRange(left.range.start_us, right.range.end_us),
            text=(left.text.rstrip() + separator + right.text.lstrip()).strip(),
            revision=left.revision + 1,
            evidence_ids=tuple(dict.fromkeys((*left.evidence_ids, *right.evidence_ids))),
        )
        cues[index : index + 2] = [merged]
        return self._commit(self._replace_cues(language, cues))

    def adjust_time(
        self,
        cue_id: str,
        *,
        start_us: int,
        end_us: int,
        language: str = "source",
    ) -> ProjectSnapshot:
        cues = list(self._cues(language))
        index = self._index(tuple(cues), cue_id)
        cues[index] = replace(
            cues[index],
            range=TimeRange(start_us, end_us),
            revision=cues[index].revision + 1,
        )
        self._assert_timeline(cues)
        return self._commit(self._replace_cues(language, cues))

    def shift(self, cue_id: str, delta_us: int, *, language: str = "source") -> ProjectSnapshot:
        if type(delta_us) is not int:
            raise TypeError("Subtitle shift must use integer microseconds")
        cues = self._cues(language)
        cue = cues[self._index(cues, cue_id)]
        return self.adjust_time(
            cue_id,
            start_us=cue.range.start_us + delta_us,
            end_us=cue.range.end_us + delta_us,
            language=language,
        )

    def auto_break(
        self,
        *,
        language: str = "source",
        cue_id: str | None = None,
        max_chars: int | None = None,
    ) -> ProjectSnapshot:
        limit = max_chars or (
            20 if language == "source" else VIETNAMESE_SUBTITLE_WRAP_CHARS
        )
        cues = list(self._cues(language))
        changed = False
        for index, cue in enumerate(cues):
            if cue_id is not None and cue.id != cue_id:
                continue
            text = auto_break_text(cue.text, max_chars=limit)
            if text != cue.text:
                cues[index] = replace(cue, text=text, revision=cue.revision + 1)
                changed = True
        if not changed:
            return self.snapshot
        return self._commit(self._replace_cues(language, cues))

    def find_replace(
        self,
        find: str,
        replacement: str,
        *,
        language: str = "source",
        expected_count: int | None = None,
    ) -> ProjectSnapshot:
        if not find:
            raise SubtitleEditError("Find text cannot be empty")
        match_count, _ = self.count_matches(find, language=language)
        if expected_count is not None and match_count != expected_count:
            raise SubtitleEditError(
                "Nội dung phụ đề đã thay đổi sau khi đếm. Hãy bấm Thay tất cả lại để kiểm tra."
            )
        cues = list(self._cues(language))
        changed = False
        for index, cue in enumerate(cues):
            text = cue.text.replace(find, replacement)
            if text != cue.text:
                if not text.strip():
                    raise SubtitleEditError("Find/replace cannot empty a cue")
                cues[index] = replace(cue, text=text, revision=cue.revision + 1)
                changed = True
        if not changed:
            return self.snapshot
        return self._commit(self._replace_cues(language, cues))

    def count_matches(self, find: str, *, language: str = "source") -> tuple[int, int]:
        if not find:
            raise SubtitleEditError("Find text cannot be empty")
        counts = [cue.text.count(find) for cue in self._cues(language)]
        return sum(counts), sum(count > 0 for count in counts)

    def delete(self, cue_id: str) -> ProjectSnapshot:
        source = list(self.snapshot.source_cues)
        translated = list(self.snapshot.translated_cues)
        matched = next(
            (cue for cue in (*source, *translated) if cue.id == cue_id),
            None,
        )
        if matched is None:
            raise LookupError(f"Unknown subtitle cue: {cue_id}")

        cue_index = matched.index
        matching_ids = {
            cue.id
            for cue in (*source, *translated)
            if cue.id == cue_id or cue.index == cue_index
        }
        source = [
            cue
            for cue in source
            if cue.id not in matching_ids and cue.index != cue_index
        ]
        translated = [
            cue
            for cue in translated
            if cue.id not in matching_ids and cue.index != cue_index
        ]
        self._assert_timeline(source)
        self._assert_timeline(translated)
        normalized_source = self._normalize_cues(source)
        normalized_translated = self._normalize_cues(translated)
        source_by_id = {cue.id: cue for cue in normalized_source}
        translated_by_id = {cue.id: cue for cue in normalized_translated}
        tracks: list[TimelineTrack] = []
        for track in self.snapshot.tracks:
            if track.kind is not TrackKind.SUBTITLE:
                tracks.append(track)
                continue
            clips: list[TimelineClip] = []
            for clip in track.clips:
                if (
                    clip.cue_id in matching_ids
                    or (clip.cue_id is None and clip.cue_index == cue_index)
                ):
                    continue
                cue = source_by_id.get(clip.cue_id or "")
                if cue is None and clip.cue_index is not None:
                    cue = next(
                        (
                            item
                            for item in normalized_source
                            if item.index == clip.cue_index
                        ),
                        None,
                    )
                if cue is None:
                    clips.append(clip)
                    continue
                translated_cue = translated_by_id.get(cue.id)
                clips.append(
                    replace(
                        clip,
                        cue_id=cue.id,
                        cue_index=cue.index,
                        cue_text=(translated_cue.text if translated_cue else cue.text),
                        payload_revision=clip.payload_revision + 1,
                    )
                )
            if clips:
                tracks.append(replace(track, clips=tuple(clips)))
        candidate = replace(
            self.snapshot,
            source_cues=normalized_source,
            translated_cues=normalized_translated,
            tracks=tuple(tracks),
        )
        return self._commit(candidate)

    def apply_ocr_rescan(
        self,
        cue_id: str,
        *,
        text: str,
        confidence: float,
        evidence_ids: tuple[str, ...],
        source: str,
    ) -> ProjectSnapshot:
        if not text.strip() or not source.strip() or not evidence_ids:
            raise SubtitleEditError("OCR rescan requires text, source and evidence")
        if not 0 <= confidence <= 1:
            raise SubtitleEditError("OCR rescan confidence must be between zero and one")
        cues = list(self.snapshot.source_cues)
        index = self._index(tuple(cues), cue_id)
        cues[index] = replace(
            cues[index],
            text=text.strip(),
            confidence=confidence,
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            source=source,
            revision=cues[index].revision + 1,
        )
        return self._commit(self._replace_cues("source", cues))

    def undo(self) -> ProjectSnapshot:
        if not self._undo:
            raise SubtitleEditError("No subtitle edit to undo")
        previous = self._undo.pop()
        current = self.snapshot
        self._redo.append(current)
        # Revisions are immutable and strictly monotonic in ProjectRepository.
        # Undo restores the previous content, but publishes it as a new
        # revision instead of moving the project head backwards.
        self.snapshot = replace(previous, revision=current.revision + 1)
        return self.snapshot

    def redo(self) -> ProjectSnapshot:
        if not self._redo:
            raise SubtitleEditError("No subtitle edit to redo")
        next_snapshot = self._redo.pop()
        current = self.snapshot
        self._undo.append(current)
        # Redo follows the same rule: restore content while allocating a new
        # immutable project revision so concurrent clients never observe a
        # revision rollback.
        self.snapshot = replace(next_snapshot, revision=current.revision + 1)
        return self.snapshot
