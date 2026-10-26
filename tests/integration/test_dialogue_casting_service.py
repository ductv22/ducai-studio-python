from __future__ import annotations

import json
import shutil
import wave
from array import array
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication

import ducai_studio.job_manager.service as service_module
from ducai_studio.application.media_commands import MediaImportService
from ducai_studio.application.voice_pipeline import file_sha256, write_pcm16_mono
from ducai_studio.domain.dialogue_casting import (
    CastingOverride,
    CastingResult,
    CastingVoice,
    DialogueCastingState,
)
from ducai_studio.domain.jobs import JobState
from ducai_studio.infrastructure.dialogue_casting_runtime import CASTING_ANALYZER_ID
from ducai_studio.job_manager.server import JobManagerServer
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.media_probe import MediaProbeResult
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage
from ducai_studio.workers.tts_worker import TtsWorker


class FixtureProbe:
    def probe(self, _media_path: str | Path) -> MediaProbeResult:
        return MediaProbeResult(5_000_000, True, True, 1280, 720, "mp4")


class VoiceFixtureProbe:
    def probe(self, _media_path: str | Path) -> MediaProbeResult:
        return MediaProbeResult(5_000_000, False, True, None, None, "wav")


class VoiceConformFixtureProbe:
    def probe(self, media_path: str | Path) -> MediaProbeResult:
        path = Path(media_path)
        if path.suffix.casefold() != ".wav":
            return FixtureProbe().probe(path)
        with wave.open(str(path), "rb") as stream:
            duration_us = round(stream.getnframes() * 1_000_000 / stream.getframerate())
        return MediaProbeResult(duration_us, False, True, None, None, "wav")


class CacheOnlyConformProcessor:
    def time_stretch(self, *_args, **_kwargs) -> None:
        raise AssertionError("Fixture conform should reuse the 1x cue WAV")

    def master_ebu_r128(self, source: Path, target: Path, **_kwargs) -> dict[str, float]:
        shutil.copyfile(source, target)
        return {"input_i": -18.0, "target_offset": 0.0}


def _data_root(tmp_path: Path) -> Path:
    data_root = tmp_path / "data"
    voice_root = data_root / "piper_voices"
    voice_root.mkdir(parents=True)
    for voice_id, payload in (
        ("vi_VN-vais1000-medium", b"male-model"),
        ("vi_VN-vivos-x_low", b"female-model-a"),
        ("vi_VN-25hours_single-low", b"female-model-b"),
    ):
        model = voice_root / f"{voice_id}.onnx"
        model.write_bytes(payload)
        model.with_suffix(".onnx.json").write_text(
            json.dumps({"audio": {"sample_rate": 22_050}, "phoneme_id_map": {"_": [0]}}),
            encoding="utf-8",
        )
    runtime = data_root / "piper_runtime" / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"fixture-python")
    return data_root


def _project_with_translation(service: JobManagerService, tmp_path: Path) -> str:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"fixture-video")
    source = tmp_path / "film_ZH_FINAL.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n你好。\n\n"
        "2\n00:00:01,200 --> 00:00:02,200\n出发吧。\n",
        encoding="utf-8",
    )
    translated = tmp_path / "film_ZH_FINAL_codex_vi.srt"
    translated.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nXin chào.\n\n"
        "2\n00:00:01,200 --> 00:00:02,200\nLên đường thôi.\n",
        encoding="utf-8",
    )
    project = service.create_project(project_path=str(tmp_path / "project.ducai"), name="Film")
    project_id = project["id"]
    service.import_media(project_id=project_id, media_path=str(video))
    service.import_media(project_id=project_id, media_path=str(source))
    snapshot = service.projects.load(project_id)
    snapshot = service.media_imports.import_srt(snapshot, translated, language="vi")
    summary = service.projects.get_summary(project_id)
    service.projects.save(project_path=summary["path"], snapshot=snapshot)
    return project_id


def _voice(voice_id: str) -> dict[str, object]:
    return {
        "provider_id": "piper",
        "voice_id": voice_id,
        "profile_path": "",
        "reference_audio_path": "",
        "reference_text": "",
        "reference_denoise": False,
    }


def _write_conform_wav(path: Path, duration_us: int) -> None:
    frames = round(duration_us * 48_000 / 1_000_000)
    samples = array(
        "f",
        (0.08 if index % 100 < 50 else -0.08 for index in range(frames)),
    )
    write_pcm16_mono(path, samples, 48_000)


def _finish_as_failed(service: JobManagerService, job_id: str) -> None:
    service.jobs.transition(job_id, JobState.START_REQUESTED)
    service.jobs.transition(job_id, JobState.RUNNING)
    service.jobs.transition(job_id, JobState.FAILED, error_code="test_complete")


