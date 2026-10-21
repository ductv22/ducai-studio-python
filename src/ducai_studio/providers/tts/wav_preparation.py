"""Deterministic per-cue preparation; never publishes shared cache or ledgers."""

from __future__ import annotations

import json
import math
import os
import shutil
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ducai_studio.application.voice_pipeline import (
    AudioQualityReport,
    VoiceFitPlan,
    VoicePipelineSettings,
    analyze_audio,
    file_sha256,
    plan_fit,
    process_voice_segment,
    read_pcm16_mono,
)
from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.providers.audio_processing import FfmpegAudioProcessor
from ducai_studio.providers.tts.contracts import TtsCueRequest


def resolved_wav_path(path: Path) -> Path:
    """Compare resolved Windows aliases consistently, retaining junction checks."""
    resolved = path.resolve()
    value = str(resolved)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + value[8:])
        if value.startswith("\\\\?\\"):
            return Path(value[4:])
    return resolved


@dataclass(frozen=True, slots=True)
class ArtifactApproval:
    approved: bool
    retryable_inference: bool = False
    blocker: str = ""
    required_speed: float | None = None
    qa: AudioQualityReport | None = None


@dataclass(frozen=True, slots=True)
class PreparedVoice:
    approval: ArtifactApproval
    base_path: Path | None = None
    base_qa: AudioQualityReport | None = None
    base_sha256: str = ""
    fit: VoiceFitPlan | None = None
    variant_path: Path | None = None
    variant_fingerprint: str = ""
    processor_identity: str = ""
    variant_sha256: str = ""

    def to_json(self) -> dict:
        value = asdict(self)
        for key in ("base_path", "variant_path"):
            value[key] = str(value[key]) if value[key] is not None else None
        return value

    @classmethod
    def from_json(cls, value: dict) -> PreparedVoice:
        raw = dict(value)
        approval = dict(raw.pop("approval"))
        if approval.get("qa") is not None:
            qa = dict(approval["qa"])
            qa["reasons"] = tuple(qa.get("reasons", ()))
            approval["qa"] = AudioQualityReport(**qa)
        raw["approval"] = ArtifactApproval(**approval)
        if raw.get("base_qa") is not None:
            qa = dict(raw["base_qa"])
            qa["reasons"] = tuple(qa.get("reasons", ()))
            raw["base_qa"] = AudioQualityReport(**qa)
        if raw.get("fit") is not None:
            raw["fit"] = VoiceFitPlan(**raw["fit"])
        for key in ("base_path", "variant_path"):
            raw[key] = Path(raw[key]) if raw.get(key) is not None else None
        return cls(**raw)


def prepare_artifact(
    cue: TtsCueRequest,
    source: Path,
    *,
    directory: Path,
    cache_root: Path,
    settings: VoicePipelineSettings,
    next_start_us: int | None,
    allow_timing_failure: bool,
    processor_factory: Callable[[], FfmpegAudioProcessor] = FfmpegAudioProcessor,
) -> PreparedVoice:
    directory.mkdir(parents=True, exist_ok=True)
    processed = directory / "base.wav"
    try:
        qa = process_voice_segment(source, processed, settings)
    except (OSError, ValueError, EOFError, wave.Error):
        return PreparedVoice(ArtifactApproval(
            False, retryable_inference=True, blocker="tts_audio_invalid",
        ))
    if not qa.ok:
        return PreparedVoice(ArtifactApproval(
            False, retryable_inference=True, blocker="tts_audio_quality_failed", qa=qa,
        ))
    return prepare_base_audio(
        cue, processed, qa, directory=directory, cache_root=cache_root, settings=settings,
        next_start_us=next_start_us, allow_timing_failure=allow_timing_failure,
        processor_factory=processor_factory,
    )


