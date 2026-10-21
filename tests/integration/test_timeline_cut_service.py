import sys
from dataclasses import replace

import pytest

from ducai_studio.domain.jobs import JobKind, JobSpec, JobState
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
    VoiceConformState,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.domain.timeline_edit import build_scan_plan, project_for_timeline
from ducai_studio.domain.voice_conform import (
    VoiceConformPlan,
    VoiceConformSegment,
    VoiceConformSettings,
)
from ducai_studio.job_manager.service import JobManagerService

S = 1_000_000


def test_clipboard_paste_delete_layer_keeps_v1_and_reopens(tmp_path):
    service, original = setup_project(tmp_path)
    video = original.tracks[0].clips[0]
    copied = service.copy_video_clip("p", video.id)
    assert copied["copied"]
    patch = service.timeline_command(project_id="p", command="paste_video", timeline_us=10*S)
    assert patch["_timeline_patch"]
    saved = service.projects.load("p")
    layer = next(t for t in saved.tracks if t.id == "V2")
    assert layer.clips[0].timeline_range == TimeRange(10*S, 70*S)
    plan = build_scan_plan(saved)
    assert plan["duration_us"] == 70*S
    assert plan["segments"][-1]["clip_id"] == layer.clips[0].id
    service.timeline_command(project_id="p", command="delete_video_segment",
                             clip_id=layer.clips[0].id)
    deleted = service.projects.load("p")
    assert deleted.tracks[0].clips == original.tracks[0].clips
    assert deleted.source_cues == original.source_cues
    service.timeline_command(project_id="p", command="undo")
    reopened = JobManagerService(database_path=tmp_path / "state.db",
                                 artifact_root=tmp_path / "artifacts")
    assert reopened.projects.load("p").tracks == saved.tracks


def test_timeline_markers_use_compact_patch_and_persist(tmp_path):
    service, original = setup_project(tmp_path)
    patch = service.timeline_command(
        project_id="p",
        command="set_markers",
        value={"markers": [25 * S, 5 * S, 25 * S]},
    )

    assert patch["timeline_markers"] == [5 * S, 25 * S]
    assert service.projects.load("p").timeline_markers == (5 * S, 25 * S)
    assert patch["clip_upserts"] == []
    assert patch["removed_clip_ids"] == []

    service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p").timeline_markers == original.timeline_markers
    service.timeline_command(project_id="p", command="redo")
    assert service.projects.load("p").timeline_markers == (5 * S, 25 * S)


def test_copy_after_cut_freezes_display_range_and_materializes_once(tmp_path):
    service, _ = setup_project(tmp_path)
    cut(service)
    view = project_for_timeline(service.projects.load("p"))
    video = view.tracks[0].clips[0]
    service.copy_video_clip("p", video.id)
    service.timeline_command(project_id="p", command="paste_video", timeline_us=5*S)
    pasted = service.projects.load("p")
    assert pasted.timeline_edit is None
    assert pasted.tracks[0].clips[0].source_range == TimeRange(10*S, 60*S)
    layer = next(t for t in pasted.tracks if t.id == "V2")
    assert layer.clips[0].source_range == TimeRange(10*S, 60*S)
    assert layer.clips[0].timeline_range == TimeRange(5*S, 55*S)
    service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p").timeline_edit is not None


def test_repeated_cut_materialization_keeps_unique_clip_ids_and_undo(tmp_path):
    service, original = setup_project(tmp_path)
    cut(service, "remove_range", 5*S, 10*S)
    service.timeline_command(project_id="p", command="clip_mute", clip_id="v", value=True)
    materialized = service.projects.load("p")
    cut(service, "remove_range", S, 2*S)
    view = project_for_timeline(service.projects.load("p"))
    ids = [clip.id for track in view.tracks for clip in track.clips]
    assert len(ids) == len(set(ids))
    assert build_scan_plan(view)["duration_us"] == 54*S
    service.timeline_command(project_id="p", command="clip_mute", clip_id="v", value=False)
    assert [clip.source_range for clip in service.projects.load("p").tracks[0].clips] == [
        clip.source_range for clip in view.tracks[0].clips]
    service.timeline_command(project_id="p", command="undo")
    service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p").tracks == materialized.tracks
    assert original.tracks[0].clips[0].source_range == TimeRange(0, 60*S)


