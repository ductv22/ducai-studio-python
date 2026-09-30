from __future__ import annotations

import json
import shutil
import subprocess
import sys
import wave
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest

from ducai_studio.application.voice_pipeline import (
    VoicePipelineSettings,
    classify_cue,
    file_sha256,
    read_pcm16_mono,
    write_pcm16_mono,
)
from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.domain.voice_conform import (
    VoiceConformCueInput,
    VoiceConformSettings,
    VoiceTimingMode,
    build_voice_conform_plan,
)
from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)
from ducai_studio.providers.tts.contracts import (
    TtsBatchResult,
    TtsCueArtifact,
    TtsRuntimeUnavailable,
)
from ducai_studio.workers.tts_worker import TtsWorker, _tts_failure_payload


def test_command_reader_allows_clean_exit_while_manager_stdin_stays_open():
    code = (
        "import sys,time; from ducai_studio.workers.tts_worker import TtsWorker; "
        "w=TtsWorker('exit-check'); w._reader.start(); time.sleep(.1); print('ready',flush=True)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        # Do not communicate(): closing stdin would hide the shutdown regression.
        assert process.wait(timeout=8) == 0, process.stderr.read().decode("utf-8", "replace")
        assert process.stdout.read().strip() == b"ready"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


class DeterministicTtsProvider:
    provider_id = "fixture-tts"
    model_id = "fixture-v1"
    kind = ProviderKind.VOICE

    def __init__(self, *, duration_samples: int = 12_000) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.speeds: list[float] = []
        self.duration_samples = duration_samples

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities({"deterministic": True})

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, HealthState.READY, "now", "ready")

    def preview(self, **_kwargs):
        raise NotImplementedError

    def synthesize_batch(self, *, request_id, cues, output_dir, **_kwargs) -> TtsBatchResult:
        self.calls.append(tuple(cue.cue_id for cue in cues))
        self.speeds.append(float(_kwargs.get("speed", 1.0)))
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = []
        for cue in cues:
            path = output_dir / f"{cue.index:06d}-{cue.request_fingerprint[:16]}.wav"
            samples = [0.16 * ((index % 80) / 80 - 0.5) for index in range(self.duration_samples)]
            from array import array

            write_pcm16_mono(path, array("f", samples), 48_000)
            artifacts.append(
                TtsCueArtifact(
                    cue.cue_id,
                    cue.request_fingerprint,
                    path,
                    48_000,
                    250_000,
                    "fixture",
                )
            )
        return TtsBatchResult(request_id, tuple(artifacts))

    def close(self) -> None:
        return None


class CopyAudioProcessor:
    def time_stretch(self, source: Path, target: Path, **_kwargs) -> None:
        shutil.copyfile(source, target)

    def master_ebu_r128(self, source: Path, target: Path, **_kwargs) -> dict:
        shutil.copyfile(source, target)
        return {"input_i": -18.0, "target_offset": 0.0}


class StreamingMeasureSession:
    def __init__(self, duration_us: int, progress) -> None:
        self.duration_us = duration_us
        self.progress = progress
        self.pcm = bytearray()
        self.aborted = False
        self.progress("measure", 0, duration_us, None)

    def write(self, raw: bytes) -> None:
        self.pcm.extend(raw)

    def finish(self) -> dict:
        self.progress("measure", self.duration_us, self.duration_us, 0)
        return {"input_i": -21.0, "target_offset": -0.1}

    def abort(self) -> None:
        self.aborted = True


class StreamingCopyAudioProcessor(CopyAudioProcessor):
    def __init__(self) -> None:
        self.session: StreamingMeasureSession | None = None
        self.applied_measurement: dict | None = None

    def open_pcm16_ebu_r128_measure(
        self, *, duration_us: int, progress, **_kwargs
    ) -> StreamingMeasureSession:
        self.session = StreamingMeasureSession(duration_us, progress)
        return self.session

    def apply_ebu_r128(
        self, source: Path, target: Path, *, measured: dict, progress, **_kwargs
    ) -> dict:
        assert self.session is not None
        with wave.open(str(source), "rb") as stream:
            assert stream.readframes(stream.getnframes()) == bytes(self.session.pcm)
            duration_us = round(stream.getnframes() * 1_000_000 / stream.getframerate())
        self.applied_measurement = dict(measured)
        progress("apply", 0, duration_us, None)
        shutil.copyfile(source, target)
        progress("apply", duration_us, duration_us, 0)
        return measured


class DeterministicTimingAudioProcessor(CopyAudioProcessor):
    def time_stretch(self, source: Path, target: Path, *, speed: float, **_kwargs) -> None:
        samples, sample_rate = read_pcm16_mono(source)
        output_length = max(1, round(len(samples) / speed))
        from array import array

        stretched = array(
            "f",
            (
                samples[min(len(samples) - 1, int(index * speed))]
                for index in range(output_length)
            ),
        )
        write_pcm16_mono(target, stretched, sample_rate)


