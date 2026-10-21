from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication

import ducai_studio.job_manager.service as service_module
from ducai_studio.application.subtitle_finalizer import render_srt
from ducai_studio.application.translation_pipeline import (
    ChunkState,
    build_ledger,
    ledger_from_dict,
    ledger_to_dict,
    plan_chunks,
    semantic_groups,
)
from ducai_studio.domain.jobs import JobKind, JobState
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.job_manager.server import JobManagerServer
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.media_probe import MediaProbeResult
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


class StaticProbe:
    def probe(self, _path):
        return MediaProbeResult(5_000_000, True, True, 1280, 720, "mp4")


def _project(service: JobManagerService, tmp_path: Path) -> tuple[str, Path]:
    video = tmp_path / "film.mp4"
    source = tmp_path / "film_ZH_FINAL.srt"
    video.write_bytes(b"video")
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n我要修炼。\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\n然后成仙。\n",
        encoding="utf-8",
    )
    created = service.create_project(project_path=str(tmp_path / "project.ducai"), name="Phim")
    project_id = str(created["id"])
    service.import_media(project_id=project_id, media_path=str(video))
    service.import_media(project_id=project_id, media_path=str(source))
    return project_id, source


def test_create_translation_job_snapshots_cues_and_only_credential_references(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, source = _project(service, tmp_path)

    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-api",
        model_id="gemini-test",
        provider_options={"credential_references": ["google-project-a", "google-project-b"]},
    )

    job = service.jobs.get(str(created["id"]))
    assert job.spec.kind is JobKind.TRANSLATE
    assert job.spec.provider_config["credential_references"] == (
        "google-project-a",
        "google-project-b",
    )
    assert "api_key" not in job.spec.provider_config
    assert len(job.spec.provider_config["source_cues"]) == 2
    assert job.spec.provider_config["output_path"] == str(
        source.with_name(source.stem + "_codex_vi.srt")
    )
    assert job.spec.provider_config["qa_mode"] == "integrated"
    assert job.spec.provider_config["max_chunk_cues"] == 80
    assert job.spec.provider_config["max_chunk_source_chars"] == 6_000


def test_translation_cache_metadata_does_not_replace_the_full_worker_spec(tmp_path: Path):
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts", media_probe=StaticProbe())
    project_id, _ = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id, provider_id="openai-compatible", model_id="test-model",
        provider_options={"credential_references": ["named-key"],
                          "base_url": "http://127.0.0.1:8045/v1",
                          "translation_custom_rules": "Giữ nghĩa chính xác."},
    )
    full = service.jobs.get(created["id"])
    compact, = service.jobs.list_project_jobs(
        project_id=project_id, kind=JobKind.TRANSLATE,
        input_fingerprint=full.spec.input_fingerprint, compact_translation=True,
    )
    assert compact.spec.provider_config == {
        "credential_references": ("named-key",), "base_url": "http://127.0.0.1:8045/v1",
        "translation_style_preset": "auto", "translation_custom_rules": "Giữ nghĩa chính xác.",
        "qa_policy": "legacy",
    }
    assert compact.spec.resume_fingerprint == full.spec.resume_fingerprint
    assert service.jobs.get(full.id).spec == full.spec
    assert len(service.jobs.get(full.id).spec.provider_config["source_cues"]) == 2


@pytest.mark.parametrize("malformed_config", ["[]", "null", "123", '"text"', "broken"])
def test_translation_cache_metadata_skips_non_object_configuration(
    tmp_path: Path, malformed_config: str,
) -> None:
    service = JobManagerService(database_path=tmp_path / "state.db",
                                artifact_root=tmp_path / "artifacts", media_probe=StaticProbe())
    project_id, _ = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id, provider_id="gemini-api", model_id="test-model",
        provider_options={"credential_references": ["named-key"]},
    )
    with service.database.write_transaction() as connection:
        connection.execute("UPDATE jobs SET config_json = ? WHERE id = ?",
                           (malformed_config, created["id"]))
    assert service.jobs.list_project_jobs(
        project_id=project_id, kind=JobKind.TRANSLATE, compact_translation=True,
    ) == ()


