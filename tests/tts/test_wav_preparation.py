from __future__ import annotations

import json
import os
from array import array
from dataclasses import asdict
from pathlib import Path

import pytest

from ducai_studio.application.voice_pipeline import (
    VoicePipelineSettings,
    file_sha256,
    write_pcm16_mono,
)
from ducai_studio.providers.audio_processing import FfmpegAudioProcessor
from ducai_studio.providers.tts.contracts import TtsCueRequest
from ducai_studio.providers.tts.postprocess_client import WavHostError, WavPreparationPool
from ducai_studio.providers.tts.wav_preparation import (
    PreparedVoice,
    prepare_artifact,
    prepare_task,
)


def _task(tmp_path: Path, index: int, *, speed: float = 1.3) -> dict:
    source = tmp_path / f"raw-{index}.wav"
    write_pcm16_mono(source, array("f", (
        0.12 * ((i % 80) / 40 - 1) for i in range(24_000 + index * 1_000)
    )), 48_000)
    cue = TtsCueRequest(f"cue-{index}", index, index * 1_000_000,
                        (index + 1) * 1_000_000, "Ta sẽ trở về.", f"fp-{index}")
    return {
        "cue": asdict(cue), "source": str(source),
        "directory": str(tmp_path / "processed" / str(index)),
        "cache_root": str(tmp_path / "cache"),
        "settings": asdict(VoicePipelineSettings(voice_read_speed=speed,
                                                max_absolute_speed=2.0, adaptive_timing=True)),
        "next_start_us": (index + 1) * 1_000_000,
        "allow_timing_failure": False, "ffmpeg": FfmpegAudioProcessor().executable,
    }


@pytest.mark.parametrize("speed", [1.0, 1.3])
def test_three_owned_cpu_hosts_preserve_exact_audio_fit_qa_and_never_publish(tmp_path, speed):
    payloads = [_task(tmp_path, i, speed=speed) for i in range(1, 7)]
    serial = [prepare_task({**p, "directory": str(tmp_path / "serial" / str(i))},
                           tmp_path / "serial") for i, p in enumerate(payloads)]
    pool = WavPreparationPool(tmp_path / "processed")
    processes = [host.process for host in pool.hosts]
    try:
        futures = [pool.submit(payload) for payload in payloads]
        results = [future.result(timeout=15) for future in futures]
        assert len(set(pool.pids)) == 3
        assert not (tmp_path / "cache").exists()
        for value, reference in zip(results, serial, strict=True):
            expected = PreparedVoice.from_json(reference["prepared"])
            assert value.approval.approved
            assert value.base_sha256 == expected.base_sha256 == file_sha256(value.base_path)
            assert file_sha256(value.variant_path) == file_sha256(expected.variant_path)
            assert value.variant_fingerprint == expected.variant_fingerprint
            assert value.processor_identity == expected.processor_identity
            assert asdict(value.fit) == asdict(expected.fit)
            assert json.dumps(asdict(value.approval), sort_keys=True) == json.dumps(
                asdict(expected.approval), sort_keys=True,
            )
    finally:
        pool.close()
    assert all(process.poll() is not None for process in processes)
    pool.close()


def test_wav_pool_force_close_interrupts_queued_work_and_reaps_owned_hosts(tmp_path):
    payloads = [_task(tmp_path, i) for i in range(1, 9)]
    pool = WavPreparationPool(tmp_path / "processed")
    processes = [host.process for host in pool.hosts]
    futures = [pool.submit(p) for p in payloads]
    pool.close(force=True)
    assert all(future.done() for future in futures)
    assert all(process.poll() is not None for process in processes)
    with pytest.raises(WavHostError, match="pool_closed"):
        pool.submit(payloads[0])