class NoStretchAudioProcessor(CopyAudioProcessor):
    def time_stretch(self, *_args, **_kwargs) -> None:
        raise AssertionError("An approved strict clip must not be post-processed again")


def test_tts_worker_uses_checkpoint_compatible_runtime_engine_override(
    tmp_path: Path, monkeypatch
) -> None:
    configured_host = tmp_path / "old-release" / "engine_hosts" / "tts_engine_host.py"
    runtime_host = tmp_path / "current-release" / "engine_hosts" / "tts_engine_host.py"
    runtime_host.parent.mkdir(parents=True)
    runtime_host.write_text("current tts host", encoding="utf-8")
    profile = tmp_path / "voice-profile.npz"
    profile.write_bytes(b"profile")
    monkeypatch.setenv("DUCAI_TTS_ENGINE_HOST_PATH", str(runtime_host))
    monkeypatch.setenv(
        "DUCAI_TTS_ENGINE_HOST_SHA256", sha256(runtime_host.read_bytes()).hexdigest()
    )
    monkeypatch.setenv("DUCAI_TTS_ENGINE_HOST_MANAGED", "1")
    worker = TtsWorker("tts-runtime-host")
    worker.spec = {"provider_id": "vieneu-3.8"}
    worker.config = {
        "runtime_python": sys.executable,
        "data_root": str(tmp_path),
        "engine_host_path": str(configured_host),
        "profile_path": str(profile),
        "batch_size": 32,
    }

    provider = worker._provider_from_config()

    assert provider.host_script == runtime_host.resolve()
    assert worker.config["engine_host_path"] == str(configured_host)


def test_tts_worker_rejects_changed_runtime_engine_host(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_host = tmp_path / "current-release" / "engine_hosts" / "tts_engine_host.py"
    runtime_host.parent.mkdir(parents=True)
    runtime_host.write_text("changed tts host", encoding="utf-8")
    monkeypatch.setenv("DUCAI_TTS_ENGINE_HOST_PATH", str(runtime_host))
    monkeypatch.setenv("DUCAI_TTS_ENGINE_HOST_SHA256", "0" * 64)
    monkeypatch.setenv("DUCAI_TTS_ENGINE_HOST_MANAGED", "1")
    worker = TtsWorker("tts-runtime-host-hash")
    worker.spec = {"provider_id": "vieneu-3.8"}
    worker.config = {
        "runtime_python": sys.executable,
        "data_root": str(tmp_path),
        "engine_host_path": str(runtime_host),
        "profile_path": str(tmp_path / "voice-profile.npz"),
    }

    with pytest.raises(TtsRuntimeUnavailable, match="không khớp manifest runtime"):
        worker._provider_from_config()


def test_tts_worker_ignores_inherited_runtime_override_for_custom_host(
    tmp_path: Path, monkeypatch
) -> None:
    custom_host = tmp_path / "custom" / "tts_engine_host.py"
    custom_host.parent.mkdir(parents=True)
    custom_host.write_text("custom tts host", encoding="utf-8")
    inherited_host = tmp_path / "stale-system" / "tts_engine_host.py"
    inherited_host.parent.mkdir(parents=True)
    inherited_host.write_text("stale system host", encoding="utf-8")
    monkeypatch.setenv("DUCAI_TTS_ENGINE_HOST_PATH", str(inherited_host))
    monkeypatch.setenv(
        "DUCAI_TTS_ENGINE_HOST_SHA256", sha256(inherited_host.read_bytes()).hexdigest()
    )
    monkeypatch.setenv("DUCAI_TTS_ENGINE_HOST_MANAGED", "0")
    worker = TtsWorker("tts-custom-runtime-host")
    worker.spec = {"provider_id": "vieneu-3.8"}
    worker.config = {
        "runtime_python": sys.executable,
        "data_root": str(tmp_path),
        "engine_host_path": str(custom_host),
        "profile_path": str(tmp_path / "voice-profile.npz"),
    }

    provider = worker._provider_from_config()

    assert provider.host_script == custom_host.resolve()


def test_tts_runtime_failure_payload_is_actionable_vietnamese() -> None:
    payload = _tts_failure_payload(
        TtsRuntimeUnavailable("Không tìm thấy bộ điều phối TTS tại bản cũ.")
    )

    assert payload["code"] == "TtsRuntimeUnavailable"
    assert payload["message"] == "Không tìm thấy bộ điều phối TTS tại bản cũ."
    assert "bản DUCAI mới nhất" in str(payload["action"])
    assert "cache/checkpoint" in str(payload["action"])


def _config(
    tmp_path: Path,
    *,
    force_cue_ids: list[str] | None = None,
    pipeline_settings: dict | None = None,
    media_duration_us: int = 2_000_000,
) -> dict:
    return {
        "purpose": "production",
        "cues": [
            {
                "id": "cue-1",
                "index": 1,
                "start_us": 0,
                "end_us": 800_000,
                "text": "Ta sẽ trở về tông môn.",
            },
            {
                "id": "cue-2",
                "index": 2,
                "start_us": 1_000_000,
                "end_us": 1_800_000,
                "text": "Được, chúng ta lên đường.",
            },
        ],
        "media_duration_us": media_duration_us,
        "output_path": str(tmp_path / "film_voice_vi.wav"),
        "qa_path": str(tmp_path / "film_voice_vi_QA.json"),
        "cache_root": str(tmp_path / "cache"),
        "data_root": str(tmp_path),
        "voice_id": "fixture",
        "voice_identity": "fixture-v1",
        "engine_speed": 1.0,
        "batch_size": 2,
        "target_lufs": -18.0,
        "pipeline_settings": dict(pipeline_settings or {}),
        "force_cue_ids": list(force_cue_ids or []),
    }


def _worker(
    tmp_path: Path,
    monkeypatch,
    provider: DeterministicTtsProvider,
    *,
    audio_processor_factory=CopyAudioProcessor,
):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = TtsWorker(
        "tts-fixture",
        provider_factory=lambda _config: provider,
        audio_processor_factory=audio_processor_factory,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": provider.model_id,
                "provider_config": _config(tmp_path),
            }
        }
    )
    return worker, events