def test_translation_rerun_rolls_back_active_file_when_project_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, source = _project(service, tmp_path)
    active = source.with_name(source.stem + "_codex_vi.srt")
    active.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nBản cũ một.\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\nBản cũ hai.\n",
        encoding="utf-8-sig",
    )
    service.import_translation_srt(project_id=project_id, subtitle_path=str(active))
    previous_bytes = active.read_bytes()
    previous_revision = service.projects.load(project_id).revision
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-by-web",
        provider_options={"profile_path": str(tmp_path / "gemini-profile")},
    )
    config = created["spec"]["provider_config"]
    candidate = Path(config["staging_output_path"])
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nBản mới một.\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\nBản mới hai.\n",
        encoding="utf-8-sig",
    )
    candidate_qa = candidate.with_name(candidate.stem + "_QA.json")
    candidate_qa.write_text(
        json.dumps(
            {
                "status": "PASS",
                "job_id": created["id"],
                "input_fingerprint": created["spec"]["input_fingerprint"],
                "settings_fingerprint": created["spec"]["settings_fingerprint"],
                "output_path": str(candidate),
                "output_sha256": service_module._file_sha256(candidate),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        service.projects,
        "save",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("disk publish failed")),
    )

    with pytest.raises(RuntimeError, match="disk publish failed"):
        service.import_final_translation_srt(created["id"], str(candidate))

    assert active.read_bytes() == previous_bytes
    assert candidate.is_file()
    assert service.projects.load(project_id).revision == previous_revision


def test_create_translation_job_persists_multi_qa_policy_options(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)

    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-api",
        model_id="gemini-test",
        provider_options={
            "credential_references": ["multi-key"],
            "qa_policy": "multi_auto",
            "cjk_is_blocker": True,
            "auto_compact_overlong_once": True,
            "max_overlong_compaction_passes": 1,
        },
    )

    config = service.jobs.get(str(created["id"])).spec.provider_config
    assert config["qa_policy"] == "multi_auto"
    assert config["cjk_is_blocker"] is True
    assert config["auto_compact_overlong_once"] is True
    assert config["max_overlong_compaction_passes"] == 1


def test_project_translation_style_is_persisted_and_changes_only_translation_cache(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    first = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-api",
        model_id="gemini-test",
        provider_options={"credential_references": ["style-key"]},
    )
    service.jobs.transition(str(first["id"]), JobState.CANCEL_REQUESTED)
    service.jobs.transition(str(first["id"]), JobState.CANCELLED)

    service.set_translation_preferences(
        project_id=project_id,
        preset="xianxia",
        custom_rules="Hồng Ly luôn được gọi là Hồng Ly tỷ tỷ.",
    )
    second = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-api",
        model_id="gemini-test",
        provider_options={"credential_references": ["style-key"]},
    )

    first_spec = service.jobs.get(str(first["id"])).spec
    second_spec = service.jobs.get(str(second["id"])).spec
    assert first_spec.input_fingerprint == second_spec.input_fingerprint
    assert first_spec.settings_fingerprint != second_spec.settings_fingerprint
    assert second_spec.provider_config["translation_style_preset"] == "xianxia"
    assert second_spec.provider_config["translation_custom_rules"] == (
        "Hồng Ly luôn được gọi là Hồng Ly tỷ tỷ."
    )


def test_named_translation_style_is_saved_and_reused_without_overwriting(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)

    saved = service.save_translation_style(
        project_id=project_id,
        name="  Cổ trang riêng  ",
        preset="xianxia",
        custom_rules="Giữ xưng hô theo vai vế.",
    )
    preferences = saved["translation_preferences"]
    assert preferences["preset"] == "xianxia"
    assert preferences["active_style_name"] == "Cổ trang riêng"
    assert preferences["custom_rules"] == "Giữ xưng hô theo vai vế."
    assert preferences["saved_styles"] == [
        {
            "name": "Cổ trang riêng",
            "preset": "xianxia",
            "custom_rules": "Giữ xưng hô theo vai vế.",
        }
    ]
    loaded = service.projects.load(project_id)
    assert loaded.translation_preferences.saved_styles[0].name == "Cổ trang riêng"

    service.set_translation_preferences(
        project_id=project_id,
        preset="neutral",
        custom_rules="Giữ câu ngắn.",
    )
    preserved = service.projects.load(project_id).translation_preferences
    assert preserved.preset == "neutral"
    assert preserved.saved_styles[0].name == "Cổ trang riêng"
    assert preserved.active_style_name == ""

    service.save_translation_style(
        project_id=project_id,
        name="Cổ trang riêng 2",
        preset="xianxia",
        custom_rules="Giữ xưng hô theo vai vế.",
    )
    service.set_translation_preferences(
        project_id=project_id,
        preset="xianxia",
        custom_rules="Giữ xưng hô theo vai vế.",
        active_style_name="Cổ trang riêng",
    )
    reopened = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    reloaded = reopened.projects.load(project_id).translation_preferences
    assert reloaded.active_style_name == "Cổ trang riêng"
    assert len(reloaded.saved_styles) == 2

    with pytest.raises(ValueError, match="Tên văn phong đã tồn tại"):
        service.save_translation_style(
            project_id=project_id,
            name="cổ trang riêng",
            preset="modern",
            custom_rules="Không được ghi đè.",
        )


