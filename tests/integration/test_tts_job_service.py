from __future__ import annotations

import json
import shutil
import wave
from array import array
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ducai_studio.application.voice_pipeline import (
    VoicePipelineSettings,
    analyze_audio,
    file_sha256,
    write_pcm16_mono,
)
from ducai_studio.domain.jobs import JobKind, JobState
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.media_probe import MediaProbeResult
from ducai_studio.workers.tts_worker import TtsWorker


class FixtureProbe:
    def probe(self, media_path: str | Path) -> MediaProbeResult:
        path = Path(media_path)
        if path.suffix.casefold() == ".wav":
            return MediaProbeResult(5_000_000, False, True, format_name="wav")
        return MediaProbeResult(5_000_000, True, True, 1280, 720, "mp4")


class RealWavFixtureProbe(FixtureProbe):
    def probe(self, media_path: str | Path) -> MediaProbeResult:
        path = Path(media_path)
        if path.suffix.casefold() != ".wav":
            return super().probe(path)
        with wave.open(str(path), "rb") as stream:
            duration_us = round(
                stream.getnframes() * 1_000_000 / stream.getframerate()
            )
        return MediaProbeResult(duration_us, False, True, format_name="wav")


class CacheOnlyAudioProcessor:
    def time_stretch(self, *_args, **_kwargs) -> None:
        raise AssertionError("stretch-video conform must preserve the base cue WAV")

    def master_ebu_r128(self, source: Path, target: Path, **_kwargs) -> dict:
        shutil.copyfile(source, target)
        return {"input_i": -18.0, "target_offset": 0.0}


def _data_root(tmp_path: Path) -> Path:
    data_root = tmp_path / "data"
    voice_root = data_root / "piper_voices"
    voice_root.mkdir(parents=True)
    model = voice_root / "nghitts-chieu-thanh.onnx"
    model.write_bytes(b"fixture-model")
    model.with_suffix(".onnx.json").write_text(
        json.dumps(
            {
                "audio": {"sample_rate": 22_050},
                "phoneme_id_map": {"_": [0]},
            }
        ),
        encoding="utf-8",
    )
    runtime = data_root / "piper_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"fixture-python")
    return data_root


def _vieneu_data_root(tmp_path: Path) -> Path:
    data_root = tmp_path / "vieneu-data"
    runtime = data_root / "vieneu_3_8_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"fixture-python")
    presets = (
        runtime.parent.parent
        / "Lib"
        / "site-packages"
        / "vieneu"
        / "assets"
        / "voices_v3_turbo.json"
    )
    presets.parent.mkdir(parents=True)
    presets.write_text(
        json.dumps(
            {"presets": {"Mai Anh": {"description": "Nữ miền Bắc"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return data_root


def _project_with_translation(service: JobManagerService, tmp_path: Path) -> str:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"fixture-video")
    source = tmp_path / "film_ZH_FINAL.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n你好。\n\n2\n00:00:01,200 --> 00:00:02,200\n出发吧。\n",
        encoding="utf-8",
    )
    translated = tmp_path / "film_ZH_FINAL_codex_vi.srt"
    translated.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nXin chào.\n\n"
        "2\n00:00:01,200 --> 00:00:02,200\nLên đường thôi.\n",
        encoding="utf-8",
    )
    project_path = tmp_path / "project.ducai"
    project = service.create_project(project_path=str(project_path), name="Film")
    project_id = project["id"]
    service.import_media(project_id=project_id, media_path=str(video))
    service.import_media(project_id=project_id, media_path=str(source))
    snapshot = service.projects.load(project_id)
    snapshot = service.media_imports.import_srt(snapshot, translated, language="vi")
    summary = service.projects.get_summary(project_id)
    service.projects.save(project_path=summary["path"], snapshot=snapshot)
    return project_id


def test_voice_timing_recovery_does_not_borrow_findings_from_an_older_job(
    tmp_path: Path,
) -> None:
    """Legacy recovery is scoped to the job whose review is being inspected."""

    cue = {
        "id": "cue-1",
        "index": 1,
        "start_us": 0,
        "end_us": 1_000_000,
        "text": "Một câu dài",
    }
    current = SimpleNamespace(
        id="current",
        spec=SimpleNamespace(
            project_id="project",
            input_fingerprint="input",
            provider_id="piper",
            model_id="model",
            provider_config={
                "cues": [cue],
                "pipeline_settings": {},
                "voice_identity": "voice",
            },
        ),
    )
    old = SimpleNamespace(
        id="old",
        spec=SimpleNamespace(
            kind=JobKind.TTS,
            project_id="project",
            input_fingerprint="input",
            provider_id="piper",
            model_id="model",
            provider_config={"voice_identity": "voice"},
        ),
    )
    ledger_path = tmp_path / "old-ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "entries": {
                    "cue-1": {
                        "fit": {
                            "fits": False,
                            "audio_duration_us": 2_000_000,
                            "allowed_duration_us": 1_000_000,
                        }
                    }
                },
                "blockers": {"cue-1": {"code": "CUE_TOO_LONG"}},
            }
        ),
        encoding="utf-8",
    )
    service = object.__new__(JobManagerService)
    service.jobs = SimpleNamespace(list_recent=lambda **_kwargs: [old])
    service.load_worker_checkpoint = lambda job_id, **_kwargs: (
        {"artifact": {"uri": str(ledger_path)}} if job_id == "old" else None
    )

    assert service._recover_voice_timing_issues(current) == []