@pytest.mark.parametrize("with_cut", [False, True])
@pytest.mark.parametrize("command", ["paste_video", "duplicate_video", "clip_mute"])
def test_clipboard_commands_materialize_multirate_conform_once(tmp_path, command, with_cut):
    service, original = setup_project(tmp_path)
    plan = VoiceConformPlan(1, VoiceConformSettings(), 60*S, 80*S, (
        VoiceConformSegment(0, 20*S, 0, 40*S, .5),
        VoiceConformSegment(20*S, 60*S, 40*S, 80*S, 1.0),
    ), (), (), "conform")
    voice = MediaAsset("voice", str(tmp_path / "voice.wav"), MediaKind.AUDIO, 80*S)
    base = replace(voice, id="base", path=str(tmp_path / "base.wav"))
    original = replace(original, revision=2, media=(*original.media, voice, base),
        voice_conform=VoiceConformState(plan, "base", "voice", "tts"),
        tracks=(*original.tracks, TimelineTrack("D1", TrackKind.VOICE, "D1", (
            TimelineClip("d", "D1", TimeRange(0, 80*S), TimeRange(0, 80*S), "voice"),))))
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=original)
    if with_cut:
        cut(service, "keep_from", 10*S, 80*S)
    before = service.projects.load("p")
    view = service._materialized_video_view(before)
    selected = view.tracks[0].clips[0]
    service.copy_video_clip("p", selected.id)
    args = {"timeline_us": 5*S} if command == "paste_video" else {"clip_id": selected.id}
    if command == "clip_mute":
        args["value"] = True
    service.timeline_command(project_id="p", command=command, **args)
    saved = service.projects.load("p")
    assert saved.voice_conform is None and saved.timeline_edit is None
    base_clips = view.tracks[0].clips
    if command == "paste_video":
        pasted = next(track for track in saved.tracks if track.id == "V2").clips
        expected = base_clips[:1] if with_cut else base_clips
        assert [clip.source_range for clip in pasted] == [clip.source_range for clip in expected]
        assert [clip.speed for clip in pasted] == [clip.speed for clip in expected]
        assert pasted[0].timeline_range.start_us == 5*S
        assert pasted[-1].timeline_range.end_us == 5*S + sum(
            clip.timeline_range.duration_us for clip in expected)
    elif command == "duplicate_video":
        expected = base_clips[:1] if with_cut else base_clips
        assert build_scan_plan(saved)["duration_us"] == build_scan_plan(view)["duration_us"] + sum(
            clip.timeline_range.duration_us for clip in expected)
    else:
        assert saved.tracks[0].clips[0].muted
        assert all(clip.muted for clip in saved.tracks[0].clips) is (not with_cut)
    service.timeline_command(project_id="p", command="undo")
    restored = service.projects.load("p")
    assert restored.tracks == before.tracks and restored.voice_conform == before.voice_conform
    assert restored.timeline_edit == before.timeline_edit


def test_compact_delete_reindexes_without_resending_full_cue_evidence(tmp_path):
    import json
    from dataclasses import asdict

    from PySide6.QtCore import QCoreApplication

    from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel

    QCoreApplication.instance() or QCoreApplication([])
    service, original = setup_project(tmp_path)
    model = ProjectStateModel()
    model.replace(json.loads(json.dumps(asdict(original))))
    patch = service.subtitle_command(project_id="p", command="delete", cue_id="c1", compact=True)
    assert patch["source_cue_upserts"] == patch["translated_cue_upserts"] == []
    assert patch["source_cue_indices"] == {"c2": 1, "c3": 2}
    model.apply_subtitle_patch(patch)
    assert model._canonical_snapshot["source_cues"] == json.loads(json.dumps(
        asdict(service.projects.load("p"))))["source_cues"]


def test_layer_track_undo_patch_preserves_authoritative_order(tmp_path):
    import json
    from dataclasses import asdict

    from PySide6.QtCore import QCoreApplication

    from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel

    QCoreApplication.instance() or QCoreApplication([])
    service, original = setup_project(tmp_path)
    service.copy_video_clip("p", original.tracks[0].clips[0].id)
    for _ in range(2):
        service.timeline_command(project_id="p", command="paste_video", timeline_us=0)
    saved = service.projects.load("p")
    model = ProjectStateModel()
    model.replace(json.loads(json.dumps(asdict(saved))))
    patch = service.timeline_command(project_id="p", command="delete_tracks", track_ids=("V2",))
    model.apply_timeline_patch(patch)
    patch = service.timeline_command(project_id="p", command="undo")
    model.apply_timeline_patch(patch)
    assert [track["id"] for track in model._canonical_snapshot["tracks"]] == [
        track.id for track in service.projects.load("p").tracks]
    v3 = next(track for track in saved.tracks if track.id == "V3").clips[0]
    assert model.previewDescriptor(1*S)["clipId"] == v3.id