def test_worker_overlaps_timeline_assembly_with_exact_pcm_measurement(
    tmp_path, monkeypatch
) -> None:
    provider = DeterministicTtsProvider()
    processor = StreamingCopyAudioProcessor()
    worker, events = _worker(
        tmp_path,
        monkeypatch,
        provider,
        audio_processor_factory=lambda: processor,
    )

    worker._execute()

    phases = [payload.get("phase") for kind, payload in events if kind == "progress"]
    assert "timeline_assembly_measure" in phases
    assert "final_master_apply" in phases
    assert "final_master_measure" not in phases
    assert processor.session is not None and processor.session.aborted is True
    assert processor.applied_measurement == {"input_i": -21.0, "target_offset": -0.1}


def test_worker_generates_every_eligible_cue_and_final_wav(tmp_path, monkeypatch) -> None:
    provider = DeterministicTtsProvider()
    worker, events = _worker(tmp_path, monkeypatch, provider)

    worker._execute()

    assert provider.calls == [("cue-1", "cue-2")]
    assert (tmp_path / "film_voice_vi.wav").is_file()
    assert events[-1][0] == "succeeded"
    assert events[-1][1]["coverage_ratio"] == 1.0


def test_worker_announces_synthesis_before_provider_starts(tmp_path, monkeypatch) -> None:
    provider = DeterministicTtsProvider()
    worker, events = _worker(tmp_path, monkeypatch, provider)
    original_synthesize = provider.synthesize_batch
    observed_latest_events: list[tuple[str, dict]] = []

    def observed_synthesize(**kwargs):
        observed_latest_events.append(events[-1])
        return original_synthesize(**kwargs)

    provider.synthesize_batch = observed_synthesize  # type: ignore[method-assign]

    worker._execute()

    assert observed_latest_events
    kind, payload = observed_latest_events[0]
    assert kind == "progress"
    assert payload["phase"] == "synthesis"
    assert payload["phase_current"] == 0
    assert payload["eta_ms"] is None


def test_large_cache_scan_reports_elapsed_cadence_and_exact_final_count(tmp_path, monkeypatch):
    from ducai_studio.workers import tts_worker as module

    provider = DeterministicTtsProvider()
    worker, events = _worker(tmp_path, monkeypatch, provider)
    worker.cues = tuple(
        replace(worker.cues[0], cue_id=f"cue-{i}", index=i) for i in range(1, 8588)
    )
    ticks = iter(i / 1000 for i in range(100_000))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(worker, "_restore_global_cache", lambda cue: True)
    monkeypatch.setattr(worker, "_checkpoint", lambda: None)
    monkeypatch.setattr(worker, "_publish_result", lambda: None)

    worker._execute()

    updates = [p for k, p in events if k == "progress" and p["phase"] == "cache_restore"]
    assert 2 <= len(updates) <= 12
    assert updates[0]["phase_current"] == 0
    assert updates[-1]["phase_current"] == updates[-1]["phase_total"] == 8587
    assert provider.calls == []


def test_provider_stage_progress_keeps_real_cue_count(tmp_path, monkeypatch):
    worker, events = _worker(tmp_path, monkeypatch, DeterministicTtsProvider())
    worker._provider_progress("model_loading", "Đang nạp VieNeu lên GPU")
    kind, payload = events[-1]
    assert kind == "progress"
    assert payload["phase"] == "model_loading"
    assert payload["phase_detail"] == "Đang nạp VieNeu lên GPU"
    assert payload["current"] == 0
    assert payload["eta_ms"] is None