def test_create_tts_job_has_exact_cues_and_final_import_replaces_voice_track(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)

    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root)},
    )
    config = job["spec"]["provider_config"]

    assert [cue["text"] for cue in config["cues"]] == ["Xin chào.", "Lên đường thôi."]
    assert [cue["start_us"] for cue in config["cues"]] == [0, 1_200_000]
    assert config["force_cue_ids"] == []
    assert config["voice_read_speed"] == 1.0
    assert config["pipeline_settings"]["voice_read_speed"] == 1.0
    assert config["defer_timing_conform"] is True
    output = Path(config["output_path"])
    write_pcm16_mono(output, array("f", [0.0]) * (48_000 * 5), 48_000)

    imported = service.import_final_voice_wav(job["id"], str(output))

    voice_tracks = [track for track in imported["tracks"] if track["kind"] == "voice"]
    assert len(voice_tracks) == 1
    assert voice_tracks[0]["id"] == "D1"
    assert voice_tracks[0]["clips"][0]["timeline_range"]["end_us"] == 5_000_000


def test_voice_generation_and_import_join_reordered_scene_dialogue_by_identity(tmp_path):
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts",
                                media_probe=FixtureProbe())
    project_id = _project_with_translation(service, tmp_path)
    original = service.projects.load(project_id)
    scene = replace(original.source_cues[1], range=TimeRange(0, 4_000_000),
                    role="scene_text", subtitle_track_id="S2")
    translation = replace(original.translated_cues[1], range=scene.range,
                          role=scene.role, subtitle_track_id=scene.subtitle_track_id)
    edited = replace(original, revision=original.revision + 1,
                     source_cues=(original.source_cues[0], scene),
                     translated_cues=(translation, original.translated_cues[0]))
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=edited)
    job = service.create_tts_job(project_id=project_id, provider_id="piper",
        voice_id="nghitts-chieu-thanh", provider_options={"data_root": str(_data_root(tmp_path))})
    assert job["spec"]["input_revision"] == edited.revision
    assert job["spec"]["provider_config"]["cues"] == [{
        "id": original.source_cues[0].id, "index": 1,
        "start_us": 0, "end_us": 1_000_000, "text": "Xin chào.",
    }]
    assert not service.tts_job_needs_rebase(job["id"])
    assert service.rebase_tts_retry(job["id"]) is None
    output = Path(job["spec"]["provider_config"]["output_path"])
    write_pcm16_mono(output, array("f", [0.0]) * (48_000 * 5), 48_000)
    imported = service.import_final_voice_wav(job["id"], str(output))
    assert imported["translated_cues"][0]["id"] == translation.id
    assert imported["translated_cues"][1]["text"] == "Xin chào."
    assert any(track["id"] == "D1" for track in imported["tracks"])


def test_voice_retry_ignores_translation_order_but_uses_latest_text(tmp_path):
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts",
                                media_probe=FixtureProbe())
    project_id = _project_with_translation(service, tmp_path)
    original = service.create_tts_job(project_id=project_id, provider_id="piper",
        voice_id="nghitts-chieu-thanh", provider_options={"data_root": str(_data_root(tmp_path))})
    snapshot = service.projects.load(project_id)
    changed = replace(snapshot, revision=snapshot.revision + 1,
                      translated_cues=tuple(reversed(snapshot.translated_cues)))
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=changed)
    assert not service.tts_job_needs_rebase(original["id"])
    assert service.rebase_tts_retry(original["id"]) is None
    cue_id = snapshot.translated_cues[0].id
    service.subtitle_command(project_id=project_id, command="update_text",
        cue_id=cue_id, language="translated", params={"text": "Câu vừa chỉnh sửa."},
        deferred=True)
    latest = service.projects.load(project_id)
    replacement = service.rebase_tts_retry(original["id"])
    assert replacement["spec"]["input_revision"] == latest.revision
    assert replacement["spec"]["provider_config"]["cues"][0]["text"] == "Câu vừa chỉnh sửa."
    assert service.projects.persisted_head_revision(project_id) == latest.revision


