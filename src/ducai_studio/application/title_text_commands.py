"""Independent title edits using the existing typography/geometry commands."""

from dataclasses import replace
from typing import Any

from ducai_studio.application.subtitle_commands import SubtitleEditor
from ducai_studio.domain.project import ProjectSnapshot, TimelineClip, TimelineTrack, TrackKind


def edit_title_text(
    snapshot: ProjectSnapshot, command: str, cue_id: str | None, values: dict[str, Any]
) -> ProjectSnapshot:
    """Adapt only titles, then restore the untouched subtitle/job collections.

    The transient scene editor never persists. Its overlap lane allocation is
    intentionally not copied: independent titles may overlap on the Text line.
    """
    track = next((item for item in snapshot.tracks if item.kind is TrackKind.TEXT), None)
    if track is not None and track.locked:
        raise ValueError("Track Text đang khóa; hãy mở khóa trước khi sửa")
    # A title line may intentionally contain simultaneous labels. Give each
    # transient cue its own lane so scene-only overlap guards cannot reject
    # title move/resize; all lanes are restored to T1 below.
    cues = tuple(
        replace(cue, role="scene_text", subtitle_track_id=f"S{index + 2}")
        for index, cue in enumerate(snapshot.title_cues)
    )
    transient = replace(
        snapshot,
        source_cues=cues,
        translated_cues=(),
        title_cues=(),
        tracks=(),
        voice_conform=None,
        timeline_edit=None,
    )
    editor = SubtitleEditor(transient)
    ids = tuple(values.get("cue_ids") or ([cue_id] if cue_id else []))
    if command == "add_title_text":
        options = {
            key: value
            for key, value in values.items()
            if key not in {"title_mode", "display_time", "cue_ids", "track_id", "cue_role"}
        }
        options.setdefault("font_size", 40)
        options.setdefault("style_hint", "standard")
        options.setdefault("text_color", "#FFFFFF")
        options.setdefault("background_color", "")
        options.setdefault("background_style", "solid")
        options.setdefault("background_opacity_percent", 100)
        options.setdefault("fade_enabled", False)
        options["visual_box"] = tuple(options.get("visual_box", (0.04, 0.04, 0.40, 0.14)))
        updated = editor.add_subtitle_cue(cue_role="scene_text", **options)
    elif command == "update_text":
        updated = editor.update_text(cue_id or "", values["text"], language="source")
    elif command == "set_text_format":
        options = {
            key: value
            for key, value in values.items()
            if key not in {"title_mode", "display_time", "cue_ids"}
        }
        updated = editor.set_text_format_many(ids, **options)
    elif command == "transform_cue_geometry":
        options = {
            key: value
            for key, value in values.items()
            if key not in {"title_mode", "display_time", "cue_ids"}
        }
        updated = editor.transform_cue_geometry(ids, **options)
    elif command == "set_scene_text_fade":
        updated = editor.set_scene_fade_many(
            ids, enabled=values["enabled"], duration_ms=values.get("duration_ms", 300)
        )
    elif command == "set_scene_text_font_scale":
        updated = editor.set_scene_text_font_scale(cue_id or "", values["scale"])
    elif command in {"set_scene_text_timing", "adjust_time"}:
        updated = editor.set_scene_text_timing(
            cue_id or "", start_us=values["start_us"], end_us=values["end_us"]
        )
    elif command == "move_scene_texts":
        updated = editor.move_scene_texts(
            ids,
            delta_us=values["delta_us"],
            lane_delta=0,
            range_overrides=values.get("range_overrides"),
        )
    elif command == "resize_scene_texts":
        updated = editor.resize_scene_texts(
            ids,
            edge=values["edge"],
            delta_us=values["delta_us"],
            range_overrides=values.get("range_overrides"),
        )
    elif command in {"delete", "delete_cues"}:
        updated = editor.delete_many(ids)
    else:
        raise ValueError(f"Thao tác Text chưa được hỗ trợ: {command}")
    titles = tuple(
        replace(cue, role="title_text", source="manual_title_text", subtitle_track_id="T1")
        for cue in updated.source_cues
    )
    previous = {clip.cue_id: clip for clip in track.clips} if track else {}
    clips = tuple(
        TimelineClip(
            id=previous[cue.id].id if cue.id in previous else f"title-{cue.id}",
            track_id="T1",
            timeline_range=cue.range,
            cue_id=cue.id,
            cue_index=cue.index,
            cue_text=cue.text,
            payload_revision=cue.revision,
        )
        for cue in titles
    )
    title_track = TimelineTrack(
        "T1",
        TrackKind.TEXT,
        "Text",
        clips,
        locked=track.locked if track else False,
        visible=track.visible if track else True,
    )
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        title_cues=titles,
        tracks=(
            title_track,
            *(item for item in snapshot.tracks if item.kind is not TrackKind.TEXT),
        ),
    )