def test_worker_resumes_approved_cache_without_provider_calls(tmp_path, monkeypatch) -> None:
    first_provider = DeterministicTtsProvider()
    first, events = _worker(tmp_path, monkeypatch, first_provider)
    first._execute()
    checkpoint = next(
        payload
        for kind, payload in reversed(events)
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )
    (tmp_path / "film_voice_vi.wav").unlink()
    second_provider = DeterministicTtsProvider()
    second = TtsWorker(
        "tts-fixture-resume",
        provider_factory=lambda _config: second_provider,
        audio_processor_factory=CopyAudioProcessor,
    )
    second_events: list[tuple[str, dict]] = []
    second.send = lambda kind, payload: second_events.append((kind, payload))  # type: ignore[method-assign]
    second._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": second_provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": second_provider.model_id,
                "provider_config": _config(tmp_path),
            },
            "checkpoint": {"artifact": {"uri": checkpoint["artifact_path"]}},
        }
    )

    second._execute()

    assert second_provider.calls == []
    assert (tmp_path / "film_voice_vi.wav").is_file()
    final_checkpoint_index = next(
        index
        for index, (kind, payload) in enumerate(second_events)
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )
    succeeded_index = next(
        index for index, (kind, _payload) in enumerate(second_events) if kind == "succeeded"
    )
    assert final_checkpoint_index < succeeded_index
    final_checkpoint = second_events[final_checkpoint_index][1]
    persisted = json.loads(
        Path(final_checkpoint["artifact_path"]).read_text(encoding="utf-8")
    )
    assert set(persisted["entries"]) == {"cue-1", "cue-2"}
    assert second.ledger_path.is_file()
    assert second_events[-1][0] == "succeeded"


def test_engine_host_upgrade_keeps_per_cue_cache_fingerprints(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))

    def configured(job_id: str, host_hash: str) -> TtsWorker:
        provider = DeterministicTtsProvider()
        worker = TtsWorker(
            job_id,
            provider_factory=lambda _config: provider,
            audio_processor_factory=CopyAudioProcessor,
        )
        config = _config(tmp_path)
        config["engine_host_sha256"] = host_hash
        worker._configure(
            {
                "job": {
                    "kind": "tts",
                    "provider_id": provider.provider_id,
                    "provider_version": "fixture-v1",
                    "model_id": provider.model_id,
                    "provider_config": config,
                }
            }
        )
        return worker

    previous = configured("tts-host-previous", "a" * 64)
    upgraded = configured("tts-host-upgraded", "b" * 64)

    assert [cue.request_fingerprint for cue in previous.cues] == [
        cue.request_fingerprint for cue in upgraded.cues
    ]
    assert previous.ledger["signature"] == upgraded.ledger["signature"]


def test_worker_restores_legacy_cache_metadata_with_previous_path_name(
    tmp_path, monkeypatch
) -> None:
    provider = DeterministicTtsProvider()
    worker, events = _worker(tmp_path, monkeypatch, provider)
    cue = worker.cues[0]
    cache_root = Path(worker.config["cache_root"])
    legacy_path = cache_root / "legacy-path-name.wav"
    from array import array

    write_pcm16_mono(legacy_path, array("f", [0.1] * 12_000), 48_000)
    qa = {
        "ok": True,
        "duration_us": 250_000,
        "sample_rate": 48_000,
        "peak_dbfs": -10.0,
        "rms_dbfs": -21.0,
        "clipping_ratio": 0.0,
        "silence_ratio": 0.1,
        "onset_peak_dbfs": -12.0,
        "reasons": [],
        "repaired_onset": False,
    }
    metadata = {
        "schema_version": 1,
        "cue_id": cue.cue_id,
        "request_fingerprint": cue.request_fingerprint,
        "cache_path": str(legacy_path),
        "cache_sha256": sha256(legacy_path.read_bytes()).hexdigest(),
        "qa": qa,
    }
    (cache_root / "legacy-metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )

    worker._execute()

    assert provider.calls == [("cue-2",)]
    assert worker.ledger["entries"][cue.cue_id]["request_fingerprint"] == cue.request_fingerprint
    assert worker.ledger["entries"][cue.cue_id]["cache_path"] != str(legacy_path)
    assert worker.ledger["entries"][cue.cue_id]["base_cache_sha256"] == file_sha256(
        legacy_path
    )
    assert events[-1][0] == "succeeded"


def test_worker_restores_a36_global_cache_without_prior_job_ledger(
    tmp_path, monkeypatch
) -> None:
    provider = DeterministicTtsProvider()
    worker, events = _worker(tmp_path, monkeypatch, provider)
    cue = worker.cues[0]
    cache_root = Path(worker.config["cache_root"])
    a36_fingerprint = worker._a36_request_fingerprints[cue.cue_id]
    a36_path = cache_root / f"{a36_fingerprint}.wav"
    from array import array

    write_pcm16_mono(a36_path, array("f", [0.1] * 12_000), 48_000)
    metadata = {
        "schema_version": 1,
        "cue_id": cue.cue_id,
        "request_fingerprint": a36_fingerprint,
        "cache_path": str(a36_path),
        "cache_sha256": sha256(a36_path.read_bytes()).hexdigest(),
        "qa": {
            "ok": True,
            "duration_us": 250_000,
            "sample_rate": 48_000,
            "peak_dbfs": -10.0,
            "rms_dbfs": -21.0,
            "clipping_ratio": 0.0,
            "silence_ratio": 0.1,
            "onset_peak_dbfs": -12.0,
            "reasons": [],
            "repaired_onset": False,
        },
    }
    a36_path.with_suffix(".json").write_text(json.dumps(metadata), encoding="utf-8")

    worker._execute()

    assert provider.calls == [("cue-2",)]
    adopted = worker.ledger["entries"][cue.cue_id]
    assert adopted["request_fingerprint"] == cue.request_fingerprint
    assert adopted["cache_path"] != str(a36_path)
    assert adopted["base_cache_sha256"] == file_sha256(a36_path)
    assert events[-1][0] == "succeeded"


def test_worker_forces_only_selected_cue_then_reassembles_full_wav(
    tmp_path, monkeypatch
) -> None:
    initial_provider = DeterministicTtsProvider()
    initial, _events = _worker(tmp_path, monkeypatch, initial_provider)
    initial._execute()

    forced_provider = DeterministicTtsProvider()
    forced = TtsWorker(
        "tts-fixture-force",
        provider_factory=lambda _config: forced_provider,
        audio_processor_factory=CopyAudioProcessor,
    )
    forced_events: list[tuple[str, dict]] = []
    forced.send = lambda kind, payload: forced_events.append((kind, payload))  # type: ignore[method-assign]
    forced._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": forced_provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": forced_provider.model_id,
                "provider_config": _config(tmp_path, force_cue_ids=["cue-2"]),
            }
        }
    )

    forced._execute()

    assert forced_provider.calls == [("cue-2",)]
    assert forced_events[-1][0] == "succeeded"
    assert forced_events[-1][1]["cue_count"] == 2
    assert (tmp_path / "film_voice_vi.wav").is_file()