@pytest.mark.parametrize("apply_cut", [False, True])
def test_voice_job_uses_unsaved_text_timing_add_delete_and_cut_edits(tmp_path, apply_cut):
    from ducai_studio.domain.timeline_edit import project_for_timeline, range_on_timeline

    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts",
                                media_probe=FixtureProbe())
    project_id = _project_with_translation(service, tmp_path)
    original = service.projects.load(project_id)
    first, second = original.source_cues
    service.subtitle_command(project_id=project_id, command="update_text",
        cue_id=first.id, language="translated", params={"text": "Thoại đã sửa."}, deferred=True)
    service.subtitle_command(project_id=project_id, command="move_scene_texts",
        cue_id=first.id, params={"cue_ids": [first.id], "delta_us": 100_000}, deferred=True)
    service.subtitle_command(project_id=project_id, command="add_subtitle_cue",
        params={"text": "Thoại thêm tay.", "start_us": 2_500_000, "end_us": 3_500_000,
                "cue_role": "dialogue", "visual_box": [.1, .8, .8, .1]}, deferred=True)
    service.subtitle_command(project_id=project_id, command="delete", cue_id=second.id,
                             deferred=True)
    if apply_cut:
        service.timeline_command(project_id=project_id, command="cut", operation="keep_from",
                                 range_start_us=1_150_000, range_end_us=5_000_000, deferred=True)
    working = service.projects.load(project_id)
    assert service.projects.has_pending_edits(project_id)
    job = service.create_tts_job(project_id=project_id, provider_id="piper",
        voice_id="nghitts-chieu-thanh", provider_options={"data_root": str(_data_root(tmp_path))})
    assert job["spec"]["input_revision"] == working.revision
    cues = job["spec"]["provider_config"]["cues"]
    expected_text = ["Thoại thêm tay."] if apply_cut else ["Thoại đã sửa.", "Thoại thêm tay."]
    assert [cue["text"] for cue in cues] == expected_text
    if not apply_cut:
        assert cues[0]["id"] == first.id
        assert (cues[0]["start_us"], cues[0]["end_us"]) == (100_000, 1_100_000)
    visible = {cue.id: cue for cue in project_for_timeline(working).source_cues}
    for cue in cues:
        slot = TimeRange(cue["start_us"], cue["end_us"])
        assert range_on_timeline(working, slot) == visible[cue["id"]].range
    assert service.projects.persisted_head_revision(project_id) == working.revision
    assert not service.projects.has_pending_edits(project_id)
    assert service.projects.load(project_id, original.revision) == original


@pytest.mark.parametrize("invalid", ["missing", "duplicate_source", "duplicate_translation",
                                     "identity", "index", "range", "empty"])
def test_voice_inputs_reject_invalid_pairs_without_mutating_project(tmp_path, invalid):
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts",
                                media_probe=FixtureProbe())
    project_id = _project_with_translation(service, tmp_path)
    snapshot = service.projects.load(project_id)
    source, translated = snapshot.source_cues, snapshot.translated_cues
    with pytest.raises(ValueError):
        if invalid == "missing":
            translated = translated[:-1]
        elif invalid == "duplicate_source":
            source = (source[0], source[0])
        elif invalid == "duplicate_translation":
            translated = (translated[0], translated[0])
        else:
            values = {"identity": {"id": "unlinked"}, "index": {"index": 3},
                      "range": {"range": TimeRange(50_000, 1_000_000)}, "empty": {"text": ""}}
            translated = (replace(translated[0], **values[invalid]), translated[1])
        malformed = replace(snapshot, source_cues=source, translated_cues=translated)
        service._tts_cues_for_snapshot(malformed)
    assert service.projects.load(project_id) == snapshot


def test_create_tts_job_excludes_scene_text_from_voice_requests(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    snapshot = service.projects.load(project_id)
    snapshot = replace(
        snapshot,
        revision=snapshot.revision + 1,
        source_cues=(
            replace(snapshot.source_cues[0], subtitle_track_id="S2"),
            replace(
                snapshot.source_cues[1],
                role="scene_text",
                orientation="vertical",
                style_hint="calligraphy",
                visual_box=(0.72, 0.22, 0.08, 0.31),
                visual_box_source="ocr",
                panel_box=(0.68, 0.18, 0.16, 0.39),
            ),
        ),
        translated_cues=(
            replace(snapshot.translated_cues[0], subtitle_track_id="S2"),
            replace(
                snapshot.translated_cues[1],
                role="scene_text",
                orientation="vertical",
                style_hint="calligraphy",
                visual_box=(0.72, 0.22, 0.08, 0.31),
                visual_box_source="ocr",
                panel_box=(0.68, 0.18, 0.16, 0.39),
            ),
        ),
    )
    summary = service.projects.get_summary(project_id)
    service.projects.save(project_path=summary["path"], snapshot=snapshot)

    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(_data_root(tmp_path))},
    )

    assert [cue["id"] for cue in job["spec"]["provider_config"]["cues"]] == [
        snapshot.source_cues[0].id
    ]
    assert [cue["text"] for cue in job["spec"]["provider_config"]["cues"]] == [
        "Xin chào."
    ]


