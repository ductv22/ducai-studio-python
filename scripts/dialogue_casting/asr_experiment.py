"""Explicit, local Whisper comparison; never changes the application runtime."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import sys
import time
import wave
from pathlib import Path

from .assets import file_sha256, read_json, write_json

REPOSITORY = "BELLE-2/Belle-whisper-large-v3-zh"
REVISION = "a72022a6a93289a2580a14c219439ed578c3e45b"
WEIGHTS_SHA256 = "aecaece9f55fee22d418f6a7b24eef4166e52586a78371a4c92bd2b23b30c03f"
WEIGHTS_SIZE = 6_174_112_608


def download_model(output: Path) -> dict:
    """Network use exists only in this explicitly requested installation command."""
    from huggingface_hub import snapshot_download

    output.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=REPOSITORY,
        revision=REVISION,
        local_dir=output,
        allow_patterns=["*.json", "*.txt", "README.md", "model.safetensors"],
        max_workers=3,
    )
    weights = output / "model.safetensors"
    if weights.stat().st_size != WEIGHTS_SIZE or file_sha256(weights) != WEIGHTS_SHA256:
        raise RuntimeError("Official BELLE model checksum mismatch")
    provenance = {
        "repository": REPOSITORY,
        "revision": REVISION,
        "license": "Apache-2.0 (upstream model card)",
        "model_card": f"https://huggingface.co/{REPOSITORY}/blob/{REVISION}/README.md",
        "files": {
            p.name: {"size": p.stat().st_size, "sha256": file_sha256(p)}
            for p in sorted(output.iterdir())
            if p.is_file() and p.name != "download-provenance.json"
        },
    }
    write_json(output / "download-provenance.json", provenance)
    return provenance


def convert_model(source: Path, output: Path, ct2_site: Path) -> dict:
    # Reuse installed libraries in this process only; never pip-install into TTS/ASR.
    import torch  # noqa: F401
    import transformers  # noqa: F401

    sys.path.append(str(ct2_site.resolve(strict=True)))
    from ctranslate2.converters import TransformersConverter

    weights = source / "model.safetensors"
    if weights.stat().st_size != WEIGHTS_SIZE or file_sha256(weights) != WEIGHTS_SHA256:
        raise RuntimeError("Conversion requires verified official BELLE weights")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    converter = TransformersConverter(
        str(source.resolve(strict=True)),
        copy_files=["tokenizer.json", "preprocessor_config.json"],
        load_as_float16=True,
        trust_remote_code=False,
    )
    converter.convert(str(output), quantization="float16", force=False)
    provenance = {
        "repository": REPOSITORY,
        "revision": REVISION,
        "original_weights_sha256": WEIGHTS_SHA256,
        "quantization": "float16",
        "license": "Apache-2.0; retain upstream attribution",
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("torch", "transformers", "ctranslate2", "huggingface-hub")
        },
        "files": {
            p.name: {"size": p.stat().st_size, "sha256": file_sha256(p)}
            for p in sorted(output.iterdir())
            if p.is_file()
        },
    }
    write_json(output / "conversion-provenance.json", provenance)
    return provenance


def host_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "src/ducai_studio/engine_hosts/whisper_engine_host.py"
    )
    spec = importlib.util.spec_from_file_location("ducai_asr_lab_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def transcribe_demo(root: Path, model_path: Path, model_id: str, device: str) -> dict:
    """Decode the same bounded original WAVs with matching production settings."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    host = host_module()
    if device == "cuda":
        host._configure_cuda_paths()
    from faster_whisper import WhisperModel

    spec = read_json(root / "experiment.json")
    started = time.monotonic()
    compute_type = "float16" if device == "cuda" else "int8"
    model = WhisperModel(
        str(model_path.resolve(strict=True)),
        device=device,
        compute_type=compute_type,
        local_files_only=True,
    )
    rows = []
    settings = {
        "language": "zh",
        "task": "transcribe",
        "beam_size": 5,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "vad_filter": True,
        "initial_prompt": host.CHINESE_PROMPT,
        "hotwords": host.CHINESE_HOTWORDS,
    }
    for index, item in enumerate(spec["audio"], 1):
        path = (root / item["path"]).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or file_sha256(path) != item["sha256"]:
            raise ValueError("ASR input clip identity mismatch")
        with wave.open(str(path), "rb") as audio:
            duration_us = round(audio.getnframes() * 1_000_000 / audio.getframerate())
            if audio.getnchannels() != 1 or audio.getframerate() != 16000:
                raise ValueError("ASR experiment requires mono 16 kHz audio")
            if not 0 < duration_us <= 150_000_000:
                raise ValueError("ASR experiment audio must be bounded to 150 seconds")
        segments, info = model.transcribe(str(path), **settings)
        output = []
        for segment in segments:
            start = max(0, round(segment.start * 1_000_000))
            end = min(duration_us, round(segment.end * 1_000_000))
            text = segment.text.strip()
            if text and end > start:
                words = [
                    {
                        "start_us": max(0, min(duration_us, round(w.start * 1_000_000))),
                        "end_us": max(0, min(duration_us, round(w.end * 1_000_000))),
                        "text": w.word,
                    }
                    for w in (segment.words or [])
                ]
                output.append({"start_us": start, "end_us": end, "text": text, "words": words})
        rows.append({
            "audio_id": item["id"],
            "audio_sha256": item["sha256"],
            "duration_us": duration_us,
            "language_probability": info.language_probability,
            "segments": output,
        })
        print(f"{model_id}: {index}/{len(spec['audio'])} clips", flush=True)
    result = {
        "schema": "ducai-whisper-comparison-v1",
        "experiment_id": spec["experiment_id"],
        "model_id": model_id,
        "model_path": str(model_path.resolve()),
        "model_sha256": file_sha256(model_path / "model.bin"),
        "device": device,
        "compute_type": compute_type,
        "settings": settings,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("faster-whisper", "ctranslate2", "numpy")
        },
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "asr_accuracy": "NOT_MEASURED: no independently corrected Chinese transcripts",
        "results": rows,
    }
    write_json(root / f"asr-{model_id}.json", result)
    return {key: value for key, value in result.items() if key != "results"}