def test_worker_does_not_reinfer_a_cue_that_exceeds_the_duration_cap(
    tmp_path, monkeypatch
) -> None:
    provider = DeterministicTtsProvider(duration_samples=96_000)
    worker, events = _worker(tmp_path, monkeypatch, provider)

    worker._execute()

    # The audio is technically valid but cannot fit the cue slot. A second
    # inference cannot change that geometry, so the worker must finish review
    # after the single batch call.
    assert provider.calls == [("cue-1", "cue-2")]
    assert events[-1][0] == "failed"
    assert events[-1][1]["code"] == "tts_review_required"
    report = (tmp_path / "film_voice_vi_QA.json").read_text(encoding="utf-8")
    assert "CUE_SYNC_LIMIT_EXCEEDED" in report


def test_non_blocking_qa_publishes_long_voice_with_timing_warning(
    tmp_path, monkeypatch
) -> None:
    provider = DeterministicTtsProvider(duration_samples=96_000)
    config = _config(tmp_path)
    config["qa_non_blocking"] = True
    worker = TtsWorker(
        "tts-non-blocking",
        provider_factory=lambda _config: provider,
        audio_processor_factory=CopyAudioProcessor,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )

    worker._execute()

    assert events[-1][0] == "succeeded"
    report = json.loads(
        (tmp_path / "film_voice_vi_QA.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "PASS"
    assert report["timing_issue_count"] == 2
    assert report["qa_non_blocking"] is True
    assert (tmp_path / "film_voice_vi.wav").is_file()


def test_strict_timing_last_cue_exceeding_media_by_150ms_requires_review(
    tmp_path, monkeypatch
) -> None:
    provider = DeterministicTtsProvider(duration_samples=12_000)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    config = _config(tmp_path, media_duration_us=1_800_000)
    config["cues"][-1].update(start_us=1_700_000, end_us=1_800_000)
    worker = TtsWorker(
        "tts-strict-overflow",
        provider_factory=lambda _config: provider,
        audio_processor_factory=CopyAudioProcessor,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )

    worker._execute()

    assert events[-1][0] == "failed"
    assert events[-1][1]["code"] == "tts_review_required"
    report = json.loads(
        (tmp_path / "film_voice_vi_QA.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "REVIEW"
    assert report["blockers"][-1]["code"] == "CUE_SYNC_LIMIT_EXCEEDED"
    assert report["blockers"][-1]["fit"]["timeline_overflow_us"] == 150_000
    assert (tmp_path / "film_voice_vi.wav").exists() is False


def test_timing_override_keeps_the_complete_voice_beyond_media_duration(
    tmp_path, monkeypatch
) -> None:
    provider = DeterministicTtsProvider(duration_samples=12_000)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    config = _config(tmp_path, media_duration_us=1_800_000)
    config["cues"][-1].update(start_us=1_700_000, end_us=1_800_000)
    config["allow_timeline_overflow"] = True
    worker = TtsWorker(
        "tts-timing-override",
        provider_factory=lambda _config: provider,
        audio_processor_factory=CopyAudioProcessor,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )

    worker._execute()

    assert events[-1][0] == "succeeded"
    output = tmp_path / "film_voice_vi.wav"
    assert output.is_file()
    with wave.open(str(output), "rb") as stream:
        duration_us = round(stream.getnframes() * 1_000_000 / stream.getframerate())
    assert duration_us == 1_950_000
    report = json.loads(
        (tmp_path / "film_voice_vi_QA.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "PASS"
    assert report["timing_override"] == {
        "enabled": True,
        "overflow_us": 150_000,
        "original_media_duration_us": 1_800_000,
        "assembled_duration_us": 1_950_000,
    }


def test_timing_override_serializes_overlapping_voice_clips(
    tmp_path, monkeypatch
) -> None:
    """Keeping full voice must never publish overlapping clips."""

    provider = DeterministicTtsProvider(duration_samples=12_000)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    config = _config(tmp_path, media_duration_us=1_000_000)
    config["cues"][0].update(start_us=0, end_us=800_000)
    config["cues"][1].update(start_us=100_000, end_us=900_000)
    config["allow_timeline_overflow"] = True

    worker = TtsWorker(
        "tts-timing-override-overlap",
        provider_factory=lambda _config: provider,
        audio_processor_factory=CopyAudioProcessor,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )

    worker._execute()

    assert events[-1][0] == "succeeded"
    report = json.loads(
        (tmp_path / "film_voice_vi_QA.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "PASS"
    assert report["timing"] == {
        "mode": "base_no_loss",
        "shifted_cues": 1,
        "maximum_delay_us": 150_000,
        "final_voice_end_us": 500_000,
        "source_media_duration_us": 1_000_000,
        "assembled_duration_us": 1_000_000,
        "post_conform_required": True,
    }
    assert report["mix"]["overlap_count"] == 0
    assert report["mix"]["duration_us"] == 1_000_000


def test_adaptive_timing_speeds_long_cues_and_shifts_following_voice_without_overlap(
    tmp_path, monkeypatch
) -> None:
    provider = DeterministicTtsProvider(duration_samples=96_000)
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = TtsWorker(
        "tts-adaptive",
        provider_factory=lambda _config: provider,
        audio_processor_factory=DeterministicTimingAudioProcessor,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": provider.model_id,
                "provider_config": _config(
                    tmp_path,
                    pipeline_settings={
                        "adaptive_timing": True,
                        "max_absolute_speed": 3.0,
                    },
                    media_duration_us=3_000_000,
                ),
            }
        }
    )

    worker._execute()

    assert provider.calls == [("cue-1", "cue-2")]
    assert events[-1][0] == "succeeded"
    report = json.loads(
        (tmp_path / "film_voice_vi_QA.json").read_text(encoding="utf-8")
    )
    assert report["timing"]["mode"] == "adaptive_no_loss"
    assert report["timing"]["shifted_cues"] == 0
    assert report["mix"]["overlap_count"] == 0
    assert report["adjusted_cue_count"] == 2
    assert report["maximum_additional_factor"] > 1.0
    assert report["unresolved_timing_cue_count"] == 0
    assert events[-1][1]["adjusted_cue_count"] == 2


def test_vieneu_read_speed_uses_cache_variant_and_never_changes_inference_speed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    provider = DeterministicTtsProvider(duration_samples=24_000)
    provider.provider_id = "vieneu-3.8"
    config = _config(
        tmp_path,
        pipeline_settings={
            "voice_read_speed": 1.3,
            "max_absolute_speed": 1.3,
            "adaptive_timing": False,
        },
        media_duration_us=3_000_000,
    )
    config["voice_read_speed"] = 1.3
    worker = TtsWorker(
        "tts-vieneu-read-speed",
        provider_factory=lambda _config: provider,
        audio_processor_factory=DeterministicTimingAudioProcessor,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": "vieneu-3.8",
                "provider_version": "3.8.1",
                "model_id": "v3turbo",
                "provider_config": config,
            }
        }
    )

    worker._execute()

    assert provider.speeds == [1.0]
    assert events[-1][0] == "succeeded"
    assert events[-1][1]["voice_read_speed"] == 1.3
    assert events[-1][1]["cue_max_total_speed"] == 1.3
    assert events[-1][1]["adjusted_cue_count"] == 0
    assert events[-1][1]["maximum_additional_factor"] == 1.0
    for entry in worker.ledger["entries"].values():
        assert entry["read_speed"] == 1.3
        assert entry["total_speed"] == 1.3
        assert entry["qa"]["duration_us"] < entry["base_qa"]["duration_us"]


def test_changing_read_speed_reuses_base_cache_without_provider_call(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))

    def configured_worker(job_id: str, speed: float, provider: DeterministicTtsProvider):
        config = _config(
            tmp_path,
            pipeline_settings={
                "voice_read_speed": speed,
                "max_absolute_speed": speed,
                "adaptive_timing": False,
            },
            media_duration_us=3_000_000,
        )
        config["voice_read_speed"] = speed
        worker = TtsWorker(
            job_id,
            provider_factory=lambda _config: provider,
            audio_processor_factory=DeterministicTimingAudioProcessor,
        )
        worker.send = lambda _kind, _payload: None  # type: ignore[method-assign]
        worker._configure(
            {
                "job": {
                    "kind": "tts",
                    "provider_id": provider.provider_id,
                    "provider_version": "fixture-v1",
                    "model_id": provider.model_id,
                    "provider_config": config,
                }
            }
        )
        return worker

    first_provider = DeterministicTtsProvider(duration_samples=24_000)
    first = configured_worker("tts-read-13", 1.3, first_provider)
    first._execute()
    assert first_provider.calls == [("cue-1", "cue-2")]

    second_provider = DeterministicTtsProvider(duration_samples=24_000)
    second = configured_worker("tts-read-15", 1.5, second_provider)
    second._execute()

    assert second_provider.calls == []
    assert {entry["read_speed"] for entry in second.ledger["entries"].values()} == {1.5}
    variant_speeds = {
        json.loads(path.read_text(encoding="utf-8")).get("read_speed")
        for path in (tmp_path / "cache").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("cache_tier") == "variant"
    }
    assert {1.3, 1.5} <= variant_speeds


def test_adaptive_timing_reuses_matching_raw_audio_from_prior_strict_job(
    tmp_path, monkeypatch
) -> None:
    strict_provider = DeterministicTtsProvider(duration_samples=96_000)
    strict, strict_events = _worker(tmp_path, monkeypatch, strict_provider)
    strict._execute()
    assert strict_events[-1][0] == "failed"

    # Reproduce the exact on-disk identity emitted by a36.  That release did
    # not have the adaptive_timing field at all; serialising ``False`` produces
    # a different fingerprint and would silently miss every old raw WAV.
    a36_pipeline = asdict(VoicePipelineSettings())
    a36_pipeline.pop("adaptive_timing")
    a36_pipeline.pop("voice_read_speed")
    a36_cues = []
    for raw in _config(tmp_path)["cues"]:
        eligibility = classify_cue(raw["text"])
        cue_value = {
            "cue_id": raw["id"],
            "index": raw["index"],
            "start_us": raw["start_us"],
            "end_us": raw["end_us"],
            "text": eligibility.text,
        }
        fingerprint = stable_fingerprint(
            {
                "schema": "ducai-voice-cue-v1",
                "cue": cue_value,
                "provider": strict_provider.provider_id,
                "provider_version": "fixture-v1",
                "model": strict_provider.model_id,
                "voice": "fixture-v1",
                "pipeline": a36_pipeline,
            }
        )
        a36_cues.append({**cue_value, "request_fingerprint": fingerprint})
        current = next(strict.raw_root.glob(f"{raw['index']:06d}-*.wav"))
        current.rename(strict.raw_root / f"{raw['index']:06d}-{fingerprint[:16]}.wav")
    ledger = json.loads(strict.ledger_path.read_text(encoding="utf-8"))
    ledger["signature"] = stable_fingerprint(
        {
            "schema": "ducai-voice-ledger-v1",
            "provider_id": strict_provider.provider_id,
            "provider_version": "fixture-v1",
            "model_id": strict_provider.model_id,
            "voice_identity": "fixture-v1",
            "cues": a36_cues,
            "ineligible": {},
            "pipeline": a36_pipeline,
        }
    )
    strict.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    adaptive_provider = DeterministicTtsProvider(duration_samples=96_000)
    adaptive = TtsWorker(
        "tts-adaptive-reuse",
        provider_factory=lambda _config: adaptive_provider,
        audio_processor_factory=DeterministicTimingAudioProcessor,
    )
    adaptive_events: list[tuple[str, dict]] = []
    adaptive.send = lambda kind, payload: adaptive_events.append((kind, payload))  # type: ignore[method-assign]
    adaptive._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": adaptive_provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": adaptive_provider.model_id,
                "provider_config": _config(
                    tmp_path,
                    pipeline_settings={
                        "adaptive_timing": True,
                        "max_absolute_speed": 3.0,
                    },
                    media_duration_us=3_000_000,
                ),
            }
        }
    )

    adaptive._execute()

    assert adaptive_provider.calls == []
    assert adaptive_events[-1][0] == "succeeded"