def test_imported_role_sample_survives_source_removal_and_reopening(tmp_path, monkeypatch):
    data_root = _data_root(tmp_path)
    monkeypatch.setattr(service_module, "discover_data_root", lambda: data_root)
    runtime = data_root / "vieneu_3_8_runtime/.venv/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"fixture-runtime")
    database = tmp_path / "state.db"
    service = JobManagerService(database_path=database, artifact_root=tmp_path / "artifacts",
                                media_probe=FixtureProbe())
    project_id = _project_with_translation(service, tmp_path)
    original = tmp_path / "giọng nữ.wav"
    write_pcm16_mono(original, array("f", [0.02]) * 48_000, 48_000)
    content = original.read_bytes()
    result = service.configure_dialogue_casting(project_id, {
        "female_voice": {"provider_id": "vieneu-3.8", "voice_id": "clone",
                         "reference_audio_path": str(original)},
    })
    before = JobManagerService._resolve_tts_voice(
        "vieneu-3.8", "clone",
        {"reference_audio_path": result["config"]["female_voice"]["reference_audio_path"]},
        data_root,
    )
    original.unlink()
    reopened = JobManagerService(database_path=database, artifact_root=tmp_path / "artifacts",
                                 media_probe=FixtureProbe())
    voice = reopened.get_dialogue_casting(project_id)["config"]["female_voice"]
    after = JobManagerService._resolve_tts_voice(
        "vieneu-3.8", "clone", {"reference_audio_path": voice["reference_audio_path"]},
        data_root,
    )
    assert Path(after["reference_audio_path"]).read_bytes() == content
    assert Path(after["reference_audio_path"]).is_relative_to(data_root)
    assert after["voice_identity"] == before["voice_identity"]
    assert after["profile_path"] == before["profile_path"]
    assert Path(after["reference_audio_path"]).name == "giọng nữ.wav"


def test_imported_role_samples_with_same_name_remain_distinct(tmp_path, monkeypatch):
    data_root = _data_root(tmp_path)
    monkeypatch.setattr(service_module, "discover_data_root", lambda: data_root)
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts", media_probe=FixtureProbe())
    project_id = _project_with_translation(service, tmp_path)
    samples = []
    for index in (1, 2):
        source = tmp_path / str(index) / "sample.wav"
        source.parent.mkdir()
        write_pcm16_mono(source, array("f", [index * 0.02]) * 48_000, 48_000)
        samples.append(source)
    result = service.configure_dialogue_casting(project_id, {
        role + "_voice": {"provider_id": "vieneu-3.8", "voice_id": "clone",
                          "reference_audio_path": str(sample)}
        for role, sample in zip(("male", "female"), samples, strict=True)
    })
    managed = [Path(result["config"][role + "_voice"]["reference_audio_path"])
               for role in ("male", "female")]
    assert managed[0] != managed[1]
    assert all(path.is_relative_to(data_root) for path in managed)
    assert [path.read_bytes() for path in managed] == [path.read_bytes() for path in samples]


def test_dialogue_casting_config_persists_and_voice_change_only_invalidates_affected_cue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "worker-staging"))
    database_path = tmp_path / "state.db"
    artifact_root = tmp_path / "artifacts"
    service = JobManagerService(
        database_path=database_path,
        artifact_root=artifact_root,
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    rows = service.get_dialogue_casting(project_id)["rows"]
    male_cue, female_cue = rows[0]["id"], rows[1]["id"]
    service.configure_dialogue_casting(
        project_id,
        {
            "enabled": True,
            "male_voice": _voice("vi_VN-vais1000-medium"),
            "female_voice": _voice("vi_VN-vivos-x_low"),
            "fallback_voice": _voice("vi_VN-vais1000-medium"),
        },
    )
    service.set_dialogue_casting_labels(project_id, [male_cue], "male")
    service.set_dialogue_casting_labels(project_id, [female_cue], "female")

    reopened = JobManagerService(
        database_path=database_path,
        artifact_root=artifact_root,
        media_probe=FixtureProbe(),
    )
    persisted = reopened.get_dialogue_casting(project_id)
    assert persisted["config"]["enabled"] is True
    assert {row["id"]: row["label"] for row in persisted["rows"]} == {
        male_cue: "male",
        female_cue: "female",
    }

    first = reopened.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="vi_VN-vais1000-medium",
        provider_options={"data_root": str(data_root)},
    )
    first_config = first["spec"]["provider_config"]
    assert first_config["cue_voices"][male_cue]["voice_id"] == "vi_VN-vais1000-medium"
    assert first_config["cue_voices"][female_cue]["voice_id"] == "vi_VN-vivos-x_low"
    first_worker = TtsWorker(first["id"])
    first_worker._configure({"job": first["spec"]})
    first_fingerprints = {cue.cue_id: cue.request_fingerprint for cue in first_worker.cues}
    _finish_as_failed(reopened, first["id"])

    reopened.configure_dialogue_casting(
        project_id,
        {
            "enabled": True,
            "male_voice": _voice("vi_VN-vais1000-medium"),
            "female_voice": _voice("vi_VN-25hours_single-low"),
            "fallback_voice": _voice("vi_VN-vais1000-medium"),
        },
    )
    second = reopened.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="vi_VN-vais1000-medium",
        provider_options={"data_root": str(data_root)},
    )
    second_worker = TtsWorker(second["id"])
    second_worker._configure({"job": second["spec"]})
    second_fingerprints = {cue.cue_id: cue.request_fingerprint for cue in second_worker.cues}

    assert second_fingerprints[male_cue] == first_fingerprints[male_cue]
    assert second_fingerprints[female_cue] != first_fingerprints[female_cue]


