from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

from .analysis import Settings, clear_ranges, interval, merged_ranges
from .assets import LOCK_PATH, file_sha256, json_digest, model_files, read_json, runtime_versions

SAMPLE_RATE = 16000


def decode_audio(media: Path, start_us: int, end_us: int, ffmpeg: str) -> bytes:
    """Read one bounded source interval, never an entire film into memory."""
    if start_us < 0 or end_us <= start_us or end_us - start_us > 150_000_000:
        raise ValueError("Audio decode must be a positive interval of at most 150 seconds")
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{start_us / 1_000_000:.6f}",
        "-i",
        str(media),
        "-t",
        f"{(end_us - start_us) / 1_000_000:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    completed = subprocess.run(args, capture_output=True, timeout=180, check=False)
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"Cannot decode original audio: {media}: {detail}")
    expected_samples = (end_us - start_us) * SAMPLE_RATE // 1_000_000
    if len(completed.stdout) % 4 or len(completed.stdout) < max(4, expected_samples * 4 - 6400):
        raise RuntimeError(f"Original-audio interval is missing/truncated: {media}")
    if len(completed.stdout) > (expected_samples + SAMPLE_RATE) * 4:
        raise RuntimeError("Audio decoder exceeded its bounded interval")
    return completed.stdout


class Recognizer:
    def __init__(self, models: Path, settings: Settings):
        self.versions = runtime_versions()
        files = model_files(models)
        # Framework imports happen only in the explicitly installed external runtime.
        import numpy as np
        import onnxruntime as ort
        import sherpa_onnx

        self.np = np
        self.settings = settings
        self.model_lock_sha256 = file_sha256(LOCK_PATH)
        config = read_json(files["gender-config"])
        preprocessing = read_json(files["gender-preprocessor"])
        if config.get("id2label") != {"0": "female", "1": "male"}:
            raise RuntimeError("Unrecognized casting classifier label order")
        if preprocessing.get("sampling_rate") != SAMPLE_RATE or not preprocessing.get(
            "do_normalize"
        ):
            raise RuntimeError("Unrecognized casting classifier preprocessing")
        options = ort.SessionOptions()
        options.intra_op_num_threads = settings.threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        ort.disable_telemetry_events()
        self.classifier = ort.InferenceSession(
            str(files["gender"]), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.inputs = {item.name: item.type for item in self.classifier.get_inputs()}
        if (
            set(self.inputs) - {"input_values", "attention_mask"}
            or "input_values" not in self.inputs
        ):
            raise RuntimeError(f"Unexpected gender ONNX inputs: {self.inputs}")
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(files["segmentation"])
                ),
                num_threads=settings.threads,
                provider="cpu",
                debug=False,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(files["embedding"]),
                num_threads=settings.threads,
                provider="cpu",
                debug=False,
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=-1, threshold=settings.clustering_threshold
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not cfg.validate():
            raise RuntimeError("Casting diarization configuration/model is invalid; run setup")
        self.diarizer = sherpa_onnx.OfflineSpeakerDiarization(cfg)
        if self.diarizer.sample_rate != SAMPLE_RATE:
            raise RuntimeError("Diarizer sample rate is not 16 kHz")

    def female_probability(self, samples) -> float:
        np = self.np
        if samples.size < self.settings.min_sample_us * SAMPLE_RATE // 1_000_000:
            raise ValueError("Classifier sample is shorter than the reference minimum")
        if samples.size > self.settings.max_sample_us * SAMPLE_RATE // 1_000_000:
            raise ValueError("Classifier reference exceeds five seconds")
        if not np.isfinite(samples).all():
            raise RuntimeError("Original audio contains non-finite samples")
        # Single, unpadded example: identical to Wav2Vec2FeatureExtractor normalization.
        normalized = (samples - samples.mean()) / np.sqrt(samples.var() + 1e-7)
        values = normalized.astype(np.float32)[None, :]
        feed = {"input_values": values}
        if "attention_mask" in self.inputs:
            dtype = {"tensor(int64)": np.int64, "tensor(int32)": np.int32}.get(
                self.inputs["attention_mask"]
            )
            if dtype is None:
                raise RuntimeError("Unsupported classifier attention mask dtype")
            feed["attention_mask"] = np.ones(values.shape, dtype=dtype)
        logits = np.asarray(self.classifier.run(None, feed)[0], dtype=np.float64)
        if logits.shape != (1, 2) or not np.isfinite(logits).all():
            raise RuntimeError("Invalid gender ONNX output; inference failed")
        probabilities = np.exp(logits[0] - logits[0].max())
        return float(probabilities[0] / probabilities.sum())

    def analyze(self, media: Path, start_us: int, end_us: int, ffmpeg: str) -> dict:
        np = self.np
        samples = np.frombuffer(decode_audio(media, start_us, end_us, ffmpeg), dtype="<f4").copy()
        if not np.isfinite(samples).all():
            raise RuntimeError("Audio decode returned non-finite values")
        turns = []
        for item in self.diarizer.process(samples).sort_by_start_time():
            start = max(start_us, start_us + round(item.start * 1_000_000))
            end = min(end_us, start_us + round(item.end * 1_000_000))
            if start < end:
                turns.append(
                    {
                        "start_us": start,
                        "end_us": end,
                        "speaker": f"window-{start_us}-speaker-{item.speaker}",
                    }
                )
        candidates: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for turn in turns:
            candidates[turn["speaker"]].extend(clear_ranges(turn, turns))
        evidence = []
        for speaker, ranges in sorted(candidates.items()):
            for start, end in sorted(
                merged_ranges(ranges), key=lambda r: r[1] - r[0], reverse=True
            )[: self.settings.max_samples_per_speaker]:
                if end - start < self.settings.min_sample_us:
                    continue
                length = min(end - start, self.settings.max_sample_us)
                start += (end - start - length) // 2
                end = start + length
                first = (start - start_us) * SAMPLE_RATE // 1_000_000
                last = (end - start_us) * SAMPLE_RATE // 1_000_000
                probability = self.female_probability(samples[first:last])
                evidence.append(
                    {
                        "speaker": speaker,
                        "start_us": start,
                        "end_us": end,
                        "female_probability": probability,
                    }
                )
        return {"turns": turns, "evidence": evidence}


