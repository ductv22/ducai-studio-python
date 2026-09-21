from __future__ import annotations

import hashlib
import json
import math
import os
import re
import wave
from array import array
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.domain.jobs import JsonValue, freeze_json
from ducai_studio.providers.tts.contracts import TtsCueRequest

_NON_DIALOGUE = re.compile(
    r"^\s*(?:\[(?:nhạc|âm nhạc|music|sfx|tiếng động)[^]]*\]|"
    r"\((?:nhạc|âm nhạc|music|sfx|tiếng động)[^)]*\))\s*$",
    re.IGNORECASE,
)
_MARKUP = re.compile(r"<[^>]+>|\{\\[^}]+\}")
_SPACE = re.compile(r"[ \t\u00a0]+")
_PCM16_COPY_FRAMES = 512 * 1024
_PCM16_SILENCE = bytes(_PCM16_COPY_FRAMES * 2)


@dataclass(frozen=True, slots=True)
class VoicePipelineSettings:
    sample_rate: int = 48_000
    max_absolute_speed: float = 1.28
    voice_read_speed: float = 1.0
    adaptive_timing: bool = False
    max_reflow_us: int = 350_000
    silence_threshold_db: float = -43.0
    trim_padding_ms: float = 28.0
    fade_in_ms: float = 8.0
    fade_out_ms: float = 14.0
    segment_rms_db: float = -21.0
    peak_ceiling_db: float = -1.5
    min_speech_ms: float = 70.0

    def __post_init__(self) -> None:
        if not 8_000 <= self.sample_rate <= 192_000:
            raise ValueError("Voice pipeline sample rate is invalid")
        if not math.isfinite(self.voice_read_speed) or not 1.0 <= self.voice_read_speed <= 3.0:
            raise ValueError("Voice read speed must be between 1.0 and 3.0")
        if (
            not math.isfinite(self.max_absolute_speed)
            or not self.voice_read_speed <= self.max_absolute_speed <= 3.0
        ):
            raise ValueError(
                "Absolute voice speed cap must be between read speed and 3.0"
            )
        if type(self.adaptive_timing) is not bool:
            raise ValueError("Adaptive voice timing option must be boolean")
        if type(self.max_reflow_us) is not int or not 0 <= self.max_reflow_us <= 2_000_000:
            raise ValueError("Voice reflow allowance is invalid")


@dataclass(frozen=True, slots=True)
class CueEligibility:
    eligible: bool
    text: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AudioQualityReport:
    ok: bool
    duration_us: int
    sample_rate: int
    peak_dbfs: float
    rms_dbfs: float
    clipping_ratio: float
    silence_ratio: float
    onset_peak_dbfs: float
    reasons: tuple[str, ...] = ()
    repaired_onset: bool = False


@dataclass(frozen=True, slots=True)
class VoiceFitPlan:
    cue_id: str
    slot_duration_us: int
    allowed_duration_us: int
    audio_duration_us: int
    absolute_speed: float
    uses_reflow_us: int
    fits: bool
    blocker: str = ""
    timeline_overflow_us: int = 0
    adaptive_timing: bool = False
    base_speed: float = 1.0
    required_total_speed: float = 1.0
    applied_total_speed: float = 1.0
    base_duration_us: int = 0
    duration_after_base_us: int = 0
    final_duration_us: int = 0
    overflow_tolerance_us: int = 50_000


@dataclass(frozen=True, slots=True)
class VoiceClip:
    cue_id: str
    start_us: int
    end_us: int
    wav_path: Path
    qa: AudioQualityReport
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class VoiceCoverageReport:
    total_cues: int
    eligible_cues: int
    approved_cues: int
    ineligible_cues: int
    blocked_cues: int
    coverage_ratio: float
    status: str
    ineligible: tuple[dict[str, JsonValue], ...] = ()
    blockers: tuple[dict[str, JsonValue], ...] = ()

    def as_json(self) -> dict[str, JsonValue]:
        return dict(freeze_json(asdict(self)))


def normalize_voice_text(value: str) -> str:
    text = _MARKUP.sub(" ", str(value or ""))
    text = text.replace("\r", " ").replace("\n", " ")
    text = _SPACE.sub(" ", text).strip()
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    text = re.sub(r"([,.;:!?…])(?=[^\s,.;:!?…])", r"\1 ", text)
    return text


