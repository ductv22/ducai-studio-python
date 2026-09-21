"""Version-neutral streaming faster-whisper process.

The host runs in the separately managed Whisper Python environment.  It emits
one JSON response per decoded segment so the parent worker can checkpoint or
stop without waiting for a full-file transcription.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import site
import sys
from pathlib import Path

PROTOCOL_VERSION = 1
CHINESE_PROMPT = "仙侠对白，简体中文，自然标点。"
CHINESE_HOTWORDS = "元婴 炼虚 化神 合体 渡劫 道友 修为 聚宝盆 宗门 妖兽"
_DLL_DIRECTORY_HANDLES = []


def _configure_cuda_paths():
    candidates = []
    for root in site.getsitepackages():
        base = Path(root) / "nvidia"
        for package in (
            "cublas",
            "cudnn",
            "cuda_runtime",
            "cuda_nvrtc",
            "cufft",
            "curand",
            "nvjitlink",
        ):
            candidates.append(base / package / "bin")
    found = [str(path.resolve()) for path in candidates if path.is_dir()]
    if found:
        os.environ["PATH"] = os.pathsep.join([*found, os.environ.get("PATH", "")])
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            for directory in found:
                with contextlib.suppress(OSError):
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(directory))
    return found


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _word_rows(segment, offset_seconds):
    result = []
    for word in list(getattr(segment, "words", None) or []):
        start = getattr(word, "start", None)
        end = getattr(word, "end", None)
        if start is None or end is None:
            continue
        result.append(
            {
                "start_us": max(0, int(round((float(start) + offset_seconds) * 1_000_000))),
                "end_us": max(1, int(round((float(end) + offset_seconds) * 1_000_000))),
                "word": str(getattr(word, "word", "")),
                "probability": round(
                    float(getattr(word, "probability", 0.0) or 0.0), 6
                ),
            }
        )
    return result


class WhisperEngineHost:
    def __init__(self, model_factory=None, audio_decoder=None):
        self.model_factory = model_factory
        self.audio_decoder = audio_decoder

    def health(self):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "python": sys.version.split()[0],
            "faster_whisper": _version("faster-whisper"),
            "ctranslate2": _version("ctranslate2"),
            "streaming_segments": True,
            "devices": ["cuda", "cpu"],
        }

    def transcribe(self, payload):
        audio_path = Path(str(payload.get("audio_path", ""))).expanduser().resolve(
            strict=True
        )
        model_path = Path(str(payload.get("model_path", ""))).expanduser().resolve(
            strict=True
        )
        if not (model_path / "model.bin").is_file():
            raise ValueError("whisper_model_missing")
        start_us = payload.get("start_us", 0)
        duration_us = payload.get("duration_us")
        if type(start_us) is not int or start_us < 0:
            raise ValueError("whisper_start_invalid")
        if type(duration_us) is not int or duration_us <= 0 or start_us >= duration_us:
            raise ValueError("whisper_duration_invalid")
        language = str(payload.get("language", "zh")).strip().casefold() or "zh"
        if language not in {"zh", "zh-cn", "zh-tw"}:
            raise ValueError("whisper_language_unsupported")
        device = str(payload.get("device", "cuda")).strip().casefold()
        compute_type = str(
            payload.get("compute_type", "float16" if device == "cuda" else "int8")
        ).strip()
        if device not in {"cuda", "cpu"}:
            raise ValueError("whisper_device_invalid")
        if device == "cuda":
            _configure_cuda_paths()

        decoder = self.audio_decoder
        model_factory = self.model_factory
        if decoder is None:
            from faster_whisper import decode_audio

            decoder = decode_audio
        if model_factory is None:
            from faster_whisper import WhisperModel

            model_factory = WhisperModel
        audio = decoder(str(audio_path), sampling_rate=16_000)
        start_sample = min(len(audio), int(round(start_us * 16_000 / 1_000_000)))
        audio = audio[start_sample:]
        if len(audio) == 0:
            raise ValueError("whisper_audio_empty")
        model = model_factory(
            str(model_path),
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )
        vad_parameters = payload.get("vad_parameters", {})
        if not isinstance(vad_parameters, dict):
            raise ValueError("whisper_vad_invalid")
        segments, info = model.transcribe(
            audio,
            language="zh" if language.startswith("zh") else language,
            beam_size=int(payload.get("beam_size", 5)),
            temperature=float(payload.get("temperature", 0.0)),
            condition_on_previous_text=False,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=vad_parameters,
            initial_prompt=CHINESE_PROMPT if language.startswith("zh") else None,
            hotwords=CHINESE_HOTWORDS if language.startswith("zh") else None,
        )
        offset_seconds = start_us / 1_000_000
        yield (
            "metadata",
            {
                "device": device,
                "compute_type": compute_type,
                "language": str(getattr(info, "language", language)),
                "language_probability": float(
                    getattr(info, "language_probability", 0.0) or 0.0
                ),
                "duration_us": duration_us,
                "start_us": start_us,
            },
        )
        count = 0
        for segment in segments:
            text = str(getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            segment_start_us = max(
                start_us,
                int(round((float(segment.start) + offset_seconds) * 1_000_000)),
            )
            segment_end_us = min(
                duration_us,
                int(round((float(segment.end) + offset_seconds) * 1_000_000)),
            )
            if segment_end_us <= segment_start_us:
                continue
            count += 1
            words = _word_rows(segment, offset_seconds)
            probabilities = [
                float(word["probability"])
                for word in words
                if float(word["probability"]) > 0
            ]
            yield (
                "segment",
                {
                    "index": count,
                    "start_us": segment_start_us,
                    "end_us": segment_end_us,
                    "text": text,
                    "avg_logprob": float(
                        getattr(segment, "avg_logprob", -0.75) or -0.75
                    ),
                    "no_speech_prob": float(
                        getattr(segment, "no_speech_prob", 0.0) or 0.0
                    ),
                    "compression_ratio": float(
                        getattr(segment, "compression_ratio", 1.0) or 1.0
                    ),
                    "avg_word_probability": (
                        sum(probabilities) / len(probabilities) if probabilities else None
                    ),
                    "words": words,
                },
            )
        yield (
            "complete",
            {
                "segment_count": count,
                "device": device,
                "compute_type": compute_type,
            },
        )


def _response(request_id, *, event="complete", result=None, error=None):
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "id": request_id,
        "ok": error is None,
        "event": event,
    }
    if error is None:
        body["result"] = result if result is not None else {}
    else:
        body["error"] = error
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"


def serve(input_stream=None, output_stream=None, host=None):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    host = host or WhisperEngineHost()
    for line in input_stream:
        request_id = "unknown"
        message = {}
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("request_must_be_object")
            if message.get("protocol_version") != PROTOCOL_VERSION:
                raise ValueError("unsupported_protocol_version")
            request_id = message.get("id")
            action = message.get("action")
            payload = message.get("payload", {})
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("request_id_required")
            if not isinstance(payload, dict):
                raise ValueError("payload_must_be_object")
            if action == "health":
                output_stream.write(_response(request_id, result=host.health()))
                output_stream.flush()
                continue
            if action == "shutdown":
                output_stream.write(_response(request_id, result={"stopped": True}))
                output_stream.flush()
                return 0
            if action != "transcribe":
                raise ValueError("unknown_action")
            for event, result in host.transcribe(payload):
                output_stream.write(_response(request_id, event=event, result=result))
                output_stream.flush()
        except Exception as exc:
            raw = str(exc)
            code = raw if raw.replace("_", "").isalnum() else type(exc).__name__
            if str(message.get("payload", {}).get("device", "")) == "cuda":
                code = "whisper_cuda_failed"
            output_stream.write(
                _response(request_id, event="error", error={"code": code[:80]})
            )
            output_stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