def test_dialogue_casting_rejects_clone_only_options_on_piper_voice(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    cue_ids = [row["id"] for row in service.get_dialogue_casting(project_id)["rows"]]
    invalid_voice = {**_voice("vi_VN-vais1000-medium"),
                     "reference_text": "Piper không dùng nội dung tham chiếu clone."}
    service.configure_dialogue_casting(
        project_id,
        {
            "enabled": True,
            "male_voice": invalid_voice,
            "female_voice": _voice("vi_VN-vivos-x_low"),
            "fallback_voice": _voice("vi_VN-25hours_single-low"),
        },
    )
    service.set_dialogue_casting_labels(project_id, cue_ids, "male")

    with pytest.raises(
        ValueError,
        match=r"Unknown TTS provider options: \['reference_text'\]",
    ):
        service.create_tts_job(
            project_id=project_id,
            provider_id="piper",
            voice_id="vi_VN-vais1000-medium",
            provider_options={"data_root": str(data_root)},
        )


def test_no_speech_is_not_voiced_until_manual_gender_and_automatic_restores_skip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "worker-staging"))
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    rows = service.get_dialogue_casting(project_id)["rows"]
    no_speech_cue, uncertain_cue = rows[0], rows[1]
    snapshot = service.projects.load(project_id)
    state = DialogueCastingState(
        enabled=True,
        male_voice=CastingVoice(**_voice("vi_VN-vais1000-medium")),
        female_voice=CastingVoice(**_voice("vi_VN-vivos-x_low")),
        fallback_voice=CastingVoice(**_voice("vi_VN-25hours_single-low")),
        results=(
            CastingResult(no_speech_cue["id"], no_speech_cue["audio_key"], "unknown",
                          "no_speech"),
            CastingResult(uncertain_cue["id"], uncertain_cue["audio_key"], "unknown",
                          "low_confidence", 0.31),
        ),
        analyzer_id=CASTING_ANALYZER_ID,
    )
    snapshot = replace(
        snapshot,
        revision=snapshot.revision + 1,
        dialogue_casting=state,
    )
    summary = service.projects.get_summary(project_id)
    service.projects.save(project_path=summary["path"], snapshot=snapshot)

    automatic = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="vi_VN-vais1000-medium",
        provider_options={"data_root": str(data_root)},
    )
    first_config = automatic["spec"]["provider_config"]
    assert first_config["casting_skipped_cues"] == {
        no_speech_cue["id"]: "dialogue_casting_no_speech"
    }
    assert no_speech_cue["id"] not in first_config["cue_voices"]
    assert first_config["cue_voices"][uncertain_cue["id"]]["voice_id"] == (
        "vi_VN-25hours_single-low"
    )
    worker = TtsWorker(automatic["id"])
    worker._configure({"job": automatic["spec"]})
    assert [cue.cue_id for cue in worker.cues] == [uncertain_cue["id"]]
    assert worker.ineligible[no_speech_cue["id"]] == "dialogue_casting_no_speech"
    _finish_as_failed(service, automatic["id"])

    service.set_dialogue_casting_labels(project_id, [no_speech_cue["id"]], "male")
    assigned = service.rebase_tts_retry(automatic["id"])
    assert assigned is not None
    assigned_config = assigned["spec"]["provider_config"]
    assert assigned_config["casting_skipped_cues"] == {}
    assert assigned_config["cue_voices"][no_speech_cue["id"]]["voice_id"] == (
        "vi_VN-vais1000-medium"
    )
    assert assigned["spec"]["input_fingerprint"] != automatic["spec"]["input_fingerprint"]
    assigned_worker = TtsWorker(assigned["id"])
    assigned_worker._configure({"job": assigned["spec"]})
    assert {cue.cue_id for cue in assigned_worker.cues} == {
        no_speech_cue["id"], uncertain_cue["id"]
    }
    _finish_as_failed(service, assigned["id"])

    service.set_dialogue_casting_labels(project_id, [no_speech_cue["id"]], "auto")
    reset = service.rebase_tts_retry(assigned["id"])
    assert reset is not None
    reset_config = reset["spec"]["provider_config"]
    assert reset_config["casting_skipped_cues"] == {
        no_speech_cue["id"]: "dialogue_casting_no_speech"
    }
    assert no_speech_cue["id"] not in reset_config["cue_voices"]
    assert reset["spec"]["input_fingerprint"] != assigned["spec"]["input_fingerprint"]