def test_create_tts_job_accepts_vieneu_preset_without_clone_profile(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _vieneu_data_root(tmp_path)

    job = service.create_tts_job(
        project_id=project_id,
        provider_id="vieneu-3.8",
        voice_id="preset:Mai Anh",
        provider_options={"data_root": str(data_root)},
    )
    config = job["spec"]["provider_config"]

    assert config["voice_id"] == "preset:Mai Anh"
    assert config["profile_path"] == ""
    assert config["reference_audio_path"] == ""
    assert config["voice_identity"].startswith("preset:Mai Anh:vieneu-3.8.1-preset-")


def test_production_tts_rerun_promotes_candidate_and_replaces_active_d1(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    first = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root)},
    )
    first_config = first["spec"]["provider_config"]
    active = Path(first_config["output_path"])
    write_pcm16_mono(active, array("f", [0.0]) * (48_000 * 5), 48_000)
    service.import_final_voice_wav(first["id"], str(active))
    service.transition_job(first["id"], "start_requested")
    service.transition_job(first["id"], "running")
    service.transition_job(first["id"], "succeeded")
    old_bytes = active.read_bytes()

    second = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root)},
    )
    second_config = second["spec"]["provider_config"]
    candidate = Path(second_config["staging_output_path"])
    candidate.parent.mkdir(parents=True, exist_ok=True)
    write_pcm16_mono(candidate, array("f", [0.25]) * (48_000 * 5), 48_000)
    candidate_qa = Path(second_config["staging_qa_path"])
    candidate_qa.write_text(
        json.dumps(
            {
                "status": "PASS",
                "job_id": second["id"],
                "input_fingerprint": second["spec"]["input_fingerprint"],
                "settings_fingerprint": second["spec"]["settings_fingerprint"],
                "output_path": str(candidate),
                "output_sha256": file_sha256(candidate),
            }
        ),
        encoding="utf-8",
    )

    imported = service.import_final_voice_wav(second["id"], str(candidate))

    assert active.read_bytes() != old_bytes
    assert not candidate.exists()
    voice_tracks = [track for track in imported["tracks"] if track["id"] == "D1"]
    assert len(voice_tracks) == 1
    assert len(service.jobs.list_project_jobs(project_id=project_id)) == 2


def test_prepare_tts_rerun_removes_only_d1_before_creating_replacement(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    first = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root)},
    )
    config = first["spec"]["provider_config"]
    active = Path(config["output_path"])
    write_pcm16_mono(active, array("f", [0.0]) * (48_000 * 5), 48_000)
    service.import_final_voice_wav(first["id"], str(active))
    service.transition_job(first["id"], "start_requested")
    service.transition_job(first["id"], "running")
    service.transition_job(first["id"], "succeeded")
    before = service.projects.load(project_id)

    prepared = service.prepare_tts_rerun(project_id=project_id)

    assert prepared["revision"] == before.revision + 1
    assert all(track["kind"] != "voice" for track in prepared["tracks"])
    assert all(item["path"] != str(active) for item in prepared["media"])
    assert prepared["voice_conform"] is None
    assert active.is_file()
    assert len(service.jobs.list_project_jobs(project_id=project_id)) == 1

    replacement = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root)},
    )
    assert replacement["spec"]["input_revision"] == prepared["revision"]
    assert replacement["spec"]["provider_config"]["cache_root"] == config["cache_root"]