def classify_cue(text: str) -> CueEligibility:
    normalized = normalize_voice_text(text)
    if not normalized:
        return CueEligibility(False, "", "empty_after_normalization")
    if _NON_DIALOGUE.fullmatch(normalized):
        return CueEligibility(False, normalized, "non_dialogue_marker")
    if not any(character.isalnum() for character in normalized):
        return CueEligibility(False, normalized, "no_speakable_content")
    return CueEligibility(True, normalized)


def cue_request_fingerprint(
    cue: TtsCueRequest | dict[str, JsonValue],
    *,
    provider_id: str,
    provider_version: str,
    model_id: str,
    voice_identity: str,
    settings: VoicePipelineSettings,
    synthesis_speed: float = 1.0,
    include_timing_policy: bool = False,
) -> str:
    if isinstance(cue, TtsCueRequest):
        cue_value: dict[str, JsonValue] = {
            "cue_id": cue.cue_id,
            "index": cue.index,
            "start_us": cue.start_us,
            "end_us": cue.end_us,
            "text": cue.text,
        }
    else:
        cue_value = dict(cue)
    pipeline = asdict(settings)
    schema = "ducai-voice-cue-v1"
    if not include_timing_policy:
        # Synthesis/quality identity deliberately excludes post-synthesis timing.
        # A slider or conform-mode change must reuse the exact same normalized
        # VieNeu/Piper WAV instead of creating a new inference request.
        for key in (
            "max_absolute_speed",
            "voice_read_speed",
            "adaptive_timing",
            "max_reflow_us",
        ):
            pipeline.pop(key, None)
        schema = "ducai-voice-base-cue-v2"
    payload: dict[str, object] = {
        "schema": schema,
        "cue": cue_value,
        "provider": provider_id,
        "provider_version": provider_version,
        "model": model_id,
        "voice": voice_identity,
        "pipeline": pipeline,
    }
    if abs(float(synthesis_speed) - 1.0) > 1e-9:
        payload["synthesis_speed"] = round(float(synthesis_speed), 8)
    return stable_fingerprint(payload)


def read_pcm16_mono(path: Path) -> tuple[array, int]:
    path = path.expanduser().resolve(strict=True)
    with wave.open(str(path), "rb") as stream:
        if stream.getsampwidth() != 2 or stream.getcomptype() != "NONE":
            raise ValueError("Voice pipeline requires uncompressed PCM16 WAV")
        channels = stream.getnchannels()
        sample_rate = stream.getframerate()
        raw = stream.readframes(stream.getnframes())
    values = array("h")
    values.frombytes(raw)
    if channels == 1:
        mono = values
    else:
        mono = array(
            "h",
            (
                int(sum(values[index : index + channels]) / channels)
                for index in range(0, len(values), channels)
            ),
        )
    return array("f", (sample / 32768.0 for sample in mono)), sample_rate


def write_pcm16_mono(path: Path, samples: array, sample_rate: int) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    pcm = array("h")
    for sample in samples:
        value = max(-1.0, min(0.999969, float(sample)))
        pcm.append(int(round(value * 32767.0)))
    with wave.open(str(temp), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm.tobytes())
    os.replace(temp, path)


def resample_linear(samples: array, source_rate: int, target_rate: int) -> array:
    if source_rate == target_rate or not samples:
        return array("f", samples)
    output_length = max(1, int(round(len(samples) * target_rate / source_rate)))
    scale = (len(samples) - 1) / max(1, output_length - 1)
    output = array("f")
    for output_index in range(output_length):
        position = output_index * scale
        left = int(position)
        right = min(len(samples) - 1, left + 1)
        fraction = position - left
        output.append(samples[left] * (1.0 - fraction) + samples[right] * fraction)
    return output