def test_close_reaps_every_host_even_when_one_cleanup_raises(tmp_path, monkeypatch):
    pool = WavPreparationPool(tmp_path / "processed")
    processes = [host.process for host in pool.hosts]
    original_close = pool.hosts[0].close

    def failed_close(*, force=False):
        original_close(force=force)
        raise OSError("fixture cleanup error")

    monkeypatch.setattr(pool.hosts[0], "close", failed_close)
    try:
        with pytest.raises(OSError):
            pool.close(force=True)
        assert all(process.poll() is not None for process in processes)
        assert pool.executor._shutdown
    finally:
        for host in pool.hosts[1:]:
            host.close(force=True)
        pool.executor.shutdown(wait=True, cancel_futures=True)


def test_partial_host_start_failure_closes_already_owned_processes(tmp_path, monkeypatch):
    from ducai_studio.providers.tts import postprocess_client

    original_host = postprocess_client._WavHost
    owned = []

    def start(root, program):
        if owned:
            raise OSError("fixture startup failure")
        host = original_host(root, program)
        owned.append(host)
        return host

    monkeypatch.setattr(postprocess_client, "_WavHost", start)
    with pytest.raises(OSError):
        WavPreparationPool(tmp_path / "processed")
    assert owned[0].process.poll() is not None


def test_crashed_host_fails_promptly_and_reaps_remaining_hosts(tmp_path):
    pool = WavPreparationPool(tmp_path / "processed")
    try:
        pool.hosts[0].tree.close()
        pool.hosts[0].process.wait(timeout=5)
        with pytest.raises(WavHostError):
            pool.submit(_task(tmp_path, 1)).result(timeout=5)
    finally:
        pool.close(force=True)
    assert all(host.process.poll() is not None for host in pool.hosts)


def test_preparation_timing_boundary_blocks_without_publishing_a_base(tmp_path):
    payload = _task(tmp_path, 1, speed=1.0)
    payload["next_start_us"] = payload["cue"]["start_us"] + 100_000
    result = prepare_task(payload, tmp_path / "processed")
    prepared = PreparedVoice.from_json(result["prepared"])
    assert not prepared.approval.approved
    assert prepared.approval.blocker == "CUE_SYNC_LIMIT_EXCEEDED"
    assert not prepared.base_sha256
    assert not (tmp_path / "cache").exists()


def test_wav_host_rejects_output_outside_private_staging(tmp_path):
    payload = _task(tmp_path, 1)
    payload["directory"] = str(tmp_path / "cache")
    with pytest.raises(ValueError, match="staging_out_of_range"):
        prepare_task(payload, tmp_path / "processed")
    assert not (tmp_path / "cache").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path representation")
def test_extended_path_alias_still_resolves_to_the_owned_staging(tmp_path):
    payload = _task(tmp_path, 1, speed=1.0)
    payload["directory"] = "\\\\?\\" + payload["directory"]
    prepared = PreparedVoice.from_json(prepare_task(payload, tmp_path / "processed")["prepared"])
    assert prepared.approval.approved
    assert not (tmp_path / "cache").exists()


def test_invalid_raw_audio_remains_a_retryable_cue_not_a_host_failure(tmp_path):
    payload = _task(tmp_path, 1)
    payload["source"] = str(tmp_path / "missing.wav")
    pool = WavPreparationPool(tmp_path / "processed")
    try:
        result = pool.submit(payload).result(timeout=15)
        assert result.approval.retryable_inference
        assert result.approval.blocker == "tts_audio_invalid"
    finally:
        pool.close()


def test_serial_preparation_uses_existing_atempo_identity_and_cache_hash(tmp_path):
    payload = _task(tmp_path, 1)
    result = prepare_artifact(
        TtsCueRequest(**payload["cue"]), Path(payload["source"]),
        directory=Path(payload["directory"]), cache_root=Path(payload["cache_root"]),
        settings=VoicePipelineSettings(**payload["settings"]), next_start_us=None,
        allow_timing_failure=False,
    )
    assert result.approval.approved
    assert result.processor_identity.startswith(
        "ducai_studio.providers.audio_processing.FfmpegAudioProcessor:",
    )
