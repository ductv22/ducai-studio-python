from __future__ import annotations

import hashlib
import shutil
import subprocess
import wave

import pytest

from ducai_studio.engine_hosts import tts_engine_host as host_module
from ducai_studio.providers.tts.host_client import EngineHostError
from ducai_studio.workers.tts_worker import _tts_failure_payload


@pytest.fixture
def aac_sample(tmp_path):
    sample = tmp_path / "giọng riêng #1.m4a"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000",
        "-t", "0.4", "-ac", "2", "-c:a", "aac", str(sample),
    ], check=True, capture_output=True, timeout=20)
    return sample


def test_compressed_reference_decodes_to_pcm_and_is_cleaned(aac_sample):
    before = hashlib.sha256(aac_sample.read_bytes()).hexdigest()
    with host_module._enrollment_reference(aac_sample) as decoded:
        assert decoded != aac_sample
        with wave.open(str(decoded), "rb") as stream:
            assert stream.getnchannels() == 1
            assert stream.getsampwidth() == 2
            assert .35 < stream.getnframes() / stream.getframerate() < .5
    assert not decoded.exists()
    assert hashlib.sha256(aac_sample.read_bytes()).hexdigest() == before


def test_reference_decode_cleanup_when_enrollment_fails(aac_sample):
    with (pytest.raises(RuntimeError, match="enrollment failed"),
          host_module._enrollment_reference(aac_sample) as decoded):
        raise RuntimeError("enrollment failed")
    assert not decoded.exists()
    assert aac_sample.is_file()


def test_native_pcm_reference_does_not_require_decoder(tmp_path, monkeypatch):
    source = tmp_path / "sample.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x01\x00" * 2400)
    monkeypatch.setattr(shutil, "which", lambda _program: None)
    with host_module._enrollment_reference(source) as decoded:
        assert decoded == source
    assert source.is_file()


def test_corrupt_sample_reports_decode_failure(tmp_path):
    source = tmp_path / "corrupt.m4a"
    source.write_bytes(b"not an audio file")
    with (pytest.raises(ValueError, match="^vieneu_reference_decode_failed$"),
          host_module._enrollment_reference(source)):
        pytest.fail("invalid audio must not reach enrollment")


def test_compressed_sample_requires_available_decoder(aac_sample, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _program: None)
    with (pytest.raises(ValueError, match="^vieneu_reference_decoder_missing$"),
          host_module._enrollment_reference(aac_sample)):
        pytest.fail("missing decoder must be reported")


@pytest.mark.parametrize("code", [
    "vieneu_reference_decode_failed", "vieneu_reference_decoder_missing",
    "vieneu_reference_decode_timeout", "LibsndfileError",
])
def test_reference_errors_do_not_claim_engine_disconnection(code):
    payload = _tts_failure_payload(EngineHostError(code))
    assert payload["code"] == code
    assert "file giọng" in payload["message"]
    assert "phiên kết nối" not in payload["message"]


def test_real_connection_failure_keeps_recovery_guidance():
    payload = _tts_failure_payload(EngineHostError("engine_host_timeout"))
    assert "Tiếp tục từ cache" in payload["message"]