def process_voice_segment(
    source: Path,
    target: Path,
    settings: VoicePipelineSettings,
) -> AudioQualityReport:
    samples, source_rate = read_pcm16_mono(source)
    samples = resample_linear(samples, source_rate, settings.sample_rate)
    if not samples:
        report = analyze_audio(samples, settings.sample_rate, settings)
        return report
    dc = sum(samples) / len(samples)
    samples = array("f", (sample - dc for sample in samples))
    samples = _trim_silence(samples, settings)
    samples, repaired = _repair_onset(samples, settings)
    samples = _normalize_segment(samples, settings)
    _fade_edges(samples, settings)
    report = analyze_audio(samples, settings.sample_rate, settings, repaired_onset=repaired)
    if report.ok:
        write_pcm16_mono(target, samples, settings.sample_rate)
    return report


def analyze_audio(
    samples: array,
    sample_rate: int,
    settings: VoicePipelineSettings,
    *,
    repaired_onset: bool = False,
) -> AudioQualityReport:
    duration_us = int(round(len(samples) * 1_000_000 / sample_rate)) if sample_rate else 0
    if not samples:
        return AudioQualityReport(
            False,
            duration_us,
            sample_rate,
            -120.0,
            -120.0,
            0.0,
            1.0,
            -120.0,
            ("empty_audio",),
            repaired_onset,
        )
    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    threshold = 10 ** (settings.silence_threshold_db / 20.0)
    silent = sum(1 for sample in samples if abs(sample) < threshold)
    clipping = sum(1 for sample in samples if abs(sample) >= 0.999)
    onset_count = min(len(samples), max(1, int(sample_rate * 0.035)))
    onset_peak = max(abs(samples[index]) for index in range(onset_count))
    reasons: list[str] = []
    if duration_us < int(settings.min_speech_ms * 1_000):
        reasons.append("audio_too_short")
    if rms < 10 ** (-48.0 / 20.0):
        reasons.append("audio_near_silent")
    if clipping / len(samples) > 0.001:
        reasons.append("clipping_detected")
    if not all(math.isfinite(sample) for sample in samples):
        reasons.append("non_finite_audio")
    return AudioQualityReport(
        not reasons,
        duration_us,
        sample_rate,
        _dbfs(peak),
        _dbfs(rms),
        clipping / len(samples),
        silent / len(samples),
        _dbfs(onset_peak),
        tuple(reasons),
        repaired_onset,
    )


def plan_fit(
    cue: TtsCueRequest,
    *,
    audio_duration_us: int,
    next_start_us: int | None,
    settings: VoicePipelineSettings,
) -> VoiceFitPlan:
    slot = cue.duration_us
    # Timing QA is cue-local by contract. Reflow remains in the data model for
    # compatibility, but it must not hide a voice that is longer than its SRT slot.
    allowed = max(1, slot)
    base_speed = settings.voice_read_speed
    required_speed = max(1.0, audio_duration_us / allowed)
    speed = base_speed
    if settings.adaptive_timing and required_speed > base_speed:
        speed = min(required_speed, settings.max_absolute_speed)
    base_duration = int(math.ceil(audio_duration_us / base_speed))
    fitted_duration = int(math.ceil(audio_duration_us / speed))
    overflow = max(0, fitted_duration - allowed)
    crosses_next_timestamp = (
        next_start_us is not None
        and cue.start_us + fitted_duration > next_start_us
    )
    # The 50 ms acoustic tolerance remains valid when the cue ends at the end
    # of the programme or inside a real subtitle gap.  It must never permit a
    # cue to cross the next SRT timestamp: doing so either overlaps voices or
    # forces every following cue to drift later.
    fits = overflow <= 50_000 and not crosses_next_timestamp
    blocker = "" if fits else "CUE_SYNC_LIMIT_EXCEEDED"
    return VoiceFitPlan(
        cue.cue_id,
        slot,
        allowed,
        audio_duration_us,
        speed,
        0,
        fits,
        blocker,
        overflow,
        settings.adaptive_timing,
        base_speed,
        required_speed,
        speed,
        audio_duration_us,
        base_duration,
        fitted_duration,
        50_000,
    )