def test_adaptive_timing_adopts_prior_approved_clips_without_stretching_again(
    tmp_path, monkeypatch
) -> None:
    strict_provider = DeterministicTtsProvider(duration_samples=24_000)
    strict, strict_events = _worker(tmp_path, monkeypatch, strict_provider)
    strict._execute()
    assert strict_events[-1][0] == "succeeded"
    for cached in Path(strict.config["cache_root"]).glob("*.json"):
        cached.unlink()

    adaptive_provider = DeterministicTtsProvider(duration_samples=24_000)
    adaptive = TtsWorker(
        "tts-adaptive-processed-reuse",
        provider_factory=lambda _config: adaptive_provider,
        audio_processor_factory=NoStretchAudioProcessor,
    )
    adaptive_events: list[tuple[str, dict]] = []
    adaptive.send = lambda kind, payload: adaptive_events.append((kind, payload))  # type: ignore[method-assign]
    adaptive._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": adaptive_provider.provider_id,
                "provider_version": "fixture-v1",
                "model_id": adaptive_provider.model_id,
                "provider_config": _config(
                    tmp_path,
                    pipeline_settings={
                        "adaptive_timing": True,
                        "max_absolute_speed": 1.5,
                    },
                    media_duration_us=3_000_000,
                ),
            }
        }
    )

    adaptive._execute()

    assert adaptive_provider.calls == []
    cache_event = [
        payload
        for kind, payload in adaptive_events
        if kind == "progress" and payload.get("phase") == "cache_restore"
    ][-1]
    assert cache_event["reused_processed_cues"] == 2
    assert cache_event["phase_current"] == 2
    assert cache_event["phase_total"] == 2
    assert cache_event["pending_synthesis_cues"] == 0
    assert "cache" in cache_event["phase_detail"].lower()
    assert adaptive_events[-1][0] == "succeeded"