def test_scissors_twice_then_delete_middle_segment_ripples_everything(tmp_path):
    service, original = setup_project(tmp_path)
    service.timeline_command(project_id='p', command='split_video', timeline_us=20*S)
    service.timeline_command(project_id='p', command='split_video', timeline_us=30*S)
    split = service.projects.load('p')
    assert len(split.tracks[0].clips) == 3
    assert split.source_cues == original.source_cues
    middle = next(c for c in split.tracks[0].clips if c.timeline_range.start_us == 20*S)
    service.timeline_command(project_id='p', command='delete_video_segment', clip_id=middle.id)
    saved = service.projects.load('p')
    view = project_for_timeline(saved)
    assert [c.id for c in view.source_cues] == ['c1', 'c3']
    assert view.source_cues[-1].range == TimeRange(30*S, 35*S)
    assert not view.overlays
    plan = build_scan_plan(saved)
    assert plan['duration_us'] == 50*S
    assert [s['source_start_us'] for s in plan['segments']] == [0, 30*S]
    service.timeline_command(project_id='p', command='undo')
    assert service.projects.load('p').source_cues == split.source_cues
    assert len(service.projects.load('p').tracks[0].clips) == 3
    service.timeline_command(project_id='p', command='redo')
    reopened = JobManagerService(database_path=tmp_path/'state.db',
                                 artifact_root=tmp_path/'artifacts')
    assert build_scan_plan(reopened.projects.load('p')) == plan


def test_scissors_after_existing_cut_uses_display_time_once(tmp_path):
    service, _ = setup_project(tmp_path)
    cut(service)
    service.timeline_command(project_id='p', command='split_video', timeline_us=10*S)
    split = service.projects.load('p')
    assert split.timeline_edit is None
    assert split.tracks[0].clips[0].source_range == TimeRange(10*S, 20*S)
    segment = split.tracks[0].clips[0]
    service.timeline_command(project_id='p', command='delete_video_segment', clip_id=segment.id)
    plan = build_scan_plan(service.projects.load('p'))
    assert plan['duration_us'] == 40*S
    assert plan['segments'][0]['source_start_us'] == 20*S


@pytest.mark.parametrize('translation_id', ['c3', 'vi3'])
def test_synchronized_cut_preserves_sparse_translation_pair(translation_id, tmp_path):
    service, original = setup_project(tmp_path)
    source = replace(original, revision=2, translated_cues=(
        replace(original.translated_cues[-1], id=translation_id),))
    service.projects.save(project_path=tmp_path/'project.ducai', snapshot=source)
    cut(service)
    saved = service.projects.load('p')
    assert saved.source_cues[-1].id == 'c3'
    assert saved.source_cues[-1].index == saved.translated_cues[0].index == 2