def build_voice_timeline_rows(
    cues: Sequence[TtsCueRequest],
    entries: Mapping[str, object],
    blockers: Mapping[str, object],
    *,
    serialize_overlaps: bool = False,
    default_speed: float = 1.0,
    global_early_shift_us: int = 0,
) -> tuple[dict[str, JsonValue], ...]:
    """Build truthful per-cue Timeline rows from durable TTS evidence.

    The rows are a presentation contract, not editable timeline clips.  A
    D1 is stored as one mastered media asset, while Timeline can render every
    measured cue as a separate clip at its actual timestamp.
    Legacy no-loss/override outputs may opt into serialized starts so the UI
    exposes their real accumulated delay instead of drawing a false alignment.
    """

    rows: list[dict[str, JsonValue]] = []
    occupied_until_us = 0
    for cue in sorted(cues, key=lambda item: (item.start_us, item.index, item.cue_id)):
        raw_entry = entries.get(cue.cue_id)
        entry = raw_entry if isinstance(raw_entry, Mapping) else {}
        raw_blocker = blockers.get(cue.cue_id)
        blocker = raw_blocker if isinstance(raw_blocker, Mapping) else {}
        raw_fit = entry.get("fit")
        if not isinstance(raw_fit, Mapping):
            raw_fit = blocker.get("fit")
        fit = raw_fit if isinstance(raw_fit, Mapping) else {}
        raw_base_qa = entry.get("base_qa")
        base_qa = raw_base_qa if isinstance(raw_base_qa, Mapping) else {}
        raw_qa = entry.get("qa")
        qa = raw_qa if isinstance(raw_qa, Mapping) else {}

        def positive_int(*values: object, fallback: int) -> int:
            for value in values:
                if type(value) is int and value > 0:
                    return value
            return fallback

        audio_duration_us = positive_int(
            base_qa.get("duration_us"),
            fit.get("base_duration_us"),
            fit.get("audio_duration_us"),
            qa.get("duration_us"),
            fallback=cue.duration_us,
        )
        rendered_duration_us = positive_int(
            qa.get("duration_us"),
            fit.get("final_duration_us"),
            fit.get("duration_after_base_us"),
            fallback=cue.duration_us,
        )
        raw_speed = entry.get("total_speed", fit.get("applied_total_speed", default_speed))
        audio_speed = (
            float(raw_speed)
            if isinstance(raw_speed, (int, float))
            and not isinstance(raw_speed, bool)
            and math.isfinite(float(raw_speed))
            and float(raw_speed) > 0
            else max(1.0, float(default_speed))
        )
        start_us = max(0, cue.start_us - max(0, int(global_early_shift_us)))
        if serialize_overlaps:
            start_us = max(start_us, occupied_until_us)
        end_us = start_us + rendered_duration_us
        occupied_until_us = max(occupied_until_us, end_us)
        issue_code = str(blocker.get("code", ""))
        unresolved = bool(issue_code) or fit.get("fits") is False
        rows.append(
            {
                "cue_id": cue.cue_id,
                "index": cue.index,
                "source_start_us": cue.start_us,
                "source_end_us": cue.end_us,
                "output_start_us": cue.start_us,
                "output_end_us": cue.end_us,
                "voice_start_us": start_us,
                "voice_end_us": end_us,
                "audio_duration_us": audio_duration_us,
                "rendered_audio_duration_us": rendered_duration_us,
                "audio_speed": audio_speed,
                "adjusted": audio_speed > max(1.0, float(default_speed)) + 1e-9,
                "unresolved": unresolved,
                "issue_code": issue_code,
            }
        )
    return tuple(rows)


