from __future__ import annotations

import os
from pathlib import Path

import pytest

from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.media_probe import MediaProbeResult
from ducai_studio.providers.tts import PiperTtsProvider, discover_data_root
from ducai_studio.workers.tts_worker import TtsWorker


class VideoProbe:
    def probe(self, _media_path: str | Path) -> MediaProbeResult:
        return MediaProbeResult(5_000_000, True, True, 1280, 720, "mp4")


@pytest.mark.skipif(
    os.environ.get("DUCAI_RUN_REAL_TTS") != "1",
    reason="Set DUCAI_RUN_REAL_TTS=1 to exercise the installed Piper runtime.",
)
def test_real_piper_preview_runs_through_worker_and_final_master(
    tmp_path: Path, monkeypatch
) -> None:
    provider = PiperTtsProvider()
    if provider.health().state.value != "ready":
        pytest.skip(provider.health().summary)
    provider.close()
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        media_probe=VideoProbe(),
    )
    video = tmp_path / "film.mp4"
    video.write_bytes(b"fixture")
    project = service.create_project(
        project_path=str(tmp_path / "project.ducai"), name="Real Piper Preview"
    )
    service.import_media(project_id=project["id"], media_path=str(video))
    job = service.create_tts_job(
        project_id=project["id"],
        provider_id="piper",
        voice_id="nghitts-chieu-thanh",
        provider_options={
            "purpose": "preview",
            "data_root": str(discover_data_root()),
            "preview_text": "Đây là phép thử Piper thật của DUCAI Studio hai.",
        },
    )
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = TtsWorker(job["id"])
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure({"job": job["spec"]})

    worker._execute()

    output = Path(job["spec"]["provider_config"]["output_path"])
    assert output.is_file()
    assert output.stat().st_size > 100_000
    assert events[-1][0] == "succeeded"
    assert events[-1][1]["purpose"] == "preview"