def test_conform_variant_fingerprint_includes_absolute_frame_count(
    tmp_path, monkeypatch
) -> None:
    """Near-identical rounded durations must not collide in the shared cache."""

    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    sample_rate = 48_000
    base_wav = tmp_path / "base.wav"
    from array import array

    base_samples = array(
        "f",
        (0.08 if index % 100 < 50 else -0.08 for index in range(76_801)),
    )
    write_pcm16_mono(base_wav, base_samples, sample_rate)
    base_sha256 = file_sha256(base_wav)
    durations = (1_600_020, 1_600_020, 1_600_000, 1_600_020)
    cues = tuple(
        VoiceConformCueInput(
            f"cue-{index}",
            index,
            (index - 1) * 2_000_000,
            index * 2_000_000,
            durations[index - 1],
        )
        for index in range(1, 5)
    )
    plan = build_voice_conform_plan(
        cues,
        source_duration_us=8_000_000,
        settings=VoiceConformSettings(mode=VoiceTimingMode.STRETCH_VIDEO),
    )
    expected_frames = tuple(
        round(decision.voice_end_us * sample_rate / 1_000_000)
        - round(decision.voice_start_us * sample_rate / 1_000_000)
        for decision in plan.cues
    )
    assert expected_frames == (76_801, 76_801, 76_800, 76_801)

    provider_factory_calls: list[dict] = []

    def fail_if_provider_constructed(config: dict):
        provider_factory_calls.append(config)
        raise AssertionError("voice conform must not construct a TTS provider")

    output = tmp_path / "conformed.wav"
    qa_path = tmp_path / "conformed_QA.json"
    worker = TtsWorker(
        "tts-conform-frame-collision",
        provider_factory=fail_if_provider_constructed,
        audio_processor_factory=CopyAudioProcessor,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "tts",
                "provider_id": "voice-conform",
                "provider_version": "piecewise-cache-v2",
                "model_id": "cache-postprocess",
                "provider_config": {
                    "job_mode": "voice_conform",
                    "conform_plan": plan.as_json(),
                    "cues": [
                        {
                            "id": cue.cue_id,
                            "index": cue.index,
                            "start_us": cue.start_us,
                            "end_us": cue.end_us,
                            "text": f"Câu {cue.index}",
                            "base_wav_path": str(base_wav),
                            "base_wav_sha256": base_sha256,
                            "base_qa": {
                                "ok": True,
                                "duration_us": round(len(base_samples) * 1_000_000 / sample_rate),
                                "sample_rate": sample_rate,
                                "peak_dbfs": -21.9,
                                "rms_dbfs": -24.9,
                                "clipping_ratio": 0.0,
                                "silence_ratio": 0.0,
                                "onset_peak_dbfs": -21.9,
                                "reasons": [],
                                "repaired_onset": False,
                            },
                        }
                        for cue in cues
                    ],
                    "conform_cache_root": str(tmp_path / "conform-cache"),
                    "output_path": str(output),
                    "qa_path": str(qa_path),
                    "sample_rate": sample_rate,
                    "target_lufs": -18.0,
                },
            }
        }
    )

    assert worker.provider is None
    worker._execute()

    entries = worker.ledger["entries"]
    paths = [entries[f"cue-{index}"]["cache_path"] for index in range(1, 5)]
    fingerprints = [
        entries[f"cue-{index}"]["variant_fingerprint"] for index in range(1, 5)
    ]
    assert paths[0] == paths[1] == paths[3]
    assert fingerprints[0] == fingerprints[1] == fingerprints[3]
    assert paths[2] != paths[0]
    assert fingerprints[2] != fingerprints[0]
    assert len(list((tmp_path / "conform-cache").glob("*.wav"))) == 2
    assert provider_factory_calls == []
    assert events[-1][0] == "succeeded"
    assert events[-1][1]["purpose"] == "voice_conform"
    report = json.loads(qa_path.read_text(encoding="utf-8"))
    assert report["mix"]["overlap_count"] == 0
    assert report["cache_hits"] == 2
    assert report["rendered_variants"] == 2