def prepare_base_audio(
    cue: TtsCueRequest,
    base_path: Path,
    base_qa: AudioQualityReport,
    *,
    directory: Path,
    cache_root: Path,
    settings: VoicePipelineSettings,
    next_start_us: int | None,
    allow_timing_failure: bool,
    processor_factory: Callable[[], FfmpegAudioProcessor] = FfmpegAudioProcessor,
) -> PreparedVoice:
    fit = plan_fit(
        cue, audio_duration_us=base_qa.duration_us, next_start_us=next_start_us,
        settings=settings,
    )
    if not fit.fits and not allow_timing_failure:
        return PreparedVoice(ArtifactApproval(
            False, blocker=fit.blocker, required_speed=fit.required_total_speed, qa=base_qa,
        ))

    base_sha256 = file_sha256(base_path)
    total_speed = fit.applied_total_speed
    processor = processor_factory() if total_speed > 1.0005 else None
    processor_identity = (
        f"{type(processor).__module__}.{type(processor).__qualname__}:"
        f"{getattr(processor, 'executable', '')}"
        if processor is not None else "pcm-copy-v1"
    )
    variant_fingerprint = stable_fingerprint({
        "schema": "ducai-voice-speed-variant-v1",
        "base_wav_sha256": base_sha256,
        "total_speed": round(total_speed, 8),
        "processor": processor_identity,
        "sample_rate": settings.sample_rate,
        "qa_settings": {
            key: value for key, value in asdict(settings).items()
            if key not in {"voice_read_speed", "max_absolute_speed", "adaptive_timing"}
        },
    })
    cache_path = cache_root / f"{variant_fingerprint}.wav"
    metadata_path = cache_path.with_suffix(".json")
    qa: AudioQualityReport | None = None
    if cache_path.is_file() and metadata_path.is_file():
        try:
            cached = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and cached.get("variant_fingerprint") == variant_fingerprint
                and cached.get("base_cache_sha256") == base_sha256
                and cached.get("cache_sha256") == file_sha256(cache_path)
                and isinstance(cached.get("qa"), dict)
            ):
                raw_qa = dict(cached["qa"])
                raw_qa["reasons"] = tuple(raw_qa.get("reasons", ()))
                qa = AudioQualityReport(**raw_qa)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            qa = None
    directory.mkdir(parents=True, exist_ok=True)
    variant = directory / "variant.wav"
    if qa is not None:
        # Read-only cache lookup cannot make a mutable shared path part of an
        # immutable result. Verify our private copy against the observed hash.
        try:
            shutil.copyfile(cache_path, variant)
            if file_sha256(variant) != cached["cache_sha256"]:
                qa = None
        except OSError:
            qa = None
    if qa is None:
        if processor is None:
            shutil.copyfile(base_path, variant)
        else:
            processor.time_stretch(
                base_path, variant, speed=total_speed, sample_rate=settings.sample_rate,
            )
        samples, sample_rate = read_pcm16_mono(variant)
        qa = analyze_audio(samples, sample_rate, settings, repaired_onset=base_qa.repaired_onset)
        expected_duration_us = int(math.ceil(base_qa.duration_us / total_speed))
        if not qa.ok or abs(qa.duration_us - expected_duration_us) > 50_000:
            variant.unlink(missing_ok=True)
            return PreparedVoice(
                ArtifactApproval(False, blocker="tts_fit_validation_failed",
                                 required_speed=fit.required_total_speed, qa=qa),
                base_path, base_qa, base_sha256, fit,
            )
    return PreparedVoice(
        ArtifactApproval(True, qa=qa), base_path, base_qa, base_sha256, fit,
        variant, variant_fingerprint, processor_identity, file_sha256(variant),
    )


def prepare_task(payload: dict, staging_root: Path) -> dict:
    """Decode one immutable JSON task within the host's private output root."""
    staging_root = resolved_wav_path(staging_root)
    directory = resolved_wav_path(Path(payload["directory"]))
    if directory == staging_root or not directory.is_relative_to(staging_root):
        raise ValueError("tts_wav_staging_out_of_range")
    cue = TtsCueRequest(**payload["cue"])
    ffmpeg = str(payload["ffmpeg"])
    prepared = prepare_artifact(
        cue, Path(payload["source"]), directory=directory,
        cache_root=Path(payload["cache_root"]),
        settings=VoicePipelineSettings(**payload["settings"]),
        next_start_us=payload["next_start_us"],
        allow_timing_failure=payload["allow_timing_failure"] is True,
        processor_factory=lambda: FfmpegAudioProcessor(ffmpeg),
    )
    return {
        "cue_id": cue.cue_id, "request_fingerprint": cue.request_fingerprint,
        "prepared": prepared.to_json(),
    }