def test_manual_multi_translation_review_requires_all_cues_without_cjk(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-api",
        model_id="gemini-test",
        provider_options={
            "credential_references": ["multi-key"],
            "qa_policy": "multi_auto",
            "cjk_is_blocker": True,
            "auto_compact_overlong_once": True,
            "max_overlong_compaction_passes": 1,
        },
    )
    job_id = str(created["id"])
    service.jobs.transition(job_id, JobState.START_REQUESTED)
    service.jobs.transition(job_id, JobState.RUNNING)
    service.jobs.transition(
        job_id,
        JobState.FAILED,
        error_code="translation_review_required",
        error_payload={"cjk_cue_ids": [2]},
    )

    snapshot = service.projects.load(project_id)
    review = replace(
        snapshot,
        revision=snapshot.revision + 1,
        translated_cues=tuple(
            replace(
                cue,
                text=("Bản dịch còn 中文" if cue.index == 2 else "Bản dịch đã sửa"),
                source="translation_review",
            )
            for cue in snapshot.source_cues
        ),
    )
    service.projects.save(
        project_path=service.projects.get_summary(project_id)["path"],
        snapshot=review,
    )
    pending = service.get_manual_translation_review_state(project_id, job_id)
    assert pending["available"] is True
    assert pending["ready"] is False
    assert pending["cjk_cue_ids"] == [2]

    fixed = replace(
        review,
        revision=review.revision + 1,
        translated_cues=tuple(
            replace(cue, text="Bản dịch tiếng Việt", source="translation_review")
            for cue in review.translated_cues
        ),
    )
    service.projects.save(
        project_path=service.projects.get_summary(project_id)["path"],
        snapshot=fixed,
    )
    ready = service.get_manual_translation_review_state(project_id, job_id)
    assert ready["ready"] is True
    assert ready["cjk_cue_ids"] == []


def test_refresh_multi_policy_preserves_cancelled_translation_job_identity(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-on-web",
        provider_options={
            "profile_path": str(tmp_path / "gemini-profile"),
            "headed": True,
        },
    )
    job_id = str(created["id"])
    original = service.jobs.get(job_id)
    service.jobs.transition(job_id, JobState.CANCEL_REQUESTED)
    service.jobs.transition(job_id, JobState.CANCELLED)

    refreshed = service.refresh_translation_multi_policy(job_id=job_id)
    stored = service.jobs.get(job_id)

    assert refreshed["id"] == job_id
    assert stored.spec.settings_fingerprint == original.spec.settings_fingerprint
    assert stored.spec.provider_config["qa_policy"] == "multi_auto"
    assert stored.spec.provider_config["cjk_is_blocker"] is True
    assert stored.spec.provider_config["auto_compact_overlong_once"] is True
    assert stored.spec.provider_config["max_overlong_compaction_passes"] == 1
    assert stored.spec.provider_config["headed"] is False


def test_create_translation_job_rejects_a_second_active_job_for_the_same_project(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    first = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-on-web",
        provider_options={"profile_path": str(tmp_path / "gemini-profile")},
    )

    with pytest.raises(ValueError, match="đang hoạt động"):
        service.create_translation_job(
            project_id=project_id,
            provider_id="gemini-web",
            model_id="selected-on-web",
            provider_options={"profile_path": str(tmp_path / "gemini-profile")},
        )

    assert service.jobs.get(str(first["id"])).state is JobState.QUEUED