def test_multi_tts_retry_preserves_frozen_dialogue_casting_settings(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    first_row, second_row = service.get_dialogue_casting(project_id)["rows"]
    snapshot = service.projects.load(project_id)
    state = DialogueCastingState(
        enabled=True,
        male_voice=CastingVoice(**_voice("vi_VN-vais1000-medium")),
        female_voice=CastingVoice(**_voice("vi_VN-vivos-x_low")),
        fallback_voice=CastingVoice(**_voice("vi_VN-25hours_single-low")),
        results=(
            CastingResult(first_row["id"], first_row["audio_key"], "unknown", "no_speech"),
            CastingResult(second_row["id"], second_row["audio_key"], "unknown", "low_confidence"),
        ),
        analyzer_id=CASTING_ANALYZER_ID,
    )
    snapshot = replace(
        snapshot,
        revision=snapshot.revision + 1,
        dialogue_casting=state,
    )
    summary = service.projects.get_summary(project_id)
    service.projects.save(project_path=summary["path"], snapshot=snapshot)

    frozen = {
        "enabled": True,
        "male_voice": _voice("vi_VN-vais1000-medium"),
        "female_voice": _voice("vi_VN-vivos-x_low"),
        "fallback_voice": _voice("vi_VN-25hours_single-low"),
        "overrides": [
            {"cue_id": first_row["id"], "audio_key": first_row["audio_key"], "label": "male"}
        ],
    }
    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="vi_VN-vais1000-medium",
        provider_options={"data_root": str(data_root), "dialogue_casting": frozen},
    )
    original_config = job["spec"]["provider_config"]
    assert original_config["dialogue_casting_config"] == frozen
    assert first_row["id"] not in original_config["casting_skipped_cues"]
    assert original_config["cue_voices"][first_row["id"]]["voice_id"] == (
        "vi_VN-vais1000-medium"
    )

    _finish_as_failed(service, job["id"])
    service.set_dialogue_casting_labels(project_id, [first_row["id"]], "female")

    assert service.tts_job_needs_rebase(job["id"]) is False
    assert service.rebase_tts_retry(job["id"]) is None


def test_voice_import_replaces_old_d1_with_only_voiced_cue_clips(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    snapshot = service.projects.load(project_id)
    first, second = snapshot.source_cues
    old_voice = tmp_path / "old-voice.wav"
    new_voice = tmp_path / "new-voice.wav"
    old_voice.write_bytes(b"old-voice")
    new_voice.write_bytes(b"new-voice")
    voice_imports = MediaImportService(VoiceFixtureProbe())

    with_old_d1 = voice_imports.import_voice_media(snapshot, old_voice)
    old_d1 = next(track for track in with_old_d1.tracks if track.id == "D1")
    old_media_id = old_d1.clips[0].media_id
    with_skipped_cue = voice_imports.import_voice_media(
        with_old_d1,
        new_voice,
        cue_timing=(
            {
                "cue_id": second.id,
                "index": second.index,
                "timeline_start_us": 1_200_000,
                "timeline_end_us": 1_700_000,
                "source_start_us": 1_200_000,
                "source_end_us": 1_700_000,
                "timing_slot_start_us": second.range.start_us,
                "timing_slot_end_us": second.range.end_us,
                "cue_text": "Lên đường thôi.",
            },
        ),
        excluded_cue_ids=(first.id,),
    )
    d1 = next(track for track in with_skipped_cue.tracks if track.id == "D1")
    assert [clip.cue_id for clip in d1.clips] == [second.id]
    assert old_media_id not in {media.id for media in with_skipped_cue.media}

    without_voice = voice_imports.import_voice_media(
        with_skipped_cue,
        new_voice,
        cue_timing=(),
        excluded_cue_ids=(first.id, second.id),
    )
    assert all(track.id != "D1" for track in without_voice.tracks)
    assert all(media.id != d1.clips[0].media_id for media in without_voice.media)


def test_all_no_speech_cues_produce_silence_without_tts_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "worker-staging"))
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    rows = service.get_dialogue_casting(project_id)["rows"]
    snapshot = service.projects.load(project_id)
    state = DialogueCastingState(
        enabled=True,
        male_voice=CastingVoice(**_voice("vi_VN-vais1000-medium")),
        female_voice=CastingVoice(**_voice("vi_VN-vivos-x_low")),
        fallback_voice=CastingVoice(**_voice("vi_VN-25hours_single-low")),
        results=tuple(
            CastingResult(row["id"], row["audio_key"], "unknown", "no_speech")
            for row in rows
        ),
        analyzer_id=CASTING_ANALYZER_ID,
    )
    snapshot = replace(snapshot, revision=snapshot.revision + 1, dialogue_casting=state)
    summary = service.projects.get_summary(project_id)
    service.projects.save(project_path=summary["path"], snapshot=snapshot)

    job = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="vi_VN-vais1000-medium",
        provider_options={"data_root": str(data_root)},
    )
    worker = TtsWorker(
        job["id"],
        provider_factory=lambda _config: (_ for _ in ()).throw(
            AssertionError("No TTS provider should be created for silent cues")
        ),
    )
    worker._configure({"job": job["spec"]})
    assert worker.cues == ()
    worker._execute_synthesis()

    config = job["spec"]["provider_config"]
    output = Path(config["staging_output_path"])
    qa = json.loads(Path(config["staging_qa_path"]).read_text(encoding="utf-8"))
    with wave.open(str(output), "rb") as stream:
        assert stream.getnframes() == 5 * 48_000
        assert not any(stream.readframes(5 * 48_000))
    assert qa["status"] == "PASS"
    assert qa["eligible_cues"] == 0
    assert qa["ineligible_cues"] == len(rows)
    assert qa["master"] == {"status": "not_required", "reason": "no_speech"}
    assert not Path(config["cache_root"]).exists()