def analysis_windows(cues: list[dict], film_duration_us: int, settings: Settings) -> list[dict]:
    blocks: dict[int, list[dict]] = defaultdict(list)
    for cue in cues:
        start, end = interval(cue)
        if end > film_duration_us:
            raise ValueError(f"Cue extends beyond original media: {cue['id']}")
        if end - start > 20_000_000:
            raise ValueError(f"Lab cue exceeds 20 seconds; split it explicitly: {cue['id']}")
        blocks[start // settings.block_us].append(cue)
    result = []
    for block, items in sorted(blocks.items()):
        start = max(0, block * settings.block_us - settings.context_us)
        end = min(
            film_duration_us,
            max((block + 1) * settings.block_us, max(cue["end_us"] for cue in items))
            + settings.context_us,
        )
        result.append({"start_us": start, "end_us": end, "cues": items})
    return result


def window_key(film: dict, window: dict, settings: Settings) -> str:
    return json_digest(
        {
            "source_sha256": film["source_sha256"],
            "start_us": window["start_us"],
            "end_us": window["end_us"],
            "model_lock_sha256": file_sha256(LOCK_PATH),
            "requirements_sha256": file_sha256(Path(__file__).with_name("requirements.txt")),
            "implementation_sha256": implementation_digest(),
            "settings": settings.analysis_identity(),
        }
    )


def implementation_digest() -> str:
    return json_digest(
        {
            name: file_sha256(Path(__file__).with_name(name))
            for name in ("inference.py", "analysis.py")
        }
    )