def test_voice_timing_repair_marks_only_affected_chunks_pending(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    base = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-on-web",
        provider_options={
            "profile_path": str(tmp_path / "gemini-profile"),
            "max_chunk_cues": 1,
            "max_chunk_source_chars": 200,
        },
    )
    base_id = str(base["id"])
    snapshot = service.projects.load(project_id)
    chunks = plan_chunks(semantic_groups(snapshot.source_cues), max_cues=1, max_source_chars=200)
    ledger = build_ledger(
        snapshot.source_cues,
        chunks,
        provider_id="gemini-web",
        model_id="selected-on-web",
    )
    ledger = replace(
        ledger,
        chunks=tuple(replace(chunk, state=ChunkState.QA_PASS) for chunk in ledger.chunks),
        translations={1: "Ta muốn tu luyện.", 2: "Sau đó thành tiên."},
    )
    ledger_path = service.checkpoints.staging_root / base_id / "translation-ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger_to_dict(ledger)), encoding="utf-8")
    base_job = service.jobs.get(base_id)
    service.checkpoints.commit(
        job_id=base_id,
        stage_key="translation",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=base_job.spec.resume_fingerprint,
        cursor_us=2_500_000,
        payload={"completed_cues": 2, "next_chunk": 2},
        artifact_path=ledger_path,
        artifact_kind="translation_ledger",
        event_id="base-translation-ledger",
    )
    service.jobs.transition(base_id, JobState.START_REQUESTED)
    service.jobs.transition(base_id, JobState.RUNNING)
    service.jobs.transition(base_id, JobState.SUCCEEDED)
    output = Path(str(base["spec"]["provider_config"]["output_path"]))
    output.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nTa muốn tu luyện.\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\nSau đó thành tiên.\n",
        encoding="utf-8-sig",
    )
    service.import_final_translation_srt(base_id, str(output))
    tts_parent = service.create_job(
        {
            "kind": "tts",
            "project_id": project_id,
            "input_revision": service.projects.load(project_id).revision,
            "provider_id": "vieneu-3.8",
            "provider_version": "fixture",
            "model_id": "clone",
            "input_fingerprint": "voice-input",
            "settings_fingerprint": "voice-settings",
            "provider_config": {
                "purpose": "production",
                "timing_repair_cycle": 0,
                "max_timing_repair_cycles": 3,
            },
        }
    )
    tts_parent_id = str(tts_parent["id"])
    service.commit_worker_checkpoint(
        tts_parent_id,
        {
            "stage_key": "tts",
            "checkpoint_key": "review",
            "schema_version": 1,
            "payload": {
                "coverage": {
                    "timing_issues": [
                        {
                            "id": 2,
                            "cue_id": snapshot.source_cues[1].id,
                            "message": "Cue dài.",
                            "target_chars": 10,
                        }
                    ]
                }
            },
        },
        event_id="tts-timing-review",
    )
    service.jobs.transition(tts_parent_id, JobState.START_REQUESTED)
    service.jobs.transition(tts_parent_id, JobState.RUNNING)
    service.jobs.transition(
        tts_parent_id,
        JobState.FAILED,
        error_code="tts_review_required",
    )

    repair = service.create_voice_timing_repair_job(
        project_id=project_id,
        tts_job_id=tts_parent_id,
        translation_job_id=base_id,
    )

    repair_config = repair["spec"]["provider_config"]
    seeded = ledger_from_dict(repair_config["seed_ledger"])
    assert [chunk.state for chunk in seeded.chunks] == [
        ChunkState.QA_PASS,
        ChunkState.PENDING,
    ]
    assert seeded.chunks[0].qa_issues == ()
    assert seeded.chunks[1].qa_issues[0].code == "CUE_TOO_LONG"
    assert seeded.translations == ledger.translations