@pytest.mark.parametrize(
    ("invalid_skip", "expected_skip_error"),
    [
        ("reason", "TTS casting skip list is invalid"),
        ("unproven", "TTS ledger does not match the dialogue casting skip list"),
        ("unfrozen", "TTS casting skip list does not match the frozen dialogue input"),
        ("contradictory", "TTS ledger does not match the dialogue casting skip list"),
    ],
)
def test_voice_conform_skips_no_speech_cue_and_keeps_voiced_d1_after_reset(
    tmp_path: Path,
    monkeypatch,
    invalid_skip: str,
    expected_skip_error: str,
) -> None:
    """A casting skip is a verified silent cue, not a missing WAV cache."""
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "worker-staging"))
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=VoiceConformFixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    data_root = _data_root(tmp_path)
    rows = service.get_dialogue_casting(project_id)["rows"]
    no_speech_cue, voiced_cue = rows
    snapshot = service.projects.load(project_id)
    state = DialogueCastingState(
        enabled=True,
        male_voice=CastingVoice(**_voice("vi_VN-vais1000-medium")),
        female_voice=CastingVoice(**_voice("vi_VN-vivos-x_low")),
        fallback_voice=CastingVoice(**_voice("vi_VN-25hours_single-low")),
        results=(
            CastingResult(no_speech_cue["id"], no_speech_cue["audio_key"], "unknown", "no_speech"),
            CastingResult(voiced_cue["id"], voiced_cue["audio_key"], "male", "fixture", 0.99),
        ),
        analyzer_id=CASTING_ANALYZER_ID,
    )
    summary = service.projects.get_summary(project_id)
    service.projects.save(
        project_path=summary["path"],
        snapshot=replace(
            snapshot,
            revision=snapshot.revision + 1,
            dialogue_casting=state,
        ),
    )

    parent = service.create_tts_job(
        project_id=project_id,
        provider_id="piper",
        voice_id="vi_VN-vais1000-medium",
        provider_options={"data_root": str(data_root)},
    )
    parent_id = str(parent["id"])
    parent_config = parent["spec"]["provider_config"]
    assert parent_config["casting_skipped_cues"] == {
        no_speech_cue["id"]: "dialogue_casting_no_speech"
    }

    voiced_wav = tmp_path / "voiced-cue.wav"
    _write_conform_wav(voiced_wav, 1_000_000)
    voiced_qa = {"ok": True, "duration_us": 1_000_000}
    ledger_path = service.checkpoints.staging_root / parent_id / "parent-voice-ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ineligible": {no_speech_cue["id"]: "dialogue_casting_no_speech"},
                "entries": {
                    voiced_cue["id"]: {
                        "cue_id": voiced_cue["id"],
                        "cache_path": str(voiced_wav),
                        "cache_sha256": file_sha256(voiced_wav),
                        "base_qa": voiced_qa,
                    }
                },
                "blockers": {},
            }
        ),
        encoding="utf-8",
    )
    service.commit_worker_checkpoint(
        parent_id,
        {
            "stage_key": "tts",
            "checkpoint_key": "latest",
            "schema_version": 1,
            "payload": {"approved_cues": 1},
            "artifact_path": str(ledger_path),
            "artifact_kind": "voice_ledger",
            "artifact_state": "technical",
        },
        event_id="parent-voice-ledger",
    )
    checkpoint = service.load_worker_checkpoint(parent_id, stage_key="tts", checkpoint_key="latest")
    ledger_path = Path(checkpoint["artifact"]["uri"])

    parent_output = Path(parent_config["output_path"])
    _write_conform_wav(parent_output, 5_000_000)
    Path(parent_config["qa_path"]).write_text(
        json.dumps(
            {
                "status": "PASS",
                "timing_issues": [],
                "timing": {"shifted_cues": 0},
                "voice_read_speed": 1.0,
            }
        ),
        encoding="utf-8",
    )
    service.jobs.transition(parent_id, JobState.START_REQUESTED)
    service.jobs.transition(parent_id, JobState.RUNNING)
    service.jobs.transition(parent_id, JobState.SUCCEEDED)
    service.import_final_voice_wav(parent_id, str(parent_output))

    base_snapshot = service.projects.load(project_id)
    base_track = next(track for track in base_snapshot.tracks if track.id == "D1")
    assert [clip.cue_id for clip in base_track.clips] == [voiced_cue["id"]]

    # Editing today's labels must not change the cues in the completed parent D1.
    revised_casting = service.set_dialogue_casting_labels(
        project_id, [no_speech_cue["id"]], "female"
    )
    assert revised_casting["rows"][0]["label"] == "female"
    assert revised_casting["rows"][0]["manual"] is True

    estimate = service.estimate_voice_conform(
        project_id=project_id,
        parent_tts_job_id=parent_id,
        mode="stretch_video",
    )
    assert estimate["status"] == "PASS"
    assert estimate["unresolved_count"] == 0
    assert [cue["cue_id"] for cue in estimate["plan"]["cues"]] == [voiced_cue["id"]]

    conform = service.create_voice_conform_job(
        project_id=project_id,
        parent_tts_job_id=parent_id,
        mode="stretch_video",
    )
    worker = TtsWorker(
        conform["id"],
        audio_processor_factory=CacheOnlyConformProcessor,
    )
    worker.send = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    worker._configure({"job": conform["spec"]})
    worker._execute()
    conform_id = str(conform["id"])
    service.jobs.transition(conform_id, JobState.START_REQUESTED)
    service.jobs.transition(conform_id, JobState.RUNNING)
    service.jobs.transition(conform_id, JobState.SUCCEEDED)
    service.import_final_voice_conform(
        conform_id,
        conform["spec"]["provider_config"]["output_path"],
    )

    conformed = service.projects.load(project_id)
    conformed_track = next(track for track in conformed.tracks if track.id == "D1")
    assert [clip.cue_id for clip in conformed_track.clips] == [voiced_cue["id"]]
    assert conformed.source_cues == base_snapshot.source_cues
    assert conformed.translated_cues == base_snapshot.translated_cues

    reset = service.reset_voice_conform(project_id)
    reset_track = next(track for track in reset["tracks"] if track["id"] == "D1")
    assert [clip["cue_id"] for clip in reset_track["clips"]] == [voiced_cue["id"]]
    restored = service.projects.load(project_id)
    assert restored.source_cues == base_snapshot.source_cues
    assert restored.translated_cues == base_snapshot.translated_cues

    # A skip requires both frozen cue membership and unambiguous ledger proof.
    skipped_wav = tmp_path / "malformed-skip-cue.wav"
    _write_conform_wav(skipped_wav, 1_000_000)
    malformed_config = dict(parent_config)
    malformed_config["casting_skipped_cues"] = {
        no_speech_cue["id"]: (
            "manual" if invalid_skip == "reason" else "dialogue_casting_no_speech"
        )
    }
    if invalid_skip == "unfrozen":
        malformed_config["cues"] = [
            cue for cue in parent_config["cues"] if cue["id"] != no_speech_cue["id"]
        ]
    malformed = service.create_job(
        {
            "kind": "tts",
            "project_id": project_id,
            "input_revision": service.projects.load(project_id).revision,
            "provider_id": parent["spec"]["provider_id"],
            "provider_version": parent["spec"]["provider_version"],
            "model_id": parent["spec"]["model_id"],
            "input_fingerprint": "malformed-casting-skip-parent",
            "settings_fingerprint": "malformed-casting-skip-settings",
            "provider_config": malformed_config,
        }
    )
    malformed_id = str(malformed["id"])
    malformed_ledger = service.checkpoints.staging_root / malformed_id / "parent-voice-ledger.json"
    malformed_ledger.parent.mkdir(parents=True, exist_ok=True)
    malformed_entries = {
        no_speech_cue["id"]: {
            "cue_id": no_speech_cue["id"],
            "cache_path": str(skipped_wav),
            "cache_sha256": file_sha256(skipped_wav),
            "base_qa": voiced_qa,
        },
        voiced_cue["id"]: {
            "cue_id": voiced_cue["id"],
            "cache_path": str(skipped_wav),
            "cache_sha256": file_sha256(skipped_wav),
            "base_qa": voiced_qa,
        },
    }
    malformed_evidence: dict[str, object] = {
        "schema_version": 1, "entries": malformed_entries, "blockers": {}
    }
    if invalid_skip in {"unfrozen", "contradictory"}:
        malformed_evidence["ineligible"] = {
            no_speech_cue["id"]: "dialogue_casting_no_speech"
        }
    if invalid_skip == "unfrozen":
        malformed_entries.pop(no_speech_cue["id"])
    malformed_ledger.write_text(json.dumps(malformed_evidence), encoding="utf-8")
    service.commit_worker_checkpoint(
        malformed_id,
        {
            "stage_key": "tts",
            "checkpoint_key": "latest",
            "schema_version": 1,
            "payload": {"approved_cues": 2},
            "artifact_path": str(malformed_ledger),
            "artifact_kind": "voice_ledger",
            "artifact_state": "technical",
        },
        event_id="malformed-skip-ledger",
    )
    service.jobs.transition(malformed_id, JobState.START_REQUESTED)
    service.jobs.transition(malformed_id, JobState.RUNNING)
    service.jobs.transition(malformed_id, JobState.SUCCEEDED)
    with pytest.raises(ValueError, match=expected_skip_error):
        service.estimate_voice_conform(
            project_id=project_id,
            parent_tts_job_id=malformed_id,
            mode="stretch_video",
        )

    skipped_wav.unlink()
    voiced_wav.unlink()
    with pytest.raises(ValueError, match="Cache WAV nền cue 2 không còn tồn tại"):
        service.estimate_voice_conform(
            project_id=project_id,
            parent_tts_job_id=parent_id,
            mode="stretch_video",
        )

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["entries"].pop(voiced_cue["id"])
    missing_ledger = service.checkpoints.staging_root / parent_id / "missing-voice-ledger.json"
    missing_ledger.parent.mkdir(parents=True, exist_ok=True)
    missing_ledger.write_text(json.dumps(ledger), encoding="utf-8")
    service.commit_worker_checkpoint(
        parent_id,
        {
            "stage_key": "tts",
            "checkpoint_key": "latest",
            "schema_version": 1,
            "payload": {"approved_cues": 0},
            "artifact_path": str(missing_ledger),
            "artifact_kind": "voice_ledger",
            "artifact_state": "technical",
        },
        event_id="missing-voiced-cue-ledger",
    )
    with pytest.raises(ValueError, match="Cache WAV nền.*cue 2"):
        service.estimate_voice_conform(
            project_id=project_id,
            parent_tts_job_id=parent_id,
            mode="stretch_video",
        )


