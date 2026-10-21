import json
from dataclasses import replace

import pytest

from ducai_studio.domain.project import (
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.job_manager.service import JobManagerService


def role_service(tmp_path, count=3):
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts")
    source = tuple(SubtitleCue(f"c{i}", i + 1, TimeRange(i * 1_000_000, (i + 1) * 1_000_000),
                               "原文", "zh", subtitle_track_id="S1") for i in range(count))
    translated = tuple(replace(cue, text=f"Thoại {cue.index}", language="vi") for cue in source)
    tracks = (TimelineTrack("S1", TrackKind.SUBTITLE, "Thoại", tuple(
        TimelineClip(f"clip-{cue.id}", "S1", cue.range, cue.range,
                     cue_id=cue.id, cue_index=cue.index, cue_text=translated[index].text)
        for index, cue in enumerate(source))),
        TimelineTrack("D1", TrackKind.VOICE, "Giọng", (
            TimelineClip("voice", "D1", source[0].range, cue_id=source[0].id),)))
    snapshot = ProjectSnapshot("roles", "Roles", 1, source_cues=source,
                               translated_cues=translated, tracks=tracks)
    service.projects.save(project_path=tmp_path / "roles.ducai", snapshot=snapshot)
    return service, snapshot


def test_role_patch_is_bounded_on_twelve_thousand_cues(tmp_path):
    service, original = role_service(tmp_path, 12_000)
    patch = service.subtitle_command(project_id="roles", command="set_cue_role", cue_id="c5999",
                                     params={"role": "scene_text", "cue_ids": ["c5999"]},
                                     compact=True, deferred=True)
    assert patch["revision"] == 2
    assert [cue["id"] for cue in patch["source_cue_upserts"]] == ["c5999"]
    assert [cue["id"] for cue in patch["translated_cue_upserts"]] == ["c5999"]
    assert [clip["id"] for clip in patch["subtitle_clip_upserts"]] == ["clip-c5999"]
    assert len(json.dumps(patch).encode()) < 12_000
    updated = service.projects.load("roles")
    assert next(track for track in updated.tracks if track.id == "D1") == original.tracks[1]
    assert service.projects.persisted_head_revision("roles") == 1
    service.save_project_edits("roles")
    reopened = JobManagerService(database_path=tmp_path / "state.db",
                                 artifact_root=tmp_path / "artifacts")
    assert reopened.projects.load("roles") == updated
    assert next(cue for cue in updated.source_cues if cue.id == "c5999").subtitle_track_id == "S2"


def test_bulk_role_command_shares_history_and_noop_does_not_save_again(tmp_path):
    service, original = role_service(tmp_path)
    params = {"role": "scene_text", "cue_ids": ["c0", "c1"]}
    service.subtitle_command(project_id="roles", command="set_cue_role", params=params,
                             compact=True, deferred=True)
    converted = service.projects.load("roles")
    noop = service.subtitle_command(project_id="roles", command="set_cue_role", params=params,
                                    compact=True, deferred=True)
    assert noop["revision"] == converted.revision
    assert not noop["source_cue_upserts"] and not noop["subtitle_clip_upserts"]
    service.timeline_command(project_id="roles", command="undo", deferred=True)
    assert service.projects.load("roles").source_cues == original.source_cues
    service.timeline_command(project_id="roles", command="redo", deferred=True)
    assert service.projects.load("roles").source_cues == converted.source_cues


@pytest.mark.parametrize("values", [
    {"role": "title_text", "cue_ids": ["c0"]},
    {"role": "dialogue", "cue_ids": ["c0", "missing"]},
    {"role": "dialogue", "cue_ids": ["c0", 99]},
    {"role": "dialogue", "cue_ids": []},
])
def test_invalid_role_service_command_never_leaves_partial_draft(tmp_path, values):
    service, original = role_service(tmp_path)
    with pytest.raises((ValueError, LookupError)):
        service.subtitle_command(project_id="roles", command="set_cue_role", params=values,
                                 compact=True, deferred=True)
    assert service.projects.load("roles") == original
    assert not service.projects.has_pending_edits("roles")


def test_dialogue_conversion_rehome_is_one_persisted_undoable_patch(tmp_path):
    service, base = role_service(tmp_path)
    scene = replace(base.source_cues[0], role="scene_text")
    source = (
        replace(scene, id="old1", index=1, range=TimeRange(0, 500_000)),
        replace(scene, id="old2", index=2, range=TimeRange(500_000, 1_000_000)),
        replace(scene, id="target", index=3, subtitle_track_id="S3"),
        replace(scene, id="occupied", index=4, subtitle_track_id="S2"),
    )
    translated = tuple(replace(cue, text=f"Dịch {cue.id}", language="vi") for cue in source)
    tracks = tuple(TimelineTrack(lane, TrackKind.SUBTITLE, "Chữ trong cảnh", tuple(
        TimelineClip(f"clip-{cue.id}", lane, cue.range, cue.range,
                     cue_id=cue.id, cue_index=cue.index, cue_text=f"Dịch {cue.id}")
        for cue in source if cue.subtitle_track_id == lane)) for lane in ("S1", "S2", "S3"))
    original = replace(base, revision=2, source_cues=source,
                       translated_cues=translated, tracks=tracks + (base.tracks[1],))
    service.projects.save(project_path=tmp_path / "roles.ducai", snapshot=original)
    assert service.projects.load("roles") == original
    patch = service.subtitle_command(
        project_id="roles", command="set_cue_role", cue_id="target",
        params={"role": "dialogue", "cue_ids": ["target"]}, compact=True, deferred=True,
    )
    assert {cue["id"] for cue in patch["source_cue_upserts"]} == {"old1", "old2", "target"}
    assert {cue["id"] for cue in patch["translated_cue_upserts"]} == {"old1", "old2", "target"}
    assert {clip["id"] for clip in patch["subtitle_clip_upserts"]} == {
        "clip-old1", "clip-old2", "clip-target",
    }
    updated = service.projects.load("roles")
    assert {cue.id: cue.subtitle_track_id for cue in updated.source_cues} == {
        "old1": "S3", "old2": "S3", "target": "S1", "occupied": "S2",
    }
    assert next(track for track in updated.tracks if track.id == "D1") == base.tracks[1]
    service.timeline_command(project_id="roles", command="undo", deferred=True)
    undone = service.projects.load("roles")
    assert undone.source_cues == original.source_cues and undone.tracks == original.tracks
    service.timeline_command(project_id="roles", command="redo", deferred=True)
    redone = service.projects.load("roles")
    assert redone.source_cues == updated.source_cues and redone.tracks == updated.tracks
    service.save_project_edits("roles")
    reopened = JobManagerService(database_path=tmp_path / "state.db",
                                 artifact_root=tmp_path / "artifacts")
    assert reopened.projects.load("roles") == redone