@pytest.mark.parametrize('command', ['split_video', 'delete_video_segment'])
def test_scissors_and_segment_delete_reject_active_jobs_and_rollback_save(
    tmp_path, monkeypatch, command,
):
    service, original = setup_project(tmp_path)
    job = service.jobs.create(JobSpec(JobKind.OCR, 'p', 1, 'paddle', '1', 'm', 'i', 's', {}))
    for state in (JobState.START_REQUESTED, JobState.RUNNING,
                  JobState.PAUSE_REQUESTED, JobState.PAUSED):
        service.jobs.transition(job.id, state)
    params = {'project_id': 'p', 'command': command, 'timeline_us': 20*S, 'clip_id': 'v'}
    with pytest.raises(ValueError, match='tạm dừng chưa đủ'):
        service.timeline_command(**params)
    assert service.projects.load('p') == original
    service.jobs.transition(job.id, JobState.CANCEL_REQUESTED)
    service.jobs.transition(job.id, JobState.CANCELLED)
    if command == 'delete_video_segment':
        service.timeline_command(project_id='p', command='split_video', timeline_us=20*S)
        original = service.projects.load('p')
        params['clip_id'] = original.tracks[0].clips[0].id
    def fail_save(**_kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(service.projects, 'save', fail_save)
    with pytest.raises(OSError, match='disk full'):
        service.timeline_command(**params)
    assert service.projects.load('p') == original
    assert service._timeline_editors['p'].snapshot == original


def test_scissors_rejects_clip_edge_and_deleting_everything(tmp_path):
    service, original = setup_project(tmp_path)
    with pytest.raises(ValueError, match='mép nối'):
        service.timeline_command(project_id='p', command='split_video', timeline_us=0)
    with pytest.raises(ValueError, match='toàn bộ'):
        service.timeline_command(project_id='p', command='delete_video_segment', clip_id='v')
    assert service.projects.load('p') == original


def test_scissors_and_delete_compose_conform_video_and_d1_once(tmp_path):
    service, original = setup_project(tmp_path)
    plan = VoiceConformPlan(1, VoiceConformSettings(), 60*S, 80*S, (
        VoiceConformSegment(0, 20*S, 0, 40*S, .5),
        VoiceConformSegment(20*S, 60*S, 40*S, 80*S, 1.0),
    ), (), (), 'conform')
    voice = MediaAsset('voice', str(tmp_path/'voice.wav'), MediaKind.AUDIO, 80*S)
    base = replace(voice, id='base', path=str(tmp_path/'base.wav'))
    original = replace(original, revision=2, media=(*original.media, voice, base),
        voice_conform=VoiceConformState(plan, 'base', 'voice', 'tts'),
        tracks=(*original.tracks, TimelineTrack('D1', TrackKind.VOICE, 'D1', (
            TimelineClip('d', 'D1', TimeRange(0, 80*S), TimeRange(0, 80*S), 'voice'),))))
    service.projects.save(project_path=tmp_path/'project.ducai', snapshot=original)
    service.timeline_command(project_id='p', command='split_video', timeline_us=30*S)
    saved = service.projects.load('p')
    assert saved.voice_conform is None
    assert saved.tracks[0].clips[0].source_range == TimeRange(0, 15*S)
    middle = next(c for c in saved.tracks[0].clips if c.timeline_range.start_us == 30*S)
    service.timeline_command(project_id='p', command='delete_video_segment', clip_id=middle.id)
    saved = service.projects.load('p')
    plan = build_scan_plan(saved)
    assert plan['duration_us'] == 70*S
    assert [s['source_start_us'] for s in plan['segments']] == [0, 20*S]
    view = project_for_timeline(saved)
    assert [c.source_range for c in view.tracks[-1].clips] == [
        TimeRange(0, 30*S), TimeRange(40*S, 80*S)]
    service.timeline_command(project_id='p', command='undo')
    service.timeline_command(project_id='p', command='undo')
    assert service.projects.load('p').voice_conform == original.voice_conform


def setup_project(tmp_path):
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts")
    media = tmp_path / "source.mp4"
    media.write_bytes(b"unchanged source fixture")
    video = TimelineClip("v", "V1", TimeRange(0, 60*S), TimeRange(0, 60*S), "m", "av")
    audio = replace(video, id="a", track_id="A1")
    cues = tuple(SubtitleCue(f"c{i}", i, TimeRange(a*S, b*S), text, "zh")
                 for i, (a, b, text) in enumerate(((1, 5, "删除"), (21, 25, "中间"),
                                                  (40, 45, "保留")), 1))
    snapshot = ProjectSnapshot("p", "Cut", 1,
                               media=(MediaAsset("m", str(media), MediaKind.VIDEO, 60*S),),
                               tracks=(TimelineTrack("V1", TrackKind.VIDEO, "V1", (video,)),
                                       TimelineTrack("A1", TrackKind.ORIGINAL_AUDIO,
                                                     "A1", (audio,))),
                               source_cues=cues,
                               translated_cues=tuple(replace(c, text="Việt", language="vi")
                                                     for c in cues),
                               overlays=(Overlay("blur", OverlayKind.BLUR, TimeRange(21*S, 25*S),
                                                 .1, .1, .2, .2),))
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=snapshot)
    return service, snapshot