def test_voice_preparation_uses_frozen_multi_overrides_to_decide_analysis_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "worker-staging"))
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    rows = service.get_dialogue_casting(project_id)["rows"]
    all_cue_ids = [row["id"] for row in rows]
    service.configure_dialogue_casting(
        project_id,
        {
            "enabled": True,
            "male_voice": _voice("vi_VN-vais1000-medium"),
            "female_voice": _voice("vi_VN-vivos-x_low"),
            "fallback_voice": _voice("vi_VN-vais1000-medium"),
        },
    )
    for cue_id in all_cue_ids:
        service.set_dialogue_casting_labels(project_id, [cue_id], "male")
    monkeypatch.setattr(
        service_module,
        "casting_runtime_config",
        lambda _root: {
            "diarization_python": "fixture-diarization-python",
            "classifier_python": "fixture-classifier-python",
            "diarization_models": "fixture-diarization-models",
            "classifier_models": "fixture-classifier-models",
        },
    )

    result = service.prepare_voice_generation(
        project_id=project_id,
        provider_id="piper",
        voice_id="vi_VN-vais1000-medium",
        provider_options={
            "dialogue_casting": {
                "enabled": True,
                "male_voice": _voice("vi_VN-vais1000-medium"),
                "female_voice": _voice("vi_VN-vivos-x_low"),
                "fallback_voice": _voice("vi_VN-vais1000-medium"),
                "overrides": [],
            }
        },
        reuse_completed=True,
    )

    assert result["spec"]["kind"] == "dialogue_casting"
    assert {row["id"] for row in result["spec"]["provider_config"]["cues"]} == set(all_cue_ids)


