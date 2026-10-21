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


def setup_session(tmp_path):
    service = JobManagerService(
        database_path=tmp_path / "state.db", artifact_root=tmp_path / "artifacts"
    )
    cues = tuple(
        SubtitleCue(f"c{i}", i + 1, TimeRange(i * 1_000_000, (i + 1) * 1_000_000), "Thoại", "vi")
        for i in range(3)
    )
    clips = tuple(
        TimelineClip(
            f"clip-{c.id}", "S1", c.range, c.range, cue_id=c.id, cue_index=c.index, cue_text=c.text
        )
        for c in cues
    )
    snapshot = ProjectSnapshot(
        "p",
        "Draft",
        1,
        source_cues=cues,
        tracks=(TimelineTrack("S1", TrackKind.SUBTITLE, "S1", clips),),
    )
    service.projects.save(project_path=tmp_path / "p.ducai", snapshot=snapshot)
    return service, snapshot


def test_edit_draft_delete_undo_and_save_do_not_write_per_action(tmp_path):
    service, original = setup_session(tmp_path)
    for number in range(20):
        patch = service.subtitle_command(
            project_id="p",
            command="update_text",
            cue_id="c0",
            params={"text": f"Text {number}"},
            compact=True,
            deferred=True,
        )
        assert patch["_project_dirty"]
    service.subtitle_command(project_id="p", command="delete", cue_id="c1", deferred=True)
    service.timeline_command(project_id="p", command="undo", deferred=True)
    working = service.projects.load("p")
    assert working.source_cues[0].text == "Text 19"
    assert len(working.source_cues) == 3
    assert service.projects.persisted_head_revision("p") == original.revision
    with service.database.connect(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM project_revisions WHERE project_id='p'"
            ).fetchone()[0]
            == 1
        )
    saved = service.save_project_edits("p")
    assert not saved["_project_dirty"]
    reopened = JobManagerService(
        database_path=tmp_path / "state.db", artifact_root=tmp_path / "artifacts"
    )
    assert reopened.projects.load("p") == working
    assert service.save_project_edits("p")["revision"] == working.revision


def test_failed_draft_save_retains_latest_working_edits(tmp_path, monkeypatch):
    service, _ = setup_session(tmp_path)
    service.subtitle_command(project_id="p", command="delete", cue_id="c1", deferred=True)
    latest = service.projects.load("p")
    save = service.projects.save
    monkeypatch.setattr(
        service.projects, "save", lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        service.save_project_edits("p")
    assert service.projects.load("p") == latest
    assert service.projects.has_pending_edits("p")
    monkeypatch.setattr(service.projects, "save", save)
    service.save_project_edits("p")
    assert not service.projects.has_pending_edits("p")


def test_save_conflict_does_not_overwrite_external_head_or_drop_draft(tmp_path):
    service, original = setup_session(tmp_path)
    service.subtitle_command(
        project_id="p", command="update_text", cue_id="c0", params={"text": "Draft"}, deferred=True
    )
    other = JobManagerService(
        database_path=tmp_path / "state.db", artifact_root=tmp_path / "artifacts"
    )
    other.projects.save(
        project_path=tmp_path / "p.ducai", snapshot=replace(original, name="External", revision=2)
    )
    with pytest.raises((ValueError, RuntimeError)):
        service.save_project_edits("p")
    assert service.projects.has_pending_edits("p")
    assert service.projects.load("p").source_cues[0].text == "Draft"
    assert other.projects.load("p").name == "External"


def test_staged_job_persists_working_revision_and_historical_input_remains_readable(tmp_path):
    service, original = setup_session(tmp_path)
    service.subtitle_command(project_id="p", command="delete", cue_id="c1", deferred=True)
    working = service.projects.load("p")
    assert service.projects.load("p", original.revision) == original
    raw = {"kind": "ocr", "project_id": "p", "input_revision": working.revision,
           "provider_id": "paddle-fast-quality", "provider_version": "1", "model_id": "ocr",
           "input_fingerprint": "input", "settings_fingerprint": "settings",
           "provider_config": {"output_path": str(tmp_path / "output_ZH_FINAL.srt")}}
    result = service._create_or_reuse_completed_job(raw, reuse_completed=False, stage_output=True)
    assert result["spec"]["input_revision"] == working.revision
    assert service.projects.persisted_head_revision("p") == working.revision
    assert not service.projects.has_pending_edits("p")


def test_deleted_project_cannot_be_reopened_from_an_orphan_draft(tmp_path):
    service, _ = setup_session(tmp_path)
    service.subtitle_command(project_id="p", command="delete", cue_id="c1", deferred=True)
    service.projects.delete_internal("p")
    assert not service.projects.has_pending_edits("p")
    with pytest.raises(LookupError):
        service.projects.load("p")