@pytest.mark.parametrize(
    ("provider_id", "model_id", "base_url"),
    [
        ("deepseek-api", "deepseek-v4-flash", "https://api.deepseek.com"),
        ("tokenhub-api", "hy3", "https://tokenhub.tencentmaas.com/v1"),
        ("qwen-api", "qwen3.7-flash", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("glm-api", "glm-5.2", "https://open.bigmodel.cn/api/paas/v4"),
    ],
)
def test_create_openai_compatible_translation_job_uses_named_pool_and_canonical_endpoint(
    tmp_path: Path,
    provider_id: str,
    model_id: str,
    base_url: str,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / f"{provider_id}.db",
        artifact_root=tmp_path / f"{provider_id}-artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)

    created = service.create_translation_job(
        project_id=project_id,
        provider_id=provider_id,
        model_id=model_id,
        provider_options={"credential_references": [f"{provider_id}-key"]},
    )

    config = service.jobs.get(str(created["id"])).spec.provider_config
    assert config["credential_references"] == (f"{provider_id}-key",)
    assert config["base_url"] == base_url
    assert config["json_mode"] is True
    assert "api_key" not in config


def test_custom_openai_compatible_translation_job_requires_safe_base_url(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "custom.db",
        artifact_root=tmp_path / "custom-artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)

    with pytest.raises(ValueError, match="base URL"):
        service.create_translation_job(
            project_id=project_id,
            provider_id="openai-compatible",
            model_id="custom-model",
            provider_options={"credential_references": ["custom-key"]},
        )

    with pytest.raises(ValueError, match="HTTPS"):
        service.create_translation_job(
            project_id=project_id,
            provider_id="openai-compatible",
            model_id="custom-model",
            provider_options={
                "credential_references": ["custom-key"],
                "base_url": "http://remote.example/v1",
            },
        )


def test_translation_preview_loads_immutable_checkpoint_for_open_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-api",
        model_id="gemini-test",
        provider_options={"credential_references": ["google-project-a"]},
    )
    job_id = str(created["id"])
    staging = service.checkpoints.staging_root / job_id / "ledger.json.partial"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "signature": "preview-ledger",
                "provider_id": "gemini-api",
                "model_id": "gemini-test",
                "film_bible": {
                    "title": "Phim",
                    "profile": "xianxia",
                    "synopsis": "",
                    "characters": [],
                    "terms": [],
                },
                "chunks": [
                    {
                        "chunk_index": 0,
                        "cue_ids": [1, 2],
                        "state": "pending",
                        "attempts": 0,
                        "response_fingerprint": "",
                        "qa_issues": [],
                    }
                ],
                "translations": {"1": "Ta muốn tu luyện."},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service.commit_worker_checkpoint(
        job_id,
        {
            "stage_key": "translation",
            "checkpoint_key": "latest",
            "schema_version": 1,
            "payload": {"completed_cues": 1, "next_chunk": 0},
            "artifact_path": str(staging),
            "artifact_kind": "translation_ledger",
            "artifact_state": "technical",
        },
        event_id="translation-preview-checkpoint",
    )

    preview = service.get_translation_preview(project_id)
    source_cue = service.projects.load(project_id).source_cues[0]

    assert preview["job_id"] == job_id
    assert preview["completed_cues"] == 1
    assert preview["values"] == [
        {"id": source_cue.id, "index": 1, "text": "Ta muốn tu luyện."}
    ]

    original_get = service.jobs.get
    lookups = []

    def counted_get(identifier: str):
        lookups.append(identifier)
        return original_get(identifier)

    monkeypatch.setattr(service.jobs, "get", counted_get)
    selected_preview = service.get_translation_preview(project_id, job_id)
    assert selected_preview["values"] == preview["values"]
    assert lookups == [job_id]


def test_translation_cache_list_and_default_preview_prefer_the_most_complete_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)

    def make_cache(
        *,
        translated: dict[str, str],
        terminal: bool,
        valid_ledger: bool = True,
        malformed_issue: bool = False,
        existing_job_id: str = "",
    ) -> str:
        if existing_job_id:
            job_id = existing_job_id
        else:
            created = service.create_translation_job(
                project_id=project_id,
                provider_id="gemini-web",
                model_id="selected-on-web",
                provider_options={"profile_path": str(tmp_path / "gemini-profile")},
            )
            job_id = str(created["id"])
        staging = service.checkpoints.staging_root / job_id / "ledger.json.partial"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "signature": f"cache-{job_id}",
                    "provider_id": "gemini-web",
                    "model_id": "selected-on-web",
                    "film_bible": {
                        "title": "Phim",
                        "profile": "xianxia",
                        "synopsis": "",
                        "characters": [],
                        "terms": [],
                    },
                    "chunks": [
                        {
                            "chunk_index": 0,
                            "cue_ids": [1, 2],
                            "state": "pending",
                            "attempts": 0,
                            "response_fingerprint": "",
                            "qa_issues": (
                                [
                                    {
                                        "code": "broken",
                                        "severity": "blocker",
                                        "message": "Thiếu ID",
                                    }
                                ]
                                if malformed_issue
                                else []
                            ),
                        }
                    ],
                    "translations": translated,
                },
                ensure_ascii=False,
            )
            if valid_ledger
            else "ledger bị hỏng",
            encoding="utf-8",
        )
        service.commit_worker_checkpoint(
            job_id,
            {
                "stage_key": "translation",
                "checkpoint_key": "latest",
                "schema_version": 1,
                "payload": {"completed_cues": len(translated), "next_chunk": 0},
                "artifact_path": str(staging),
                "artifact_kind": "translation_ledger",
                "artifact_state": "technical",
            },
            event_id=f"checkpoint-{job_id}",
        )
        if terminal:
            service.jobs.transition(job_id, JobState.START_REQUESTED)
            service.jobs.transition(job_id, JobState.RUNNING)
            service.jobs.transition(job_id, JobState.FAILED)
        return job_id

    complete_id = make_cache(
        translated={"1": "Ta muốn tu luyện.", "2": "Sau đó sẽ thành tiên."},
        terminal=True,
    )
    corrupt_id = make_cache(translated={}, terminal=True, valid_ledger=False)
    malformed_schema_id = make_cache(
        translated={"1": "Cache schema hỏng."},
        terminal=True,
        malformed_issue=True,
    )
    wrong_record = service.jobs.create(
        replace(
            service.jobs.get(complete_id).spec,
            input_fingerprint="different-source-cue-fingerprint",
        )
    )
    wrong_fingerprint_id = make_cache(
        translated={"1": "Không được xuất hiện."},
        terminal=True,
        existing_job_id=wrong_record.id,
    )
    partial_id = make_cache(translated={"1": "Bản mới chưa hoàn tất."}, terminal=False)

    # Listing ledgers needs immutable identity, not the source cue payload.
    # The selected preview/worker still obtains its full validated input.
    with monkeypatch.context() as patch:
        patch.setattr(service.jobs, "get", lambda *_args, **_kwargs:
                      (_ for _ in ()).throw(AssertionError("Selector decoded full job input")))
        caches = service.list_translation_caches(project_id)
    preview = service.get_translation_preview(project_id)

    assert [item["job_id"] for item in caches] == [complete_id, partial_id]
    listed_ids = {item["job_id"] for item in caches}
    assert corrupt_id not in listed_ids
    assert malformed_schema_id not in listed_ids
    assert wrong_fingerprint_id not in listed_ids
    assert caches[0]["completed_cues"] == caches[0]["cue_total"] == 2
    assert preview["job_id"] == complete_id
    assert [item["text"] for item in preview["values"]] == [
        "Ta muốn tu luyện.",
        "Sau đó sẽ thành tiên.",
    ]


