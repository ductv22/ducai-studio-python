from __future__ import annotations

import json
import math
import os
import queue
import shutil
import sys
import threading
import time
import wave
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from ducai_studio.application.voice_pipeline import (
    AudioQualityReport,
    VoiceClip,
    VoicePipelineSettings,
    analyze_audio,
    assemble_voice_timeline,
    atomic_write_json,
    classify_cue,
    cue_request_fingerprint,
    file_sha256,
    measured_voice_timing_issues,
    plan_fit,
    process_voice_segment,
    read_pcm16_mono,
    write_pcm16_mono,
)
from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.domain.voice_conform import (
    VoiceConformPlan,
    decode_voice_conform_plan,
)
from ducai_studio.providers.audio_processing import FfmpegAudioProcessor
from ducai_studio.providers.tts import PiperTtsProvider, TtsCueRequest, VieNeuTtsProvider
from ducai_studio.providers.tts.contracts import (
    TtsCueArtifact,
    TtsProvider,
    TtsRuntimeUnavailable,
)
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


class WorkerStopped(RuntimeError):
    pass


def _tts_failure_payload(exc: Exception) -> dict[str, object]:
    if isinstance(exc, TtsRuntimeUnavailable):
        message = str(exc).strip() or "Bộ máy lồng tiếng chưa sẵn sàng."
        return {
            "code": type(exc).__name__,
            "message": message,
            "action": (
                "Mở bằng bản DUCAI mới nhất, kiểm tra Chẩn đoán runtime và giọng clone, "
                "sau đó bấm Tiếp tục để dùng lại cache/checkpoint hiện có."
            ),
        }
    code = str(exc) if str(exc).replace("_", "").isalnum() else type(exc).__name__
    return {"code": code[:80] or "tts_worker_failed"}


@contextmanager
def _exclusive_cache_lock(
    lock_path: Path,
    *,
    control_point: Callable[[], None],
    timeout_seconds: float = 600.0,
):
    """Serialize publication of a shared conform-cache fingerprint."""

    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while descriptor is None:
        control_point()
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > timeout_seconds
            except FileNotFoundError:
                continue
            if stale:
                with suppress(FileNotFoundError):
                    lock_path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("voice_conform_cache_lock_timeout") from None
            time.sleep(0.1)
    try:
        yield
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            lock_path.unlink()


@dataclass(frozen=True, slots=True)
class ArtifactApproval:
    """Result of the non-destructive audio/fit gate."""

    approved: bool
    retryable_inference: bool = False
    blocker: str = ""
    required_speed: float | None = None
    qa: AudioQualityReport | None = None