def test_rejected_tts_candidate_does_not_touch_active_voice(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    created = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(_data_root(tmp_path))},
    )
    config = created["spec"]["provider_config"]
    active = Path(config["output_path"])
    active.write_bytes(b"previous-approved-voice")
    candidate = Path(config["staging_output_path"])
    candidate.parent.mkdir(parents=True, exist_ok=True)
    write_pcm16_mono(candidate, array("f", [0.5]) * (48_000 * 5), 48_000)
    Path(config["staging_qa_path"]).write_text(
        json.dumps(
            {
                "status": "PASS",
                "job_id": created["id"],
                "input_fingerprint": created["spec"]["input_fingerprint"],
                "settings_fingerprint": created["spec"]["settings_fingerprint"],
                "output_path": str(candidate),
                "output_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="QA report"):
        service.import_final_voice_wav(created["id"], str(candidate))

    assert active.read_bytes() == b"previous-approved-voice"
    assert candidate.is_file()


def test_final_voice_import_uses_cue_timing_rows_to_split_d1(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root)},
    )
    config = job["spec"]["provider_config"]
    output = Path(config["output_path"])
    write_pcm16_mono(output, array("f", [0.0]) * (48_000 * 5), 48_000)
    Path(config["qa_path"]).write_text(
        json.dumps({"status": "PASS"}),
        encoding="utf-8",
    )
    rows = [
        {
            "cue_id": config["cues"][0]["id"],
            "index": 1,
            "voice_start_us": 0,
            "voice_end_us": 900_000,
        },
        {
            "cue_id": config["cues"][1]["id"],
            "index": 2,
            "voice_start_us": 1_200_000,
            "voice_end_us": 2_350_000,
        },
    ]
    service._voice_timing_timeline_rows = lambda *_args, **_kwargs: rows  # type: ignore[method-assign]

    imported = service.import_final_voice_wav(job["id"], str(output))

    voice_track = next(track for track in imported["tracks"] if track["id"] == "D1")
    assert len(voice_track["clips"]) == 2
    assert [clip["timeline_range"] for clip in voice_track["clips"]] == [
        {"start_us": 0, "end_us": 900_000},
        {"start_us": 1_200_000, "end_us": 2_350_000},
    ]
    assert len({clip["media_id"] for clip in voice_track["clips"]}) == 1


def test_cache_only_tts_checkpoint_keeps_split_d1_import(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(_data_root(tmp_path))},
    )
    job_id = str(job["id"])
    monkeypatch.setenv(
        "DUCAI_ARTIFACT_STAGING_ROOT",
        str(service.checkpoints.staging_root),
    )

    class CacheOnlyProvider:
        calls = 0

        def synthesize_batch(self, **_kwargs):
            self.calls += 1
            raise AssertionError("cache-only rerun must not call the TTS provider")

        def close(self) -> None:
            return None

    provider = CacheOnlyProvider()
    worker = TtsWorker(
        job_id,
        provider_factory=lambda _config: provider,
        audio_processor_factory=CacheOnlyAudioProcessor,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure({"job": job["spec"]})

    cache_root = Path(job["spec"]["provider_config"]["cache_root"])
    cache_root.mkdir(parents=True, exist_ok=True)
    samples = array(
        "f",
        (0.16 * ((index % 80) / 80 - 0.5) for index in range(12_000)),
    )
    for cue in worker.cues:
        cache_path = cache_root / f"{cue.request_fingerprint}.wav"
        write_pcm16_mono(cache_path, samples, 48_000)
        qa = analyze_audio(samples, 48_000, VoicePipelineSettings())
        assert qa.ok
        metadata = {
            "schema_version": 3,
            "cache_tier": "base",
            "cue_id": cue.cue_id,
            "request_fingerprint": cue.request_fingerprint,
            "cache_path": str(cache_path),
            "cache_sha256": file_sha256(cache_path),
            "base_cache_path": str(cache_path),
            "base_cache_sha256": file_sha256(cache_path),
            "read_speed": 1.0,
            "total_speed": 1.0,
            "qa": asdict(qa),
            "base_qa": asdict(qa),
        }
        cache_path.with_suffix(".json").write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )

    worker._execute()

    assert provider.calls == 0
    checkpoint_index = next(
        index
        for index, (kind, payload) in enumerate(events)
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )
    success_index = next(
        index for index, (kind, _payload) in enumerate(events) if kind == "succeeded"
    )
    assert checkpoint_index < success_index
    checkpoint = events[checkpoint_index][1]
    service.commit_worker_checkpoint(
        job_id,
        checkpoint,
        event_id="cache-only-final-ledger",
    )
    output = Path(events[success_index][1]["output_path"])
    imported = service.import_final_voice_wav(job_id, str(output))

    voice_track = next(track for track in imported["tracks"] if track["id"] == "D1")
    assert len(voice_track["clips"]) == len(worker.cues) == 2
    assert [clip["cue_id"] for clip in voice_track["clips"]] == [
        cue.cue_id for cue in worker.cues
    ]


def test_running_tts_publish_serializes_measured_cues_before_job_success(
    tmp_path: Path,
) -> None:
    """Worker success is imported before the durable job state turns succeeded."""

    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(_data_root(tmp_path))},
    )
    job_id = str(job["id"])
    config = job["spec"]["provider_config"]
    first, second = config["cues"]
    ledger_path = (
        service.checkpoints.staging_root / job_id / "voice-ledger-working.json"
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(
            {
                "entries": {
                    first["id"]: {
                        "qa": {"duration_us": 1_229_333},
                        "base_qa": {"duration_us": 1_229_333},
                    },
                    second["id"]: {
                        "qa": {"duration_us": 900_000},
                        "base_qa": {"duration_us": 900_000},
                    },
                },
                "blockers": {},
            }
        ),
        encoding="utf-8",
    )
    coverage = {
        "status": "PASS",
        "timing_issues": [],
        "timing": {"mode": "base_no_loss", "shifted_cues": 1},
    }
    Path(config["qa_path"]).write_text(json.dumps(coverage), encoding="utf-8")
    output = Path(config["output_path"])
    write_pcm16_mono(output, array("f", [0.0]) * (48_000 * 5), 48_000)
    service.jobs.transition(job_id, JobState.START_REQUESTED)
    service.jobs.transition(job_id, JobState.RUNNING)

    running = service.jobs.get(job_id)
    rows = service._voice_timing_timeline_rows(running, coverage)
    imported = service.import_final_voice_wav(job_id, str(output))

    assert running.state is JobState.RUNNING
    assert rows[0]["voice_end_us"] == 1_229_333
    assert rows[1]["voice_start_us"] == 1_229_333
    voice_track = next(track for track in imported["tracks"] if track["id"] == "D1")
    assert len(voice_track["clips"]) == 2
    assert voice_track["clips"][0]["timeline_range"]["end_us"] == 1_229_333
    assert voice_track["clips"][1]["timeline_range"]["start_us"] == 1_229_333