def test_create_translation_job_rejects_raw_key_and_unknown_options(tmp_path: Path) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)

    with pytest.raises(ValueError, match="Unknown translation provider options"):
        service.create_translation_job(
            project_id=project_id,
            provider_id="gemini-api",
            model_id="gemini-test",
            provider_options={
                "credential_references": ["safe-ref"],
                "api_key": "must-never-persist",
            },
        )


def test_credential_service_rejects_secret_shaped_reference_before_storage() -> None:
    service = object.__new__(JobManagerService)
    leaked_reference = "AIza" + "A" * 34

    with pytest.raises(ValueError) as captured:
        service.store_credentials(
            credentials=[{"reference": leaked_reference, "secret": "stored-secret"}]
        )

    assert leaked_reference not in str(captured.value)


def test_credential_service_lists_only_named_vault_references(monkeypatch) -> None:
    class FakeCredentialStore:
        def list_references(self) -> tuple[str, ...]:
            return ("tqboyvn", "gemini-1")

    monkeypatch.setattr(service_module, "WindowsCredentialStore", FakeCredentialStore)

    service = object.__new__(JobManagerService)

    assert service.list_credentials() == {"references": ["tqboyvn", "gemini-1"]}


def test_retry_api_translation_refreshes_key_pool_without_invalidating_checkpoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeCredentialStore:
        def list_references(self) -> tuple[str, ...]:
            return ("tqboyvn", "gemini-1")

    monkeypatch.setattr(service_module, "WindowsCredentialStore", FakeCredentialStore)
    QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-api",
        model_id="gemini-test",
        provider_options={"credential_references": ["gemini-1"]},
    )
    job_id = str(created["id"])
    original = service.jobs.get(job_id)
    service.checkpoints.commit(
        job_id=job_id,
        stage_key="translation",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=original.spec.resume_fingerprint,
        cursor_us=513_800_000,
        payload={"completed_cues": 237, "next_chunk": 12},
        event_id="checkpoint-before-key-refresh",
    )
    service.transition_job(job_id, JobState.START_REQUESTED.value)
    service.transition_job(job_id, JobState.RUNNING.value)
    service.jobs.transition(job_id, JobState.FAILED, error_code="quota_exhausted")
    server = JobManagerServer(name=f"translation-retry-{uuid4()}", service=service)

    retried = server._dispatch(
        "retry_job",
        {
            "job_id": job_id,
            "credential_references": ["gemini-1"],
        },
    )

    refreshed = service.jobs.get(job_id)
    checkpoint = service.load_worker_checkpoint(
        job_id,
        stage_key="translation",
        checkpoint_key="latest",
    )
    assert retried["state"] == "queued"
    assert refreshed.spec.provider_config["credential_references"] == ("gemini-1",)
    assert refreshed.spec.resume_fingerprint == original.spec.resume_fingerprint
    assert checkpoint is not None
    assert checkpoint["cursor_us"] == 513_800_000


def test_retry_web_translation_refreshes_guest_transport_without_losing_checkpoint(
    tmp_path: Path,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-on-web",
        provider_options={
            "profile_path": str(tmp_path / "gemini-profile"),
            "headed": False,
            "guest": True,
            "max_chunk_cues": 24,
            "max_chunk_source_chars": 1_800,
        },
    )
    job_id = str(created["id"])
    original = service.jobs.get(job_id)
    service.checkpoints.commit(
        job_id=job_id,
        stage_key="translation",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=original.spec.resume_fingerprint,
        cursor_us=5_000_000,
        payload={"completed_cues": 1, "next_chunk": 0},
        event_id="checkpoint-before-web-refresh",
    )
    service.transition_job(job_id, JobState.START_REQUESTED.value)
    service.transition_job(job_id, JobState.RUNNING.value)
    service.jobs.transition(job_id, JobState.INTERRUPTED)
    server = JobManagerServer(name=f"translation-retry-{uuid4()}", service=service)

    retried = server._dispatch(
        "retry_job",
        {
            "job_id": job_id,
            "provider_options": {
                "headed": True,
                "guest": True,
                "max_chunk_cues": 150,
                "max_chunk_source_chars": 8_000,
                "web_max_requests_per_session": 5,
                "codex_fallback_enabled": False,
                "qa_mode": "integrated",
            },
        },
    )

    refreshed = service.jobs.get(job_id)
    checkpoint = service.load_worker_checkpoint(
        job_id,
        stage_key="translation",
        checkpoint_key="latest",
    )
    assert retried["state"] == "queued"
    assert refreshed.spec.provider_config["headed"] is True
    assert refreshed.spec.provider_config["guest"] is True
    assert refreshed.spec.provider_config["max_chunk_cues"] == 150
    assert refreshed.spec.provider_config["max_chunk_source_chars"] == 8_000
    assert refreshed.spec.provider_config["web_max_requests_per_session"] == 5
    assert refreshed.spec.provider_config["codex_fallback_enabled"] is False
    assert refreshed.spec.provider_config["qa_mode"] == "integrated"
    assert refreshed.spec.resume_fingerprint == original.spec.resume_fingerprint
    assert checkpoint is not None
    assert checkpoint["cursor_us"] == 5_000_000