class TtsWorker:
    def __init__(
        self,
        job_id: str,
        *,
        provider_factory: Callable[[dict[str, Any]], TtsProvider] | None = None,
        audio_processor_factory: Callable[[], FfmpegAudioProcessor] = FfmpegAudioProcessor,
    ) -> None:
        self.job_id = job_id
        self.commands: queue.Queue[WorkerMessage] = queue.Queue()
        self.acknowledgements: dict[str, WorkerMessage] = {}
        self.spec: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.settings = VoicePipelineSettings()
        self.job_mode = "synthesis"
        self.conform_plan: VoiceConformPlan | None = None
        self.provider: TtsProvider | None = None
        self.provider_factory = provider_factory
        self.audio_processor_factory = audio_processor_factory
        self.cues: tuple[TtsCueRequest, ...] = ()
        self.force_cue_ids: frozenset[str] = frozenset()
        self._legacy_request_fingerprints: dict[str, str] = {}
        self._a36_request_fingerprints: dict[str, str] = {}
        self._allowed_prior_raw_signatures: frozenset[str] = frozenset()
        self._raw_job_identity_cache: dict[Path, bool] = {}
        self._matching_prior_ledger_cache: tuple[dict[str, Any], ...] | None = None
        self._prior_raw_index: dict[str, tuple[Path, ...]] | None = None
        self._global_cache_index: dict[
            tuple[str, str], tuple[dict[str, Any], ...]
        ] | None = None
        self._next_start_us: dict[int, int | None] = {}
        self.ineligible: dict[str, str] = {}
        self.ledger: dict[str, Any] = {}
        self._job_started = time.monotonic()
        self._synthesis_started = self._job_started
        self._synthesis_baseline_terminal = 0
        self._phase = "preparing"
        self._phase_detail = ""
        self.staging_root = (
            Path(os.environ.get("DUCAI_ARTIFACT_STAGING_ROOT", Path.cwd() / ".ducai-staging"))
            .expanduser()
            .resolve()
        )
        self.job_staging = self.staging_root / job_id
        self.job_staging.mkdir(parents=True, exist_ok=True)
        self.raw_root = self.job_staging / "raw"
        self.processed_root = self.job_staging / "processed"
        self.raw_root.mkdir(exist_ok=True)
        self.processed_root.mkdir(exist_ok=True)
        self.ledger_path = self.job_staging / "voice-ledger-working.json"
        self._reader = threading.Thread(target=self._read_commands, daemon=True)

    def send(self, kind: str, payload: dict[str, Any]) -> WorkerMessage:
        message = WorkerMessage(
            PROTOCOL_VERSION,
            self.job_id,
            str(uuid4()),
            kind,
            payload,
        )
        sys.stdout.buffer.write(message.to_json_line())
        sys.stdout.buffer.flush()
        return message

    def acknowledge(self, command_id: str, kind: str, payload: dict[str, Any]) -> None:
        message = self.send(kind, {"command_id": command_id, **payload})
        self.acknowledgements[command_id] = message

    def _read_commands(self) -> None:
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return
            try:
                message = WorkerMessage.from_json_line(line)
            except Exception:
                continue
            if message.job_id == self.job_id:
                self.commands.put(message)

    def run(self) -> int:
        self._reader.start()
        try:
            start = self.commands.get(timeout=30)
        except queue.Empty:
            self.send("failed", {"code": "tts_start_timeout"})
            return 2
        if start.kind != "control" or start.payload.get("action") != "start":
            self.send("failed", {"code": "tts_start_command_invalid"})
            return 2
        command_id = start.payload.get("command_id")
        if not isinstance(command_id, str):
            self.send("failed", {"code": "tts_start_command_invalid"})
            return 2
        try:
            self._configure(dict(start.payload))
            self.acknowledge(command_id, "started", {"status": "running"})
            self._execute()
            return 0
        except WorkerStopped:
            return 0
        except Exception as exc:
            self.send("failed", _tts_failure_payload(exc))
            return 2
        finally:
            if self.provider is not None:
                self.provider.close()

    def _configure(self, start_payload: dict[str, Any]) -> None:
        spec = start_payload.get("job")
        if not isinstance(spec, dict) or spec.get("kind") != "tts":
            raise ValueError("tts_job_spec_missing")
        config = spec.get("provider_config")
        if not isinstance(config, dict):
            raise ValueError("tts_provider_config_missing")
        raw_cues = config.get("cues")
        if not isinstance(raw_cues, list) or not raw_cues:
            raise ValueError("tts_cues_missing")
        raw_force_cue_ids = config.get("force_cue_ids", [])
        if (
            not isinstance(raw_force_cue_ids, list)
            or not all(isinstance(value, str) and value.strip() for value in raw_force_cue_ids)
        ):
            raise ValueError("tts_force_cue_ids_invalid")
        self.force_cue_ids = frozenset(value.strip() for value in raw_force_cue_ids)
        self.spec = spec
        self.config = config
        # New UI jobs treat semantic/timing findings as advisory.  Keep this
        # explicit at the worker boundary so legacy specs that omit it retain
        # their historical strict review behavior.
        self.qa_non_blocking = config.get("qa_non_blocking") is True
        self.job_mode = str(config.get("job_mode", "synthesis")).strip().casefold()
        if self.job_mode == "voice_conform":
            self._configure_conform(start_payload)
            return
        if self.job_mode != "synthesis":
            raise ValueError("tts_job_mode_invalid")
        raw_settings = config.get("pipeline_settings", {})
        if not isinstance(raw_settings, dict):
            raise ValueError("tts_pipeline_settings_invalid")
        self.settings = VoicePipelineSettings(**raw_settings)
        legacy_strict_settings = replace(
            self.settings,
            max_absolute_speed=1.28,
            voice_read_speed=1.0,
            adaptive_timing=False,
        )
        a36_pipeline = asdict(legacy_strict_settings)
        # a36 predates the adaptive_timing field.  ``False`` is not equivalent
        # for a content fingerprint: the missing field produced a different
        # managed raw WAV name and ledger signature.
        a36_pipeline.pop("adaptive_timing", None)
        a36_pipeline.pop("voice_read_speed", None)
        voice_identity = str(config.get("voice_identity", ""))
        synthesis_speed = (
            1.0
            if str(spec.get("provider_id", "")) == "vieneu-3.3"
            else float(config.get("engine_speed", 1.0))
        )
        prepared: list[TtsCueRequest] = []
        for raw in raw_cues:
            if not isinstance(raw, dict):
                raise ValueError("tts_cue_invalid")
            eligibility = classify_cue(str(raw.get("text", "")))
            cue_id = str(raw.get("id", ""))
            if not eligibility.eligible:
                if not cue_id:
                    raise ValueError("tts_cue_id_missing")
                self.ineligible[cue_id] = eligibility.reason
                continue
            cue_value = {
                "cue_id": cue_id,
                "index": int(raw["index"]),
                "start_us": int(raw["start_us"]),
                "end_us": int(raw["end_us"]),
                "text": eligibility.text,
            }
            fingerprint = cue_request_fingerprint(
                cue_value,
                provider_id=str(spec.get("provider_id", "")),
                provider_version=str(spec.get("provider_version", "")),
                model_id=str(spec.get("model_id", "")),
                voice_identity=voice_identity,
                settings=self.settings,
                synthesis_speed=synthesis_speed,
            )
            self._legacy_request_fingerprints[cue_id] = cue_request_fingerprint(
                cue_value,
                provider_id=str(spec.get("provider_id", "")),
                provider_version=str(spec.get("provider_version", "")),
                model_id=str(spec.get("model_id", "")),
                voice_identity=voice_identity,
                settings=legacy_strict_settings,
                synthesis_speed=synthesis_speed,
                include_timing_policy=True,
            )
            self._a36_request_fingerprints[cue_id] = stable_fingerprint(
                {
                    "schema": "ducai-voice-cue-v1",
                    "cue": cue_value,
                    "provider": str(spec.get("provider_id", "")),
                    "provider_version": str(spec.get("provider_version", "")),
                    "model": str(spec.get("model_id", "")),
                    "voice": voice_identity,
                    "pipeline": a36_pipeline,
                }
            )
            prepared.append(TtsCueRequest(request_fingerprint=fingerprint, **cue_value))
        if not prepared:
            raise ValueError("tts_no_eligible_cues")
        if [cue.index for cue in prepared] != sorted(cue.index for cue in prepared):
            raise ValueError("tts_cue_order_invalid")
        self.cues = tuple(prepared)
        self._next_start_us = {
            cue.index: (self.cues[offset + 1].start_us if offset + 1 < len(self.cues) else None)
            for offset, cue in enumerate(self.cues)
        }
        eligible_ids = {cue.cue_id for cue in self.cues}
        if not self.force_cue_ids <= eligible_ids:
            raise ValueError("tts_force_cue_not_eligible")
        signature = stable_fingerprint(
            {
                "schema": "ducai-voice-ledger-v1",
                "provider_id": spec.get("provider_id"),
                "provider_version": spec.get("provider_version"),
                "model_id": spec.get("model_id"),
                "voice_identity": voice_identity,
                "cues": [asdict(cue) for cue in self.cues],
                "ineligible": self.ineligible,
                "pipeline": asdict(self.settings),
            }
        )
        legacy_resume_pipeline = asdict(self.settings)
        legacy_resume_pipeline.pop("voice_read_speed", None)
        legacy_resume_signature = stable_fingerprint(
            {
                "schema": "ducai-voice-ledger-v1",
                "provider_id": spec.get("provider_id"),
                "provider_version": spec.get("provider_version"),
                "model_id": spec.get("model_id"),
                "voice_identity": voice_identity,
                "cues": [asdict(cue) for cue in self.cues],
                "ineligible": self.ineligible,
                "pipeline": legacy_resume_pipeline,
            }
        )
        legacy_cues = [
            replace(
                cue,
                request_fingerprint=self._legacy_request_fingerprints[cue.cue_id],
            )
            for cue in self.cues
        ]
        legacy_signature = stable_fingerprint(
            {
                "schema": "ducai-voice-ledger-v1",
                "provider_id": spec.get("provider_id"),
                "provider_version": spec.get("provider_version"),
                "model_id": spec.get("model_id"),
                "voice_identity": voice_identity,
                "cues": [asdict(cue) for cue in legacy_cues],
                "ineligible": self.ineligible,
                "pipeline": asdict(legacy_strict_settings),
            }
        )
        # Current base-cue fingerprints intentionally exclude timing policy.
        # A strict job created by this release therefore has current cue IDs
        # combined with the strict pipeline settings; keep that exact ledger
        # signature eligible when an adaptive job reuses approved WAVs.
        base_strict_signature = stable_fingerprint(
            {
                "schema": "ducai-voice-ledger-v1",
                "provider_id": spec.get("provider_id"),
                "provider_version": spec.get("provider_version"),
                "model_id": spec.get("model_id"),
                "voice_identity": voice_identity,
                "cues": [asdict(cue) for cue in self.cues],
                "ineligible": self.ineligible,
                "pipeline": asdict(legacy_strict_settings),
            }
        )
        a36_cues = [
            replace(
                cue,
                request_fingerprint=self._a36_request_fingerprints[cue.cue_id],
            )
            for cue in self.cues
        ]
        a36_signature = stable_fingerprint(
            {
                "schema": "ducai-voice-ledger-v1",
                "provider_id": spec.get("provider_id"),
                "provider_version": spec.get("provider_version"),
                "model_id": spec.get("model_id"),
                "voice_identity": voice_identity,
                "cues": [asdict(cue) for cue in a36_cues],
                "ineligible": self.ineligible,
                "pipeline": a36_pipeline,
            }
        )
        self._allowed_prior_raw_signatures = frozenset(
            (signature, base_strict_signature, legacy_signature, a36_signature)
        )
        restored = self._restore_checkpoint(
            start_payload.get("checkpoint"),
            signature,
            compatible_signatures=(legacy_resume_signature,),
        )
        self.ledger = restored or {
            "schema_version": 1,
            "signature": signature,
            "entries": {},
            "ineligible": self.ineligible,
            "blockers": {},
        }
        self.provider = self._provider_from_config()

    def _configure_conform(self, start_payload: dict[str, Any]) -> None:
        raw_plan = self.config.get("conform_plan")
        self.conform_plan = decode_voice_conform_plan(raw_plan)
        raw_cues = self.config.get("cues")
        if not isinstance(raw_cues, list) or not raw_cues:
            raise ValueError("voice_conform_cues_missing")
        prepared: list[TtsCueRequest] = []
        base_entries: list[dict[str, Any]] = []
        for raw in raw_cues:
            if not isinstance(raw, dict):
                raise ValueError("voice_conform_cue_invalid")
            base_path = Path(str(raw.get("base_wav_path", ""))).expanduser().resolve(
                strict=True
            )
            base_sha256 = str(raw.get("base_wav_sha256", ""))
            if not base_sha256 or file_sha256(base_path) != base_sha256:
                raise ValueError("voice_conform_base_cache_mismatch")
            cue = TtsCueRequest(
                cue_id=str(raw["id"]),
                index=int(raw["index"]),
                start_us=int(raw["start_us"]),
                end_us=int(raw["end_us"]),
                text=str(raw.get("text", "voice conform")) or "voice conform",
                request_fingerprint=base_sha256,
            )
            prepared.append(cue)
            base_entries.append(
                {
                    "cue_id": cue.cue_id,
                    "base_wav_path": str(base_path),
                    "base_wav_sha256": base_sha256,
                    "base_qa": raw.get("base_qa", raw.get("qa", {})),
                }
            )
        if [cue.index for cue in prepared] != sorted(cue.index for cue in prepared):
            raise ValueError("voice_conform_cue_order_invalid")
        decision_ids = {item.cue_id for item in self.conform_plan.cues}
        if decision_ids != {item.cue_id for item in prepared}:
            raise ValueError("voice_conform_plan_cue_mismatch")
        self.cues = tuple(prepared)
        self.settings = VoicePipelineSettings(
            sample_rate=int(self.config.get("sample_rate", 48_000))
        )
        signature = stable_fingerprint(
            {
                "schema": "ducai-voice-conform-ledger-v1",
                "plan": self.conform_plan.fingerprint,
                "base": base_entries,
            }
        )
        restored = self._restore_checkpoint(start_payload.get("checkpoint"), signature)
        self.ledger = restored or {
            "schema_version": 1,
            "signature": signature,
            "entries": {},
            "blockers": {},
            "base_entries": base_entries,
        }
        self.provider = None

    def _provider_from_config(self) -> TtsProvider:
        if self.provider_factory is not None:
            return self.provider_factory(self.config)
        provider_id = str(self.spec.get("provider_id", ""))
        runtime_python = Path(str(self.config["runtime_python"]))
        data_root = Path(str(self.config["data_root"]))
        managed_host = os.environ.get("DUCAI_TTS_ENGINE_HOST_MANAGED", "") == "1"
        runtime_host = (
            os.environ.get("DUCAI_TTS_ENGINE_HOST_PATH", "").strip()
            if managed_host
            else ""
        )
        host_script = Path(
            runtime_host or str(self.config["engine_host_path"])
        ).expanduser().resolve()
        runtime_host_hash = (
            os.environ.get("DUCAI_TTS_ENGINE_HOST_SHA256", "").strip()
            if managed_host
            else ""
        )
        if not host_script.is_file():
            raise TtsRuntimeUnavailable(
                f"Không tìm thấy bộ điều phối TTS tại: {host_script}"
            )
        if (
            runtime_host_hash
            and sha256(host_script.read_bytes()).hexdigest() != runtime_host_hash
        ):
            raise TtsRuntimeUnavailable(
                "Bộ điều phối TTS của bản DUCAI hiện tại không khớp manifest runtime."
            )
        if provider_id == "piper":
            return PiperTtsProvider(
                runtime_python=runtime_python,
                data_root=data_root,
                host_script=host_script,
                timeout_seconds=float(self.config.get("provider_timeout_seconds", 300)),
            )
        if provider_id == "vieneu-3.3":
            return VieNeuTtsProvider(
                runtime_python=runtime_python,
                data_root=data_root,
                profile_path=Path(str(self.config["profile_path"])),
                reference_audio_path=(
                    Path(str(self.config["reference_audio_path"]))
                    if self.config.get("reference_audio_path")
                    else None
                ),
                reference_denoise=self.config.get("reference_denoise") is True,
                host_script=host_script,
                timeout_seconds=float(self.config.get("provider_timeout_seconds", 1_800)),
                batch_size=int(self.config.get("batch_size", 64)),
            )
        raise ValueError("tts_provider_not_supported")

    def _restore_checkpoint(
        self,
        checkpoint: object,
        signature: str,
        *,
        compatible_signatures: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        if not isinstance(checkpoint, dict):
            return None
        artifact = checkpoint.get("artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("uri"), str):
            return None
        source = Path(str(artifact["uri"])).expanduser().resolve(strict=True)
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(raw, dict)
            or raw.get("signature") not in {signature, *compatible_signatures}
        ):
            raise ValueError("tts_checkpoint_signature_mismatch")
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            raise ValueError("tts_checkpoint_entries_invalid")
        valid: dict[str, Any] = {}
        for cue_id, entry in entries.items():
            if not isinstance(cue_id, str) or not isinstance(entry, dict):
                continue
            cache_path = entry.get("cache_path")
            cache_sha256 = entry.get("cache_sha256")
            if not isinstance(cache_path, str) or not isinstance(cache_sha256, str):
                continue
            path = Path(cache_path).expanduser().resolve()
            if path.is_file() and file_sha256(path) == cache_sha256:
                valid[cue_id] = entry
        raw["entries"] = valid
        raw["signature"] = signature
        return raw

    def _execute(self) -> None:
        if self.job_mode == "voice_conform":
            self._execute_conform()
            return
        assert self.provider is not None
        self._job_started = time.monotonic()
        entries = self.ledger.get("entries", {})
        if not isinstance(entries, dict):
            raise ValueError("tts_ledger_entries_invalid")
        missing: list[TtsCueRequest] = []
        reused_processed = 0
        reused_raw = 0
        cache_started = time.monotonic()
        self._send_stage_progress(
            "cache_restore",
            phase_current=0,
            phase_total=len(self.cues),
            eta_ms=None,
            extra={
                "phase_detail": "Đang đọc ledger và chuẩn bị đối chiếu cache WAV",
                "cache_restored_cues": len(entries),
                "reused_processed_cues": 0,
                "reprocessed_raw_cues": 0,
                "pending_synthesis_cues": 0,
            },
        )
        for checked, cue in enumerate(self.cues, 1):
            self._control_point()
            self._phase_detail = (
                f"Đang đối chiếu cache cue {cue.index}/{len(self.cues)}"
            )
            if cue.cue_id in entries:
                pass
            elif cue.cue_id in self.force_cue_ids:
                missing.append(cue)
            elif self._restore_global_cache(cue):
                pass
            elif self._restore_prior_processed(cue):
                reused_processed += 1
            else:
                prior_raw = self._restore_prior_raw(cue)
                if prior_raw is not None:
                    approval = self._evaluate_artifact(cue, prior_raw)
                    if approval.approved:
                        reused_raw += 1
                    elif not approval.retryable_inference:
                        self._record_blocker(cue, approval)
                    else:
                        missing.append(cue)
                else:
                    missing.append(cue)
            if checked == len(self.cues) or checked % 16 == 0:
                phase_elapsed_ms = max(
                    1, int((time.monotonic() - cache_started) * 1_000)
                )
                rate = checked / (phase_elapsed_ms / 1_000)
                remaining = max(0, len(self.cues) - checked)
                cache_eta_ms = int(remaining / rate * 1_000) if rate > 0 else None
                current_entries = self.ledger.get("entries", {})
                self._send_stage_progress(
                    "cache_restore",
                    phase_current=checked,
                    phase_total=len(self.cues),
                    eta_ms=cache_eta_ms,
                    extra={
                        "phase_detail": self._phase_detail,
                        "cache_restored_cues": (
                            len(current_entries) if isinstance(current_entries, dict) else 0
                        ),
                        "reused_processed_cues": reused_processed,
                        "reprocessed_raw_cues": reused_raw,
                        "pending_synthesis_cues": len(missing),
                    },
                )
        entries = self.ledger.get("entries", {})
        blockers = self.ledger.get("blockers", {})
        terminal = (len(entries) if isinstance(entries, dict) else 0) + (
            len(blockers) if isinstance(blockers, dict) else 0
        )
        self._synthesis_started = time.monotonic()
        self._synthesis_baseline_terminal = terminal
        self._phase_detail = (
            f"Đã dò cache; còn {len(missing)} cue cần tổng hợp"
        )
        provider_limit = 64 if str(self.spec.get("provider_id", "")) == "vieneu-3.3" else 32
        batch_size = max(1, min(provider_limit, int(self.config.get("batch_size", 16))))
        provider_speed = (
            1.0
            if str(self.spec.get("provider_id", "")) == "vieneu-3.3"
            else float(self.config.get("engine_speed", 1.0))
        )
        for offset in range(0, len(missing), batch_size):
            self._control_point()
            group = tuple(missing[offset : offset + batch_size])
            self._phase = "synthesis"
            provider_label = (
                "VieNeu ở 1,00×"
                if str(self.spec.get("provider_id", "")) == "vieneu-3.3"
                else f"Piper ở {provider_speed:.2f}×"
            )
            self._phase_detail = (
                f"Đang tổng hợp {provider_label} · batch {offset // batch_size + 1}/"
                f"{max(1, math.ceil(len(missing) / batch_size))}"
            )
            self._send_progress()
            result = self.provider.synthesize_batch(
                request_id=f"{self.job_id}-{offset}",
                cues=group,
                voice_id=str(self.config["voice_id"]),
                output_dir=self.raw_root,
                speed=provider_speed,
                batch_size=batch_size,
            )
            artifacts = {artifact.cue_id: artifact for artifact in result.artifacts}
            retry_cues: list[TtsCueRequest] = []
            for cue in group:
                self._control_point()
                artifact = artifacts.get(cue.cue_id)
                approval = self._evaluate_artifact(cue, artifact)
                if approval.retryable_inference:
                    retry_cues.append(cue)
                elif not approval.approved:
                    self._record_blocker(cue, approval)

            # Retry only technically invalid/missing audio, and keep the retry
            # batched. Duration-fit failures are terminal under the safety cap.
            if retry_cues:
                self._control_point()
                self._phase = "targeted_retry"
                self._send_progress()
                retry = self.provider.synthesize_batch(
                    request_id=f"{self.job_id}-{offset}-retry",
                    cues=tuple(retry_cues),
                    voice_id=str(self.config["voice_id"]),
                    output_dir=self.raw_root / "retry",
                    speed=provider_speed,
                    batch_size=min(batch_size, len(retry_cues)),
                )
                retry_artifacts = {artifact.cue_id: artifact for artifact in retry.artifacts}
                for cue in retry_cues:
                    approval = self._evaluate_artifact(cue, retry_artifacts.get(cue.cue_id))
                    if not approval.approved:
                        self._record_blocker(cue, approval)
            self._checkpoint()
            self._send_progress()
        # A cache-only run adopts every cue into this job's in-memory ledger,
        # but it never enters the synthesis loop above. Persist that adopted
        # ledger before publishing success so the importer can reconstruct one
        # D1 clip per cue instead of falling back to a single legacy clip.
        if not missing:
            self._checkpoint()
        self._publish_result()

    def _execute_conform(self) -> None:
        plan = self.conform_plan
        if plan is None:
            raise ValueError("voice_conform_plan_missing")
        self._job_started = time.monotonic()
        qa_path = Path(
            str(self.config.get("staging_qa_path", self.config["qa_path"]))
        ).expanduser().resolve()
        if not plan.applicable:
            issues = [
                {
                    "id": decision.index,
                    "cue_id": decision.cue_id,
                    "start_us": decision.source_start_us,
                    "end_us": decision.source_end_us,
                    "code": decision.issue_code,
                    "message": decision.issue_message,
                }
                for decision in plan.cues
                if decision.unresolved
            ]
            report = {
                "schema_version": 1,
                "kind": "voice_conform",
                "status": "REVIEW",
                "plan": plan.as_json(),
                "issues": issues,
            }
            atomic_write_json(qa_path, report)
            self._checkpoint(
                checkpoint_key="review",
                artifact_path=qa_path,
                artifact_kind="voice_conform_review_report",
                extra_payload={"stage": "review", "coverage": report},
            )
            self.send(
                "failed",
                {
                    "code": "voice_conform_review_required",
                    "status": "REVIEW",
                    "qa_path": str(qa_path),
                    "blocked_cues": len(issues),
                },
            )
            return

        raw_base_entries = self.ledger.get("base_entries", [])
        if not isinstance(raw_base_entries, list):
            raise ValueError("voice_conform_base_entries_invalid")
        base_by_id = {
            str(item.get("cue_id", "")): item
            for item in raw_base_entries
            if isinstance(item, dict)
        }
        entries = self.ledger.setdefault("entries", {})
        if not isinstance(entries, dict):
            raise ValueError("voice_conform_ledger_entries_invalid")
        cache_root = Path(str(self.config["conform_cache_root"])).expanduser().resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        processor = self.audio_processor_factory()
        decisions = tuple(sorted(plan.cues, key=lambda item: item.index))
        started = time.monotonic()
        cache_hits = 0
        rendered = 0
        self._phase_detail = "Đang đọc cache WAV nền và đối chiếu fingerprint"
        self._send_stage_progress(
            "conform_cache_restore",
            phase_current=0,
            phase_total=len(decisions),
            eta_ms=None,
            extra={"cache_hits": 0, "rendered_variants": 0},
        )
        for current, decision in enumerate(decisions, 1):
            self._control_point()
            base = base_by_id.get(decision.cue_id)
            if not isinstance(base, dict):
                raise ValueError("voice_conform_base_cue_missing")
            base_path = Path(str(base["base_wav_path"])).expanduser().resolve(strict=True)
            base_sha256 = str(base["base_wav_sha256"])
            if file_sha256(base_path) != base_sha256:
                raise ValueError("voice_conform_base_cache_mismatch")
            expected_frames = max(
                1,
                round(decision.voice_end_us * self.settings.sample_rate / 1_000_000)
                - round(decision.voice_start_us * self.settings.sample_rate / 1_000_000),
            )
            variant_fingerprint = stable_fingerprint(
                {
                    "schema": "ducai-voice-conform-wav-v2",
                    "base_sha256": base_sha256,
                    "audio_speed": round(decision.audio_speed, 8),
                    "sample_rate": self.settings.sample_rate,
                    "frame_count": expected_frames,
                    "processor": (
                        f"{type(processor).__module__}.{type(processor).__qualname__}:"
                        f"{getattr(processor, 'executable', '')}"
                    ),
                    "qa_settings": asdict(self.settings),
                }
            )
            variant_path = cache_root / f"{variant_fingerprint}.wav"
            variant_meta = variant_path.with_suffix(".json")
            reused = False
            with _exclusive_cache_lock(
                variant_path.with_suffix(".lock"),
                control_point=self._control_point,
            ):
                raw_meta: dict[str, Any] = {}
                if variant_path.is_file() and variant_meta.is_file():
                    try:
                        candidate_meta = json.loads(variant_meta.read_text(encoding="utf-8"))
                        if isinstance(candidate_meta, dict):
                            raw_meta = candidate_meta
                        reused = bool(
                            raw_meta.get("schema_version") == 2
                            and raw_meta.get("fingerprint") == variant_fingerprint
                            and raw_meta.get("base_sha256") == base_sha256
                            and raw_meta.get("frame_count") == expected_frames
                            and raw_meta.get("wav_sha256") == file_sha256(variant_path)
                        )
                    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                        reused = False
                if not reused:
                    self._phase = "conform_audio_stretch"
                    self._phase_detail = (
                        f"Đang hậu xử lý cue {decision.index} ở {decision.audio_speed:.2f}×"
                    )
                    temp_variant = variant_path.with_name(
                        f".{variant_path.stem}.{uuid4().hex}.tmp.wav"
                    )
                    try:
                        if abs(decision.audio_speed - 1.0) < 1e-9:
                            shutil.copyfile(base_path, temp_variant)
                        else:
                            processor.time_stretch(
                                base_path,
                                temp_variant,
                                speed=decision.audio_speed,
                                sample_rate=self.settings.sample_rate,
                            )
                        samples, sample_rate = read_pcm16_mono(temp_variant)
                        if sample_rate != self.settings.sample_rate:
                            raise ValueError("voice_conform_variant_sample_rate_mismatch")
                        if len(samples) != expected_frames:
                            samples = samples[:expected_frames]
                            if len(samples) < expected_frames:
                                samples.extend([0.0] * (expected_frames - len(samples)))
                            write_pcm16_mono(temp_variant, samples, sample_rate)
                        qa = analyze_audio(samples, sample_rate, self.settings)
                        if not qa.ok:
                            raise ValueError("voice_conform_variant_qa_failed")
                        if len(samples) != expected_frames:
                            raise ValueError("voice_conform_variant_duration_mismatch")
                        wav_sha256 = file_sha256(temp_variant)
                        os.replace(temp_variant, variant_path)
                        atomic_write_json(
                            variant_meta,
                            {
                                "schema_version": 2,
                                "fingerprint": variant_fingerprint,
                                "base_sha256": base_sha256,
                                "audio_speed": decision.audio_speed,
                                "frame_count": expected_frames,
                                "wav_sha256": wav_sha256,
                                "qa": asdict(qa),
                            },
                        )
                    except BaseException:
                        temp_variant.unlink(missing_ok=True)
                        raise
                    rendered += 1
                else:
                    qa = AudioQualityReport(**dict(raw_meta["qa"]))
                    cache_hits += 1
            entries[decision.cue_id] = {
                "cue_id": decision.cue_id,
                "cache_path": str(variant_path),
                "cache_sha256": file_sha256(variant_path),
                "qa": asdict(qa),
                "variant_fingerprint": variant_fingerprint,
                "audio_speed": decision.audio_speed,
            }
            elapsed = max(0.001, time.monotonic() - started)
            rate = current / elapsed
            eta_ms = int((len(decisions) - current) / rate * 1_000) if rate > 0 else None
            if current == len(decisions) or current % 8 == 0:
                self._checkpoint()
                self._send_stage_progress(
                    "conform_audio_stretch",
                    phase_current=current,
                    phase_total=len(decisions),
                    eta_ms=eta_ms,
                    extra={
                        "cache_hits": cache_hits,
                        "rendered_variants": rendered,
                        "phase_detail": self._phase_detail,
                    },
                )

        clips: list[VoiceClip] = []
        decision_by_id = {item.cue_id: item for item in decisions}
        for cue in self.cues:
            decision = decision_by_id[cue.cue_id]
            entry = entries[cue.cue_id]
            qa = AudioQualityReport(**dict(entry["qa"]))
            clips.append(
                VoiceClip(
                    cue.cue_id,
                    decision.voice_start_us,
                    decision.voice_start_us + qa.duration_us,
                    Path(str(entry["cache_path"])),
                    qa,
                    str(entry["variant_fingerprint"]),
                )
            )
        self._phase = "conform_timeline_assembly"
        self._phase_detail = "Đang ráp D1 theo bản đồ thời gian đầu ra"
        assembled = self.job_staging / "voice-conformed-assembled.wav"
        mix = assemble_voice_timeline(
            tuple(clips),
            assembled,
            total_duration_us=plan.output_duration_us,
            sample_rate=self.settings.sample_rate,
            progress=lambda current, total: self._send_stage_progress(
                "conform_timeline_assembly",
                phase_current=current,
                phase_total=total,
                eta_ms=None,
            ),
        )
        if int(mix["overlap_count"]) > 0:
            raise ValueError("voice_conform_timeline_overlap_guard_failed")
        output_path = Path(
            str(self.config.get("staging_output_path", self.config["output_path"]))
        ).expanduser().resolve()
        self._phase = "conform_final_master"
        self._phase_detail = "Đang chuẩn hóa loudness D1 đã đồng bộ"

        def master_progress(
            stage: str,
            current_us: int,
            duration_us: int,
            eta_ms: int | None,
        ) -> None:
            self._control_point()
            self._send_stage_progress(
                f"conform_master_{stage}",
                phase_current=current_us,
                phase_total=duration_us,
                eta_ms=eta_ms,
                extra={"phase_detail": self._phase_detail},
            )

        master = processor.master_ebu_r128(
            assembled,
            output_path,
            target_lufs=float(self.config.get("target_lufs", -18.0)),
            progress=master_progress,
        )
        # Close the worker-side cancel window after the final blocking FFmpeg
        # phase. The manager also rejects late success defensively.
        self._control_point()
        report = {
            "schema_version": 1,
            "kind": "voice_conform",
            "status": "PASS",
            "plan": plan.as_json(),
            "output_path": str(output_path),
            "output_sha256": file_sha256(output_path),
            "cache_hits": cache_hits,
            "rendered_variants": rendered,
            "mix": mix,
            "master": master,
        }
        atomic_write_json(qa_path, report)
        self.send(
            "succeeded",
            {
                "status": "PASS",
                "purpose": "voice_conform",
                "output_path": str(output_path),
                "qa_path": str(qa_path),
                "cue_count": len(clips),
                "coverage_ratio": 1.0,
                "plan_fingerprint": plan.fingerprint,
            },
        )

    def _approve_artifact(self, cue: TtsCueRequest, artifact: TtsCueArtifact) -> bool:
        return self._evaluate_artifact(cue, artifact).approved

    def _evaluate_artifact(
        self, cue: TtsCueRequest, artifact: TtsCueArtifact | None
    ) -> ArtifactApproval:
        if artifact is None:
            return ArtifactApproval(False, retryable_inference=True, blocker="tts_artifact_missing")
        processed = self.processed_root / f"{cue.index:06d}-{cue.request_fingerprint[:16]}.wav"
        try:
            qa = process_voice_segment(artifact.wav_path, processed, self.settings)
        except (OSError, ValueError, EOFError, wave.Error):
            return ArtifactApproval(False, retryable_inference=True, blocker="tts_audio_invalid")
        if not qa.ok:
            return ArtifactApproval(
                False,
                retryable_inference=True,
                blocker="tts_audio_quality_failed",
                qa=qa,
            )
        return self._evaluate_base_audio(cue, processed, qa)

    def _evaluate_base_audio(
        self,
        cue: TtsCueRequest,
        base_path: Path,
        base_qa: AudioQualityReport,
    ) -> ArtifactApproval:
        """Create the selected speed variant directly from the immutable 1x WAV."""

        next_start = self._next_start_us.get(cue.index)
        fit = plan_fit(
            cue,
            audio_duration_us=base_qa.duration_us,
            next_start_us=next_start,
            settings=self.settings,
        )
        defer_timing = (
            self.config.get("defer_timing_conform") is True
            and self.config.get("purpose") == "production"
        ) or self.config.get("allow_timeline_overflow") is True
        if not fit.fits and not defer_timing and not self.qa_non_blocking:
            return ArtifactApproval(
                False,
                blocker=fit.blocker,
                required_speed=fit.required_total_speed,
                qa=base_qa,
            )

        cache_root = Path(str(self.config["cache_root"])).expanduser().resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        base_sha256 = file_sha256(base_path)
        base_cache_path = cache_root / f"{cue.request_fingerprint}.base.wav"
        if not base_cache_path.is_file() or file_sha256(base_cache_path) != base_sha256:
            temp_base = base_cache_path.with_name(
                base_cache_path.name + f".{uuid4().hex}.tmp"
            )
            shutil.copyfile(base_path, temp_base)
            os.replace(temp_base, base_cache_path)
        base_sha256 = file_sha256(base_cache_path)
        base_metadata = {
            "schema_version": 3,
            "cache_tier": "base",
            "cue_id": cue.cue_id,
            "request_fingerprint": cue.request_fingerprint,
            "cache_path": str(base_cache_path),
            "cache_sha256": base_sha256,
            "base_cache_path": str(base_cache_path),
            "base_cache_sha256": base_sha256,
            "read_speed": 1.0,
            "total_speed": 1.0,
            "qa": asdict(base_qa),
            "base_qa": asdict(base_qa),
        }
        atomic_write_json(base_cache_path.with_suffix(".json"), base_metadata)

        total_speed = fit.applied_total_speed
        processor = self.audio_processor_factory() if total_speed > 1.0005 else None
        processor_identity = (
            f"{type(processor).__module__}.{type(processor).__qualname__}:"
            f"{getattr(processor, 'executable', '')}"
            if processor is not None
            else "pcm-copy-v1"
        )
        variant_fingerprint = stable_fingerprint(
            {
                "schema": "ducai-voice-speed-variant-v1",
                "base_wav_sha256": base_sha256,
                "total_speed": round(total_speed, 8),
                "processor": processor_identity,
                "sample_rate": self.settings.sample_rate,
                "qa_settings": {
                    key: value
                    for key, value in asdict(self.settings).items()
                    if key not in {"voice_read_speed", "max_absolute_speed", "adaptive_timing"}
                },
            }
        )
        cache_path = cache_root / f"{variant_fingerprint}.wav"
        metadata_path = cache_path.with_suffix(".json")
        qa: AudioQualityReport | None = None
        if cache_path.is_file() and metadata_path.is_file():
            try:
                cached_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (
                    isinstance(cached_metadata, dict)
                    and cached_metadata.get("variant_fingerprint") == variant_fingerprint
                    and cached_metadata.get("base_cache_sha256") == base_sha256
                    and cached_metadata.get("cache_sha256") == file_sha256(cache_path)
                    and isinstance(cached_metadata.get("qa"), dict)
                ):
                    qa = AudioQualityReport(**dict(cached_metadata["qa"]))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                qa = None
        if qa is None:
            self._phase = (
                "cue_timing_fit"
                if total_speed > self.settings.voice_read_speed + 1e-9
                else "base_read_speed"
            )
            self._phase_detail = (
                f"Đang khớp cue dài {cue.index} ở tổng {total_speed:.2f}×"
                if self._phase == "cue_timing_fit"
                else f"Đang tạo WAV tốc độ gốc {self.settings.voice_read_speed:.2f}×"
            )
            temp_variant = cache_path.with_name(
                cache_path.name + f".{uuid4().hex}.tmp.wav"
            )
            if processor is None:
                shutil.copyfile(base_cache_path, temp_variant)
            else:
                processor.time_stretch(
                    base_cache_path,
                    temp_variant,
                    speed=total_speed,
                    sample_rate=self.settings.sample_rate,
                )
            samples, sample_rate = read_pcm16_mono(temp_variant)
            qa = analyze_audio(
                samples,
                sample_rate,
                self.settings,
                repaired_onset=base_qa.repaired_onset,
            )
            expected_duration_us = int(math.ceil(base_qa.duration_us / total_speed))
            if not qa.ok or abs(qa.duration_us - expected_duration_us) > 50_000:
                temp_variant.unlink(missing_ok=True)
                return ArtifactApproval(
                    False,
                    blocker="tts_fit_validation_failed",
                    required_speed=fit.required_total_speed,
                    qa=qa,
                )
            os.replace(temp_variant, cache_path)

        metadata = {
            "schema_version": 3,
            "cache_tier": "variant",
            "cue_id": cue.cue_id,
            "request_fingerprint": cue.request_fingerprint,
            "cache_path": str(cache_path),
            "cache_sha256": file_sha256(cache_path),
            "variant_fingerprint": variant_fingerprint,
            "base_cache_path": str(base_cache_path),
            "base_cache_sha256": base_sha256,
            "read_speed": self.settings.voice_read_speed,
            "total_speed": total_speed,
            "additional_cue_speed": total_speed / self.settings.voice_read_speed,
            "processor": processor_identity,
            "qa": asdict(qa),
            "base_qa": asdict(base_qa),
            "fit": asdict(fit),
        }
        atomic_write_json(metadata_path, metadata)
        self.ledger.setdefault("entries", {})[cue.cue_id] = metadata
        self.ledger.setdefault("blockers", {}).pop(cue.cue_id, None)
        return ArtifactApproval(True, qa=qa)

    def _record_blocker(self, cue: TtsCueRequest, approval: ArtifactApproval) -> None:
        fit_evidence: dict[str, Any] = {}
        if approval.qa is not None and approval.qa.duration_us > 0:
            planned = plan_fit(
                cue,
                audio_duration_us=approval.qa.duration_us,
                next_start_us=self._next_start_us.get(cue.index),
                settings=self.settings,
            )
            fit_evidence = asdict(planned)
        self.ledger.setdefault("blockers", {})[cue.cue_id] = {
            "index": cue.index,
            "code": approval.blocker or "tts_cue_retry_exhausted",
            **({"fit": fit_evidence} if fit_evidence else {}),
            **(
                {
                    "required_speed": approval.required_speed,
                    "required_total_speed": approval.required_speed,
                    "applied_total_speed": (
                        fit_evidence.get("applied_total_speed")
                        if fit_evidence
                        else self.settings.voice_read_speed
                    ),
                    "base_speed": self.settings.voice_read_speed,
                    "speed_cap": self.settings.max_absolute_speed,
                }
                if approval.required_speed is not None
                else {}
            ),
        }

    def _restore_global_cache(self, cue: TtsCueRequest) -> bool:
        cache_root = Path(str(self.config["cache_root"])).expanduser().resolve()
        fingerprints = tuple(
            dict.fromkeys(
                (
                    cue.request_fingerprint,
                    self._legacy_request_fingerprints.get(cue.cue_id, ""),
                    self._a36_request_fingerprints.get(cue.cue_id, ""),
                )
            )
        )
        for fingerprint in fingerprints:
            if not fingerprint:
                continue
            candidates: list[dict[str, Any]] = []
            cache_path = cache_root / f"{fingerprint}.wav"
            metadata_path = cache_path.with_suffix(".json")
            raw = self._read_global_cache_metadata(
                cue,
                fingerprint=fingerprint,
                cache_path=cache_path if cache_path.is_file() else None,
                metadata_path=metadata_path if metadata_path.is_file() else None,
            )
            if raw is not None:
                candidates.append(raw)
            candidates.extend(
                self._global_cache_metadata_index().get((cue.cue_id, fingerprint), ())
            )
            seen_paths: set[str] = set()
            for candidate in candidates:
                candidate_path = str(candidate.get("cache_path", ""))
                if not candidate_path or candidate_path in seen_paths:
                    continue
                seen_paths.add(candidate_path)
                raw = self._read_global_cache_metadata(
                    cue,
                    fingerprint=fingerprint,
                    cache_path=None,
                    metadata_path=None,
                    metadata=candidate,
                )
                if raw is None:
                    continue
                base_path_raw = raw.get("base_cache_path", raw.get("cache_path"))
                base_sha256 = str(
                    raw.get("base_cache_sha256", raw.get("cache_sha256", ""))
                )
                base_qa_raw = raw.get("base_qa", raw.get("qa"))
                if not isinstance(base_path_raw, str) or not isinstance(base_qa_raw, dict):
                    continue
                base_path = Path(base_path_raw).expanduser().resolve()
                try:
                    if (
                        not base_path.is_relative_to(cache_root)
                        or not base_path.is_file()
                        or file_sha256(base_path) != base_sha256
                    ):
                        continue
                    base_qa = AudioQualityReport(**dict(base_qa_raw))
                except (OSError, TypeError, ValueError):
                    continue
                expected_fit = plan_fit(
                    cue,
                    audio_duration_us=base_qa.duration_us,
                    next_start_us=self._next_start_us.get(cue.index),
                    settings=self.settings,
                )
                cached_read_speed = float(raw.get("read_speed", 1.0) or 1.0)
                cached_total_speed = float(
                    raw.get("total_speed", raw.get("read_speed", 1.0)) or 1.0
                )
                cached_fit = raw.get("fit")
                exact_variant = (
                    raw.get("cache_tier") == "variant"
                    and isinstance(cached_fit, dict)
                    and abs(cached_read_speed - self.settings.voice_read_speed) < 1e-9
                    and abs(cached_total_speed - expected_fit.applied_total_speed) < 1e-9
                    and cached_fit.get("adaptive_timing") is self.settings.adaptive_timing
                )
                if exact_variant:
                    adopted = dict(raw)
                    adopted["cue_id"] = cue.cue_id
                    adopted["request_fingerprint"] = cue.request_fingerprint
                    self.ledger.setdefault("entries", {})[cue.cue_id] = adopted
                    self.ledger.setdefault("blockers", {}).pop(cue.cue_id, None)
                    return True
                approval = self._evaluate_base_audio(cue, base_path, base_qa)
                if approval.approved:
                    return True
                if not approval.retryable_inference:
                    self._record_blocker(cue, approval)
                    return True
        return False

    def _global_cache_metadata_index(
        self,
    ) -> dict[tuple[str, str], tuple[dict[str, Any], ...]]:
        if self._global_cache_index is not None:
            return self._global_cache_index
        cache_root = Path(str(self.config["cache_root"])).expanduser().resolve()
        collected: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for metadata_path in cache_root.glob("*.json"):
            try:
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            cue_id = raw.get("cue_id")
            fingerprint = raw.get("request_fingerprint")
            if isinstance(cue_id, str) and cue_id and isinstance(fingerprint, str) and fingerprint:
                collected.setdefault((cue_id, fingerprint), []).append(raw)
        self._global_cache_index = {
            key: tuple(values) for key, values in collected.items()
        }
        return self._global_cache_index

    def _read_global_cache_metadata(
        self,
        cue: TtsCueRequest,
        *,
        fingerprint: str,
        cache_path: Path | None,
        metadata_path: Path | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        raw = metadata
        if raw is None:
            if cache_path is None or metadata_path is None:
                return None
            try:
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return None
        if not isinstance(raw, dict):
            return None
        if (
            raw.get("request_fingerprint") != fingerprint
            or raw.get("cue_id") != cue.cue_id
            or not isinstance(raw.get("cache_path"), str)
            or not isinstance(raw.get("cache_sha256"), str)
            or not isinstance(raw.get("qa"), dict)
            or raw["qa"].get("ok") is not True
        ):
            return None
        cache_root = Path(str(self.config["cache_root"])).expanduser().resolve()
        resolved_cache_path = Path(raw["cache_path"]).expanduser().resolve()
        try:
            if not resolved_cache_path.is_relative_to(cache_root):
                return None
            if (
                not resolved_cache_path.is_file()
                or file_sha256(resolved_cache_path) != raw["cache_sha256"]
            ):
                return None
        except OSError:
            return None
        return raw

    def _restore_prior_raw(self, cue: TtsCueRequest) -> TtsCueArtifact | None:
        """Reuse matching raw inference from an earlier managed TTS job.

        Timing policy is intentionally post-inference. A switch from strict
        timing to adaptive 1.5x must not ask VieNeu to synthesize the identical
        text/profile again. The legacy fingerprint keeps the complete provider,
        model, voice and cue identity while restoring the previous strict timing
        fields used by a36. A deliberate force-regeneration never reaches here.
        """

        fingerprints = tuple(
            dict.fromkeys(
                (
                    cue.request_fingerprint,
                    self._legacy_request_fingerprints.get(cue.cue_id, ""),
                    self._a36_request_fingerprints.get(cue.cue_id, ""),
                )
            )
        )
        candidates: list[Path] = []
        for fingerprint in fingerprints:
            if not fingerprint:
                continue
            filename = f"{cue.index:06d}-{fingerprint[:16]}.wav"
            candidates.extend(self._prior_raw_candidates(filename))
        for path in sorted(
            {candidate.resolve() for candidate in candidates},
            key=lambda candidate: candidate.stat().st_mtime_ns if candidate.is_file() else 0,
            reverse=True,
        ):
            if (
                not path.is_file()
                or not path.is_relative_to(self.staging_root)
            ):
                continue
            try:
                with wave.open(str(path), "rb") as stream:
                    sample_rate = stream.getframerate()
                    duration_us = int(
                        round(stream.getnframes() * 1_000_000 / sample_rate)
                    )
            except (OSError, EOFError, wave.Error, ZeroDivisionError):
                continue
            return TtsCueArtifact(
                cue.cue_id,
                cue.request_fingerprint,
                path,
                sample_rate,
                duration_us,
                "managed-prior-raw",
            )
        return None

    def _prior_raw_candidates(self, filename: str) -> tuple[Path, ...]:
        index = self._prior_raw_index
        if index is None:
            collected: dict[str, list[Path]] = {}
            for candidate in self.staging_root.glob("*/raw/*.wav"):
                try:
                    path = candidate.resolve()
                    if (
                        path.is_file()
                        and path.parent.parent != self.job_staging.resolve()
                        and path.is_relative_to(self.staging_root)
                    ):
                        collected.setdefault(path.name, []).append(path)
                except OSError:
                    continue
            index = {
                name: tuple(
                    sorted(
                        paths,
                        key=lambda path: path.stat().st_mtime_ns if path.is_file() else 0,
                        reverse=True,
                    )
                )
                for name, paths in collected.items()
            }
            self._prior_raw_index = index
        return index.get(filename, ())

    def _restore_prior_processed(self, cue: TtsCueRequest) -> bool:
        """Adopt a stricter prior approved clip without running FFmpeg again.

        A clip that passed a36's 1.28x timing gate also passes the adaptive
        1.5x gate unchanged.  Only prior blockers need raw post-processing;
        already-approved clips keep their exact managed WAV and QA evidence.
        """

        allowed_fingerprints = {
            cue.request_fingerprint,
            self._legacy_request_fingerprints.get(cue.cue_id, ""),
            self._a36_request_fingerprints.get(cue.cue_id, ""),
        }
        allowed_fingerprints.discard("")
        for ledger in self._matching_prior_ledgers():
            entries = ledger.get("entries")
            if not isinstance(entries, dict):
                continue
            entry = entries.get(cue.cue_id)
            if not isinstance(entry, dict):
                continue
            if entry.get("request_fingerprint") not in allowed_fingerprints:
                continue
            qa = entry.get("qa")
            fit = entry.get("fit")
            if (
                not isinstance(qa, dict)
                or qa.get("ok") is not True
                or not isinstance(fit, dict)
                or fit.get("fits") is not True
            ):
                continue
            try:
                prior_read_speed = float(entry.get("read_speed", 1.0) or 1.0)
                prior_total_speed = float(
                    entry.get(
                        "total_speed",
                        fit.get("applied_total_speed", fit.get("absolute_speed", 1.0)),
                    )
                    or 1.0
                )
                base_qa_raw = entry.get("base_qa", qa)
                if not isinstance(base_qa_raw, dict):
                    continue
                base_duration_us = int(base_qa_raw.get("duration_us", 0) or 0)
                expected_fit = plan_fit(
                    cue,
                    audio_duration_us=base_duration_us,
                    next_start_us=self._next_start_us.get(cue.index),
                    settings=self.settings,
                )
            except (TypeError, ValueError):
                continue
            if (
                abs(prior_read_speed - self.settings.voice_read_speed) > 1e-9
                or abs(prior_total_speed - expected_fit.applied_total_speed) > 1e-9
            ):
                continue
            cache_path = entry.get("cache_path")
            cache_sha256 = entry.get("cache_sha256")
            if not isinstance(cache_path, str) or not isinstance(cache_sha256, str):
                continue
            path = Path(cache_path).expanduser().resolve()
            try:
                if not path.is_file() or file_sha256(path) != cache_sha256:
                    continue
            except OSError:
                continue
            adopted = dict(entry)
            adopted["cue_id"] = cue.cue_id
            adopted["request_fingerprint"] = cue.request_fingerprint
            self.ledger.setdefault("entries", {})[cue.cue_id] = adopted
            self.ledger.setdefault("blockers", {}).pop(cue.cue_id, None)
            return True
        return False

    def _matching_prior_ledgers(self) -> tuple[dict[str, Any], ...]:
        cached = self._matching_prior_ledger_cache
        if cached is not None:
            return cached
        ledgers: list[dict[str, Any]] = []
        for ledger_path in self.staging_root.glob("*/voice-ledger-working.json"):
            job_root = ledger_path.parent.resolve()
            if job_root == self.job_staging.resolve() or not job_root.is_relative_to(
                self.staging_root
            ):
                continue
            try:
                raw = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(raw, dict)
                and raw.get("signature") in self._allowed_prior_raw_signatures
            ):
                ledgers.append(raw)
        self._matching_prior_ledger_cache = tuple(ledgers)
        return self._matching_prior_ledger_cache

    def _prior_raw_job_matches(self, job_root: Path) -> bool:
        job_root = job_root.resolve()
        cached = self._raw_job_identity_cache.get(job_root)
        if cached is not None:
            return cached
        matches = False
        ledger_path = job_root / "voice-ledger-working.json"
        if job_root.is_relative_to(self.staging_root) and ledger_path.is_file():
            try:
                raw = json.loads(ledger_path.read_text(encoding="utf-8"))
                matches = (
                    isinstance(raw, dict)
                    and raw.get("signature") in self._allowed_prior_raw_signatures
                )
            except (OSError, json.JSONDecodeError):
                matches = False
        self._raw_job_identity_cache[job_root] = matches
        return matches

    def _publish_result(self) -> None:
        entries = self.ledger.get("entries", {})
        blockers = self.ledger.get("blockers", {})
        if not isinstance(entries, dict) or not isinstance(blockers, dict):
            raise ValueError("tts_ledger_invalid")
        expected_ids = {cue.cue_id for cue in self.cues}
        missing = sorted(expected_ids - set(entries))
        for cue_id in missing:
            blockers.setdefault(cue_id, {"code": "tts_missing_approved_clip"})
        coverage = {
            "total_cues": len(self.cues) + len(self.ineligible),
            "eligible_cues": len(self.cues),
            "approved_cues": len(entries),
            "ineligible_cues": len(self.ineligible),
            "blocked_cues": len(blockers),
            "coverage_ratio": len(entries) / max(1, len(self.cues)),
            "ineligible": [
                {"cue_id": cue_id, "reason": reason}
                for cue_id, reason in sorted(self.ineligible.items())
            ],
            "blockers": [
                {"cue_id": cue_id, **value}
                for cue_id, value in sorted(blockers.items())
                if isinstance(value, dict)
            ],
        }
        timing_issues = measured_voice_timing_issues(
            self.cues,
            entries,
            blockers,
            max_absolute_speed=self.settings.max_absolute_speed,
        )
        applied_total_speeds = [
            float(entry.get("total_speed", self.settings.voice_read_speed))
            for entry in entries.values()
            if isinstance(entry, dict)
        ]
        adjusted_cue_count = sum(
            speed > self.settings.voice_read_speed + 1e-9
            for speed in applied_total_speeds
        )
        maximum_additional_factor = max(
            (
                speed / self.settings.voice_read_speed
                for speed in applied_total_speeds
            ),
            default=1.0,
        )
        coverage.update(
            {
                "job_id": self.job_id,
                "input_fingerprint": self.spec.get("input_fingerprint", ""),
                "settings_fingerprint": self.spec.get("settings_fingerprint", ""),
                "voice_read_speed": self.settings.voice_read_speed,
                "cue_max_total_speed": self.settings.max_absolute_speed,
                "adjusted_cue_count": adjusted_cue_count,
                "maximum_total_speed_applied": max(
                    applied_total_speeds,
                    default=self.settings.voice_read_speed,
                ),
                "maximum_additional_factor": maximum_additional_factor,
                "unresolved_timing_cue_count": len(timing_issues),
                "qa_non_blocking": self.qa_non_blocking,
            }
        )
        coverage["timing_issue_count"] = len(timing_issues)
        coverage["timing_warning_count"] = len(timing_issues)
        coverage["timing_issues"] = timing_issues
        qa_path = Path(
            str(self.config.get("staging_qa_path", self.config["qa_path"]))
        ).expanduser().resolve()
        if blockers or len(entries) != len(self.cues):
            coverage["status"] = "REVIEW"
            atomic_write_json(qa_path, coverage)
            self._checkpoint(
                checkpoint_key="review",
                artifact_path=qa_path,
                artifact_kind="tts_review_report",
                extra_payload={"stage": "review", "coverage": coverage},
            )
            self.send(
                "failed",
                {
                    "code": "tts_review_required",
                    "status": "REVIEW",
                    "qa_path": str(qa_path),
                    "blocked_cues": len(blockers),
                    "voice_read_speed": self.settings.voice_read_speed,
                    "cue_max_total_speed": self.settings.max_absolute_speed,
                    "adjusted_cue_count": adjusted_cue_count,
                    "maximum_additional_factor": maximum_additional_factor,
                    "unresolved_timing_cue_count": len(timing_issues),
                },
            )
            return
        clips: list[VoiceClip] = []
        occupied_until_us = 0
        shifted_cues = 0
        maximum_delay_us = 0
        # The full-voice timeline override is a no-loss workflow: a cue that
        # runs past the next source timestamp must be placed after the
        # previous clip instead of being mixed on top of it.  The old
        # override only enabled duration growth, leaving this guard to reject
        # the otherwise valid cached WAVs.  Treat the explicit overflow
        # option as opting into the same serialized timeline policy.
        defer_timing = (
            self.config.get("purpose") == "production"
            and (
                self.config.get("defer_timing_conform") is True
                or self.config.get("allow_timeline_overflow") is True
                or self.qa_non_blocking
            )
        )
        for cue in self.cues:
            entry = entries[cue.cue_id]
            qa = AudioQualityReport(**entry["qa"])
            clip_start_us = cue.start_us
            # Adaptive variants have already been measured and fitted against
            # their own SRT slots.  Keep their absolute source timestamps so a
            # long cue can never push every later sentence out of sync.  Only
            # the explicit legacy no-loss workflow serializes overlapping WAVs.
            if defer_timing:
                clip_start_us = max(cue.start_us, occupied_until_us)
            delay_us = max(0, clip_start_us - cue.start_us)
            if delay_us:
                shifted_cues += 1
                maximum_delay_us = max(maximum_delay_us, delay_us)
            clip_end_us = clip_start_us + qa.duration_us
            occupied_until_us = clip_end_us
            clips.append(
                VoiceClip(
                    cue.cue_id,
                    clip_start_us,
                    clip_end_us,
                    Path(entry["cache_path"]),
                    qa,
                    cue.request_fingerprint,
                )
            )
        self._phase = "timeline_assembly"
        assembled = self.job_staging / "voice-assembled.wav"
        total_duration_us = int(self.config["media_duration_us"])
        if self.config.get("purpose") == "preview":
            total_duration_us = max(clip.start_us + clip.qa.duration_us + 250_000 for clip in clips)
        elif defer_timing and clips:
            source_media_duration_us = total_duration_us
            final_voice_end_us = max(clip.end_us for clip in clips)
            total_duration_us = max(total_duration_us, final_voice_end_us)
            coverage["timing"] = {
                "mode": "base_no_loss",
                "shifted_cues": shifted_cues,
                "maximum_delay_us": maximum_delay_us,
                "final_voice_end_us": final_voice_end_us,
                "source_media_duration_us": source_media_duration_us,
                "assembled_duration_us": total_duration_us,
                "post_conform_required": True,
            }
            if (
                self.config.get("allow_timeline_overflow") is True
                and final_voice_end_us > source_media_duration_us
            ):
                coverage["timing_override"] = {
                    "enabled": True,
                    "overflow_us": final_voice_end_us - source_media_duration_us,
                    "original_media_duration_us": source_media_duration_us,
                    "assembled_duration_us": final_voice_end_us,
                }
        elif self.settings.adaptive_timing and clips:
            overflow_us = max(0, max(clip.end_us for clip in clips) - total_duration_us)
            available_lead_us = min(clip.start_us for clip in clips)
            early_shift_us = min(overflow_us, available_lead_us)
            if early_shift_us:
                clips = [
                    VoiceClip(
                        clip.cue_id,
                        clip.start_us - early_shift_us,
                        clip.end_us - early_shift_us,
                        clip.wav_path,
                        clip.qa,
                        clip.request_fingerprint,
                    )
                    for clip in clips
                ]
            timing = {
                "mode": "adaptive_no_loss",
                "max_speed": self.settings.max_absolute_speed,
                "shifted_cues": shifted_cues,
                "maximum_delay_us": maximum_delay_us,
                "global_early_shift_us": early_shift_us,
                "final_voice_end_us": max(clip.end_us for clip in clips),
                "media_duration_us": total_duration_us,
            }
            coverage["timing"] = timing
        if self.config.get("purpose") != "preview" and clips:
            final_voice_end_us = max(clip.end_us for clip in clips)
            remaining_overflow_us = max(0, final_voice_end_us - total_duration_us)
            if not self.settings.adaptive_timing and not defer_timing:
                coverage["timing"] = {
                    "mode": "strict",
                    "final_voice_end_us": final_voice_end_us,
                    "media_duration_us": total_duration_us,
                }
            if remaining_overflow_us and self.config.get("allow_timeline_overflow") is True:
                coverage["timing_override"] = {
                    "enabled": True,
                    "overflow_us": remaining_overflow_us,
                    "original_media_duration_us": total_duration_us,
                    "assembled_duration_us": final_voice_end_us,
                }
                total_duration_us = final_voice_end_us
            elif remaining_overflow_us:
                overflow_code = (
                    "adaptive_voice_exceeds_media_duration"
                    if self.settings.adaptive_timing
                    else "strict_voice_exceeds_media_duration"
                )
                coverage["status"] = "REVIEW"
                coverage["blockers"].append(
                    {
                        "code": overflow_code,
                        "overflow_us": remaining_overflow_us,
                    }
                )
                coverage["blocked_cues"] = int(coverage["blocked_cues"]) + 1
                atomic_write_json(qa_path, coverage)
                self._checkpoint(
                    checkpoint_key="review",
                    artifact_path=qa_path,
                    artifact_kind="tts_review_report",
                    extra_payload={"stage": "review", "coverage": coverage},
                )
                self.send(
                    "failed",
                    {
                        "code": "tts_review_required",
                        "status": "REVIEW",
                        "qa_path": str(qa_path),
                        "blocked_cues": int(coverage["blocked_cues"]),
                        "voice_read_speed": self.settings.voice_read_speed,
                        "cue_max_total_speed": self.settings.max_absolute_speed,
                        "adjusted_cue_count": adjusted_cue_count,
                        "maximum_additional_factor": maximum_additional_factor,
                        "unresolved_timing_cue_count": len(timing_issues),
                    },
                )
                return
        assembly_started = time.monotonic()
        last_assembly_report = 0
        self._phase_detail = "Đang ghép các WAV đã PASS vào timeline PCM16"

        def assembly_progress(current: int, total: int) -> None:
            nonlocal last_assembly_report
            self._control_point()
            if current != total and current - last_assembly_report < 64:
                return
            last_assembly_report = current
            phase_elapsed_ms = max(
                1, int((time.monotonic() - assembly_started) * 1_000)
            )
            rate = current / (phase_elapsed_ms / 1_000)
            remaining = max(0, total - current)
            eta_ms = int(remaining / rate * 1_000) if rate > 0 else None
            self._send_stage_progress(
                "timeline_assembly",
                phase_current=current,
                phase_total=total,
                eta_ms=eta_ms,
            )
        output_path = Path(
            str(self.config.get("staging_output_path", self.config["output_path"]))
        ).expanduser().resolve()
        target_lufs = float(self.config.get("target_lufs", -18.0))
        audio_processor = self.audio_processor_factory()

        def master_progress(
            stage: str,
            current_us: int,
            duration_us: int,
            eta_ms: int | None,
        ) -> None:
            self._control_point()
            phase = "final_master_apply" if stage == "apply" else "final_master_measure"
            self._phase_detail = (
                "Đang chuẩn hóa loudness và true peak bằng FFmpeg"
                if stage == "apply"
                else "Đang đo EBU R128 toàn bộ timeline"
            )
            self._phase = phase
            self._send_stage_progress(
                phase,
                phase_current=current_us,
                phase_total=duration_us,
                eta_ms=eta_ms,
            )

        open_stream_measure = getattr(
            audio_processor, "open_pcm16_ebu_r128_measure", None
        )
        apply_master = getattr(audio_processor, "apply_ebu_r128", None)
        if callable(open_stream_measure) and callable(apply_master):
            self._phase_detail = "Đang ghép timeline và đo EBU R128 đồng thời"

            def combined_progress(
                _stage: str,
                current_us: int,
                duration_us: int,
                eta_ms: int | None,
            ) -> None:
                self._control_point()
                self._send_stage_progress(
                    "timeline_assembly_measure",
                    phase_current=current_us,
                    phase_total=duration_us,
                    eta_ms=eta_ms,
                    extra={"phase_detail": self._phase_detail},
                )

            measure_session = open_stream_measure(
                sample_rate=self.settings.sample_rate,
                duration_us=total_duration_us,
                target_lufs=target_lufs,
                progress=combined_progress,
            )
            try:
                mix = assemble_voice_timeline(
                    tuple(clips),
                    assembled,
                    total_duration_us=total_duration_us,
                    sample_rate=self.settings.sample_rate,
                    progress=lambda current, total: self._control_point(),
                    pcm_sink=measure_session.write,
                )
                if int(mix["overlap_count"]) > 0:
                    raise ValueError("tts_timeline_overlap_guard_failed")
                measured = measure_session.finish()
            finally:
                measure_session.abort()
            self._phase_detail = "Đang chuẩn hóa loudness và true peak bằng FFmpeg"
            master = apply_master(
                assembled,
                output_path,
                measured=measured,
                target_lufs=target_lufs,
                progress=master_progress,
            )
        else:
            self._send_stage_progress(
                "timeline_assembly",
                phase_current=0,
                phase_total=len(clips),
                eta_ms=None,
            )
            mix = assemble_voice_timeline(
                tuple(clips),
                assembled,
                total_duration_us=total_duration_us,
                sample_rate=self.settings.sample_rate,
                progress=assembly_progress,
            )
            if int(mix["overlap_count"]) > 0:
                raise ValueError("tts_timeline_overlap_guard_failed")
            self._phase = "final_master_measure"
            self._phase_detail = "Đang đo EBU R128 toàn bộ timeline"
            master = audio_processor.master_ebu_r128(
                assembled,
                output_path,
                target_lufs=target_lufs,
                progress=master_progress,
            )
        coverage.update(
            {
                "status": "PASS",
                "output_path": str(output_path),
                "output_sha256": file_sha256(output_path),
                "mix": mix,
                "master": master,
            }
        )
        output_stat = output_path.stat()
        coverage["output_size"] = output_stat.st_size
        coverage["output_mtime_ns"] = output_stat.st_mtime_ns
        atomic_write_json(qa_path, coverage)
        self.send(
            "succeeded",
            {
                "status": "PASS",
                "purpose": str(self.config.get("purpose", "production")),
                "output_path": str(output_path),
                "qa_path": str(qa_path),
                "cue_count": len(clips),
                "coverage_ratio": 1.0,
                "voice_read_speed": self.settings.voice_read_speed,
                "cue_max_total_speed": self.settings.max_absolute_speed,
                "adjusted_cue_count": adjusted_cue_count,
                "maximum_total_speed_applied": coverage[
                    "maximum_total_speed_applied"
                ],
                "maximum_additional_factor": maximum_additional_factor,
                "unresolved_timing_cue_count": len(timing_issues),
                "timing_warning_count": len(timing_issues),
                "qa_non_blocking": self.qa_non_blocking,
            },
        )

    def _control_point(self) -> None:
        while True:
            try:
                message = self.commands.get_nowait()
            except queue.Empty:
                return
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if isinstance(command_id, str) and command_id in self.acknowledgements:
                cached = self.acknowledgements[command_id]
                sys.stdout.buffer.write(cached.to_json_line())
                sys.stdout.buffer.flush()
                continue
            if action == "pause" and isinstance(command_id, str):
                self._checkpoint()
                self.acknowledge(command_id, "paused", {"status": "paused"})
                self._wait_while_paused()
            elif action in {"cancel", "stop"}:
                self._checkpoint()
                payload = {"status": str(action)}
                if isinstance(command_id, str):
                    payload["command_id"] = command_id
                self.send("stopped", payload)
                raise WorkerStopped

    def _wait_while_paused(self) -> None:
        while True:
            message = self.commands.get()
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if action == "stop" and not isinstance(command_id, str):
                self.send("stopped", {"status": "stop"})
                raise WorkerStopped
            if not isinstance(command_id, str):
                continue
            if action == "resume":
                self.acknowledge(command_id, "resumed", {"status": "running"})
                return
            if action in {"cancel", "stop"}:
                self.send("stopped", {"status": str(action), "command_id": command_id})
                raise WorkerStopped

    def _send_progress(self) -> None:
        entries = self.ledger.get("entries", {})
        completed = len(entries) if isinstance(entries, dict) else 0
        blockers = self.ledger.get("blockers", {})
        terminal = completed + (len(blockers) if isinstance(blockers, dict) else 0)
        elapsed_ms = max(1, int((time.monotonic() - self._job_started) * 1_000))
        synthesis_elapsed_ms = max(
            1, int((time.monotonic() - self._synthesis_started) * 1_000)
        )
        synthesis_terminal = max(0, terminal - self._synthesis_baseline_terminal)
        rate = synthesis_terminal / (synthesis_elapsed_ms / 1_000)
        remaining = max(0, len(self.cues) - terminal)
        eta_ms = int(remaining / rate * 1_000) if rate > 0 else None
        self.send(
            "progress",
            {
                "phase": self._phase,
                "phase_detail": self._phase_detail,
                "current": terminal,
                "total": len(self.cues),
                "elapsed_ms": elapsed_ms,
                "eta_ms": eta_ms,
                "phase_current": synthesis_terminal,
                "phase_total": max(
                    0, len(self.cues) - self._synthesis_baseline_terminal
                ),
                "ineligible_cues": len(self.ineligible),
                "blocked_cues": len(self.ledger.get("blockers", {})),
            },
        )

    def _send_stage_progress(
        self,
        phase: str,
        *,
        phase_current: int,
        phase_total: int,
        eta_ms: int | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entries = self.ledger.get("entries", {})
        blockers = self.ledger.get("blockers", {})
        terminal = (len(entries) if isinstance(entries, dict) else 0) + (
            len(blockers) if isinstance(blockers, dict) else 0
        )
        self._phase = phase
        self.send(
            "progress",
            {
                "phase": phase,
                "phase_detail": str((extra or {}).get("phase_detail", self._phase_detail)),
                "current": terminal,
                "total": len(self.cues),
                "elapsed_ms": max(
                    1, int((time.monotonic() - self._job_started) * 1_000)
                ),
                "eta_ms": eta_ms,
                "phase_current": phase_current,
                "phase_total": phase_total,
                "ineligible_cues": len(self.ineligible),
                "blocked_cues": len(blockers) if isinstance(blockers, dict) else 0,
                **(extra or {}),
            },
        )

    def _checkpoint(
        self,
        *,
        checkpoint_key: str = "latest",
        artifact_path: Path | None = None,
        artifact_kind: str = "tts_ledger",
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        source = artifact_path
        if source is not None and not source.resolve().is_relative_to(self.job_staging.parent):
            staged = self.job_staging / f"artifact-{uuid4().hex}{source.suffix}.partial"
            shutil.copyfile(source, staged)
            source = staged
        if source is None:
            atomic_write_json(self.ledger_path, self.ledger)
            source = self.job_staging / f"ledger-{uuid4().hex}.json.partial"
            shutil.copyfile(self.ledger_path, source)
        digest = sha256(source.read_bytes()).hexdigest()
        entries = self.ledger.get("entries", {})
        completed_ids = set(entries) if isinstance(entries, dict) else set()
        completed_cues = [cue for cue in self.cues if cue.cue_id in completed_ids]
        cursor_us = completed_cues[-1].end_us if completed_cues else 0
        self.send(
            "checkpoint",
            {
                "stage_key": "tts",
                "checkpoint_key": checkpoint_key,
                "schema_version": 1,
                "cursor_us": cursor_us,
                "payload": {
                    "stage": "tts",
                    "completed_cues": len(completed_cues),
                    **(extra_payload or {}),
                },
                "artifact_path": str(source),
                "artifact_kind": artifact_kind,
                "artifact_state": "technical",
                "expected_sha256": digest,
            },
        )


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        return 2
    return TtsWorker(sys.argv[1]).run()


if __name__ == "__main__":
    raise SystemExit(main())