def test_final_voice_import_rejects_stale_subtitle_content(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(_data_root(tmp_path))},
    )
    output = Path(job["spec"]["provider_config"]["output_path"])
    write_pcm16_mono(output, array("f", [0.0]) * (48_000 * 5), 48_000)
    snapshot = service.projects.load(project_id)
    summary = service.projects.get_summary(project_id)
    changed = replace(
        snapshot,
        revision=snapshot.revision + 1,
        translated_cues=(
            replace(snapshot.translated_cues[0], text="Nội dung đã sửa."),
            *snapshot.translated_cues[1:],
        ),
    )
    service.projects.save(
        project_path=summary["path"],
        snapshot=changed,
    )

    with pytest.raises(ValueError, match="Phụ đề hoặc nội dung dịch đã thay đổi"):
        service.import_final_voice_wav(str(job["id"]), str(output))

    reloaded = service.projects.load(project_id)
    assert reloaded == changed
    assert all(track.id != "D1" for track in reloaded.tracks)


def test_final_voice_import_rejects_stale_cue_timestamps_without_mutation(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(_data_root(tmp_path))},
    )
    output = Path(job["spec"]["provider_config"]["output_path"])
    write_pcm16_mono(output, array("f", [0.0]) * (48_000 * 5), 48_000)
    snapshot = service.projects.load(project_id)
    summary = service.projects.get_summary(project_id)
    shifted_range = TimeRange(100_000, 1_100_000)
    changed = replace(
        snapshot,
        revision=snapshot.revision + 1,
        source_cues=(
            replace(snapshot.source_cues[0], range=shifted_range),
            *snapshot.source_cues[1:],
        ),
        translated_cues=(
            replace(snapshot.translated_cues[0], range=shifted_range),
            *snapshot.translated_cues[1:],
        ),
    )
    service.projects.save(project_path=summary["path"], snapshot=changed)

    with pytest.raises(ValueError, match="project revision đã thay đổi"):
        service.import_final_voice_wav(str(job["id"]), str(output))

    reloaded = service.projects.load(project_id)
    assert reloaded == changed
    assert all(track.id != "D1" for track in reloaded.tracks)


def test_final_voice_import_accepts_visual_only_revision(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(_data_root(tmp_path))},
    )
    output = Path(job["spec"]["provider_config"]["output_path"])
    write_pcm16_mono(output, array("f", [0.0]) * (48_000 * 5), 48_000)
    snapshot = service.projects.load(project_id)
    summary = service.projects.get_summary(project_id)
    visual_only = replace(
        snapshot,
        revision=snapshot.revision + 1,
        translated_cues=(
            replace(snapshot.translated_cues[0], text_font_size=48, text_color="#00FF00"),
            *snapshot.translated_cues[1:],
        ),
    )
    service.projects.save(project_path=summary["path"], snapshot=visual_only)

    imported = service.import_final_voice_wav(str(job["id"]), str(output))

    assert imported["revision"] == visual_only.revision + 1
    assert any(track["id"] == "D1" for track in imported["tracks"])
    assert imported["translated_cues"][0]["text_font_size"] == 48


def test_tts_retry_ignores_visual_revision_but_rebases_changed_dialogue(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    original = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={
            "data_root": str(_data_root(tmp_path)),
            "voice_read_speed": 1.3,
            "batch_size": 7,
        },
    )
    snapshot = service.projects.load(project_id)
    summary = service.projects.get_summary(project_id)
    visual_only = replace(
        snapshot,
        revision=snapshot.revision + 1,
        translated_cues=(
            replace(snapshot.translated_cues[0], text_font_size=48, text_color="#00FF00"),
            *snapshot.translated_cues[1:],
        ),
    )
    service.projects.save(project_path=summary["path"], snapshot=visual_only)

    assert service.tts_job_needs_rebase(str(original["id"])) is False
    assert service.rebase_tts_retry(str(original["id"])) is None

    dialogue_changed = replace(
        visual_only,
        revision=visual_only.revision + 1,
        translated_cues=(
            replace(visual_only.translated_cues[0], text="Nội dung thoại mới."),
            *visual_only.translated_cues[1:],
        ),
    )
    service.projects.save(project_path=summary["path"], snapshot=dialogue_changed)

    replacement = service.rebase_tts_retry(str(original["id"]))

    assert replacement is not None
    assert replacement["replacement"] is True
    assert replacement["replaced_job_id"] == original["id"]
    assert replacement["spec"]["input_revision"] == dialogue_changed.revision
    config = replacement["spec"]["provider_config"]
    assert config["replaces_job_id"] == original["id"]
    assert config["voice_id"] == "nghitts-chieu-thanh"
    assert config["voice_read_speed"] == 1.3
    assert config["batch_size"] == 7
    assert config["cues"][0]["text"] == "Nội dung thoại mới."


def test_tts_input_fingerprint_includes_voice_settings(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    first = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root), "voice_read_speed": 1.0},
    )
    service.transition_job(str(first["id"]), JobState.CANCEL_REQUESTED.value)
    service.transition_job(str(first["id"]), JobState.CANCELLED.value)

    second = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root), "voice_read_speed": 1.15},
    )

    first_config = first["spec"]["provider_config"]
    second_config = second["spec"]["provider_config"]
    assert first_config["tts_input_fingerprint_schema"] == "ducai-tts-input-v2"
    assert second_config["tts_input_fingerprint_schema"] == "ducai-tts-input-v2"
    assert first_config["cues"] == second_config["cues"]
    assert first["spec"]["input_fingerprint"] != second["spec"]["input_fingerprint"]