def test_retry_cancelled_web_translation_requeues_same_job_and_checkpoint(
    tmp_path: Path,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-on-web",
        provider_options={
            "profile_path": str(tmp_path / "gemini-profile"),
            "headed": False,
            "guest": True,
        },
    )
    job_id = str(created["id"])
    original = service.jobs.get(job_id)
    service.checkpoints.commit(
        job_id=job_id,
        stage_key="translation",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=original.spec.resume_fingerprint,
        cursor_us=513_800_000,
        payload={"completed_cues": 237, "next_chunk": 12},
        event_id="checkpoint-before-cancel",
    )
    service.jobs.transition(job_id, JobState.START_REQUESTED)
    service.jobs.transition(job_id, JobState.RUNNING)
    service.jobs.transition(job_id, JobState.CANCEL_REQUESTED)
    service.jobs.transition(job_id, JobState.CANCELLED)
    server = JobManagerServer(name=f"translation-cancelled-retry-{uuid4()}", service=service)

    retried = server._dispatch(
        "retry_job",
        {
            "job_id": job_id,
            "provider_options": {
                "headed": True,
                "guest": True,
                "qa_mode": "integrated",
            },
        },
    )

    refreshed = service.jobs.get(job_id)
    checkpoint = service.load_worker_checkpoint(
        job_id,
        stage_key="translation",
        checkpoint_key="latest",
    )
    assert retried["id"] == job_id
    assert retried["state"] == "queued"
    assert refreshed.spec.resume_fingerprint == original.spec.resume_fingerprint
    assert checkpoint is not None
    assert checkpoint["cursor_us"] == 513_800_000


def test_retry_cancelled_api_translation_refreshes_keys_without_rebinding_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class EmptyCredentialStore:
        def list_references(self) -> tuple[str, ...]:
            return ()

    monkeypatch.setattr(service_module, "WindowsCredentialStore", EmptyCredentialStore)
    QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-api",
        model_id="gemini-test",
        provider_options={"credential_references": ["gemini-old"]},
    )
    job_id = str(created["id"])
    original = service.jobs.get(job_id)
    service.checkpoints.commit(
        job_id=job_id,
        stage_key="translation",
        checkpoint_key="latest",
        schema_version=1,
        resume_fingerprint=original.spec.resume_fingerprint,
        cursor_us=400_000_000,
        payload={"completed_cues": 180, "next_chunk": 9},
        event_id="api-checkpoint-before-cancel",
    )
    service.jobs.transition(job_id, JobState.START_REQUESTED)
    service.jobs.transition(job_id, JobState.RUNNING)
    service.jobs.transition(job_id, JobState.CANCEL_REQUESTED)
    service.jobs.transition(job_id, JobState.CANCELLED)
    server = JobManagerServer(name=f"api-cancelled-retry-{uuid4()}", service=service)

    retried = server._dispatch(
        "retry_job",
        {
            "job_id": job_id,
            "credential_references": ["gemini-new", "gemini-fallback"],
        },
    )

    refreshed = service.jobs.get(job_id)
    checkpoint = service.load_worker_checkpoint(
        job_id,
        stage_key="translation",
        checkpoint_key="latest",
    )
    assert retried["id"] == job_id
    assert retried["state"] == "queued"
    assert refreshed.spec.provider_config["credential_references"] == (
        "gemini-new",
        "gemini-fallback",
    )
    assert refreshed.spec.resume_fingerprint == original.spec.resume_fingerprint
    assert checkpoint is not None
    assert checkpoint["cursor_us"] == 400_000_000


def test_external_vietnamese_srt_import_is_atomic_and_preserves_source_contract(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    translated = tmp_path / "translated-outside.srt"
    translated.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nTa muốn tu luyện.\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\nSau đó sẽ thành tiên.\n",
        encoding="utf-8",
    )

    imported = service.import_translation_srt(
        project_id=project_id,
        subtitle_path=str(translated),
    )

    assert [cue["text"] for cue in imported["translated_cues"]] == [
        "Ta muốn tu luyện.",
        "Sau đó sẽ thành tiên.",
    ]
    assert [cue["id"] for cue in imported["translated_cues"]] == [
        cue["id"] for cue in imported["source_cues"]
    ]

    invalid = tmp_path / "retimed-outside.srt"
    invalid.write_text(
        "1\n00:00:00,100 --> 00:00:01,000\nSai timing.\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\nCâu hai.\n",
        encoding="utf-8",
    )
    revision_before = service.projects.load(project_id).revision

    with pytest.raises(ValueError, match="timing"):
        service.import_translation_srt(
            project_id=project_id,
            subtitle_path=str(invalid),
        )

    assert service.projects.load(project_id).revision == revision_before