def test_mixed_text_clip_and_effect_history_restores_latest_edit_in_order(tmp_path):
    service, original = setup_project(tmp_path)
    service.subtitle_command(project_id="p", command="update_text", cue_id="c1",
                             language="translated", params={"text": "Lời mới"})
    text_edit = service.projects.load("p")
    service.timeline_command(project_id="p", command="split_video", timeline_us=20*S)
    split = service.projects.load("p")
    service.effect_command(project_id="p", command="update", overlay_id="blur",
                           values={"x": .3})
    assert service.projects.load("p").overlays[0].x == .3
    service.subtitle_command(project_id="p", command="set_text_format",
                             params={"cue_ids": ["c1"], "font_size": 250,
                                     "orientation": "horizontal", "background_color": "#000000",
                                     "background_padding_x": 300, "background_padding_y": 150})
    latest = service.projects.load("p")
    service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p").source_cues[0].text_font_size == 12
    # Explicit subtitle undo follows the same order and uses the general patch.
    patch = service.subtitle_command(project_id="p", command="undo", compact=True)
    assert patch["_timeline_patch"] and patch["overlay_upserts"][0]["x"] == .1
    service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p").tracks == text_edit.tracks
    service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p").translated_cues == original.translated_cues
    for _ in range(4):
        service.timeline_command(project_id="p", command="redo")
    assert service.projects.load("p").tracks == split.tracks
    assert service.projects.load("p").source_cues == latest.source_cues
    assert service.projects.load("p").overlays == latest.overlays