def measured_voice_timing_issues(
    cues: Sequence[TtsCueRequest],
    entries: Mapping[str, object],
    blockers: Mapping[str, object],
    *,
    max_absolute_speed: float,
) -> tuple[dict[str, JsonValue], ...]:
    """Normalize strict/adaptive fit evidence into cue-scoped repair issues."""

    issues: list[dict[str, JsonValue]] = []
    for cue in cues:
        entry = entries.get(cue.cue_id)
        fit = entry.get("fit") if isinstance(entry, Mapping) else None
        blocker = blockers.get(cue.cue_id)
        blocker = blocker if isinstance(blocker, Mapping) else {}
        if not isinstance(fit, Mapping):
            fit = blocker.get("fit")
        fit = fit if isinstance(fit, Mapping) else {}
        overflow_us = int(fit.get("timeline_overflow_us", 0) or 0)
        required_speed_raw = blocker.get("required_speed")
        if not isinstance(required_speed_raw, (int, float)) or isinstance(
            required_speed_raw, bool
        ):
            audio_duration_us = int(fit.get("audio_duration_us", 0) or 0)
            allowed_duration_us = int(fit.get("allowed_duration_us", 0) or 0)
            required_speed = (
                audio_duration_us / allowed_duration_us
                if audio_duration_us > 0 and allowed_duration_us > 0
                else 1.0
            )
        else:
            required_speed = float(required_speed_raw)
            audio_duration_us = 0
            allowed_duration_us = max(1, cue.duration_us)
        strict_timing_blocker = blocker.get("code") in {
            "absolute_speed_cap_exceeded",
            "CUE_SYNC_LIMIT_EXCEEDED",
            "CUE_TOO_LONG",
        }
        # A positive overflow is not automatically an error.  ``plan_fit``
        # accepts up to 50 ms for sample rounding, and stores ``fits=True``
        # for those cues.  Do not surface those harmless overages as repair
        # blockers; only an explicit failed fit, strict blocker, or overflow
        # beyond the tolerance belongs in the timing-review list.
        fit_failed = fit.get("fits") is False
        if (
            not fit_failed
            and not strict_timing_blocker
            and overflow_us <= 50_000
        ):
            continue
        if not audio_duration_us:
            audio_duration_us = int(round(required_speed * allowed_duration_us))
        current_chars = len(cue.text.strip())
        target_ratio = min(
            0.9,
            max(0.12, max_absolute_speed / max(required_speed, 1.0) * 0.9),
        )
        target_chars = max(4, min(current_chars - 1, int(current_chars * target_ratio)))
        if current_chars <= 4:
            target_chars = current_chars
        overflow_seconds = overflow_us / 1_000_000
        message = (
            f"Cue dài: voice cần khoảng {required_speed:.2f}× để vừa khung, "
            f"vượt giới hạn {max_absolute_speed:.2f}×"
            + (f" và còn tràn {overflow_seconds:.2f} giây." if overflow_us else ".")
            + f" Hãy rút gọn còn khoảng {target_chars} ký tự nhưng giữ đủ nghĩa và tên riêng."
        )
        issues.append(
            {
                "id": cue.index,
                "cue_id": cue.cue_id,
                "start_us": cue.start_us,
                "end_us": cue.end_us,
                "code": "CUE_TOO_LONG",
                "message": message,
                "translated_text": cue.text,
                "slot_duration_us": cue.duration_us,
                "allowed_duration_us": allowed_duration_us,
                "audio_duration_us": audio_duration_us,
                "overflow_us": overflow_us,
                "required_speed": required_speed,
                "required_total_speed": float(
                    fit.get("required_total_speed", required_speed) or required_speed
                ),
                "applied_total_speed": float(
                    fit.get("applied_total_speed", fit.get("absolute_speed", 1.0)) or 1.0
                ),
                "base_speed": float(fit.get("base_speed", 1.0) or 1.0),
                "base_duration_us": int(
                    fit.get("base_duration_us", audio_duration_us) or audio_duration_us
                ),
                "duration_after_base_us": int(
                    fit.get("duration_after_base_us", audio_duration_us) or audio_duration_us
                ),
                "final_duration_us": int(
                    fit.get("final_duration_us", audio_duration_us) or audio_duration_us
                ),
                "overflow_tolerance_us": int(
                    fit.get("overflow_tolerance_us", 50_000) or 50_000
                ),
                "speed_cap": max_absolute_speed,
                "current_chars": current_chars,
                "target_chars": target_chars,
            }
        )
    return tuple(issues)