def test_external_vietnamese_srt_import_rejects_an_active_translation_job(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    translated = tmp_path / "translated-while-running.srt"
    translated.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nCâu một.\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\nCâu hai.\n",
        encoding="utf-8",
    )
    service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-by-web",
        provider_options={"profile_path": str(tmp_path / "gemini-profile")},
    )
    revision_before = service.projects.load(project_id).revision

    with pytest.raises(ValueError, match="active"):
        service.import_translation_srt(
            project_id=project_id,
            subtitle_path=str(translated),
        )

    assert service.projects.load(project_id).revision == revision_before


def test_final_translation_import_requires_identical_count_and_timeline(
    tmp_path: Path,
) -> None:
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-by-web",
        provider_options={"profile_path": str(tmp_path / "gemini-profile")},
    )
    job_id = str(created["id"])
    job = service.jobs.get(job_id)
    output = Path(str(job.spec.provider_config["output_path"]))
    snapshot = service.projects.load(project_id)
    translated = tuple(
        SubtitleCue(
            cue.id,
            cue.index,
            cue.range,
            "Ta sẽ bắt đầu tu luyện." if cue.index == 1 else "Sau đó sẽ thành tiên.",
            "vi",
        )
        for cue in snapshot.source_cues
    )
    output.write_text(render_srt(translated), encoding="utf-8-sig")

    imported = service.import_final_translation_srt(job_id, str(output))

    assert len(imported["translated_cues"]) == 2
    assert imported["translated_cues"][0]["id"] == imported["source_cues"][0]["id"]
    assert imported["translated_cues"][1]["range"] == imported["source_cues"][1]["range"]


def _running_translation_job(
    service: JobManagerService, project_id: str, tmp_path: Path
) -> tuple[str, Path]:
    created = service.create_translation_job(
        project_id=project_id,
        provider_id="gemini-web",
        model_id="selected-by-web",
        provider_options={"profile_path": str(tmp_path / "gemini-profile")},
    )
    job_id = str(created["id"])
    service.transition_job(job_id, "start_requested")
    service.transition_job(job_id, "running")
    output = Path(str(service.jobs.get(job_id).spec.provider_config["output_path"]))
    return job_id, output


def test_server_pass_event_auto_imports_translation_without_ui_callback(
    tmp_path: Path,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    job_id, output = _running_translation_job(service, project_id, tmp_path)
    snapshot = service.projects.load(project_id)
    translated = tuple(
        SubtitleCue(
            cue.id, cue.index, cue.range, f"Bản dịch {cue.index}.", "vi"
        )
        for cue in snapshot.source_cues
    )
    output.write_text(render_srt(translated), encoding="utf-8-sig")
    server = JobManagerServer(name=f"translation-{uuid4()}", service=service)

    server._persist_worker_event(
        job_id,
        WorkerMessage(
            PROTOCOL_VERSION,
            job_id,
            str(uuid4()),
            "succeeded",
            {"status": "PASS", "output_path": str(output), "cue_count": 2},
        ),
    )

    assert service.jobs.get(job_id).state.value == "succeeded"
    assert [cue.text for cue in service.projects.load(project_id).translated_cues] == [
        "Bản dịch 1.",
        "Bản dịch 2.",
    ]


def test_server_review_event_imports_technical_srt_but_does_not_false_pass(
    tmp_path: Path,
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=StaticProbe(),
    )
    project_id, _source = _project(service, tmp_path)
    job_id, output = _running_translation_job(service, project_id, tmp_path)
    snapshot = service.projects.load(project_id)
    review = service.checkpoints.staging_root / "translation-review.srt.partial"
    translated = tuple(
        SubtitleCue(
            cue.id, cue.index, cue.range, f"Cần kiểm tra {cue.index}.", "vi"
        )
        for cue in snapshot.source_cues
    )
    review.write_text(render_srt(translated), encoding="utf-8-sig")
    server = JobManagerServer(name=f"translation-{uuid4()}", service=service)
    checkpoint_id = str(uuid4())
    server._persist_worker_event(
        job_id,
        WorkerMessage(
            PROTOCOL_VERSION,
            job_id,
            checkpoint_id,
            "checkpoint",
            {
                "stage_key": "translation",
                "checkpoint_key": "review",
                "schema_version": 1,
                "payload": {"stage": "review"},
                "artifact_path": str(review),
                "artifact_kind": "translation_review_srt",
                "artifact_state": "technical",
            },
        ),
    )
    server._persist_worker_event(
        job_id,
        WorkerMessage(
            PROTOCOL_VERSION,
            job_id,
            str(uuid4()),
            "failed",
            {"code": "translation_review_required", "status": "REVIEW"},
        ),
    )

    loaded = service.projects.load(project_id)
    assert service.jobs.get(job_id).state.value == "failed"
    assert all(cue.source == "translation_review" for cue in loaded.translated_cues)
    assert not output.exists()
