from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from uuid import uuid4

from ducai_studio.domain.project import (
    ProjectSnapshot,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import SpeedRatio, TimeRange, TimeUs
from ducai_studio.domain.timeline import (
    VOICE_TIMING_OVERFLOW,
    VOICE_TIMING_SNAP_TOLERANCE_US,
    TimelineEditError,
    classify_voice_timing,
    resize_clip,
    split_clip,
    trim_head,
    trim_tail,
    with_speed,
    with_volume,
)


@dataclass(frozen=True, slots=True)
class VideoClipBundle:
    project_id: str
    start_us: int
    end_us: int
    tracks: tuple[TimelineTrack, ...]


class TimelineEditor:
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

    def _new_id(self, prefix: str) -> str:
        value = self._id_factory().strip()
        if not value:
            raise RuntimeError("Timeline id factory returned a blank value")
        return f"{prefix}-{value}"

    def _find_clip(self, clip_id: str) -> TimelineClip:
        for track in self.snapshot.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return clip
        raise LookupError(f"Unknown timeline clip: {clip_id}")

    def _find_track(self, track_id: str) -> TimelineTrack:
        for track in self.snapshot.tracks:
            if track.id == track_id:
                return track
        raise LookupError(f"Unknown timeline track: {track_id}")

    def _assert_editable(self, clips: tuple[TimelineClip, ...]) -> None:
        target_ids = {clip.id for clip in clips}
        locked_tracks = [
            track.id
            for track in self.snapshot.tracks
            if track.locked and any(clip.id in target_ids for clip in track.clips)
        ]
        if locked_tracks:
            raise TimelineEditError(
                "Cannot edit clips on locked track: " + ", ".join(locked_tracks)
            )

    def _targets(self, clip_id: str, *, apply_linked: bool) -> tuple[TimelineClip, ...]:
        selected = self._find_clip(clip_id)
        if not apply_linked or selected.link_group_id is None:
            self._assert_editable((selected,))
            return (selected,)
        targets = tuple(
            clip
            for track in self.snapshot.tracks
            for clip in track.clips
            if clip.link_group_id == selected.link_group_id
        )
        if not targets:
            raise TimelineEditError("Linked clip group is empty")
        self._assert_editable(targets)
        return targets

    def _replace_clips(
        self,
        replacements: dict[str, tuple[TimelineClip, ...]],
    ) -> ProjectSnapshot:
        tracks: list[TimelineTrack] = []
        for track in self.snapshot.tracks:
            clips: list[TimelineClip] = []
            for clip in track.clips:
                clips.extend(replacements.get(clip.id, (clip,)))
            tracks.append(replace(track, clips=tuple(clips)))
        return replace(self.snapshot, tracks=tuple(tracks))

    def _commit(self, candidate: ProjectSnapshot) -> ProjectSnapshot:
        if candidate.id != self.snapshot.id:
            raise TimelineEditError("Timeline command cannot change project identity")
        previous = self.snapshot
        self.snapshot = replace(candidate, revision=previous.revision + 1)
        self._undo.append(previous)
        self._redo.clear()
        return self.snapshot

    def split(
        self,
        clip_id: str,
        timeline_us: TimeUs,
        *,
        apply_linked: bool = True,
    ) -> ProjectSnapshot:
        targets = self._targets(clip_id, apply_linked=apply_linked)
        if any(not clip.timeline_range.contains(timeline_us) for clip in targets):
            raise TimelineEditError("Split point must be inside every linked clip")
        linked = apply_linked and len(targets) > 1
        left_group = self._new_id("link") if linked else None
        right_group = self._new_id("link") if linked else None
        replacements: dict[str, tuple[TimelineClip, ...]] = {}
        for clip in targets:
            replacements[clip.id] = split_clip(
                clip,
                timeline_us,
                left_id=self._new_id("clip"),
                right_id=self._new_id("clip"),
                left_link_group_id=left_group,
                right_link_group_id=right_group,
            )
        return self._commit(self._replace_clips(replacements))

    def trim_head(
        self,
        clip_id: str,
        timeline_us: TimeUs,
        *,
        apply_linked: bool = True,
    ) -> ProjectSnapshot:
        targets = self._targets(clip_id, apply_linked=apply_linked)
        replacements = {
            clip.id: (trim_head(clip, timeline_us, unlink=not apply_linked),) for clip in targets
        }
        return self._commit(self._replace_clips(replacements))

    def cut(self, operation: str, start_us: int, end_us: int) -> ProjectSnapshot:
        from ducai_studio.domain.timeline_edit import cut_state, retained_cues

        if any(track.locked for track in self.snapshot.tracks):
            raise TimelineEditError("Hãy mở khóa các track trước khi cắt đồng bộ video")
        state = cut_state(self.snapshot, operation, start_us, end_us)
        candidate = replace(self.snapshot, timeline_edit=state)
        retained_source = retained_cues(candidate, candidate.source_cues)
        source = tuple(replace(cue, index=index) for index, cue in enumerate(retained_source, 1))
        by_id = {cue.id: cue.index for cue in source}
        by_previous_index = {
            old.index: new.index for old, new in zip(retained_source, source, strict=True)
        }
        translated = tuple(
            replace(cue, index=by_id.get(cue.id) or by_previous_index.get(cue.index, index))
            for index, cue in enumerate(retained_cues(candidate, candidate.translated_cues), 1)
        )
        return self._commit(replace(candidate, source_cues=source, translated_cues=translated))

    def set_markers(self, markers: object) -> ProjectSnapshot:
        """Set sorted display-time bookmarks as one undoable Timeline edit."""

        if not isinstance(markers, (list, tuple)):
            raise TimelineEditError("Danh sách mark Timeline không hợp lệ")
        if any(type(value) is not int or value < 0 for value in markers):
            raise TimelineEditError("Mark Timeline phải là mốc mili giây không âm")
        normalized = sorted(set(markers))
        if normalized == list(self.snapshot.timeline_markers):
            return self.snapshot
        return self._commit(replace(self.snapshot, timeline_markers=tuple(normalized)))

    def trim_tail(
        self,
        clip_id: str,
        timeline_us: TimeUs,
        *,
        apply_linked: bool = True,
    ) -> ProjectSnapshot:
        targets = self._targets(clip_id, apply_linked=apply_linked)
        replacements = {
            clip.id: (trim_tail(clip, timeline_us, unlink=not apply_linked),) for clip in targets
        }
        return self._commit(self._replace_clips(replacements))

    def move(
        self,
        clip_id: str,
        timeline_start_us: TimeUs,
        *,
        apply_linked: bool = True,
    ) -> ProjectSnapshot:
        """Move a media clip (and its linked A/V partner) without trimming it."""

        if type(timeline_start_us) is not int or timeline_start_us < 0:
            raise TimelineEditError("Vị trí clip trên Timeline không hợp lệ")
        selected = self._find_clip(clip_id)
        track = self._find_track(selected.track_id)
        if track.kind not in {TrackKind.VIDEO, TrackKind.ORIGINAL_AUDIO}:
            raise TimelineEditError("Chỉ clip video hoặc âm thanh gốc mới có thể kéo vị trí")
        targets = self._targets(clip_id, apply_linked=apply_linked)
        delta_us = timeline_start_us - selected.timeline_range.start_us
        if delta_us == 0:
            return self.snapshot
        if any(clip.timeline_range.start_us + delta_us < 0 for clip in targets):
            raise TimelineEditError("Clip liên kết không thể nằm trước đầu Timeline")
        replacements = {
            clip.id: (
                replace(
                    clip,
                    timeline_range=TimeRange(
                        clip.timeline_range.start_us + delta_us,
                        clip.timeline_range.end_us + delta_us,
                    ),
                    payload_revision=clip.payload_revision + 1,
                ),
            )
            for clip in targets
        }
        return self._commit(self._replace_clips(replacements))

    def set_speed_percent(
        self,
        clip_id: str,
        percent: str | int | float,
        *,
        apply_linked: bool = True,
    ) -> ProjectSnapshot:
        targets = self._targets(clip_id, apply_linked=apply_linked)
        speed = SpeedRatio.from_percent(percent)
        for clip in targets:
            if clip.track_id == "D1":
                speed_value = speed.numerator / speed.denominator
                if speed_value < 0.5 - 1e-9 or speed_value > 3.0 + 1e-9:
                    raise TimelineEditError("Tốc độ clip D1 phải nằm trong khoảng 0,50×–3,00×")
        replacements: dict[str, tuple[TimelineClip, ...]] = {}
        for clip in targets:
            changed = with_speed(clip, speed)
            if not apply_linked:
                changed = replace(changed, link_group_id=None)
            replacements[clip.id] = (changed,)
        return self._commit(self._replace_clips(replacements))

    def resize(
        self,
        clip_id: str,
        timeline_start_us: TimeUs,
        timeline_end_us: TimeUs,
        *,
        apply_linked: bool = False,
    ) -> ProjectSnapshot:
        """Resize a clip by changing playback speed, never by trimming source."""

        if timeline_start_us < 0 or timeline_end_us <= timeline_start_us:
            raise TimelineEditError("Clip resize range is invalid")
        targets = self._targets(clip_id, apply_linked=apply_linked)
        replacements: dict[str, tuple[TimelineClip, ...]] = {}
        for clip in targets:
            if clip.track_id != "D1" and apply_linked:
                # Linked video/audio edits retain their relative duration.  A
                # voice cue resize is intentionally audio-only by default.
                pass
            start = timeline_start_us
            end = timeline_end_us
            # Magnetic SRT snap keeps hand-edits frame-accurate while still
            # allowing free resizing outside the 80 ms attraction window.
            if clip.timing_slot is not None:
                if abs(start - clip.timing_slot.start_us) <= VOICE_TIMING_SNAP_TOLERANCE_US:
                    start = clip.timing_slot.start_us
                if abs(end - clip.timing_slot.end_us) <= VOICE_TIMING_SNAP_TOLERANCE_US:
                    end = clip.timing_slot.end_us
            if apply_linked and clip is not targets[0]:
                offset = targets[0].timeline_range.start_us - start
                start = clip.timeline_range.start_us - offset
                end = start + (timeline_end_us - timeline_start_us)
            replacements[clip.id] = (resize_clip(clip, TimeRange(start, end)),)
        return self._commit(self._replace_clips(replacements))

    def auto_fit_voice_timing(self) -> ProjectSnapshot:
        """Fit only D1 clips marked timing-unresolved to their SRT slots."""

        voice_track = next((track for track in self.snapshot.tracks if track.id == "D1"), None)
        if voice_track is None:
            raise TimelineEditError("D1 voice track does not exist")
        replacements: dict[str, tuple[TimelineClip, ...]] = {}
        unresolved: list[str] = []
        for clip in voice_track.clips:
            if (
                clip.timing_slot is None
                or classify_voice_timing(clip.timeline_range, clip.timing_slot)
                != VOICE_TIMING_OVERFLOW
            ):
                continue
            try:
                fitted = resize_clip(clip, clip.timing_slot)
            except TimelineEditError:
                unresolved.append(clip.id)
                continue
            replacements[clip.id] = (fitted,)
        if not replacements:
            if unresolved:
                raise TimelineEditError(
                    f"{len(unresolved)} cue cần tốc độ ngoài giới hạn 0,50×–3,00×"
                )
            return self.snapshot
        updated = self._commit(self._replace_clips(replacements))
        return updated

    def set_volume(self, clip_id: str, volume: float) -> ProjectSnapshot:
        clip = self._find_clip(clip_id)
        self._assert_editable((clip,))
        return self._commit(self._replace_clips({clip.id: (with_volume(clip, volume),)}))

    def delete_tracks(self, track_ids: tuple[str, ...]) -> ProjectSnapshot:
        ids = {value.strip() for value in track_ids if value.strip()}
        if not ids:
            raise TimelineEditError("No timeline tracks selected")
        unknown = ids.difference(track.id for track in self.snapshot.tracks)
        if unknown:
            raise LookupError("Unknown timeline track: " + ", ".join(sorted(unknown)))
        remaining = tuple(track for track in self.snapshot.tracks if track.id not in ids)
        removed_text = any(track.kind is TrackKind.TEXT and track.id in ids
                           for track in self.snapshot.tracks)
        return self._commit(replace(self.snapshot, tracks=remaining,
                                    title_cues=() if removed_text else self.snapshot.title_cues))

    def set_track_muted(self, track_id: str, muted: bool) -> ProjectSnapshot:
        if type(muted) is not bool:
            raise TypeError("Track muted state must be a boolean")
        selected = self._find_track(track_id)
        tracks = tuple(
            replace(track, muted=muted) if track.id == selected.id else track
            for track in self.snapshot.tracks
        )
        return self._commit(replace(self.snapshot, tracks=tracks))

    def set_track_volume(self, track_id: str, volume: float) -> ProjectSnapshot:
        selected = self._find_track(track_id)
        self._assert_editable(selected.clips)
        tracks = tuple(
            replace(
                track,
                clips=tuple(with_volume(clip, volume) for clip in track.clips),
            )
            if track.id == selected.id
            else track
            for track in self.snapshot.tracks
        )
        return self._commit(replace(self.snapshot, tracks=tracks))

    def set_track_locked(self, track_id: str, locked: bool) -> ProjectSnapshot:
        if type(locked) is not bool:
            raise TypeError("Track locked state must be a boolean")
        selected = self._find_track(track_id)
        tracks = tuple(
            replace(track, locked=locked) if track.id == selected.id else track
            for track in self.snapshot.tracks
        )
        return self._commit(replace(self.snapshot, tracks=tracks))

    def delete(self, clip_id: str, *, apply_linked: bool = True) -> ProjectSnapshot:
        targets = self._targets(clip_id, apply_linked=apply_linked)
        return self._commit(self._replace_clips({clip.id: () for clip in targets}))

    def copy_video(self, clip_id: str, *, include_fragments: bool = False) -> VideoClipBundle:
        selected = self._find_clip(clip_id)
        if self._find_track(selected.track_id).kind is not TrackKind.VIDEO:
            raise TimelineEditError("Hãy chọn đoạn video để sao chép")
        # Captured immutable values survive later edits/deletion of the original.
        videos = tuple(
            clip
            for track in self.snapshot.tracks
            for clip in track.clips
            if clip.id == clip_id
            or (
                include_fragments
                and track.id == selected.track_id
                and clip.id.startswith(clip_id + "::cut::")
            )
        )
        groups = {clip.link_group_id for clip in videos if clip.link_group_id}
        clips = tuple(
            clip
            for track in self.snapshot.tracks
            for clip in track.clips
            if clip in videos or clip.link_group_id in groups
        )
        ids = {clip.id for clip in clips}
        tracks = tuple(
            replace(track, clips=tuple(clip for clip in track.clips if clip.id in ids))
            for track in self.snapshot.tracks
            if any(clip.id in ids for clip in track.clips)
        )
        return VideoClipBundle(
            self.snapshot.id,
            min(c.timeline_range.start_us for c in videos),
            max(c.timeline_range.end_us for c in videos),
            tracks,
        )

    def paste_video(
        self, bundle: VideoClipBundle, start_us: int, *, new_layer: bool = True
    ) -> ProjectSnapshot:
        if bundle.project_id != self.snapshot.id:
            raise TimelineEditError("Hãy sao chép video trong dự án hiện tại trước khi dán")
        if type(start_us) is not int or start_us < 0:
            raise TimelineEditError("Mốc dán video không hợp lệ")
        delta = start_us - bundle.start_us
        groups = {
            clip.link_group_id: self._new_id("link")
            for track in bundle.tracks
            for clip in track.clips
            if clip.link_group_id
        }
        tracks = list(self.snapshot.tracks)
        next_video = (
            max(
                (
                    int(track.id[1:])
                    for track in tracks
                    if track.kind is TrackKind.VIDEO and track.id[1:].isdigit()
                ),
                default=1,
            )
            + 1
        )
        for source_track in bundle.tracks:
            if new_layer:
                track_id = (
                    f"V{next_video}"
                    if source_track.kind is TrackKind.VIDEO
                    else self._new_id("A-video")
                )
            else:
                track_id = source_track.id
                self._find_track(track_id)
            copied = tuple(
                replace(
                    clip,
                    id=self._new_id("clip"),
                    track_id=track_id,
                    timeline_range=TimeRange(
                        clip.timeline_range.start_us + delta, clip.timeline_range.end_us + delta
                    ),
                    link_group_id=groups.get(clip.link_group_id),
                    payload_revision=1,
                )
                for clip in source_track.clips
            )
            if new_layer:
                tracks.append(
                    replace(
                        source_track,
                        id=track_id,
                        name=track_id
                        if source_track.kind is TrackKind.VIDEO
                        else f"Âm thanh {next_video}",
                        clips=copied,
                        locked=False,
                    )
                )
            else:
                tracks = [
                    replace(
                        track,
                        clips=tuple(
                            sorted(
                                (*track.clips, *copied),
                                key=lambda c: (c.timeline_range.start_us, c.id),
                            )
                        ),
                    )
                    if track.id == track_id
                    else track
                    for track in tracks
                ]
        return self._commit(replace(self.snapshot, tracks=tuple(tracks)))

    def duplicate_video(self, clip_id: str, *, include_fragments: bool = False) -> ProjectSnapshot:
        bundle = self.copy_video(clip_id, include_fragments=include_fragments)
        if any(track.locked for track in self.snapshot.tracks):
            raise TimelineEditError("Hãy mở khóa các track trước khi nhân bản và nối video")
        insertion, duration = bundle.end_us, bundle.end_us - bundle.start_us

        def shift_range(interval):
            if interval is None or interval.end_us <= insertion:
                return interval
            return TimeRange(
                interval.start_us + (duration if interval.start_us >= insertion else 0),
                interval.end_us + duration,
            )

        tracks = []
        for track in self.snapshot.tracks:
            clips = []
            for clip in track.clips:
                interval = clip.timeline_range
                if interval.start_us < insertion < interval.end_us and clip.source_range:
                    left, right = split_clip(
                        clip,
                        insertion,
                        left_id=clip.id,
                        right_id=self._new_id("clip"),
                        left_link_group_id=clip.link_group_id,
                        right_link_group_id=(clip.link_group_id + "::after::" + str(insertion))
                        if clip.link_group_id
                        else None,
                    )
                    clips.extend(
                        (
                            replace(left, timing_slot=shift_range(left.timing_slot)),
                            replace(
                                right,
                                timeline_range=TimeRange(
                                    insertion + duration, interval.end_us + duration
                                ),
                                timing_slot=shift_range(right.timing_slot),
                            ),
                        )
                    )
                else:
                    clips.append(
                        replace(
                            clip,
                            timeline_range=shift_range(interval),
                            timing_slot=shift_range(clip.timing_slot),
                        )
                    )
            tracks.append(replace(track, clips=tuple(clips)))
        candidate = replace(
            self.snapshot,
            tracks=tuple(tracks),
            source_cues=tuple(
                replace(cue, range=shift_range(cue.range)) for cue in self.snapshot.source_cues
            ),
            translated_cues=tuple(
                replace(cue, range=shift_range(cue.range)) for cue in self.snapshot.translated_cues
            ),
            overlays=tuple(
                replace(overlay, range=shift_range(overlay.range))
                for overlay in self.snapshot.overlays
            ),
        )
        # Assemble the insert in a temporary editor: one user action, one Undo.
        temporary = TimelineEditor(candidate, id_factory=self._id_factory)
        return self._commit(temporary.paste_video(bundle, insertion, new_layer=False))

    def set_clip_muted(
        self, clip_id: str, muted: bool, *, include_fragments: bool = False
    ) -> ProjectSnapshot:
        if type(muted) is not bool:
            raise TimelineEditError("Trạng thái tắt tiếng không hợp lệ")
        targets = (
            tuple(
                clip
                for track in self.copy_video(clip_id, include_fragments=True).tracks
                for clip in track.clips
            )
            if include_fragments
            else self._targets(clip_id, apply_linked=True)
        )
        self._assert_editable(targets)
        return self._commit(
            self._replace_clips(
                {
                    clip.id: (
                        replace(clip, muted=muted, payload_revision=clip.payload_revision + 1),
                    )
                    for clip in targets
                }
            )
        )

    def undo(self) -> ProjectSnapshot:
        if not self._undo:
            raise TimelineEditError("Nothing to undo")
        previous = self._undo.pop()
        current = self.snapshot
        self._redo.append(current)
        self.snapshot = replace(previous, revision=current.revision + 1)
        return self.snapshot

    def redo(self) -> ProjectSnapshot:
        if not self._redo:
            raise TimelineEditError("Nothing to redo")
        next_snapshot = self._redo.pop()
        current = self.snapshot
        self._undo.append(current)
        self.snapshot = replace(next_snapshot, revision=current.revision + 1)
        return self.snapshot
