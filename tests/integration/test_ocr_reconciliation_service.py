import hashlib
import json

import pytest

from ducai_studio.domain.jobs import JobKind, JobSpec
from ducai_studio.domain.project import ProjectSnapshot
from ducai_studio.job_manager.service import JobManagerService


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("has_proof", [False, True])
def test_review_import_uses_declared_proof_policy_and_preserves_legacy_compatibility(
    tmp_path, strict, has_proof,
):
    database_path = tmp_path / "state.db"
    artifact_root = tmp_path / "artifacts"
    service = JobManagerService(database_path=database_path, artifact_root=artifact_root)
    snapshot = ProjectSnapshot("review", "Review", 1)
    service.projects.save(project_path=tmp_path / "project.ducai", snapshot=snapshot)
    job = service.jobs.create(JobSpec(
        JobKind.OCR, snapshot.id, snapshot.revision, "paddle-fast", "1", "model",
        "input", "settings", {},
    ))
    review = service.checkpoints.staging_root / "review.srt.partial"
    full_text = "这里是散修们聚集的一处秘境"
    partial_text = "这里是散修"
    review.write_text(
        f"1\n00:00:00,000 --> 00:00:02,000\n{full_text}\n\n"
        f"2\n00:00:00,000 --> 00:00:02,000\n{partial_text}\n",
        encoding="utf-8-sig",
    )
    qa = {
        "schema_version": 3,
        "kind": "ocr_hard_sub_coverage",
        "status": "REVIEW",
        "job_id": job.id,
        "input_fingerprint": job.spec.input_fingerprint,
        "settings_fingerprint": job.spec.settings_fingerprint,
        "output_sha256": hashlib.sha256(review.read_bytes()).hexdigest(),
        "blockers": ["cue_overlap"],
        "cue_regions": [
            {"index": index, "start_us": 0, "end_us": 2_000_000,
             "box": box, "confidence": confidence, "evidence_ids": [key]}
            for index, box, confidence, key in (
                (1, [0.3, 0.9, 0.4, 0.05], 0.99, "full"),
                (2, [0.4, 0.9, 0.2, 0.05], 0.88, "partial"),
            )
        ],
    }
    if strict:
        qa["proof_only_collapse"] = True
    if has_proof:
        qa["suppressed_readings"] = [{
            "evidence_id": "partial", "winner_evidence_id": "full",
            "reason": "same_frame_crop_cut",
        }]
    qa_path = service.checkpoints.staging_root / "review_QA.json.partial"
    qa_path.write_text(json.dumps(qa, ensure_ascii=False), encoding="utf-8")
    for key, path, kind in (
        ("review", review, "ocr_review_srt"),
        ("review-qa", qa_path, "ocr_qa_json"),
    ):
        service.checkpoints.commit(
            job_id=job.id, stage_key="ocr", checkpoint_key=key, schema_version=1,
            resume_fingerprint=job.spec.resume_fingerprint, cursor_us=2_000_000,
            payload={"stage": "review"}, artifact_path=path, artifact_kind=kind,
        )

    imported = service.import_review_ocr_srt(
        job.id,
        service.load_worker_checkpoint(job.id, stage_key="ocr", checkpoint_key="review"),
        service.load_worker_checkpoint(job.id, stage_key="ocr", checkpoint_key="review-qa"),
    )

    expected_texts = [full_text, partial_text] if strict and not has_proof else [full_text]
    assert [cue["text"] for cue in imported["source_cues"]] == expected_texts
    saved = service.projects.load(snapshot.id)
    reopened = JobManagerService(database_path=database_path, artifact_root=artifact_root)
    assert reopened.projects.load(snapshot.id) == saved
    if has_proof:
        assert saved.source_cues[0].evidence_ids == ("full",)
    clips = [clip for track in saved.tracks for clip in track.clips]
    assert len(clips) == len(expected_texts)
    assert {clip.cue_id for clip in clips} == {cue.id for cue in saved.source_cues}
