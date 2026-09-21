"""Version-neutral Piper/VieNeu host using line-delimited JSON on stdout."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import sys
import wave
from pathlib import Path

PROTOCOL_VERSION = 1


def _wav_info(path):
    with wave.open(str(path), "rb") as stream:
        sample_rate = stream.getframerate()
        frames = stream.getnframes()
    return sample_rate, int(round(frames * 1_000_000 / sample_rate))


def _profile_receipt(path):
    import numpy as np

    profile = Path(path).expanduser().resolve(strict=True)
    with np.load(profile, allow_pickle=False) as values:
        if set(values.files) != {"speaker_emb", "codes"}:
            raise ValueError("vieneu_profile_fields_invalid")
        speaker = np.asarray(values["speaker_emb"], dtype=np.float32).reshape(-1)
        codes = np.asarray(values["codes"], dtype=np.int64)
    if speaker.size != 192:
        raise ValueError("vieneu_speaker_embedding_not_192d")
    if codes.ndim != 2 or codes.shape[0] <= 0 or codes.shape[1] <= 0:
        raise ValueError("vieneu_reference_codes_missing")
    return {
        "profile_path": str(profile),
        "speaker_embedding_size": int(speaker.size),
        "reference_codes_shape": [int(value) for value in codes.shape],
        "binding": "explicit_v330_speaker_embedding_192d_ref_codes",
    }, {"speaker_emb": speaker, "codes": codes}


class TtsEngineHost:
    def __init__(self, engine):
        if engine not in {"piper", "vieneu"}:
            raise ValueError("unsupported_tts_engine")
        self.engine_name = engine
        self.engine = None
        self.identity = {}
        self.voice = None
        self.safe_batch_size = None

    def health(self):
        package = "piper-tts" if self.engine_name == "piper" else "vieneu"
        version = importlib.metadata.version(package)
        return {"engine": self.engine_name, "version": version, "loaded": self.engine is not None}

    def load(self, payload):
        if self.engine_name == "piper":
            model = Path(payload["model_path"]).expanduser().resolve(strict=True)
            config = Path(payload["config_path"]).expanduser().resolve(strict=True)
            identity = {
                "model_path": str(model),
                "config_path": str(config),
                "model_fingerprint": str(payload.get("model_fingerprint", "")),
            }
            if self.engine is None or self.identity != identity:
                with contextlib.redirect_stdout(sys.stderr):
                    from piper import PiperVoice

                    self.engine = PiperVoice.load(str(model), config_path=str(config))
                self.identity = identity
                self.safe_batch_size = None
            return {"loaded": True, **identity}

        expected = str(payload.get("runtime_version", ""))
        installed = importlib.metadata.version("vieneu")
        if expected != "3.3.0" or installed != "3.3.0":
            raise ValueError("vieneu_runtime_version_mismatch")
        if self.engine is None:
            with contextlib.redirect_stdout(sys.stderr):
                from vieneu import Vieneu

                try:
                    from vieneu_v330_optimization import (
                        install_vieneu_v330_optimizations,
                    )
                except ImportError:
                    from ducai_studio.engine_hosts.vieneu_v330_optimization import (
                        install_vieneu_v330_optimizations,
                    )

                self.engine = Vieneu(
                    mode="v3turbo",
                    max_batch_size=max(1, min(64, int(payload.get("max_batch_size", 64)))),
                )
                optimization = install_vieneu_v330_optimizations(
                    self.engine,
                    runtime_version=installed,
                )
            self.identity = {
                "runtime_version": installed,
                "mode": "v3turbo",
                "optimization": optimization,
            }
            self.safe_batch_size = None
        return {"loaded": True, **self.identity}

    def inspect_profile(self, payload):
        receipt, _voice = _profile_receipt(payload["profile_path"])
        return receipt

    def bind_profile(self, payload):
        if self.engine_name != "vieneu":
            raise ValueError("profile_binding_requires_vieneu")
        receipt, voice = _profile_receipt(payload["profile_path"])
        required = int(payload.get("require_speaker_embedding_size", 192))
        if receipt["speaker_embedding_size"] != required:
            raise ValueError("vieneu_speaker_embedding_size_mismatch")
        self.voice = voice
        return receipt

    def enroll_profile(self, payload):
        if self.engine_name != "vieneu" or self.engine is None:
            raise ValueError("vieneu_engine_must_be_loaded_before_enrollment")
        import numpy as np

        reference = Path(payload["reference_audio_path"]).expanduser().resolve(strict=True)
        output = Path(payload["profile_path"]).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stdout(sys.stderr):
            speaker, codes = self.engine.encode_reference(
                reference, denoise=payload.get("denoise") is True
            )
        speaker = np.asarray(speaker, dtype=np.float32).reshape(-1)
        codes = np.asarray(codes, dtype=np.int64)
        if speaker.size != 192:
            raise ValueError("vieneu_speaker_embedding_not_192d")
        if codes.ndim != 2 or not codes.size:
            raise ValueError("vieneu_reference_codes_missing")
        temp = output.with_name(output.name + ".tmp")
        with temp.open("wb") as stream:
            np.savez_compressed(stream, speaker_emb=speaker, codes=codes)
        temp.replace(output)
        metadata = {
            "identity": {
                "schema": "ducai-vieneu-v330-reference-v1",
                "clone_engine": "v3_3_turbo_192d_batch_qa",
                "mode": "v3turbo",
                "runtime_version": "3.3.0",
                "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
                "reference_denoise": payload.get("denoise") is True,
                "reference_max_seconds": 8.0,
                "use_ref_codes": True,
            },
            "speaker_embedding_size": int(speaker.size),
            "reference_codes_shape": [int(value) for value in codes.shape],
        }
        metadata_path = output.with_suffix(".json")
        metadata_temp = metadata_path.with_name(metadata_path.name + ".tmp")
        metadata_temp.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        metadata_temp.replace(metadata_path)
        receipt, voice = _profile_receipt(output)
        self.voice = voice
        return receipt

    def synthesize_batch(self, payload):
        if self.engine is None:
            raise ValueError("tts_engine_not_loaded")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            raise ValueError("tts_jobs_missing")
        if self.engine_name == "piper":
            return self._synthesize_piper(jobs, payload)
        if self.voice is None:
            raise ValueError("vieneu_clone_profile_not_bound")
        return self._synthesize_vieneu(jobs, payload)

    def _synthesize_piper(self, jobs, payload):
        from piper.config import SynthesisConfig

        speed = float(payload.get("speed", 1.0))
        config = SynthesisConfig(length_scale=1.0 / speed, normalize_audio=True)
        items = []
        for job in jobs:
            cue_id = str(job.get("cue_id", ""))
            try:
                output = Path(job["output_path"]).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                temp = output.with_name(output.name + ".tmp")
                with (
                    wave.open(str(temp), "wb") as wav_file,
                    contextlib.redirect_stdout(sys.stderr),
                ):
                    self.engine.synthesize_wav(str(job["text"]), wav_file, syn_config=config)
                temp.replace(output)
                sample_rate, duration_us = _wav_info(output)
                items.append(
                    {
                        "cue_id": cue_id,
                        "ok": True,
                        "output_path": str(output),
                        "sample_rate": sample_rate,
                        "duration_us": duration_us,
                    }
                )
            except Exception as exc:
                items.append({"cue_id": cue_id, "ok": False, "code": type(exc).__name__})
        return {"items": items, "batch_size": 1}

    def _synthesize_vieneu(self, jobs, payload):
        import numpy as np
        import soundfile as sf

        texts = [str(job["text"]) for job in jobs]
        requested = max(1, min(64, int(payload.get("batch_size", 64))))
        if self.safe_batch_size is not None:
            requested = min(requested, self.safe_batch_size)
        current = requested
        while True:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    audio = self.engine.infer_batch(
                        texts,
                        voice=self.voice,
                        denoise=False,
                        use_ref_codes=True,
                        apply_watermark=payload.get("apply_watermark") is not False,
                        batch_size=current,
                    )
                break
            except RuntimeError as exc:
                if "out of memory" not in str(exc).casefold() or current <= 1:
                    raise
                current = max(1, current // 2)
                try:
                    import torch

                    torch.cuda.empty_cache()
                except Exception:
                    pass
        self.safe_batch_size = current
        if len(audio) != len(jobs):
            raise ValueError("vieneu_result_count_mismatch")
        sample_rate = int(getattr(self.engine, "sample_rate", 48_000))
        items = []
        for job, samples in zip(jobs, audio, strict=True):
            cue_id = str(job.get("cue_id", ""))
            try:
                values = np.asarray(samples, dtype=np.float32).reshape(-1)
                if values.size == 0 or not np.isfinite(values).all():
                    raise ValueError("vieneu_audio_invalid")
                output = Path(job["output_path"]).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                temp = output.with_name(output.name + ".tmp.wav")
                sf.write(str(temp), values, sample_rate, subtype="PCM_16")
                temp.replace(output)
                _rate, duration_us = _wav_info(output)
                items.append(
                    {
                        "cue_id": cue_id,
                        "ok": True,
                        "output_path": str(output),
                        "sample_rate": sample_rate,
                        "duration_us": duration_us,
                    }
                )
            except Exception as exc:
                items.append({"cue_id": cue_id, "ok": False, "code": type(exc).__name__})
        return {"items": items, "batch_size": current}

    def close(self):
        self.engine = None
        self.voice = None
        self.identity = {}
        self.safe_batch_size = None


def _response(request_id, *, result=None, error=None):
    body = {"protocol_version": PROTOCOL_VERSION, "id": request_id, "ok": error is None}
    if error is None:
        body["result"] = result if result is not None else {}
    else:
        body["error"] = error
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"


def serve(engine, input_stream=None, output_stream=None):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    host = TtsEngineHost(engine)
    try:
        for line in input_stream:
            request_id = "unknown"
            try:
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("protocol_version") != 1:
                    raise ValueError("unsupported_protocol_version")
                request_id = message.get("id")
                action = message.get("action")
                payload = message.get("payload", {})
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError("request_id_required")
                if not isinstance(payload, dict):
                    raise ValueError("payload_must_be_object")
                if action == "health":
                    result = host.health()
                elif action == "load":
                    result = host.load(payload)
                elif action == "inspect_profile":
                    result = host.inspect_profile(payload)
                elif action == "bind_profile":
                    result = host.bind_profile(payload)
                elif action == "enroll_profile":
                    result = host.enroll_profile(payload)
                elif action == "synthesize_batch":
                    result = host.synthesize_batch(payload)
                elif action == "shutdown":
                    host.close()
                    output_stream.write(_response(request_id, result={"stopped": True}))
                    output_stream.flush()
                    return 0
                else:
                    raise ValueError("unknown_action")
                output_stream.write(_response(request_id, result=result))
            except Exception as exc:
                code = str(exc) if str(exc).replace("_", "").isalnum() else type(exc).__name__
                output_stream.write(_response(request_id, error={"code": code[:80]}))
            output_stream.flush()
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    selected = sys.argv[1] if len(sys.argv) > 1 else ""
    raise SystemExit(serve(selected))