def test_failed_save_does_not_consume_mixed_history(tmp_path, monkeypatch):
    service, _ = setup_project(tmp_path)
    service.subtitle_command(project_id="p", command="update_text", cue_id="c1",
                             language="translated", params={"text": "Mới"})
    head = service.projects.load("p")
    save = service.projects.save
    monkeypatch.setattr(service.projects, "save", lambda **_kw: (_ for _ in ()).throw(
        OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p") == head
    monkeypatch.setattr(service.projects, "save", save)
    service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p").translated_cues[0].text == "Việt"


def test_failed_subtitle_save_keeps_previous_clip_history(tmp_path, monkeypatch):
    service, original = setup_project(tmp_path)
    service.timeline_command(project_id="p", command="split_video", timeline_us=20*S)
    save = service.projects.save
    monkeypatch.setattr(service.projects, "save", lambda **_kw: (_ for _ in ()).throw(
        OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.subtitle_command(project_id="p", command="update_text", cue_id="c1",
                                 language="translated", params={"text": "Mới"})
    monkeypatch.setattr(service.projects, "save", save)
    service.subtitle_command(project_id="p", command="undo", compact=True)
    assert service.projects.load("p").tracks == original.tracks


def cut(service, operation="keep_from", start=10*S, end=60*S, **kwargs):
    return service.timeline_command(project_id="p", command="cut", operation=operation,
                                    range_start_us=start, range_end_us=end, **kwargs)


def test_cut_keeps_source_revisions_patch_identity_and_exported_srt(tmp_path):
    service, original = setup_project(tmp_path)
    patch = cut(service, base_revision=1, request_id="cut-1")
    assert patch["request_id"] == "cut-1"
    assert patch["removed_source_cue_ids"] == ["c1"]
    assert service.projects.load("p", 1) == original
    saved = service.projects.load("p")
    assert project_for_timeline(saved).tracks[0].clips[0].source_range == TimeRange(10*S, 60*S)
    assert build_scan_plan(saved)["duration_us"] == 50*S
    output = tmp_path / "translated.srt"
    service.export_subtitle_srt(project_id="p", language="translated", output_path=str(output))
    assert "00:00:11,000 --> 00:00:15,000" in output.read_text(encoding="utf-8-sig")
    service.timeline_command(project_id="p", command="undo")
    assert service.projects.load("p").source_cues == original.source_cues
    service.timeline_command(project_id="p", command="redo")
    assert service.projects.load("p").timeline_edit == saved.timeline_edit
    assert (tmp_path / "source.mp4").read_bytes() == b"unchanged source fixture"


def test_cut_guard_includes_paused_jobs_and_save_failure_is_atomic(tmp_path, monkeypatch):
    service, original = setup_project(tmp_path)
    job = service.jobs.create(JobSpec(JobKind.OCR, "p", 1, "paddle", "1", "m", "i", "s", {}))
    for state in (JobState.START_REQUESTED, JobState.RUNNING,
                  JobState.PAUSE_REQUESTED, JobState.PAUSED):
        service.jobs.transition(job.id, state)
    with pytest.raises(ValueError, match="tạm dừng chưa đủ"):
        cut(service)
    service.jobs.transition(job.id, JobState.CANCEL_REQUESTED)
    service.jobs.transition(job.id, JobState.CANCELLED)
    with monkeypatch.context() as context:
        def fail_save(**_kwargs):
            raise OSError("disk full")
        context.setattr(service.projects, "save", fail_save)
        with pytest.raises(OSError, match="disk full"):
            cut(service)
    assert service.projects.load("p") == original
    assert service._timeline_editors["p"].snapshot == original
    assert not service._timeline_editors["p"].can_undo
    with pytest.raises(ValueError, match="đã thay đổi"):
        cut(service, base_revision=2)
    assert service.projects.load("p") == original


def test_ocr_scope_fingerprint_and_canonical_import_mapping(tmp_path):
    service, original = setup_project(tmp_path)
    uncut = build_scan_plan(original)
    cut(service)
    saved = service.projects.load("p")
    job = service.create_ocr_job(project_id="p", sample_interval_us=800_000,
                                 engine_python=sys.executable)
    plan = job["spec"]["provider_config"]["scan_plan"]
    assert plan != uncut
    assert plan["segments"][0]["source_start_us"] == 10*S
    cue = SubtitleCue("ocr", 1, TimeRange(0, 2*S), "保留", "zh")
    imported = service._canonicalize_ocr_result(replace(saved, source_cues=(cue,),
                                                       translated_cues=()), {"scan_plan": plan})
    assert imported.source_cues[0].range == TimeRange(10*S, 12*S)
    assert project_for_timeline(imported).source_cues[0].range == TimeRange(0, 2*S)
    frozen = service.jobs.get(job["id"]).spec.provider_config
    frozen_import = service._canonicalize_ocr_result(
        replace(saved, source_cues=(cue,), translated_cues=()), frozen)
    assert frozen_import.source_cues[0].range == TimeRange(10*S, 12*S)
    service._validate_ocr_scan_layout(saved, frozen)
    with pytest.raises(ValueError, match="Timeline đã thay đổi"):
        service._validate_ocr_scan_layout(original, frozen)
    with pytest.raises(ValueError, match="Timeline đã thay đổi"):
        service._validate_ocr_scan_layout(original, {"scan_plan": plan})


def test_middle_cut_excludes_cues_overlays_and_display_edits_map_once(tmp_path):
    service, _ = setup_project(tmp_path)
    cut(service, "remove_range", 20*S, 30*S)
    saved = service.projects.load("p")
    view = project_for_timeline(saved)
    assert [cue.id for cue in view.source_cues] == ["c1", "c3"]
    assert not view.overlays
    assert view.source_cues[1].range == TimeRange(30*S, 35*S)
    overrides = service._display_cue_time_overrides(saved, ("c3",), -12*S, "move")
    assert overrides["c3"] == TimeRange(18*S, 33*S)
    resized = service._display_cue_time_overrides(saved, ("c3",), S, "right")
    assert resized["c3"] == TimeRange(40*S, 46*S)
    patch = service.timeline_command(project_id="p", command="split", clip_id="v::cut::1",
                                     timeline_us=35*S)
    assert patch["removed_overlay_ids"] == ["blur"]
    after = service.projects.load("p")
    assert after.timeline_edit is None
    assert len(after.tracks[0].clips) == 3
    patch = service.timeline_command(project_id="p", command="undo")
    assert patch["overlay_upserts"][0]["id"] == "blur"
    assert service.projects.load("p").timeline_edit == saved.timeline_edit


def test_secondary_media_changes_invalidate_ocr_input_identity(tmp_path):
    service, original = setup_project(tmp_path)
    secondary = tmp_path / "secondary.mp4"
    secondary.write_bytes(b"second source")
    second_asset = MediaAsset("m2", str(secondary), MediaKind.VIDEO, 10*S)
    second = TimelineClip("v2", "V1", TimeRange(60*S, 70*S), TimeRange(0, 10*S), "m2")
    video_track = replace(original.tracks[0], clips=(*original.tracks[0].clips, second))
    snapshot = replace(original, media=(*original.media, second_asset),
                       tracks=(video_track,
                               original.tracks[1]))
    plan = build_scan_plan(snapshot)
    before = service._ocr_source_identities(snapshot, plan)
    secondary.write_bytes(b"changed second source")
    assert service._ocr_source_identities(snapshot, plan) != before
