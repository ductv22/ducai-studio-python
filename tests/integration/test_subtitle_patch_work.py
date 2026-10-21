"""Compact edits preserve immutable sharing instead of cloning every cue."""

from ducai_studio.domain.project import ProjectSnapshot, SubtitleCue
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.job_manager import service as service_module
from ducai_studio.job_manager.service import JobManagerService


def test_one_text_edit_does_not_reconstruct_every_cue_to_compare_patch(tmp_path, monkeypatch):
    cues = tuple(SubtitleCue(f"c{i}", i + 1, TimeRange(i * 1000, i * 1000 + 900),
                             f"Cue {i}", "zh") for i in range(1000))
    snapshot = ProjectSnapshot("p", "Large edit", 1, source_cues=cues)
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts")
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=snapshot)
    original_replace = service_module.replace
    replaced_cues = []

    def record_replace(value, **changes):
        if isinstance(value, SubtitleCue):
            replaced_cues.append(value.id)
        return original_replace(value, **changes)

    monkeypatch.setattr(service_module, "replace", record_replace)
    patch = service.subtitle_command(project_id="p", command="update_text", cue_id="c500",
        params={"text": "Edited", "language": "source"}, compact=True, deferred=True)
    assert [cue["id"] for cue in patch["source_cue_upserts"]] == ["c500"]
    assert not patch["source_cue_indices"]
    assert len(replaced_cues) <= 2
    saved = service.projects.load("p")
    assert saved.source_cues[500].text == "Edited"
    assert saved.source_cues[:500] == cues[:500]
    assert saved.source_cues[501:] == cues[501:]