def assemble_voice_timeline(
    clips: tuple[VoiceClip, ...],
    target: Path,
    *,
    total_duration_us: int,
    sample_rate: int = 48_000,
    progress: Callable[[int, int], None] | None = None,
    pcm_sink: Callable[[bytes], None] | None = None,
) -> dict[str, JsonValue]:
    if total_duration_us <= 0:
        raise ValueError("Voice timeline duration must be positive")
    target = target.expanduser().resolve()
    total_samples = int(math.ceil(total_duration_us * sample_rate / 1_000_000))
    ordered = sorted(clips, key=lambda item: (item.start_us, item.cue_id))
    prepared: list[tuple[VoiceClip, int, int, int]] = []
    overlaps = 0
    occupied_until = 0
    peak = 0.0
    for clip in ordered:
        source = clip.wav_path.expanduser().resolve(strict=True)
        with wave.open(str(source), "rb") as stream:
            if (
                stream.getnchannels() != 1
                or stream.getsampwidth() != 2
                or stream.getcomptype() != "NONE"
            ):
                raise ValueError("Voice timeline requires uncompressed mono PCM16 WAV")
            source_rate = stream.getframerate()
            source_frames = stream.getnframes()
        output_frames = (
            source_frames
            if source_rate == sample_rate
            else max(1, int(round(source_frames * sample_rate / source_rate)))
        )
        start = int(round(clip.start_us * sample_rate / 1_000_000))
        if start < 0:
            target.unlink(missing_ok=True)
            raise ValueError("voice_clip_starts_before_timeline")
        if start >= total_samples:
            target.unlink(missing_ok=True)
            raise ValueError("voice_clip_starts_outside_timeline")
        if start + output_frames > total_samples:
            target.unlink(missing_ok=True)
            raise ValueError("voice_clip_exceeds_timeline")
        if start < occupied_until:
            overlaps += 1
        occupied_until = max(occupied_until, start + output_frames)
        prepared.append((clip, start, output_frames, source_rate))
        if math.isfinite(clip.qa.peak_dbfs):
            peak = max(peak, 10 ** (clip.qa.peak_dbfs / 20.0))
    if overlaps:
        target.expanduser().resolve().unlink(missing_ok=True)
        return {
            "clip_count": len(clips),
            "duration_us": total_duration_us,
            "overlap_count": overlaps,
            "peak_before_limiter": peak,
        }

    gain = 0.98 / peak if peak > 0.98 else 1.0
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    cursor = 0
    try:
        with wave.open(str(temp), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            for completed, (clip, start, output_frames, source_rate) in enumerate(prepared, 1):
                if start > cursor:
                    _write_pcm16_silence(output, start - cursor, pcm_sink=pcm_sink)
                    cursor = start
                written = _copy_voice_clip_pcm16(
                    clip.wav_path,
                    output,
                    source_rate=source_rate,
                    target_rate=sample_rate,
                    max_frames=output_frames,
                    gain=gain,
                    pcm_sink=pcm_sink,
                )
                if written != output_frames:
                    raise ValueError("voice_clip_pcm_length_mismatch")
                cursor += written
                if progress is not None:
                    progress(completed, len(prepared))
            if cursor < total_samples:
                _write_pcm16_silence(output, total_samples - cursor, pcm_sink=pcm_sink)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return {
        "clip_count": len(clips),
        "duration_us": total_duration_us,
        "overlap_count": overlaps,
        "peak_before_limiter": peak,
    }


def _write_pcm16_silence(
    output: wave.Wave_write,
    frames: int,
    *,
    pcm_sink: Callable[[bytes], None] | None = None,
) -> None:
    remaining = max(0, frames)
    while remaining:
        chunk_frames = min(remaining, _PCM16_COPY_FRAMES)
        _write_pcm16_bytes(
            output,
            _PCM16_SILENCE[: chunk_frames * 2],
            pcm_sink=pcm_sink,
        )
        remaining -= chunk_frames


def _copy_voice_clip_pcm16(
    source: Path,
    output: wave.Wave_write,
    *,
    source_rate: int,
    target_rate: int,
    max_frames: int,
    gain: float,
    pcm_sink: Callable[[bytes], None] | None = None,
) -> int:
    if max_frames <= 0:
        return 0
    if source_rate != target_rate:
        samples, actual_rate = read_pcm16_mono(source)
        samples = resample_linear(samples, actual_rate, target_rate)
        clipped = samples[:max_frames]
        pcm = array(
            "h",
            (
                int(round(max(-1.0, min(0.999969, float(sample) * gain)) * 32767.0))
                for sample in clipped
            ),
        )
        _write_pcm16_bytes(output, pcm.tobytes(), pcm_sink=pcm_sink)
        return len(clipped)

    written = 0
    with wave.open(str(source.expanduser().resolve(strict=True)), "rb") as stream:
        while written < max_frames:
            raw = stream.readframes(min(_PCM16_COPY_FRAMES, max_frames - written))
            if not raw:
                break
            if gain < 0.999999:
                pcm = array("h")
                pcm.frombytes(raw)
                for index, sample in enumerate(pcm):
                    pcm[index] = int(round(sample * gain))
                raw = pcm.tobytes()
            _write_pcm16_bytes(output, raw, pcm_sink=pcm_sink)
            written += len(raw) // 2
    return written


def _write_pcm16_bytes(
    output: wave.Wave_write,
    raw: bytes,
    *,
    pcm_sink: Callable[[bytes], None] | None,
) -> None:
    output.writeframesraw(raw)
    if pcm_sink is not None:
        pcm_sink(raw)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _trim_silence(samples: array, settings: VoicePipelineSettings) -> array:
    threshold = 10 ** (settings.silence_threshold_db / 20.0)
    active = [index for index, sample in enumerate(samples) if abs(sample) >= threshold]
    if not active:
        return array("f")
    padding = int(settings.sample_rate * settings.trim_padding_ms / 1_000)
    start = max(0, active[0] - padding)
    end = min(len(samples), active[-1] + padding + 1)
    return array("f", samples[start:end])


def _repair_onset(samples: array, settings: VoicePipelineSettings) -> tuple[array, bool]:
    if not samples:
        return samples, False
    window = min(len(samples), max(4, int(settings.sample_rate * 0.024)))
    head_peak_index = max(range(window), key=lambda index: abs(samples[index]))
    head_peak = abs(samples[head_peak_index])
    tail_start = min(len(samples), window)
    tail_end = min(len(samples), window * 3)
    tail = samples[tail_start:tail_end]
    tail_rms = math.sqrt(sum(value * value for value in tail) / len(tail)) if tail else 0.0
    local_radius = max(8, int(settings.sample_rate * 0.0015))
    local_values = [
        samples[index]
        for index in range(
            max(0, head_peak_index - local_radius), min(window, head_peak_index + local_radius)
        )
        if abs(index - head_peak_index) > 3
    ]
    local_rms = (
        math.sqrt(sum(value * value for value in local_values) / len(local_values))
        if local_values
        else 0.0
    )
    suspicious = (
        head_peak > max(0.45, tail_rms * 4.5, local_rms * 7.0) and head_peak_index < window // 2
    )
    if not suspicious:
        return samples, False
    repaired = array("f", samples)
    radius = max(2, int(settings.sample_rate * 0.0008))
    left = max(0, head_peak_index - radius)
    right = min(len(repaired) - 1, head_peak_index + radius)
    left_value = repaired[left - 1] if left > 0 else 0.0
    right_value = repaired[right + 1] if right + 1 < len(repaired) else repaired[right]
    width = max(1, right - left)
    for index in range(left, right + 1):
        ratio = (index - left) / width
        repaired[index] = left_value * (1.0 - ratio) + right_value * ratio
    original_rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    repaired_rms = math.sqrt(sum(value * value for value in repaired) / len(repaired))
    if original_rms and repaired_rms / original_rms < 0.82:
        return samples, False
    return repaired, True


def _normalize_segment(samples: array, settings: VoicePipelineSettings) -> array:
    if not samples:
        return samples
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    target = 10 ** (settings.segment_rms_db / 20.0)
    gain = min(10 ** (8.0 / 20.0), target / max(rms, 1e-9))
    ceiling = 10 ** (settings.peak_ceiling_db / 20.0)
    peak = max(abs(sample * gain) for sample in samples)
    if peak > ceiling:
        gain *= ceiling / peak
    return array("f", (max(-ceiling, min(ceiling, sample * gain)) for sample in samples))


def _fade_edges(samples: array, settings: VoicePipelineSettings) -> None:
    fade_in = min(len(samples), int(settings.sample_rate * settings.fade_in_ms / 1_000))
    fade_out = min(len(samples), int(settings.sample_rate * settings.fade_out_ms / 1_000))
    for index in range(fade_in):
        samples[index] *= (index + 1) / max(1, fade_in)
    for index in range(fade_out):
        samples[-1 - index] *= (index + 1) / max(1, fade_out)


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-6))