def test_create_tts_job_accepts_only_existing_force_cue_ids(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    selected_cue_id = service.projects.load(project_id).translated_cues[1].id

    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={
            "data_root": str(data_root),
            "force_cue_ids": [selected_cue_id],
        },
    )

    assert job["spec"]["provider_config"]["force_cue_ids"] == [selected_cue_id]


def test_create_vieneu_job_can_enroll_a_missing_profile_from_reference_audio(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = tmp_path / "data"
    runtime = data_root / "vieneu_3_8_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"fixture-vieneu-python")
    reference = tmp_path / "clone-reference.wav"
    write_pcm16_mono(reference, array("f", [0.01]) * 48_000, 48_000)

    job = service.create_tts_job(
        project_id=project_id,
        provider_id="vieneu-3.8",
        voice_id="clone",
        provider_options={
            "data_root": str(data_root),
            "reference_audio_path": str(reference),
            "reference_denoise": True,
            "adaptive_timing": True,
            "voice_read_speed": 1.3,
            "max_absolute_speed": 2.25,
            "defer_timing_conform": False,
            "batch_size": 32,
        },
    )
    config = job["spec"]["provider_config"]

    assert config["reference_audio_path"] == str(reference.resolve())
    assert config["reference_denoise"] is True
    assert config["batch_size"] == 32
    assert job["spec"]["provider_version"] == "3.8.1"
    assert config["pipeline_settings"]["adaptive_timing"] is True
    assert config["pipeline_settings"]["voice_read_speed"] == 1.3
    assert config["pipeline_settings"]["max_absolute_speed"] == 2.25
    assert config["defer_timing_conform"] is False
    assert config["profile_path"].endswith(".npz")
    assert not Path(config["profile_path"]).exists()


def test_create_timing_override_job_reuses_parent_voice_cache_contract(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    parent = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(data_root), "adaptive_timing": True},
    )
    parent_id = str(parent["id"])
    service.commit_worker_checkpoint(
        parent_id,
        {
            "stage_key": "tts",
            "checkpoint_key": "review",
            "schema_version": 1,
            "payload": {
                "coverage": {
                    "timing_issues": [
                        {
                            "id": 2,
                            "cue_id": parent["spec"]["provider_config"]["cues"][1]["id"],
                            "code": "CUE_TOO_LONG",
                        }
                    ],
                    "timing": {
                        "final_voice_end_us": 5_500_000,
                        "media_duration_us": 5_000_000,
                    },
                }
            },
        },
        event_id="tts-review-checkpoint",
    )
    service.jobs.transition(parent_id, JobState.START_REQUESTED)
    service.jobs.transition(parent_id, JobState.RUNNING)
    service.jobs.transition(
        parent_id,
        JobState.FAILED,
        error_code="tts_review_required",
        error_payload={"status": "REVIEW"},
    )

    override = service.create_tts_timing_override_job(
        project_id=project_id,
        parent_tts_job_id=parent_id,
    )

    parent_config = parent["spec"]["provider_config"]
    override_config = override["spec"]["provider_config"]
    assert override["state"] == "queued"
    assert override["spec"]["provider_id"] == parent["spec"]["provider_id"]
    assert override["spec"]["model_id"] == parent["spec"]["model_id"]
    assert override_config["allow_timeline_overflow"] is True
    assert override_config["defer_timing_conform"] is True
    assert override_config["timing_override_parent_job_id"] == parent_id
    assert override_config["voice_identity"] == parent_config["voice_identity"]
    assert override_config["cache_root"] == parent_config["cache_root"]
    assert override_config["force_cue_ids"] == []
    assert override_config["cues"] == parent_config["cues"]

    override_id = str(override["id"])
    service.jobs.transition(override_id, JobState.START_REQUESTED)
    service.jobs.transition(override_id, JobState.RUNNING)
    service.jobs.transition(
        override_id,
        JobState.FAILED,
        error_code="worker_failed",
    )

    retried = service.create_tts_timing_override_job(
        project_id=project_id,
        parent_tts_job_id=parent_id,
    )

    assert retried["id"] != override_id
    assert retried["spec"]["provider_config"]["allow_timeline_overflow"] is True