def test_dialogue_casting_success_event_imports_guarded_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    assert application is not None
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    monkeypatch.setattr(
        service_module,
        "casting_runtime_config",
        lambda _root: {
            "diarization_python": "fixture-diarization-python",
            "classifier_python": "fixture-classifier-python",
            "diarization_models": "fixture-diarization-models",
            "classifier_models": "fixture-classifier-models",
        },
    )
    created = service.create_dialogue_casting_job(project_id=project_id)
    job_id = created["id"]
    service.jobs.transition(job_id, JobState.START_REQUESTED)
    service.jobs.transition(job_id, JobState.RUNNING)
    cues = created["spec"]["provider_config"]["cues"]
    output = service.checkpoints.staging_root / job_id / "casting-results.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps(
            {
                "signature": created["spec"]["input_fingerprint"],
                "results": [
                    {
                        "cue_id": cue["id"],
                        "audio_key": cue["audio_key"],
                        "label": "male" if index == 0 else "female",
                        "reason": "fixture-classification",
                        "confidence": 0.9,
                    }
                    for index, cue in enumerate(cues)
                ],
            }
        ),
        encoding="utf-8",
    )
    server = JobManagerServer(name=f"unused-casting-{uuid4()}", service=service)
    try:
        server._persist_worker_event(
            job_id,
            WorkerMessage(
                PROTOCOL_VERSION,
                job_id,
                "casting-succeeded",
                "succeeded",
                {"status": "PASS", "output_path": str(output)},
            ),
        )

        assert service.jobs.get(job_id).state is JobState.SUCCEEDED
        casting = service.get_dialogue_casting(project_id)
        assert all(row["ready"] for row in casting["rows"])
        assert [row["label"] for row in casting["rows"]] == ["male", "female"]
        assert all(row["reason"] == "fixture-classification" for row in casting["rows"])
    finally:
        server.close()