@pytest.mark.parametrize("reorder_translations", [False, True])
def test_reset_then_reapply_creates_new_conform_job_and_reuses_variant_cache(
    tmp_path: Path,
    monkeypatch,
    reorder_translations,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=RealWavFixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    parent = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={"data_root": str(_data_root(tmp_path))},
    )
    parent_id = str(parent["id"])
    settings = VoicePipelineSettings()
    base_entries: dict[str, dict] = {}
    cue_samples = array(
        "f",
        (0.08 if index % 100 < 50 else -0.08 for index in range(96_000)),
    )
    for cue in service.projects.load(project_id).translated_cues:
        cue_wav = tmp_path / f"{cue.id}.wav"
        write_pcm16_mono(cue_wav, cue_samples, 48_000)
        qa = analyze_audio(cue_samples, 48_000, settings)
        assert qa.ok is True
        base_entries[cue.id] = {
            "cue_id": cue.id,
            "cache_path": str(cue_wav),
            "cache_sha256": file_sha256(cue_wav),
            "base_qa": asdict(qa),
        }
    ledger_path = (
        service.checkpoints.staging_root / parent_id / "parent-voice-ledger.json"
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({"schema_version": 1, "entries": base_entries}),
        encoding="utf-8",
    )
    service.commit_worker_checkpoint(
        parent_id,
        {
            "stage_key": "tts",
            "checkpoint_key": "latest",
            "schema_version": 1,
            "payload": {"approved_cues": 2},
            "artifact_path": str(ledger_path),
            "artifact_kind": "voice_ledger",
            "artifact_state": "technical",
        },
        event_id="parent-voice-ledger",
    )
    parent_output = Path(parent["spec"]["provider_config"]["output_path"])
    write_pcm16_mono(
        parent_output,
        array(
            "f",
            (0.04 if index % 100 < 50 else -0.04 for index in range(240_000)),
        ),
        48_000,
    )
    service.jobs.transition(parent_id, JobState.START_REQUESTED)
    service.jobs.transition(parent_id, JobState.RUNNING)
    service.jobs.transition(parent_id, JobState.SUCCEEDED)
    Path(parent["spec"]["provider_config"]["qa_path"]).write_text(
        json.dumps({"status": "PASS"}),
        encoding="utf-8",
    )
    service.import_final_voice_wav(parent_id, str(parent_output))

    # The untranslated SRT remains the timing authority even if somebody later
    # edits the Vietnamese cue ranges. Conform may reuse the translated text and
    # WAV identity, but it must never inherit those drifted timestamps.
    snapshot = service.projects.load(project_id)
    drifted = replace(
        snapshot,
        revision=snapshot.revision + 1,
        translated_cues=(
            replace(snapshot.translated_cues[0], range=TimeRange(111_111, 888_888)),
            replace(
                snapshot.translated_cues[1],
                range=TimeRange(1_333_333, 2_111_111),
            ),
        ),
    )
    summary = service.projects.get_summary(project_id)
    if reorder_translations:
        drifted = replace(drifted, translated_cues=tuple(reversed(drifted.translated_cues)))
    service.projects.save(project_path=summary["path"], snapshot=drifted)

    full_estimate = service.estimate_voice_conform(
        project_id=project_id,
        parent_tts_job_id=parent_id,
        mode="stretch_video",
    )
    compact_estimate = service.estimate_voice_conform(
        project_id=project_id,
        parent_tts_job_id=parent_id,
        mode="stretch_video",
        include_plan=False,
        verify_cache=False,
    )
    assert "plan" in full_estimate
    assert "plan" not in compact_estimate
    assert compact_estimate["plan_fingerprint"] == full_estimate["plan_fingerprint"]
    assert [item["source_start_us"] for item in full_estimate["plan"]["cues"]] == [
        0,
        1_200_000,
    ]
    assert [item["source_end_us"] for item in full_estimate["plan"]["cues"]] == [
        1_000_000,
        2_200_000,
    ]

    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))

    def execute_conform(job: dict) -> dict:
        worker = TtsWorker(
            str(job["id"]),
            provider_factory=lambda _config: (_ for _ in ()).throw(
                AssertionError("voice conform must never initialize VieNeu/Piper")
            ),
            audio_processor_factory=CacheOnlyAudioProcessor,
        )
        events: list[tuple[str, dict]] = []
        worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
        worker._configure({"job": job["spec"]})
        assert worker.provider is None
        worker._execute()
        assert events[-1][0] == "succeeded"
        return json.loads(
            Path(job["spec"]["provider_config"]["qa_path"]).read_text(
                encoding="utf-8"
            )
        )

    first = service.create_voice_conform_job(
        project_id=project_id,
        parent_tts_job_id=parent_id,
        mode="stretch_video",
    )
    first_qa = execute_conform(first)
    first_id = str(first["id"])
    service.jobs.transition(first_id, JobState.START_REQUESTED)
    service.jobs.transition(first_id, JobState.RUNNING)
    service.jobs.transition(first_id, JobState.SUCCEEDED)
    service.import_final_voice_conform(
        first_id,
        first["spec"]["provider_config"]["output_path"],
    )
    # The two fixture cues deliberately share one synthesis-quality WAV, so
    # the second cue can already reuse the first cue's timing variant.
    assert first_qa["cache_hits"] == 1
    assert first_qa["rendered_variants"] == 1

    reset = service.reset_voice_conform(project_id)
    reset_voice_track = next(track for track in reset["tracks"] if track["id"] == "D1")
    assert len(reset_voice_track["clips"]) == 2
    second = service.create_voice_conform_job(
        project_id=project_id,
        parent_tts_job_id=parent_id,
        mode="stretch_video",
    )
    second_qa = execute_conform(second)

    assert second["id"] != first_id
    assert second["spec"]["provider_id"] == "voice-conform"
    assert second_qa["cache_hits"] == 2
    assert second_qa["rendered_variants"] == 0