def test_analyzer_upgrade_discards_stale_machine_results_but_preserves_manual_gender(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    rows = service.get_dialogue_casting(project_id)["rows"]
    manual_cue, automatic_cue = rows
    snapshot = service.projects.load(project_id)
    state = DialogueCastingState(
        results=tuple(
            CastingResult(row["id"], row["audio_key"], "female", "old-result", 0.99)
            for row in rows
        ),
        overrides=(CastingOverride(manual_cue["id"], manual_cue["audio_key"], "male"),),
        analyzer_id="old-analyzer",
    )
    service._store_editor_snapshot(
        replace(snapshot, revision=snapshot.revision + 1, dialogue_casting=state),
        deferred=False,
    )
    monkeypatch.setattr(service_module, "casting_runtime_config", lambda _root: {})
    created = service.create_dialogue_casting_job(project_id=project_id)
    assert [row["id"] for row in created["spec"]["provider_config"]["cues"]] == [
        automatic_cue["id"]
    ]
    output = service.checkpoints.staging_root / created["id"] / "casting-results.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps({
            "signature": created["spec"]["input_fingerprint"],
            "results": [{
                "cue_id": automatic_cue["id"],
                "audio_key": automatic_cue["audio_key"],
                "label": "male",
                "reason": "new-result",
                "confidence": 0.99,
            }],
        }),
        encoding="utf-8",
    )

    service.import_dialogue_casting(created["id"], str(output))

    refreshed = service.projects.load(project_id).dialogue_casting
    assert refreshed is not None
    assert refreshed.analyzer_id == CASTING_ANALYZER_ID
    assert refreshed.overrides == state.overrides
    assert [result.cue_id for result in refreshed.results] == [automatic_cue["id"]]
    manual = service.get_dialogue_casting(project_id)["rows"][0]
    assert manual["label"] == "male"
    assert manual["ready"] is True
    assert manual["machine_reason"] == "needs_analysis"

    reset = service.set_dialogue_casting_labels(project_id, [manual_cue["id"]], "auto")
    assert reset["rows"][0]["label"] == "unknown"
    assert reset["rows"][0]["ready"] is False
    assert reset["rows"][0]["machine_reason"] == "needs_analysis"
    assert reset["rows"][1]["label"] == "male"
    assert reset["rows"][1]["ready"] is True


def test_casting_import_rejects_results_from_an_outdated_analyzer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=FixtureProbe(),
    )
    project_id = _project_with_translation(service, tmp_path)
    monkeypatch.setattr(service_module, "casting_runtime_config", lambda _root: {})
    created = service.create_dialogue_casting_job(project_id=project_id)
    output = service.checkpoints.staging_root / created["id"] / "casting-results.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps({
            "signature": created["spec"]["input_fingerprint"],
            "results": [{
                "cue_id": row["id"],
                "audio_key": row["audio_key"],
                "label": "female",
                "reason": "assigned",
                "confidence": 0.99,
            } for row in created["spec"]["provider_config"]["cues"]],
        }),
        encoding="utf-8",
    )
    before = service.projects.load(project_id)
    monkeypatch.setattr(service_module, "CASTING_ANALYZER_ID", "newer-analyzer")

    with pytest.raises(ValueError, match="chạy phân vai lại"):
        service.import_dialogue_casting(created["id"], str(output))

    assert service.projects.load(project_id) == before
