from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from ducai_studio.application.ocr_pipeline import (
    OCR_AGGREGATION_VERSION,
    apply_quality_rescue,
    coalesce_rescue_windows,
    cross_pass_consensus_candidates,
    group_visual_evidence,
    measure_hard_sub_coverage,
    merge_band_candidates,
    reconcile_dialogue_evidence,
    reinforce_candidate_ranges,
    scene_text_cues_from_evidence,
    scene_text_rescue_regions,
    targeted_rescue_windows,
)
from ducai_studio.application.subtitle_finalizer import (
    FinalizationStatus,
    collapse_proven_ocr_candidates,
    finalize_ocr_candidates,
    publish_final_srt,
    render_srt,
)
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.subtitles import (
    NormalizedBox,
    RescueWindow,
    SceneTextProbe,
    SubtitleEvidence,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.external_runtime import (
    isolated_python_environment,
    isolated_python_working_directory,
)
from ducai_studio.infrastructure.portable_runtime import paddle_cache_home_for_runtime
from ducai_studio.infrastructure.process_tree import ProcessTreeGuard
from ducai_studio.infrastructure.runtime_program import runtime_program_path
from ducai_studio.providers.ocr import DEFAULT_SUBTITLE_BANDS
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


class EngineError(RuntimeError):
    pass


_OCR_FALLBACK_DETAILS = {
    "ocr_cuda_driver_incompatible": "driver NVIDIA chưa phù hợp runtime CUDA",
    "ocr_cuda_kernel_unsupported": "runtime CUDA không hỗ trợ GPU này",
    "ocr_cuda_blackwell_runtime_incompatible": "RTX 50 cần runtime Paddle CUDA 12.9 trở lên",
    "ocr_cuda_launch_failed": "kernel CUDA gặp lỗi; kiểm tra driver/runtime",
    "ocr_cuda_out_of_memory": "GPU không còn đủ VRAM",
    "ocr_cuda_library_missing": "thiếu hoặc không nạp được thư viện CUDA/cuDNN",
    "ocr_cuda_cpu_only": "runtime Paddle đang là bản CPU",
    "ocr_cuda_device_unavailable": "Paddle không tìm thấy GPU CUDA đã chọn",
}

_OCR_EXECUTION_DETAILS = {
    **_OCR_FALLBACK_DETAILS,
    "ocr_predictor_timeout": "tiến trình nhận chữ quá thời gian chờ",
    "ocr_predictor_exited": "tiến trình nhận chữ bị ngắt",
    "BrokenPipeError": "mất kết nối tiến trình nhận chữ",
    "ConnectionResetError": "mất kết nối tiến trình nhận chữ",
    "ConnectionAbortedError": "mất kết nối tiến trình nhận chữ",
    "insufficient_free_memory": "không đủ RAM/VRAM trống cho 3 tiến trình",
    "memory_probe_unavailable": "không xác minh được RAM/VRAM trống",
    "gpu_oom": "GPU không còn đủ VRAM",
    "model_startup_failed": "không nạp được bộ nhận chữ",
    "model_missing": "thiếu model nhận chữ",
    "ocr_predictor_invalid_handshake": "phản hồi khởi tạo không hợp lệ",
    "ocr_predictor_invalid_readings": "phản hồi nhận chữ không hợp lệ",
    "ocr_predictor_result_count_mismatch": "số kết quả nhận chữ không hợp lệ",
    "RuntimeError": "bộ nhận chữ gặp lỗi",
    "ValueError": "phản hồi nhận chữ không hợp lệ",
}


class WorkerStopped(RuntimeError):
    pass


class WhisperRestart(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScanPlanSegment:
    index: int
    media_id: str
    media_path: str
    source_start_us: int
    source_end_us: int
    timeline_start_us: int
    timeline_end_us: int
    base_start_us: int
    base_end_us: int

    @property
    def timeline_duration_us(self) -> int:
        return self.timeline_end_us - self.timeline_start_us

    def contains_timeline(self, timestamp_us: int) -> bool:
        return self.timeline_start_us <= timestamp_us < self.timeline_end_us

    def timeline_to_source(self, timestamp_us: int) -> int:
        return self._project_timestamp(
            timestamp_us,
            target_start_us=self.source_start_us,
            target_end_us=self.source_end_us,
        )

    def timeline_to_base(self, timestamp_us: int) -> int:
        return self._project_timestamp(
            timestamp_us,
            target_start_us=self.base_start_us,
            target_end_us=self.base_end_us,
        )

    def source_to_timeline(self, timestamp_us: int) -> int:
        source_duration_us = self.source_end_us - self.source_start_us
        offset_us = max(0, min(source_duration_us - 1, timestamp_us - self.source_start_us))
        projected = self.timeline_start_us + (
            offset_us * self.timeline_duration_us // source_duration_us
        )
        return min(self.timeline_end_us - 1, projected)

    def _project_timestamp(
        self,
        timestamp_us: int,
        *,
        target_start_us: int,
        target_end_us: int,
    ) -> int:
        if not self.contains_timeline(timestamp_us):
            raise ValueError("scan_plan_timestamp_out_of_range")
        target_duration_us = target_end_us - target_start_us
        offset_us = timestamp_us - self.timeline_start_us
        projected = target_start_us + (offset_us * target_duration_us // self.timeline_duration_us)
        return min(target_end_us - 1, projected)


def _sampling_intervals(configured_interval_us: int) -> tuple[int, int]:
    return max(400_000, configured_interval_us // 2), configured_interval_us


class EngineHostClient:
    def __init__(self, python_path: Path, host_path: Path) -> None:
        self.python_path = python_path
        self.host_path = host_path
        self.process: subprocess.Popen[str] | None = None
        self._process_tree: ProcessTreeGuard | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        if self.process is not None:
            return
        if not self.python_path.is_file():
            raise EngineError("ocr_engine_python_missing")
        if not self.host_path.is_file():
            raise EngineError("ocr_engine_host_missing")
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        environment = isolated_python_environment()
        portable_cache = paddle_cache_home_for_runtime(self.python_path)
        if portable_cache is not None:
            environment.setdefault("PADDLE_PDX_CACHE_HOME", str(portable_cache))
        environment["DUCAI_WORKER_PROGRAM"] = str(self.python_path)
        environment["DUCAI_WORKER_ARGUMENTS_JSON"] = json.dumps(
            ["-u", str(self.host_path)], ensure_ascii=False,
        )
        self._process_tree = ProcessTreeGuard()
        try:
            self.process = subprocess.Popen(
                [str(runtime_program_path()), "-m", "ducai_studio.workers.preowned_launcher"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
                env=environment,
                cwd=isolated_python_working_directory(self.python_path),
            )
            # Assign before the first request can launch any model children.
            self._process_tree.assign_pid(self.process.pid)
            if self.process.stdin is None:
                raise EngineError("ocr_engine_pipe_failed")
            # TextIOWrapper translates LF to CRLF on Windows, but the launch
            # gate is deliberately byte-exact and must not read ahead.
            if os.write(self.process.stdin.fileno(), b"DUCAI_LAUNCH\n") != 13:
                raise EngineError("ocr_engine_pipe_failed")
        except BaseException:
            self.close()
            raise
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr.append(line.rstrip()[:500])

    def request(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.start()
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise EngineError("ocr_engine_unavailable")
        if process.poll() is not None:
            raise EngineError("ocr_engine_exited")
        request_id = str(uuid4())
        request = {
            "protocol_version": 1,
            "id": request_id,
            "action": action,
            "payload": payload,
        }
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            raise EngineError("ocr_engine_pipe_failed") from exc
        if not line:
            raise EngineError("ocr_engine_no_response")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EngineError("ocr_engine_invalid_response") from exc
        if response.get("id") != request_id:
            raise EngineError("ocr_engine_response_mismatch")
        if response.get("ok") is not True:
            error = response.get("error", {})
            code = (
                error.get("code", "ocr_engine_failed")
                if isinstance(error, dict)
                else "ocr_engine_failed"
            )
            raise EngineError(str(code)[:80])
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise EngineError("ocr_engine_invalid_result")
        return result

    def close(self) -> None:
        process = self.process
        self.process = None
        tree, self._process_tree = self._process_tree, None
        try:
            if process is not None and process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.write(json.dumps({
                            "protocol_version": 1, "id": str(uuid4()),
                            "action": "shutdown", "payload": {},
                        }) + "\n")
                        process.stdin.flush()
                    process.wait(timeout=5)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    # The guard below stops the host, venv launcher and every
                    # predictor even when the host cannot acknowledge shutdown.
                    if os.name != "nt":
                        process.kill()
        finally:
            if tree is not None:
                tree.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        # A killed pipe may fail its final flush.
                        with suppress(OSError):
                            stream.close()
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=2)
                self._stderr_thread = None


class WhisperEngineClient:
    """Read streamed host responses while keeping worker controls responsive."""

    def __init__(self, python_path: Path, host_path: Path) -> None:
        self.python_path = python_path
        self.host_path = host_path
        self.process: subprocess.Popen[str] | None = None
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=20)

    def start(self) -> None:
        if self.process is not None:
            return
        if not self.python_path.is_file():
            raise EngineError("whisper_engine_python_missing")
        if not self.host_path.is_file():
            raise EngineError("whisper_engine_host_missing")
        environment = isolated_python_environment(
            {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        )
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.process = subprocess.Popen(
            [str(self.python_path), "-u", str(self.host_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
            env=environment,
            cwd=isolated_python_working_directory(self.python_path),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            self._stdout.put(None)
            return
        for line in process.stdout:
            self._stdout.put(line)
        self._stdout.put(None)

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr.append(line.rstrip()[:500])

    def stream(
        self,
        payload: dict[str, Any],
        *,
        poll_control: Callable[[], bool],
    ):
        self.start()
        process = self.process
        if process is None or process.stdin is None:
            raise EngineError("whisper_engine_unavailable")
        request_id = str(uuid4())
        try:
            process.stdin.write(
                json.dumps(
                    {
                        "protocol_version": 1,
                        "id": request_id,
                        "action": "transcribe",
                        "payload": payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise EngineError("whisper_engine_pipe_failed") from exc
        while True:
            if poll_control():
                self.terminate()
                raise WhisperRestart
            try:
                line = self._stdout.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None:
                    raise EngineError("whisper_engine_exited") from None
                continue
            if line is None:
                raise EngineError("whisper_engine_no_response")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EngineError("whisper_engine_invalid_response") from exc
            if response.get("id") != request_id:
                raise EngineError("whisper_engine_response_mismatch")
            if response.get("ok") is not True:
                error = response.get("error", {})
                code = error.get("code") if isinstance(error, dict) else None
                raise EngineError(str(code or "whisper_engine_failed")[:80])
            event = str(response.get("event", ""))
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise EngineError("whisper_engine_invalid_result")
            yield event, result
            if event == "complete":
                return

    def terminate(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write(
                    json.dumps(
                        {
                            "protocol_version": 1,
                            "id": str(uuid4()),
                            "action": "shutdown",
                            "payload": {},
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                process.wait(timeout=3)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.kill()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


class OcrWorker:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.commands: queue.Queue[WorkerMessage] = queue.Queue()
        self.acknowledgements: dict[str, WorkerMessage] = {}
        raw_staging_root = os.environ.get("DUCAI_ARTIFACT_STAGING_ROOT", "")
        if not raw_staging_root.strip():
            raise RuntimeError("managed_staging_root_missing")
        self.staging_root = Path(raw_staging_root).expanduser().resolve()
        self.job_staging = self.staging_root / job_id
        self.job_staging.mkdir(parents=True, exist_ok=True)
        self.evidence_path = self.job_staging / "ocr-evidence-working.ndjson"
        self.whisper_segments_path = self.job_staging / "whisper-segments-working.ndjson"
        self.whisper_audio_path = self.job_staging / "dialogue-16k-mono.wav"
        self.engine: EngineHostClient | WhisperEngineClient | None = None
        self.spec: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.stage = "fast"
        self.cursor_us = 0
        self._change_events: list[int] = []
        self._scene_change_events: list[int] = []
        self._scan_plan_segments: tuple[ScanPlanSegment, ...] = ()
        self._scan_plan_duration_us = 0
        self._scan_plan_fingerprint = ""
        self._engine_mode = ""
        self._engine_media_path = ""
        self._ocr_execution: dict[str, Any] = {}
        self._ocr_fallback_reason = ""
        self._engine_video_duration_us: int | None = None
        self._decoder_video_durations: dict[str, int] = {}
        self._decoder_tail_request_counts: dict[str, int] = {}
        self._change_tracking_segment_index: int | None = None
        self._whisper_force_cpu = False
        self._whisper_runtime: dict[str, Any] = {}
        self._whisper_segment_total = 0
        self._finalization_detail = ""
        self._finalization_last_progress = 0.0
        self._finalization_started = 0.0
        self._reader = threading.Thread(target=self._read_commands, daemon=True)

    def send(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> WorkerMessage:
        if kind == "progress" and payload.get("phase") in {
            "fast", "adaptive_ocr", "quality", "scene_quality",
        } and (self._ocr_execution or self._engine_mode == "compatibility"):
            execution = self._ocr_execution
            if self._engine_mode == "compatibility":
                # Rapid's CPU provider is known even when an older host omits
                # telemetry. Never leave the configured GPU label on a CPU run.
                execution = {"execution_device": "cpu", "execution_workers": 1}
            payload = {**execution, **payload}
            if self._ocr_fallback_reason:
                payload["ocr_fallback_reason"] = self._ocr_fallback_reason
            if not payload.get("phase_detail"):
                device = execution["execution_device"]
                workers = execution["execution_workers"]
                if self._engine_mode == "compatibility":
                    detail = "CPU · OCR tương thích"
                    if self._ocr_fallback_reason:
                        detail += " · Paddle lỗi: " + _OCR_FALLBACK_DETAILS.get(
                            self._ocr_fallback_reason, self._ocr_fallback_reason,
                        )
                else:
                    detail = ("GPU" if device.startswith("gpu") else device.upper())
                    detail += f" · {workers} tiến trình nhận chữ"
                    reason = execution.get("execution_fallback_reason")
                    if reason:
                        detail += " · " + _OCR_EXECUTION_DETAILS[reason]
                    elif execution.get("execution_recovery_attempts") and workers == 3:
                        detail += " · đã khôi phục chạy song song"
                payload["phase_detail"] = detail
        message = WorkerMessage(
            PROTOCOL_VERSION,
            self.job_id,
            event_id or str(uuid4()),
            kind,
            payload,
        )
        sys.stdout.buffer.write(message.to_json_line())
        sys.stdout.buffer.flush()
        return message

    def acknowledge(self, command_id: str, kind: str, payload: dict[str, Any]) -> None:
        cached = self.acknowledgements.get(command_id)
        if cached is not None:
            sys.stdout.buffer.write(cached.to_json_line())
            sys.stdout.buffer.flush()
            return
        body = {**payload, "command_id": command_id}
        self.acknowledgements[command_id] = self.send(kind, body)

    def _read_commands(self) -> None:
        for line in sys.stdin.buffer:
            try:
                message = WorkerMessage.from_json_line(line)
            except Exception:
                continue
            if message.job_id == self.job_id and message.kind == "control":
                self.commands.put(message)

    def run(self) -> int:
        self._reader.start()
        self.send("ready", {"status": "ready", "worker": "ocr"})
        while True:
            message = self.commands.get()
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if not isinstance(command_id, str) or not command_id:
                if action == "stop":
                    self.send("stopped", {"status": "stopped"})
                    return 0
                continue
            if command_id in self.acknowledgements:
                self.acknowledge(command_id, "started", {})
                continue
            if action != "start":
                continue
            try:
                self._configure(dict(message.payload))
                self.acknowledge(
                    command_id,
                    "started",
                    {"status": "running", "resumed_cursor_us": self.cursor_us},
                )
                outcome = self._execute()
                return outcome
            except WorkerStopped:
                return 0
            except Exception as exc:
                code = str(exc) if str(exc).replace("_", "").isalnum() else type(exc).__name__
                self.send("failed", {"code": code[:80] or "ocr_worker_failed"})
                return 2
            finally:
                if self.engine is not None:
                    self.engine.close()

    def _configure(self, start_payload: dict[str, Any]) -> None:
        spec = start_payload.get("job")
        if not isinstance(spec, dict) or spec.get("kind") != "ocr":
            raise ValueError("ocr_job_spec_missing")
        config = spec.get("provider_config")
        if not isinstance(config, dict):
            raise ValueError("ocr_provider_config_missing")
        self.spec = spec
        self.config = config
        self._configure_scan_plan()
        self._restore_checkpoint(start_payload.get("checkpoint"))

    def _configure_scan_plan(self) -> None:
        self._scan_plan_segments = ()
        self._scan_plan_duration_us = 0
        self._scan_plan_fingerprint = ""
        raw_plan = self.config.get("scan_plan")
        if raw_plan is None:
            return
        if not isinstance(raw_plan, dict) or raw_plan.get("schema_version") != 1:
            raise ValueError("invalid_ocr_scan_plan")
        duration_us = raw_plan.get("duration_us")
        raw_segments = raw_plan.get("segments")
        if (
            type(duration_us) is not int
            or duration_us <= 0
            or not isinstance(raw_segments, list)
            or not raw_segments
        ):
            raise ValueError("invalid_ocr_scan_plan")
        segments: list[ScanPlanSegment] = []
        previous_timeline_end_us = 0
        for index, raw in enumerate(raw_segments):
            if not isinstance(raw, dict):
                raise ValueError("invalid_ocr_scan_plan")
            media_id = raw.get("media_id")
            media_path = raw.get("media_path")
            timing_keys = (
                "source_start_us",
                "source_end_us",
                "timeline_start_us",
                "timeline_end_us",
                "base_start_us",
                "base_end_us",
            )
            if (
                not isinstance(media_id, str)
                or not media_id
                or not isinstance(media_path, str)
                or not media_path
                or any(type(raw.get(key)) is not int for key in timing_keys)
            ):
                raise ValueError("invalid_ocr_scan_plan")
            segment = ScanPlanSegment(
                index=index,
                media_id=media_id,
                media_path=media_path,
                source_start_us=int(raw["source_start_us"]),
                source_end_us=int(raw["source_end_us"]),
                timeline_start_us=int(raw["timeline_start_us"]),
                timeline_end_us=int(raw["timeline_end_us"]),
                base_start_us=int(raw["base_start_us"]),
                base_end_us=int(raw["base_end_us"]),
            )
            if (
                segment.source_start_us < 0
                or segment.source_end_us <= segment.source_start_us
                or segment.timeline_start_us < previous_timeline_end_us
                or segment.timeline_end_us <= segment.timeline_start_us
                or segment.timeline_end_us > duration_us
                or segment.base_start_us < 0
                or segment.base_end_us <= segment.base_start_us
            ):
                raise ValueError("invalid_ocr_scan_plan")
            segments.append(segment)
            previous_timeline_end_us = segment.timeline_end_us
        if previous_timeline_end_us != duration_us:
            raise ValueError("invalid_ocr_scan_plan")
        self._scan_plan_segments = tuple(segments)
        self._scan_plan_duration_us = duration_us
        canonical = json.dumps(
            raw_plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._scan_plan_fingerprint = sha256(canonical).hexdigest()

    def _restore_checkpoint(self, checkpoint: object) -> None:
        if self.config.get("extraction_method") == "whisper_audio":
            self._restore_whisper_checkpoint(checkpoint)
            return
        if not isinstance(checkpoint, dict):
            self.evidence_path.unlink(missing_ok=True)
            return
        payload = checkpoint.get("payload", {})
        artifact = checkpoint.get("artifact")
        if not isinstance(payload, dict) or not isinstance(artifact, dict):
            return
        if self.config.get("ocr_aggregation_version") == OCR_AGGREGATION_VERSION and payload.get(
            "ocr_aggregation_version"
        ) != OCR_AGGREGATION_VERSION:
            self.evidence_path.unlink(missing_ok=True)
            return
        if (
            self._scan_plan_segments
            and payload.get("scan_plan_fingerprint") != self._scan_plan_fingerprint
        ):
            self.evidence_path.unlink(missing_ok=True)
            return
        settings_fingerprint = str(self.spec.get("settings_fingerprint", ""))
        if (
            self._scan_plan_segments
            and settings_fingerprint
            and payload.get("settings_fingerprint") != settings_fingerprint
        ):
            self.evidence_path.unlink(missing_ok=True)
            return
        input_fingerprint = str(self.spec.get("input_fingerprint", ""))
        if (
            self._scan_plan_segments
            and input_fingerprint
            and payload.get("input_fingerprint") != input_fingerprint
        ):
            self.evidence_path.unlink(missing_ok=True)
            return
        uri = artifact.get("uri")
        if not isinstance(uri, str):
            return
        source = Path(uri).expanduser().resolve(strict=True)
        shutil.copyfile(source, self.evidence_path)
        self.stage = str(payload.get("stage", "fast"))
        raw_cursor = payload.get("cursor_us", checkpoint.get("cursor_us", 0))
        self.cursor_us = int(raw_cursor) if type(raw_cursor) is int else 0
        self._change_events = self._checkpoint_event_values(payload.get("change_events_us"))
        self._scene_change_events = self._checkpoint_event_values(
            payload.get("scene_change_events_us")
        )
        if self.stage == "fast":
            interval = self._integer_config("sample_interval_us", 800_000, 400_000)
            rewind = max(0, self.cursor_us - interval * 2)
            self._filter_working_evidence(
                lambda row: (
                    not (
                        row.get("provider_id") in {"paddle-fast", "rapid-compatibility"}
                        and int(row.get("timestamp_us", 0)) >= rewind
                    )
                )
            )
            self.cursor_us = rewind
            self._change_events = [value for value in self._change_events if value < rewind]
            self._scene_change_events = [
                value for value in self._scene_change_events if value < rewind
            ]
        elif self.stage == "quality" and self.cursor_us > 0:
            fast_interval = self._integer_config("sample_interval_us", 800_000, 400_000)
            quality_interval = max(400_000, fast_interval // 2)
            rewind = max(0, self.cursor_us - quality_interval * 2)
            self._filter_working_evidence(
                lambda row: (
                    not (
                        row.get("provider_id") in {"paddle-quality", "rapid-quality-fallback"}
                        and int(row.get("timestamp_us", 0)) >= rewind
                    )
                )
            )
            self.cursor_us = rewind
        elif self.stage == "scene_quality" and self.cursor_us > 0:
            rewind = max(0, self.cursor_us - 800_000)
            self._filter_working_evidence(lambda row: not (
                (row.get("evidence_kind") == "scene" or row.get("record_kind") == "scene_probe")
                and "quality" in str(row.get("provider_id", ""))
                and int(row.get("timestamp_us", 0)) >= rewind
            ))
            self.cursor_us = rewind

    @staticmethod
    def _checkpoint_event_values(value: object) -> list[int]:
        if not isinstance(value, list):
            return []
        return sorted({item for item in value if type(item) is int and item >= 0})

    def _restore_whisper_checkpoint(self, checkpoint: object) -> None:
        self.stage = "whisper"
        self.cursor_us = 0
        self._whisper_segment_total = 0
        self.whisper_segments_path.unlink(missing_ok=True)
        if not isinstance(checkpoint, dict):
            return
        payload = checkpoint.get("payload", {})
        artifact = checkpoint.get("artifact")
        if not isinstance(payload, dict) or not isinstance(artifact, dict):
            return
        uri = artifact.get("uri")
        if artifact.get("kind") != "whisper_segments" or not isinstance(uri, str):
            return
        try:
            source = Path(uri).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("whisper_checkpoint_corrupt") from exc
        shutil.copyfile(source, self.whisper_segments_path)
        raw_cursor = payload.get("cursor_us", checkpoint.get("cursor_us", 0))
        self.cursor_us = int(raw_cursor) if type(raw_cursor) is int else 0
        self._whisper_force_cpu = payload.get("device") == "cpu"
        self._rewind_whisper_segments()

    def _filter_working_evidence(self, keep) -> None:
        if not self.evidence_path.is_file():
            return
        temporary = self.evidence_path.with_suffix(".rewrite")
        with (
            self.evidence_path.open("r", encoding="utf-8") as source,
            temporary.open("w", encoding="utf-8", newline="\n") as destination,
        ):
            for line in source:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and keep(row):
                    destination.write(json.dumps(row, ensure_ascii=False) + "\n")
        temporary.replace(self.evidence_path)

    def _integer_config(self, key: str, default: int, minimum: int) -> int:
        value = self.config.get(key, default)
        if type(value) is not int or value < minimum:
            raise ValueError(f"invalid_{key}")
        return value

    def _bands_payload(self) -> list[dict[str, Any]]:
        raw = self.config.get("bands")
        if isinstance(raw, list) and raw:
            values = [dict(item) for item in raw if isinstance(item, dict)]
            if values:
                return values
            raise ValueError("invalid_ocr_bands")
        return [
            {
                "name": band.name,
                "x1": band.x1,
                "y1": band.y1,
                "x2": band.x2,
                "y2": band.y2,
                "scale": band.scale,
                "reading_mode": band.reading_mode,
            }
            for band in DEFAULT_SUBTITLE_BANDS
        ]

    @staticmethod
    def _normalized_region_list(raw: object, *, key: str) -> list[list[float]]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError(f"invalid_{key}")
        normalized: list[list[float]] = []
        for item in raw:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 4
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) for value in item
                )
            ):
                raise ValueError(f"invalid_{key}")
            x, y, width, height = (float(value) for value in item)
            if (
                x < 0
                or y < 0
                or width <= 0
                or height <= 0
                or x + width > 1.000001
                or y + height > 1.000001
            ):
                raise ValueError(f"invalid_{key}")
            normalized.append([x, y, width, height])
        return normalized

    def _recognize_region_payload(
        self,
        bands: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return stable full-frame masks shared by every OCR host request."""

        exclusions = self._normalized_region_list(
            self.config.get("exclusion_regions", []),
            key="ocr_exclusion_regions",
        )
        raw_dialogue = self.config.get("dialogue_region")
        dialogue = self._normalized_region_list(
            [raw_dialogue] if raw_dialogue is not None else [],
            key="ocr_dialogue_region",
        )
        payload: dict[str, Any] = {"exclusion_regions": exclusions}
        scene_names = [str(item["name"]) for item in bands
                       if str(item.get("reading_mode", item["name"])) == "scene"]
        if dialogue and scene_names:
            # Scene text (S2+) is deliberately scanned outside the dialogue
            # band. User exclusions still apply globally and therefore win.
            payload["exclusion_regions_by_band"] = {name: dialogue for name in scene_names}
        return payload

    def _open_engine(self, mode: str, *, media_path: str | None = None) -> None:
        if self.engine is not None:
            self.engine.close()
        python_path = (
            Path(str(self.config.get("engine_python", sys.executable))).expanduser().resolve()
        )
        runtime_host = os.environ.get("DUCAI_OCR_ENGINE_HOST_PATH", "").strip()
        host_path = (
            Path(
                runtime_host
                or str(
                    self.config.get(
                        "engine_host_path",
                        Path(__file__).parents[1] / "engine_hosts" / "ocr_engine_host.py",
                    )
                )
            )
            .expanduser()
            .resolve()
        )
        runtime_host_hash = os.environ.get("DUCAI_OCR_ENGINE_HOST_SHA256", "").strip()
        if runtime_host_hash and sha256(host_path.read_bytes()).hexdigest() != runtime_host_hash:
            raise RuntimeError("ocr_engine_host_runtime_changed")
        self.engine = EngineHostClient(python_path, host_path)
        selected_media_path = media_path or (
            self._scan_plan_segments[0].media_path
            if self._scan_plan_segments
            else str(self.config["media_path"])
        )
        metadata = self.engine.request(
            "open",
            {
                "mode": mode,
                "media_path": selected_media_path,
                "device": str(self.config.get("device", "gpu:0")),
                "batch_size": self._integer_config("engine_batch_size", 12, 1),
            },
        )
        self._engine_mode = mode
        self._engine_media_path = selected_media_path
        self._remember_ocr_execution(metadata)
        self._remember_video_duration(metadata)
        self._change_tracking_segment_index = None

    def _switch_engine_media(self, mode: str, media_path: str) -> None:
        if self.engine is None or self._engine_mode != mode:
            self._open_engine(mode, media_path=media_path)
            return
        if self._engine_media_path == media_path:
            return
        metadata = self.engine.request(
            "open",
            {
                "mode": mode,
                "media_path": media_path,
                "device": str(self.config.get("device", "gpu:0")),
                "batch_size": self._integer_config("engine_batch_size", 12, 1),
            },
        )
        self._engine_media_path = media_path
        self._remember_ocr_execution(metadata)
        self._remember_video_duration(metadata)
        self._change_tracking_segment_index = None

    def _remember_ocr_execution(self, metadata: dict[str, Any]) -> None:
        device = metadata.get("execution_device")
        workers = metadata.get("worker_count", 1)
        self._ocr_execution = {}
        if not isinstance(device, str) or not device or len(device) > 32:
            return  # Legacy hosts do not prove an execution device.
        if type(workers) is not int or workers not in (1, 3):
            return
        self._ocr_execution = {"execution_device": device, "execution_workers": workers}
        for key in ("execution_fallback_reason", "execution_recovery_reason"):
            reason = metadata.get(key)
            if isinstance(reason, str) and reason in _OCR_EXECUTION_DETAILS:
                self._ocr_execution[key] = reason
        attempts = metadata.get("execution_recovery_attempts")
        if type(attempts) is int and 0 <= attempts <= 1:
            self._ocr_execution["execution_recovery_attempts"] = attempts

    def _remember_video_duration(self, metadata: dict[str, Any]) -> None:
        self._engine_video_duration_us = None
        self._decoder_video_durations.pop(self._engine_media_path, None)
        count = metadata.get("frame_count")
        # Older/custom hosts may not report a finite decoded frame count.
        if type(count) is not int or count <= 0:
            return
        duration = metadata.get("duration_us")
        fps = metadata.get("fps")
        if (type(duration) is not int or duration <= 0
                or isinstance(fps, bool) or not isinstance(fps, (int, float))
                or not math.isfinite(fps) or fps <= 0
                or duration != round(count * 1_000_000 / fps)):
            raise EngineError("ocr_engine_invalid_video_metadata")
        self._engine_video_duration_us = duration
        self._decoder_video_durations[self._engine_media_path] = duration

    def _request_readable_frames(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.engine is None:
            raise EngineError("ocr_engine_unavailable")
        duration = self._engine_video_duration_us
        if duration is None:
            return self.engine.request("recognize", payload)
        timestamps = payload.get("timestamps_us")
        if not isinstance(timestamps, list) or not timestamps:
            return self.engine.request("recognize", payload)
        skipped: set[int] = set()

        def readable(values: list) -> list:
            kept = []
            for value in values:
                if type(value) is int and value > duration:
                    skipped.add(value)
                else:
                    kept.append(value)
            return kept

        bounded = {**payload, "timestamps_us": readable(timestamps)}
        band_times = payload.get("band_timestamps_us")
        if isinstance(band_times, dict):
            bounded["band_timestamps_us"] = {
                name: readable(values) if isinstance(values, list) else values
                for name, values in band_times.items()
            }
        change_times = payload.get("change_timestamps_us")
        if isinstance(change_times, list):
            bounded["change_timestamps_us"] = readable(change_times)
            if change_times and not bounded["change_timestamps_us"]:
                # The host validates a nonempty probe schedule even when change
                # detection is disabled. Keep OCR samples, not discarded probes.
                bounded.pop("change_timestamps_us")
                bounded["detect_changes"] = False
        if skipped:
            media = self._engine_media_path
            self._decoder_tail_request_counts[media] = (
                self._decoder_tail_request_counts.get(media, 0) + len(skipped)
            )
        if not bounded["timestamps_us"]:
            probes = bounded.get("change_timestamps_us", [])
            if payload.get("detect_changes") is True and probes:
                # Keep valid change probes, without promoting them to OCR samples.
                bounded["timestamps_us"] = probes
                bounded["band_timestamps_us"] = {
                    band["name"]: [] for band in payload.get("bands", [])
                }
                if "evidence_kinds_by_band" not in bounded:
                    bounded["evidence_kinds_by_band"] = {
                        band["name"]: payload.get("evidence_kind", "global")
                        for band in payload.get("bands", [])
                    }
            else:
                return {"evidence": [], "watermark_detections": [], "scene_probes": [],
                        "event_timestamps_us": [], "scene_event_timestamps_us": [],
                        "frame_count": 0, "processed_until_us": timestamps[-1]}
        result = self.engine.request("recognize", bounded)
        if skipped:
            result = {**result, "processed_until_us": timestamps[-1]}
        return result

    def _segment_for_timeline(self, timestamp_us: int) -> ScanPlanSegment | None:
        return next(
            (
                segment
                for segment in self._scan_plan_segments
                if segment.contains_timeline(timestamp_us)
            ),
            None,
        )

    def _timeline_timestamps(
        self,
        start_us: int,
        end_us: int,
        interval_us: int,
    ) -> list[int]:
        if not self._scan_plan_segments:
            return list(range(start_us, end_us, interval_us))
        values: set[int] = set()
        for segment in self._scan_plan_segments:
            lower = max(start_us, segment.timeline_start_us)
            upper = min(end_us, segment.timeline_end_us)
            if lower >= upper:
                continue
            relative_lower_us = lower - segment.timeline_start_us
            step = max(0, (relative_lower_us + interval_us - 1) // interval_us)
            first = segment.timeline_start_us + step * interval_us
            values.update(range(first, upper, interval_us))
        return sorted(values)

    def _recognize(
        self,
        payload: dict[str, Any],
        *,
        mode: str,
    ) -> dict[str, Any]:
        if not self._scan_plan_segments:
            if self.engine is None:
                raise EngineError("ocr_engine_unavailable")
            result = self._request_readable_frames(payload)
            if "execution_device" in result:
                self._remember_ocr_execution(result)
            return result
        raw_timestamps = payload.get("timestamps_us")
        if not isinstance(raw_timestamps, list):
            raise EngineError("ocr_scan_plan_timestamps_invalid")
        grouped: dict[int, list[int]] = {}
        for timestamp_us in raw_timestamps:
            segment = self._segment_for_timeline(timestamp_us)
            if segment is not None:
                grouped.setdefault(segment.index, []).append(timestamp_us)
        combined: dict[str, Any] = {
            "evidence": [],
            "watermark_detections": [],
            "scene_probes": [],
            "event_timestamps_us": [],
            "scene_event_timestamps_us": [],
            "frame_count": 0,
        }
        for segment_index in sorted(grouped):
            segment = self._scan_plan_segments[segment_index]
            virtual_timestamps = sorted(set(grouped[segment_index]))
            actual_by_virtual = {
                value: segment.timeline_to_source(value) for value in virtual_timestamps
            }
            virtual_by_actual = {actual: virtual for virtual, actual in actual_by_virtual.items()}
            actual_timestamps = sorted(virtual_by_actual)
            if not actual_timestamps:
                continue
            segment_payload = {**payload, "timestamps_us": actual_timestamps}
            raw_band_timestamps = payload.get("band_timestamps_us")
            if isinstance(raw_band_timestamps, dict):
                segment_payload["band_timestamps_us"] = {
                    name: sorted(
                        {actual_by_virtual[value] for value in values if value in actual_by_virtual}
                    )
                    for name, values in raw_band_timestamps.items()
                    if isinstance(values, list)
                }
            raw_change_timestamps = payload.get("change_timestamps_us")
            if isinstance(raw_change_timestamps, list):
                change_pairs = {
                    segment.timeline_to_source(value): value
                    for value in raw_change_timestamps
                    if segment.contains_timeline(value)
                }
                if change_pairs:
                    segment_payload["change_timestamps_us"] = sorted(change_pairs)
                    virtual_by_actual.update(change_pairs)
                else:
                    segment_payload["change_timestamps_us"] = actual_timestamps
            if payload.get("detect_changes") is True:
                segment_payload["reset_change_tracking"] = (
                    self._change_tracking_segment_index != segment.index
                )
            self._switch_engine_media(mode, segment.media_path)
            if self.engine is None:
                raise EngineError("ocr_engine_unavailable")
            result = self._request_readable_frames(segment_payload)
            if "execution_device" in result:
                self._remember_ocr_execution(result)
            if payload.get("detect_changes") is True:
                self._change_tracking_segment_index = segment.index
            for key in ("evidence", "watermark_detections", "scene_probes"):
                rows = result.get(key, [])
                if not isinstance(rows, list):
                    raise EngineError("ocr_engine_invalid_result")
                combined[key].extend(
                    self._map_evidence_row(
                        row,
                        segment=segment,
                        virtual_by_actual=virtual_by_actual,
                    )
                    for row in rows
                )
            for key in ("event_timestamps_us", "scene_event_timestamps_us"):
                values = result.get(key, [])
                if not isinstance(values, list):
                    raise EngineError("ocr_engine_invalid_result")
                combined[key].extend(
                    virtual_by_actual.get(value, segment.source_to_timeline(value))
                    for value in values
                    if type(value) is int
                    and segment.source_start_us <= value < segment.source_end_us
                )
            combined["frame_count"] += result.get("frame_count", len(actual_timestamps))
            combined["processed_until_us"] = virtual_timestamps[-1]
        for key in ("event_timestamps_us", "scene_event_timestamps_us"):
            combined[key] = sorted(set(combined[key]))
        return combined

    @staticmethod
    def _map_evidence_row(
        raw: object,
        *,
        segment: ScanPlanSegment,
        virtual_by_actual: dict[int, int],
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise EngineError("ocr_engine_invalid_result")
        source_timestamp_us = raw.get("timestamp_us")
        if type(source_timestamp_us) is not int or not (
            segment.source_start_us <= source_timestamp_us < segment.source_end_us
        ):
            raise EngineError("ocr_engine_timestamp_outside_scan_segment")
        timeline_timestamp_us = virtual_by_actual.get(
            source_timestamp_us,
            segment.source_to_timeline(source_timestamp_us),
        )
        identity = sha256(
            f"{raw.get('id', '')}|{segment.index}|{timeline_timestamp_us}".encode()
        ).hexdigest()[:24]
        source_frame_id = str(raw.get("frame_id", ""))
        return {
            **raw,
            "id": f"ocr-timeline-{identity}",
            "frame_id": f"timeline-{timeline_timestamp_us:012d}-segment-{segment.index}",
            "timestamp_us": timeline_timestamp_us,
            "source_timestamp_us": source_timestamp_us,
            "base_timestamp_us": segment.timeline_to_base(timeline_timestamp_us),
            "source_evidence_id": str(raw.get("id", "")),
            "source_frame_id": source_frame_id,
            "scan_segment_index": segment.index,
            "media_id": segment.media_id,
            "media_path": segment.media_path,
        }

    def _execute(self) -> int:
        if self.config.get("extraction_method") == "whisper_audio":
            return self._execute_whisper()
        duration_us = self._scan_plan_duration_us or self._integer_config("duration_us", 0, 1)
        interval_us = self._integer_config("sample_interval_us", 800_000, 400_000)
        fast_interval_us, quality_interval_us = _sampling_intervals(interval_us)
        batch_frames = self._integer_config("window_frame_count", 12, 2)
        bands = self._bands_payload()
        configured_windows = self._configured_scan_windows(duration_us)
        if self.stage == "fast":
            scene_timestamps = self._scan_fast(
                duration_us,
                fast_interval_us,
                batch_frames,
                bands,
                configured_windows,
            )
            if scene_timestamps and self.config.get("scene_text_enabled", True):
                self._scan_scene_text(scene_timestamps, batch_frames)
            self.stage = "quality"
            self.cursor_us = 0
            self._checkpoint()
        fast_candidates, rescue_windows = self._finalization_step(
            "Tổng hợp bằng chứng thoại Fast",
            lambda: self._fast_candidates(duration_us, fast_interval_us),
        )
        if configured_windows:
            rescue_windows = configured_windows
        if rescue_windows and self.stage == "quality":
            self._scan_quality(
                duration_us,
                quality_interval_us,
                batch_frames,
                bands,
                rescue_windows,
            )
        self._checkpoint()
        final_candidates = self._finalization_step(
            "Đối chiếu kết quả Fast và Quality",
            lambda: self._final_candidates(fast_candidates, rescue_windows,
                                           duration_us, quality_interval_us),
        )
        rescan_cue_id = self.config.get("rescan_cue_id")
        if isinstance(rescan_cue_id, str):
            return self._finish_targeted_rescan(final_candidates)
        if self.stage == "quality":
            self.stage = "scene_quality"
            self.cursor_us = 0
            self._checkpoint()
        if self.stage == "scene_quality" and self.config.get("scene_text_enabled", True):
            self._scan_scene_quality(duration_us, batch_frames)
        self.stage = "finalize"
        evidence = self._finalization_step("Đọc và đối chiếu bằng chứng OCR", self._load_evidence)
        result = self._finalization_step("Hoàn thiện cue thoại", lambda: finalize_ocr_candidates(
            final_candidates,
            video_duration_us=duration_us,
            evidence=evidence,
            suppressed_evidence={
                item.evidence_id: item.winner_evidence_id
                for item in getattr(self, "_suppressed_readings", ())
            },
        ))
        scene_cues = self._finalization_step("Ghép chữ trong cảnh", lambda: self._scene_cues(
            evidence,
            duration_us=duration_us,
            first_index=len(result.cues) + 1,
            dialogue_cues=result.cues,
        ))
        if scene_cues:
            result = replace(result, cues=(*result.cues, *scene_cues))
        coverage_evidence, coverage_cues, coverage_duration = self._coverage_timeline(
            evidence,
            result.cues,
            duration_us,
        )
        coverage = self._finalization_step(
            "Kiểm tra độ phủ phụ đề", lambda: measure_hard_sub_coverage(
            coverage_evidence,
            coverage_cues,
            minimum_coverage=0.95,
            ignored_overlay_tokens=result.overlay_tokens,
            video_duration_us=coverage_duration,
            progress=self._finalization_progress,
            ),
        )
        self._finalization_step("Chuẩn bị ghi SRT và báo cáo QA", lambda: None)
        blockers = list(result.blockers)
        if not coverage.subtitle_positive_timestamps:
            blockers.append("ocr_coverage_evidence_missing")
        else:
            if coverage.coverage_ratio < coverage.minimum_coverage:
                blockers.append(
                    "ocr_coverage_below_threshold:"
                    f"{coverage.covered_timestamps}/{coverage.subtitle_positive_timestamps}:"
                    f"{coverage.coverage_ratio:.6f}"
                )
            if not coverage.timeline_continuity_passes:
                blockers.append(
                    "ocr_evidence_timeline_gap:"
                    f"{coverage.maximum_positive_gap_us}:"
                    f"{coverage.continuity_gap_limit_us}"
                )
        if result.status is not FinalizationStatus.PASS or blockers:
            review_path = self.job_staging / "ocr-review.srt.partial"
            _atomic_write_text(review_path, render_srt(result.cues), encoding="utf-8-sig")
            review_qa_path = self.job_staging / "ocr-review_QA.json.partial"
            _atomic_write_json(
                review_qa_path,
                self._qa_payload(
                    status="REVIEW",
                    output_path=review_path,
                    cue_count=len(result.cues),
                    coverage=coverage.as_json(),
                    blockers=blockers,
                    warnings=list(result.warnings),
                    cues=result.cues,
                ),
            )
            self._checkpoint(
                artifact_path=review_qa_path,
                artifact_kind="ocr_qa_json",
                checkpoint_key="review-qa",
                extra_payload={
                    "stage": "review",
                    "status": "REVIEW",
                    "coverage_ratio": coverage.coverage_ratio,
                    "coverage_threshold": coverage.minimum_coverage,
                },
            )
            self._checkpoint(
                artifact_path=review_path,
                artifact_kind="ocr_review_srt",
                checkpoint_key="review",
                extra_payload={
                    "stage": "review",
                    "blockers": blockers,
                    "warnings": list(result.warnings),
                    "coverage_ratio": coverage.coverage_ratio,
                    "coverage_threshold": coverage.minimum_coverage,
                    "qa_checkpoint_key": "review-qa",
                },
            )
            self.send(
                "failed",
                {
                    "code": "ocr_review_required",
                    "blocker_count": len(blockers),
                    "warning_count": len(result.warnings),
                    "coverage_ratio": coverage.coverage_ratio,
                    "coverage_threshold": coverage.minimum_coverage,
                },
            )
            return 3
        output_path = (
            Path(
                str(
                    self.config.get(
                        "staging_output_path",
                        self.config.get(
                            "output_path",
                            Path(str(self.config["media_path"]))
                            .with_suffix("")
                            .with_name(Path(str(self.config["media_path"])).stem + "_ZH_FINAL.srt"),
                        ),
                    )
                )
            )
            .expanduser()
            .resolve()
        )
        publish_final_srt(output_path, result)
        qa_path = output_path.with_name(output_path.stem + "_QA.json")
        _atomic_write_json(
            qa_path,
            self._qa_payload(
                status="PASS",
                output_path=output_path,
                cue_count=len(result.cues),
                coverage=coverage.as_json(),
                blockers=[],
                warnings=list(result.warnings),
                cues=result.cues,
            ),
        )
        self.send(
            "succeeded",
            {
                "status": "PASS",
                "output_path": str(output_path),
                "qa_path": str(qa_path),
                "cue_count": len(result.cues),
                "warning_count": len(result.warnings),
                "coverage_ratio": coverage.coverage_ratio,
                "coverage_threshold": coverage.minimum_coverage,
            },
        )
        return 0

    def _qa_payload(
        self,
        *,
        status: str,
        output_path: Path,
        cue_count: int,
        coverage: dict[str, Any],
        blockers: list[str],
        warnings: list[str],
        cues: tuple[SubtitleCue, ...],
    ) -> dict[str, Any]:
        output_stat = output_path.stat()
        return {
            "schema_version": 3,
            "ocr_aggregation_version": OCR_AGGREGATION_VERSION,
            "kind": "ocr_hard_sub_coverage",
            "status": status,
            "job_id": self.job_id,
            "input_fingerprint": self.spec.get("input_fingerprint", ""),
            "settings_fingerprint": self.spec.get("settings_fingerprint", ""),
            "coverage_threshold": coverage["minimum_coverage"],
            "coverage_ratio": coverage["coverage_ratio"],
            "subtitle_positive_timestamps": coverage["subtitle_positive_timestamps"],
            "covered_timestamps": coverage["covered_timestamps"],
            "continuity_gap_limit_us": coverage["continuity_gap_limit_us"],
            "maximum_positive_gap_us": coverage["maximum_positive_gap_us"],
            "timeline_continuity_passes": coverage["timeline_continuity_passes"],
            "unmatched_samples": coverage["unmatched_samples"],
            "cue_count": cue_count,
            "decoder_video_duration_us": dict(self._decoder_video_durations),
            "decoder_tail_request_counts": dict(self._decoder_tail_request_counts),
            "suppressed_readings": [
                asdict(item) for item in getattr(self, "_suppressed_readings", ())
            ],
            "proof_only_collapse": True,
            "deferred_reading_ids": [
                item.evidence_id for item in getattr(self, "_deferred_readings", ())
            ],
            "retained_deferred_reading_ids": sorted(
                {key for cue in cues for key in cue.evidence_ids}
                & {item.evidence_id for item in getattr(self, "_deferred_readings", ())}
            ),
            "blockers": blockers,
            "warnings": warnings,
            "cue_regions": [
                {
                    "id": cue.id,
                    "index": cue.index,
                    "start_us": cue.range.start_us,
                    "end_us": cue.range.end_us,
                    "box": list(cue.visual_box) if cue.visual_box is not None else None,
                    "source": cue.visual_box_source or "estimated",
                    "role": cue.role,
                    "orientation": cue.orientation,
                    "style_hint": cue.style_hint,
                    "text_font_size": cue.text_font_size,
                    "panel_box": list(cue.panel_box) if cue.panel_box is not None else None,
                    "background_color": cue.background_color,
                    "review_state": cue.review_state,
                    "confidence": cue.confidence,
                    "evidence_ids": list(cue.evidence_ids),
                }
                for cue in cues
            ],
            # Platform marks are deliberately excluded from subtitle evidence,
            # but their repeated OCR geometry remains useful for concealing the
            # baked-in mark during preview/export. A single observation is not
            # enough to create an automatic mask; _watermark_regions enforces
            # that confidence boundary.
            "watermark_regions": self._watermark_regions(),
            "output_path": str(output_path),
            "output_sha256": _file_sha256(output_path),
            "output_size": output_stat.st_size,
            "output_mtime_ns": output_stat.st_mtime_ns,
        }

    def _execute_whisper(self) -> int:
        duration_us = self._integer_config("duration_us", 0, 1)
        self.stage = "extract_audio"
        self._ensure_whisper_audio(duration_us)
        self.stage = "whisper"
        configured_device = str(self.config.get("device", "auto")).strip().casefold()
        while self.cursor_us < duration_us:
            device = "cpu" if self._whisper_force_cpu or configured_device == "cpu" else "cuda"
            compute_type = "int8" if device == "cpu" else "float16"
            self._whisper_runtime = {"device": device, "compute_type": compute_type}
            self.send(
                "progress",
                {
                    "phase": "load_model",
                    "current": 0,
                    "total": 1,
                    "cursor_us": self.cursor_us,
                    "device": device,
                    "compute_type": compute_type,
                },
            )
            self._open_whisper_engine()
            try:
                started = time.monotonic()
                assert isinstance(self.engine, WhisperEngineClient)
                stream = self.engine.stream(
                    {
                        "audio_path": str(self.whisper_audio_path),
                        "model_path": str(self.config["model_path"]),
                        "model_id": str(self.config.get("model_id", "large-v3")),
                        "language": str(self.config.get("language", "zh")),
                        "start_us": self.cursor_us,
                        "duration_us": duration_us,
                        "device": device,
                        "compute_type": compute_type,
                        "beam_size": int(self.config.get("beam_size", 5)),
                        "temperature": float(self.config.get("temperature", 0.0)),
                        "vad_parameters": dict(self.config.get("vad_parameters", {})),
                    },
                    poll_control=self._handle_whisper_controls,
                )
                for event, payload in stream:
                    if event == "metadata":
                        self._whisper_runtime.update(payload)
                        continue
                    if event != "segment":
                        continue
                    self._append_whisper_segment(payload, duration_us=duration_us)
                    self.cursor_us = int(payload["end_us"])
                    self._send_whisper_progress(duration_us, started)
                    if self._whisper_segment_count() % 8 == 0:
                        self._checkpoint_whisper()
                break
            except WhisperRestart:
                continue
            except EngineError as exc:
                if (
                    str(exc) == "whisper_cuda_failed"
                    and device == "cuda"
                    and self.config.get("allow_cpu_fallback", True) is True
                ):
                    self._whisper_force_cpu = True
                    self._whisper_runtime = {"device": "cpu", "compute_type": "int8"}
                    self._rewind_whisper_segments()
                    self._checkpoint_whisper()
                    self.send(
                        "progress",
                        {
                            "phase": "cpu_fallback",
                            "current": 0,
                            "total": 1,
                            "cursor_us": self.cursor_us,
                            "device": "cpu",
                            "compute_type": "int8",
                        },
                    )
                    continue
                raise
            finally:
                if self.engine is not None:
                    self.engine.close()
                    self.engine = None

        cues, warnings = _whisper_cues(self._load_whisper_segments(), video_duration_us=duration_us)
        if not cues:
            raise RuntimeError("whisper_no_speech_detected")
        output_path = (
            Path(
                str(
                    self.config.get(
                        "staging_output_path",
                        self.config.get("output_path", ""),
                    )
                )
            )
            .expanduser()
            .resolve()
        )
        if not output_path.name.endswith("_ZH_FINAL.srt"):
            raise ValueError("whisper_output_path_invalid")
        _atomic_write_text(
            output_path,
            "\ufeff" + render_srt(cues),
            encoding="utf-8",
        )
        output_stat = output_path.stat()
        qa_path = output_path.with_name(output_path.stem + "_QA.json")
        qa_payload = {
            "schema_version": 1,
            "kind": "whisper_asr",
            "status": "PASS",
            "job_id": self.job_id,
            "input_fingerprint": self.spec.get("input_fingerprint", ""),
            "settings_fingerprint": self.spec.get("settings_fingerprint", ""),
            "extraction_method": "whisper_audio",
            "provider_id": self.spec.get("provider_id", "faster-whisper-audio"),
            "model_id": self.spec.get("model_id", "large-v3"),
            "device": str(self._whisper_runtime.get("device", "")),
            "compute_type": str(self._whisper_runtime.get("compute_type", "")),
            "cue_count": len(cues),
            "last_cue_end_us": cues[-1].range.end_us,
            "warnings": warnings,
            "output_path": str(output_path),
            "output_sha256": _file_sha256(output_path),
            "output_size": output_stat.st_size,
            "output_mtime_ns": output_stat.st_mtime_ns,
        }
        _atomic_write_json(qa_path, qa_payload)
        self.send(
            "progress",
            {
                "phase": "finalize",
                "current": 1,
                "total": 1,
                "cursor_us": duration_us,
            },
        )
        self.send(
            "succeeded",
            {
                "status": "PASS",
                "output_path": str(output_path),
                "qa_path": str(qa_path),
                "cue_count": len(cues),
                "warning_count": len(warnings),
                "device": qa_payload["device"],
                "compute_type": qa_payload["compute_type"],
            },
        )
        return 0

    def _open_whisper_engine(self) -> None:
        if self.engine is not None:
            self.engine.close()
        python_path = Path(str(self.config.get("engine_python", sys.executable))).resolve()
        runtime_host = os.environ.get("DUCAI_OCR_ENGINE_HOST_PATH", "").strip()
        host_path = (
            Path(
                runtime_host
                or str(
                    self.config.get(
                        "engine_host_path",
                        Path(__file__).parents[1] / "engine_hosts" / "whisper_engine_host.py",
                    )
                )
            )
            .expanduser()
            .resolve()
        )
        runtime_hash = os.environ.get("DUCAI_OCR_ENGINE_HOST_SHA256", "").strip()
        if runtime_hash and sha256(host_path.read_bytes()).hexdigest() != runtime_hash:
            raise RuntimeError("whisper_engine_host_runtime_changed")
        self.engine = WhisperEngineClient(python_path, host_path)

    def _ensure_whisper_audio(self, duration_us: int) -> None:
        while not (
            self.whisper_audio_path.is_file() and self.whisper_audio_path.stat().st_size >= 4_096
        ):
            if not self._extract_whisper_audio_once(duration_us):
                return

    def _extract_whisper_audio_once(self, duration_us: int) -> bool:
        media_path = Path(str(self.config.get("media_path", ""))).expanduser().resolve(strict=True)
        ffprobe = Path(str(self.config.get("ffprobe_path", ""))).expanduser().resolve(strict=True)
        ffmpeg = Path(str(self.config.get("ffmpeg_path", ""))).expanduser().resolve(strict=True)
        stream = _probe_dialogue_stream(ffprobe, media_path)
        channels = int(stream.get("channels") or 2)
        mix = (
            "pan=mono|c0=0.18*c0+0.18*c1+0.64*c2"
            if channels >= 3
            else "pan=mono|c0=0.5*c0+0.5*c1"
            if channels == 2
            else "anull"
        )
        temporary = self.whisper_audio_path.with_name("dialogue-16k-mono.partial.wav")
        temporary.unlink(missing_ok=True)
        progress_lines: queue.Queue[str | None] = queue.Queue()
        stderr_lines: deque[str] = deque(maxlen=40)
        process = subprocess.Popen(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(media_path),
                "-map",
                f"0:{int(stream['index'])}",
                "-af",
                mix,
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                "-progress",
                "pipe:1",
                "-nostats",
                str(temporary),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        def read_progress() -> None:
            if process.stdout is not None:
                for line in process.stdout:
                    progress_lines.put(line)
            progress_lines.put(None)

        def read_errors() -> None:
            if process.stderr is not None:
                for line in process.stderr:
                    stderr_lines.append(line.rstrip()[:500])

        threading.Thread(target=read_progress, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()
        restart = False
        try:
            while process.poll() is None:
                if self._handle_whisper_controls():
                    restart = True
                    process.terminate()
                    break
                try:
                    line = progress_lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    continue
                progress_us = _ffmpeg_progress_us(line)
                if progress_us is not None:
                    self.send(
                        "progress",
                        {
                            "phase": "extract_audio",
                            "current": min(duration_us, progress_us),
                            "total": duration_us,
                            "cursor_us": 0,
                        },
                    )
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
            if restart or process.returncode not in {0, None}:
                temporary.unlink(missing_ok=True)
        if restart:
            return True
        if return_code != 0 or not temporary.is_file() or temporary.stat().st_size < 4_096:
            raise RuntimeError("whisper_audio_extract_failed")
        os.replace(temporary, self.whisper_audio_path)
        return False

    def _handle_whisper_controls(self) -> bool:
        while True:
            try:
                message = self.commands.get_nowait()
            except queue.Empty:
                return False
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if isinstance(command_id, str) and command_id in self.acknowledgements:
                cached = self.acknowledgements[command_id]
                sys.stdout.buffer.write(cached.to_json_line())
                sys.stdout.buffer.flush()
                continue
            if action == "pause" and isinstance(command_id, str):
                if isinstance(self.engine, WhisperEngineClient):
                    self.engine.terminate()
                self._checkpoint_whisper()
                self.acknowledge(command_id, "paused", {"status": "paused"})
                while True:
                    paused_message = self.commands.get()
                    paused_action = paused_message.payload.get("action")
                    paused_id = paused_message.payload.get("command_id")
                    if not isinstance(paused_id, str):
                        if paused_action == "stop":
                            self.send("stopped", {"status": "stop"})
                            raise WorkerStopped
                        continue
                    if paused_id in self.acknowledgements:
                        cached = self.acknowledgements[paused_id]
                        sys.stdout.buffer.write(cached.to_json_line())
                        sys.stdout.buffer.flush()
                        continue
                    if paused_action == "resume":
                        self._rewind_whisper_segments()
                        self.acknowledge(paused_id, "resumed", {"status": "running"})
                        return True
                    if paused_action in {"cancel", "stop"}:
                        self._checkpoint_whisper()
                        self.send(
                            "stopped",
                            {"status": str(paused_action), "command_id": paused_id},
                        )
                        raise WorkerStopped
            if action in {"cancel", "stop"}:
                if isinstance(self.engine, WhisperEngineClient):
                    self.engine.terminate()
                self._checkpoint_whisper()
                payload = {"status": str(action)}
                if isinstance(command_id, str):
                    payload["command_id"] = command_id
                self.send("stopped", payload)
                raise WorkerStopped

    def _append_whisper_segment(
        self,
        raw: dict[str, Any],
        *,
        duration_us: int,
    ) -> None:
        start_us = raw.get("start_us")
        end_us = raw.get("end_us")
        text = str(raw.get("text", "")).strip()
        if (
            type(start_us) is not int
            or type(end_us) is not int
            or start_us < 0
            or end_us <= start_us
            or end_us > duration_us + 1_000
            or not text
        ):
            raise ValueError("whisper_segment_invalid")
        row = {
            "start_us": start_us,
            "end_us": min(duration_us, end_us),
            "text": text,
            "avg_logprob": float(raw.get("avg_logprob", -0.75) or -0.75),
            "no_speech_prob": float(raw.get("no_speech_prob", 0.0) or 0.0),
            "compression_ratio": float(raw.get("compression_ratio", 1.0) or 1.0),
            "avg_word_probability": raw.get("avg_word_probability"),
            "words": raw.get("words", []),
        }
        self.whisper_segments_path.parent.mkdir(parents=True, exist_ok=True)
        with self.whisper_segments_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._whisper_segment_total += 1

    def _load_whisper_segments(self) -> list[dict[str, Any]]:
        if not self.whisper_segments_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.whisper_segments_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("whisper_checkpoint_corrupt") from exc
            if not isinstance(row, dict):
                raise ValueError("whisper_checkpoint_corrupt")
            rows.append(row)
        return rows

    def _whisper_segment_count(self) -> int:
        return self._whisper_segment_total

    def _rewind_whisper_segments(self) -> None:
        rewind_us = max(0, self.cursor_us - 5_000_000)
        rows = [
            row
            for row in self._load_whisper_segments()
            if type(row.get("end_us")) is int and int(row["end_us"]) <= rewind_us
        ]
        temporary = self.whisper_segments_path.with_suffix(".rewrite")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(temporary, self.whisper_segments_path)
        self._whisper_segment_total = len(rows)
        # Resume only at a completed segment boundary. Starting at the raw
        # five-second mark can cut through a spoken sentence and permanently
        # drop its first words when the host slices decoded audio.
        self.cursor_us = int(rows[-1]["end_us"]) if rows else 0

    def _checkpoint_whisper(self) -> None:
        if not self.whisper_segments_path.is_file():
            self.whisper_segments_path.write_text("", encoding="utf-8")
        source = self.job_staging / f"whisper-checkpoint-{uuid4().hex}.ndjson.partial"
        shutil.copyfile(self.whisper_segments_path, source)
        self.send(
            "checkpoint",
            {
                "stage_key": "ocr",
                "checkpoint_key": "latest",
                "schema_version": 1,
                "cursor_us": self.cursor_us,
                "payload": {
                    "stage": "whisper",
                    "extraction_method": "whisper_audio",
                    "cursor_us": self.cursor_us,
                    "overlap_us": 5_000_000,
                    "device": str(self._whisper_runtime.get("device", "")),
                    "compute_type": str(self._whisper_runtime.get("compute_type", "")),
                },
                "artifact_path": str(source),
                "artifact_kind": "whisper_segments",
                "artifact_state": "technical",
                "expected_sha256": _file_sha256(source),
            },
        )

    def _send_whisper_progress(self, duration_us: int, started: float) -> None:
        elapsed_ms = max(1, int((time.monotonic() - started) * 1_000))
        processed = max(0, self.cursor_us)
        rate = processed / max(0.001, elapsed_ms / 1_000)
        remaining = max(0, duration_us - processed)
        self.send(
            "progress",
            {
                "phase": "transcribe",
                "current": processed,
                "total": duration_us,
                "elapsed_ms": elapsed_ms,
                "eta_ms": int(remaining / rate * 1_000) if rate > 0 else None,
                "cursor_us": self.cursor_us,
                "cue_count": self._whisper_segment_count(),
                "device": str(self._whisper_runtime.get("device", "")),
                "compute_type": str(self._whisper_runtime.get("compute_type", "")),
            },
        )

    def _scan_fast(
        self,
        duration_us: int,
        interval_us: int,
        batch_frames: int,
        bands: list[dict[str, Any]],
        configured_windows: tuple[RescueWindow, ...],
    ) -> tuple[int, ...]:
        mode = "fast"
        try:
            self._open_engine(mode)
        except EngineError as error:
            mode = "compatibility"
            self._ocr_fallback_reason = str(error)[:80]
            self.send(
                "progress",
                {"phase": "rapid_fallback", "current": 0, "total": 0},
            )
            self._open_engine(mode)
        if configured_windows:
            timestamps = sorted(
                {
                    value
                    for window in configured_windows
                    for value in self._timeline_timestamps(
                        max(self.cursor_us, window.range.start_us),
                        window.range.end_us,
                        interval_us,
                    )
                }
            )
        else:
            timestamps = self._timeline_timestamps(self.cursor_us, duration_us, interval_us)
        started = time.monotonic()
        completed = 0
        historical_burst, historical_scene = (
            self._adaptive_timestamps(
                self._change_events,
                duration_us,
                scene_events=self._scene_change_events,
            )
            if not configured_windows
            else ((), ())
        )
        processed_burst_timestamps = {value for value in historical_burst if value < self.cursor_us}
        processed_scene_timestamps = {value for value in historical_scene if value < self.cursor_us}
        change_probe_interval_us = 200_000
        change_probe_timestamps = (
            self._timeline_timestamps(self.cursor_us, duration_us, change_probe_interval_us)
            if not configured_windows
            else []
        )
        change_probe_offset = 0
        for offset in range(0, len(timestamps), batch_frames):
            batch = timestamps[offset : offset + batch_frames]
            if not batch:
                continue
            if self._handle_controls(stage="fast", cursor_us=self.cursor_us):
                raise WorkerStopped
            change_probe_end_us = min(duration_us, batch[-1] + interval_us)
            if self._scan_plan_segments:
                final_batch_segment = self._segment_for_timeline(batch[-1])
                if final_batch_segment is None:
                    raise EngineError("ocr_scan_plan_timestamp_out_of_range")
                # Do not consume probes from the next retained segment before a
                # main OCR timestamp represents that segment in this request.
                # Otherwise _recognize cannot issue a probe-only host request
                # without also sending an unintended frame to the recognizer.
                change_probe_end_us = min(
                    change_probe_end_us,
                    final_batch_segment.timeline_end_us,
                )
            next_change_probe_offset = change_probe_offset
            while (
                next_change_probe_offset < len(change_probe_timestamps)
                and change_probe_timestamps[next_change_probe_offset] < change_probe_end_us
            ):
                next_change_probe_offset += 1
            batch_change_timestamps = change_probe_timestamps[
                change_probe_offset:next_change_probe_offset
            ]
            change_probe_offset = next_change_probe_offset
            try:
                result = self._recognize(
                    {
                        "timestamps_us": batch,
                        "bands": bands,
                        "evidence_kind": "global",
                        "detect_changes": not configured_windows,
                        "detect_scene_changes": self.config.get("scene_text_enabled", True),
                        "change_threshold": 0.115,
                        "scene_change_threshold": 0.10,
                        "change_timestamps_us": batch_change_timestamps or batch,
                        **self._recognize_region_payload(bands),
                    },
                    mode=mode,
                )
            except EngineError as error:
                if mode == "compatibility":
                    raise
                mode = "compatibility"
                self._ocr_fallback_reason = str(error)[:80]
                self.send(
                    "progress",
                    {
                        "phase": "rapid_fallback",
                        "current": completed,
                        "total": len(timestamps),
                    },
                )
                self._open_engine(mode)
                result = self._recognize(
                    {
                        "timestamps_us": batch,
                        "bands": bands,
                        "evidence_kind": "global",
                        "detect_changes": not configured_windows,
                        "detect_scene_changes": self.config.get("scene_text_enabled", True),
                        "change_threshold": 0.115,
                        "scene_change_threshold": 0.10,
                        "change_timestamps_us": batch_change_timestamps or batch,
                        **self._recognize_region_payload(bands),
                    },
                    mode=mode,
                )
            self._append_evidence(result.get("evidence", []))
            self._append_evidence(result.get("watermark_detections", []))
            raw_events = result.get("event_timestamps_us", [])
            if not isinstance(raw_events, list) or not all(
                type(value) is int for value in raw_events
            ):
                raise EngineError("ocr_change_probe_invalid")
            self._change_events = sorted({*self._change_events, *raw_events})
            raw_scene_events = result.get("scene_event_timestamps_us", raw_events)
            if not isinstance(raw_scene_events, list) or not all(
                type(value) is int for value in raw_scene_events
            ):
                raise EngineError("ocr_scene_change_probe_invalid")
            self._scene_change_events = sorted({*self._scene_change_events, *raw_scene_events})
            burst_timestamps, scene_timestamps = (
                self._adaptive_timestamps(
                    self._change_events,
                    duration_us,
                    scene_events=self._scene_change_events,
                )
                if not configured_windows
                else ((), ())
            )
            adaptive_horizon_us = min(duration_us, change_probe_end_us + 240_000)
            pending_burst = tuple(
                value
                for value in burst_timestamps
                if value <= adaptive_horizon_us and value not in processed_burst_timestamps
            )
            pending_scene = tuple(
                value
                for value in scene_timestamps
                if value <= adaptive_horizon_us and value not in processed_scene_timestamps
            )
            if pending_burst or pending_scene:
                self._scan_adaptive(
                    pending_burst,
                    pending_scene,
                    batch_frames,
                    bands,
                )
                processed_burst_timestamps.update(pending_burst)
                processed_scene_timestamps.update(pending_scene)
            self.cursor_us = batch[-1]
            completed += len(batch)
            self._send_progress("fast", completed, len(timestamps), started)
            if completed % (batch_frames * 5) == 0:
                self._checkpoint()
        return ()

    def _adaptive_timestamps(
        self,
        raw_events: list[int],
        duration_us: int,
        *,
        scene_events: list[int] | None = None,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if not self._scan_plan_segments:
            burst, scene = self._adaptive_timestamps_from_events(
                raw_events,
                duration_us,
                scene_events=scene_events,
            )
            return burst, scene if self.config.get("scene_text_enabled", True) else ()
        burst: set[int] = set()
        scene: set[int] = set()
        all_scene_events = scene_events or []
        for segment in self._scan_plan_segments:
            local_events = [
                value - segment.timeline_start_us
                for value in raw_events
                if segment.contains_timeline(value)
            ]
            local_scene_events = [
                value - segment.timeline_start_us
                for value in all_scene_events
                if segment.contains_timeline(value)
            ]
            local_burst, local_scene = self._adaptive_timestamps_from_events(
                local_events,
                segment.timeline_duration_us,
                scene_events=local_scene_events,
            )
            burst.update(segment.timeline_start_us + value for value in local_burst)
            scene.update(segment.timeline_start_us + value for value in local_scene)
        return (tuple(sorted(burst)),
                tuple(sorted(scene)) if self.config.get("scene_text_enabled", True) else ())

    @staticmethod
    def _adaptive_timestamps_from_events(
        raw_events: list[int],
        duration_us: int,
        *,
        scene_events: list[int] | None = None,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        def bounded_events(values: list[int], slots: int) -> list[int]:
            # Fixed slots select the first chronological transition in each
            # slot. This is stable while a scan is still growing, remains
            # capped per 30 seconds and is O(n), not a
            # full event-list scan for every time window.
            bounded: list[int] = []
            occupied: set[tuple[int, int]] = set()
            for value in sorted(set(values)):
                if value < 0 or value >= duration_us:
                    continue
                window = value // window_us
                offset = value - window * window_us
                slot = min(slots - 1, offset * slots // window_us)
                key = (window, slot)
                if key in occupied:
                    continue
                occupied.add(key)
                bounded.append(value)
            return bounded

        window_us = 30_000_000
        # Keep dialogue sampling unchanged; labels need a later stable frame
        # after their fade/animation, and closer anchors for static backgrounds.
        events = bounded_events(raw_events, 48)
        scene_source = list(dict.fromkeys((*raw_events, *(scene_events or ()))))
        bounded_scene_events = bounded_events(scene_source, 24)
        burst = sorted(
            {
                timestamp + offset
                for timestamp in events
                # A one-character subtitle can exist for only ~0.5 s. Sample
                # densely around the detected transition, but only in this
                # bounded local burst instead of scanning the whole film densely.
                for offset in (-100_000, 0, 100_000, 200_000, 300_000, 400_000)
                if 0 <= timestamp + offset < duration_us
            }
        )
        scene_values = {
            timestamp + offset
            for timestamp in bounded_scene_events
            for offset in (0, 240_000, 600_000)
            if 0 <= timestamp + offset < duration_us
        }
        scene_values.update(range(0, duration_us, 3_000_000))
        scene = sorted(scene_values)
        return tuple(burst), tuple(scene)

    def _scan_adaptive(
        self,
        burst_timestamps: tuple[int, ...],
        scene_timestamps: tuple[int, ...],
        batch_frames: int,
        bands: list[dict[str, Any]],
    ) -> None:
        """OCR one local, chronological adaptive window without full rescans."""

        if not self.config.get("scene_text_enabled", True):
            scene_timestamps = ()
        timestamps = sorted({*burst_timestamps, *scene_timestamps})
        if not timestamps:
            return
        scene_band = {
            "name": "scene",
            "x1": 0.0,
            "y1": 0.0,
            "x2": 1.0,
            "y2": 1.0,
            "scale": 1.0,
        }
        band_names = [str(item["name"]) for item in bands]
        started = time.monotonic()
        for offset in range(0, len(timestamps), batch_frames):
            batch = timestamps[offset : offset + batch_frames]
            if self._handle_controls(stage="fast", cursor_us=self.cursor_us):
                raise WorkerStopped
            burst_batch = [value for value in batch if value in burst_timestamps]
            scene_batch = [value for value in batch if value in scene_timestamps]
            active_bands = [*bands, scene_band] if scene_batch else bands
            band_timestamps = {name: burst_batch for name in band_names}
            if scene_batch:
                band_timestamps["scene"] = scene_batch
            result = self._recognize(
                {
                    "timestamps_us": batch,
                    "bands": active_bands,
                    "band_timestamps_us": band_timestamps,
                    "evidence_kinds_by_band": {
                        **{name: "burst" for name in band_names},
                        **({"scene": "scene"} if scene_batch else {}),
                    },
                    **self._recognize_region_payload(active_bands),
                },
                mode=self._engine_mode,
            )
            self._append_evidence(result.get("evidence", []))
            self._append_evidence(result.get("watermark_detections", []))
            self._append_evidence(result.get("scene_probes", []))
            self._send_progress(
                "adaptive_ocr",
                min(offset + len(batch), len(timestamps)),
                len(timestamps),
                started,
            )

    def _scan_scene_text(
        self,
        timestamps: tuple[int, ...],
        batch_frames: int,
    ) -> None:
        if not self.config.get("scene_text_enabled", True):
            return
        scene_band = [{"name": "scene", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0, "scale": 1.0}]
        started = time.monotonic()
        for offset in range(0, len(timestamps), batch_frames):
            batch = list(timestamps[offset : offset + batch_frames])
            if self._handle_controls(stage="fast", cursor_us=self.cursor_us):
                raise WorkerStopped
            result = self._recognize(
                {
                    "timestamps_us": batch,
                    "bands": scene_band,
                    "evidence_kind": "scene",
                    **self._recognize_region_payload(scene_band),
                },
                mode=self._engine_mode,
            )
            self._append_evidence(result.get("evidence", []))
            self._append_evidence(result.get("watermark_detections", []))
            self._append_evidence(result.get("scene_probes", []))
            self.cursor_us = batch[-1]
            self._send_progress(
                "scene_text",
                min(offset + len(batch), len(timestamps)),
                len(timestamps),
                started,
            )

    def _scan_scene_quality(self, duration_us: int, batch_frames: int) -> None:
        if not self.config.get("scene_text_enabled", True):
            return
        seeds = tuple(item for item in self._scene_evidence(self._load_evidence())
                      if "quality" not in item.provider_id)
        regions = scene_text_rescue_regions(seeds, video_duration_us=duration_us)
        jobs = []
        bands = {}
        for index, region in enumerate(regions):
            start, end = region.range.start_us, region.range.end_us
            if self._scan_plan_segments:
                segment = next((s for s in self._scan_plan_segments
                                if region.source_scope == f"{s.media_id}:{s.index}"), None)
                if segment is None:
                    continue
                start = max(start, segment.timeline_start_us)
                end = min(end, segment.timeline_end_us)
            b = region.box
            target = region.target_box
            name = f"scene-roi-{index}"
            bands[name] = {"name": name, "reading_mode": "scene", "x1": b.x, "y1": b.y,
                           "x2": b.x + b.width, "y2": b.y + b.height, "scale": 2.0,
                           "target_box": [target.x, target.y, target.width, target.height]}
            jobs.extend((t, name) for t in range(start, end, 400_000) if t >= self.cursor_us)
        jobs.sort()
        if not jobs:
            return
        mode = "quality"
        if self.engine is None or self._engine_mode != mode:
            try:
                self._open_engine(mode)
            except EngineError:
                mode = "compatibility"
                self._open_engine(mode)
        started = time.monotonic()
        for offset in range(0, len(jobs), batch_frames):
            batch = jobs[offset:offset + batch_frames]
            if self._handle_controls(stage="scene_quality", cursor_us=self.cursor_us):
                raise WorkerStopped
            names = list(dict.fromkeys(name for _, name in batch))
            crops = [bands[name] for name in names]
            payload = {
                "timestamps_us": sorted({t for t, _ in batch}), "bands": crops,
                "band_timestamps_us": {name: sorted({t for t, n in batch if n == name})
                                       for name in names},
                "evidence_kinds_by_band": {name: "scene" for name in names},
                **self._recognize_region_payload(crops),
            }
            try:
                result = self._recognize(payload, mode=mode)
            except EngineError:
                if mode == "compatibility":
                    raise
                mode = "compatibility"
                self._open_engine(mode)
                result = self._recognize(payload, mode=mode)
            for key in ("evidence", "watermark_detections", "scene_probes"):
                rows = result.get(key, [])
                if mode == "compatibility":
                    rows = [{**row, "provider_id": "rapid-quality-fallback"} for row in rows]
                self._append_evidence(rows)
            self.cursor_us = batch[-1][0]
            self._send_progress(
                "scene_quality", min(offset + len(batch), len(jobs)), len(jobs), started,
            )
            if offset // batch_frames % 5 == 4:
                self._checkpoint()
        self._checkpoint()

    def _scan_quality(
        self,
        duration_us: int,
        interval_us: int,
        batch_frames: int,
        bands: list[dict[str, Any]],
        windows,
    ) -> None:
        timestamps = sorted(
            {
                value
                for window in windows
                for value in self._timeline_timestamps(
                    max(0, window.range.start_us),
                    min(duration_us, window.range.end_us),
                    interval_us,
                )
            }
        )
        if self.stage == "quality" and self.cursor_us > 0:
            timestamps = [value for value in timestamps if value >= self.cursor_us]
        if not timestamps:
            return
        compatibility_mode = False
        try:
            self._open_engine("quality")
        except EngineError:
            self._open_engine("compatibility")
            compatibility_mode = True
        started = time.monotonic()
        for offset in range(0, len(timestamps), batch_frames):
            batch = timestamps[offset : offset + batch_frames]
            if self._handle_controls(stage="quality", cursor_us=self.cursor_us):
                raise WorkerStopped
            compatibility_fallback = compatibility_mode
            try:
                result = self._recognize(
                    {
                        "timestamps_us": batch,
                        "bands": bands,
                        **self._recognize_region_payload(bands),
                    },
                    mode="compatibility" if compatibility_mode else "quality",
                )
            except EngineError:
                if compatibility_mode:
                    raise
                self._open_engine("compatibility")
                compatibility_fallback = True
                compatibility_mode = True
                result = self._recognize(
                    {
                        "timestamps_us": batch,
                        "bands": bands,
                        **self._recognize_region_payload(bands),
                    },
                    mode="compatibility",
                )
            rows = result.get("evidence", [])
            watermark_rows = result.get("watermark_detections", [])
            if compatibility_fallback and isinstance(rows, list):
                rows = [
                    {**row, "provider_id": "rapid-quality-fallback"}
                    if isinstance(row, dict)
                    else row
                    for row in rows
                ]
            if compatibility_fallback and isinstance(watermark_rows, list):
                watermark_rows = [
                    {**row, "provider_id": "rapid-quality-fallback"}
                    if isinstance(row, dict)
                    else row
                    for row in watermark_rows
                ]
            self._append_evidence(rows)
            self._append_evidence(watermark_rows)
            self.cursor_us = batch[-1]
            self._send_progress(
                "quality",
                min(offset + len(batch), len(timestamps)),
                len(timestamps),
                started,
            )
            if (offset // batch_frames + 1) % 5 == 0:
                self._checkpoint()

    def _coverage_timeline(self, evidence, cues, duration_us):
        """Do not count intentional legacy gaps as missing OCR footage."""
        if not self._scan_plan_segments:
            return evidence, cues, duration_us
        retained_evidence = []
        retained_cues = []
        cursor = 0
        for segment in self._scan_plan_segments:
            shift = cursor - segment.timeline_start_us
            retained_evidence.extend(
                replace(item, timestamp_us=item.timestamp_us + shift)
                for item in self._segment_evidence(evidence, segment)
            )
            for cue in cues:
                start = max(cue.range.start_us, segment.timeline_start_us)
                end = min(cue.range.end_us, segment.timeline_end_us)
                if end > start:
                    retained_cues.append(replace(cue, range=TimeRange(start + shift, end + shift)))
            cursor += segment.timeline_duration_us
        return tuple(retained_evidence), tuple(retained_cues), cursor

    def _send_progress(self, phase: str, current: int, total: int, started: float) -> None:
        elapsed_ms = max(1, int((time.monotonic() - started) * 1000))
        rate = current / (elapsed_ms / 1000)
        remaining = max(0, total - current)
        eta_ms = int(remaining / rate * 1000) if rate > 0 else None
        self.send(
            "progress",
            {
                "phase": phase,
                "current": current,
                "total": total,
                "elapsed_ms": elapsed_ms,
                "eta_ms": eta_ms,
                "cursor_us": self.cursor_us,
            },
        )

    def _finalization_step(self, detail: str, callback: Callable):
        self._finalization_detail = detail
        self._finalization_started = time.monotonic()
        self._finalization_last_progress = 0.0
        try:
            self._finalization_progress(0, 0)
            return callback()
        finally:
            self._finalization_detail = ""

    def _finalization_progress(self, current: int, total: int) -> None:
        if not getattr(self, "_finalization_detail", ""):
            return
        now = time.monotonic()
        if now - self._finalization_last_progress < .5 and current != total:
            return
        self._finalization_last_progress = now
        self.send("progress", {
            "phase": "finalize", "current": 0, "total": 0,
            "phase_current": current, "phase_total": total,
            "phase_detail": self._finalization_detail,
            "elapsed_ms": int((now - self._finalization_started) * 1000),
            "eta_ms": None, "cursor_us": self.cursor_us,
        })
        if self._handle_controls(stage=self.stage, cursor_us=self.cursor_us):
            raise WorkerStopped()

    def _handle_controls(self, *, stage: str, cursor_us: int) -> bool:
        while True:
            try:
                message = self.commands.get_nowait()
            except queue.Empty:
                return False
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if isinstance(command_id, str) and command_id in self.acknowledgements:
                cached = self.acknowledgements[command_id]
                sys.stdout.buffer.write(cached.to_json_line())
                sys.stdout.buffer.flush()
                continue
            if action == "pause" and isinstance(command_id, str):
                self.stage = stage
                self.cursor_us = cursor_us
                self._checkpoint()
                self.acknowledge(command_id, "paused", {"status": "paused"})
                if self._wait_while_paused():
                    return True
            elif action in {"cancel", "stop"}:
                self.stage = stage
                self.cursor_us = cursor_us
                self._checkpoint()
                payload = {"status": str(action)}
                if isinstance(command_id, str):
                    payload["command_id"] = command_id
                self.send("stopped", payload)
                return True

    def _wait_while_paused(self) -> bool:
        while True:
            message = self.commands.get()
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if action == "stop" and not isinstance(command_id, str):
                self.send("stopped", {"status": "stop"})
                return True
            if not isinstance(command_id, str):
                continue
            if command_id in self.acknowledgements:
                cached = self.acknowledgements[command_id]
                sys.stdout.buffer.write(cached.to_json_line())
                sys.stdout.buffer.flush()
                continue
            if action == "resume":
                self.acknowledge(command_id, "resumed", {"status": "running"})
                return False
            if action in {"cancel", "stop"}:
                payload = {"status": str(action), "command_id": command_id}
                self.send("stopped", payload)
                return True

    def _append_evidence(self, raw_rows: object) -> None:
        if not isinstance(raw_rows, list):
            raise ValueError("ocr_evidence_must_be_array")
        with self.evidence_path.open("a", encoding="utf-8", newline="\n") as stream:
            for row in raw_rows:
                if not isinstance(row, dict):
                    raise ValueError("ocr_evidence_must_be_object")
                if row.get("record_kind") == "scene_probe":
                    _scene_probe_from_row(row)
                else:
                    _evidence_from_row(row)
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _load_evidence(self) -> tuple[SubtitleEvidence, ...]:
        if not self.evidence_path.is_file():
            return ()
        values: dict[str, SubtitleEvidence] = {}
        byte_total = self.evidence_path.stat().st_size
        completed_bytes = 0
        with self.evidence_path.open("r", encoding="utf-8") as stream:
            for index, line in enumerate(stream):
                completed_bytes += len(line.encode("utf-8"))
                if index % 1024 == 0:
                    self._finalization_progress(completed_bytes, byte_total)
                try:
                    raw = json.loads(line)
                    if isinstance(raw, dict) and (
                        raw.get("ignored_overlay") is True
                        or raw.get("record_kind") == "scene_probe"
                    ):
                        continue
                    item = _evidence_from_row(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                values[item.id] = item
        ordered = tuple(
            sorted(values.values(), key=lambda item: (item.timestamp_us, item.band, item.id))
        )
        crops = {
            str(band["name"]): NormalizedBox(
                float(band["x1"]),
                float(band["y1"]),
                float(band["x2"]) - float(band["x1"]),
                float(band["y2"]) - float(band["y1"]),
            )
            for band in self._bands_payload()
        }
        reconciled = reconcile_dialogue_evidence(ordered, crops)
        self._suppressed_readings = (*reconciled.suppressions, *reconciled.deferred_suppressions)
        self._deferred_readings = reconciled.deferred_suppressions
        return reconciled.evidence

    def _load_watermark_evidence(self) -> tuple[SubtitleEvidence, ...]:
        if not self.evidence_path.is_file():
            return ()
        values: dict[str, SubtitleEvidence] = {}
        with self.evidence_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or raw.get("ignored_overlay") is not True:
                        continue
                    item = _evidence_from_row(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                values[item.id] = item
        return tuple(
            sorted(values.values(), key=lambda item: (item.timestamp_us, item.band, item.id))
        )

    def _segment_evidence(
        self,
        evidence: tuple[SubtitleEvidence, ...],
        segment: ScanPlanSegment,
    ) -> tuple[SubtitleEvidence, ...]:
        return tuple(item for item in evidence if segment.contains_timeline(item.timestamp_us))

    @staticmethod
    def _clamp_candidates_to_segment(candidates, segment: ScanPlanSegment):
        return tuple(
            replace(
                item,
                range=TimeRange(
                    max(segment.timeline_start_us, item.range.start_us),
                    min(segment.timeline_end_us, item.range.end_us),
                ),
            )
            for item in candidates
            if max(segment.timeline_start_us, item.range.start_us)
            < min(segment.timeline_end_us, item.range.end_us)
        )

    def _scene_cues(
        self,
        evidence: tuple[SubtitleEvidence, ...],
        *,
        duration_us: int,
        first_index: int,
        dialogue_cues: tuple[SubtitleCue, ...],
    ) -> tuple[SubtitleCue, ...]:
        if not self.config.get("scene_text_enabled", True):
            return ()
        evidence = self._scene_evidence(evidence)
        probes = self._load_scene_probes()
        if not self._scan_plan_segments:
            return scene_text_cues_from_evidence(
                evidence,
                video_duration_us=duration_us,
                first_index=first_index,
                dialogue_cues=dialogue_cues,
                probes=probes,
                progress=self._finalization_progress,
            )
        result: list[SubtitleCue] = []
        for segment in self._scan_plan_segments:
            segment_dialogue = tuple(
                cue
                for cue in dialogue_cues
                if cue.range.start_us < segment.timeline_end_us
                and segment.timeline_start_us < cue.range.end_us
            )
            cues = scene_text_cues_from_evidence(
                self._segment_evidence(evidence, segment),
                video_duration_us=duration_us,
                first_index=first_index + len(result),
                dialogue_cues=segment_dialogue,
                probes=tuple(p for p in probes if segment.contains_timeline(p.timestamp_us)),
                progress=self._finalization_progress,
            )
            result.extend(
                replace(
                    cue,
                    range=TimeRange(
                        max(segment.timeline_start_us, cue.range.start_us),
                        min(segment.timeline_end_us, cue.range.end_us),
                    ),
                )
                for cue in cues
                if max(segment.timeline_start_us, cue.range.start_us)
                < min(segment.timeline_end_us, cue.range.end_us)
            )
        return tuple(result)

    def _scene_evidence(
        self, evidence: tuple[SubtitleEvidence, ...],
    ) -> tuple[SubtitleEvidence, ...]:
        anchors = self._load_watermark_evidence()
        stable = {}
        for anchor in anchors:
            b = anchor.box
            if b is None or not ((b.x < .15 or b.x + b.width > .85)
                                 and (b.y < .16 or b.y + b.height > .84)):
                continue
            key = (anchor.source_scope, round(b.x * 10), round(b.y * 10))
            stable.setdefault(key, []).append(anchor)
        confirmed = [rows for rows in stable.values()
                     if len({r.frame_id for r in rows}) >= 3
                     and max(r.timestamp_us for r in rows) - min(r.timestamp_us for r in rows)
                     >= 2_000_000]
        result = []
        for item in evidence:
            if item.evidence_kind != "scene" or item.box is None:
                result.append(item)
                continue
            b = item.box
            overlap = False
            for rows in confirmed:
                if rows[0].source_scope != item.source_scope:
                    continue
                if not (rows[0].timestamp_us - 3_000_000 <= item.timestamp_us
                        <= rows[-1].timestamp_us + 3_000_000):
                    continue
                for anchor in rows:
                    a = anchor.box
                    assert a is not None
                    intersection = max(
                        0.0, min(a.x + a.width, b.x + b.width) - max(a.x, b.x),
                    ) * max(
                        0.0, min(a.y + a.height, b.y + b.height) - max(a.y, b.y))
                    if intersection >= b.width * b.height * .50:
                        overlap = True
                        break
                if overlap:
                    break
            if not overlap:
                result.append(item)
        return tuple(result)

    def _load_scene_probes(self) -> tuple[SceneTextProbe, ...]:
        if not self.evidence_path.is_file():
            return ()
        values = {}
        with self.evidence_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and row.get("record_kind") == "scene_probe":
                        probe = _scene_probe_from_row(row)
                        values[(probe.timestamp_us, probe.box, probe.source_scope)] = probe
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        return tuple(values.values())

    def _watermark_regions(self) -> list[dict[str, object]]:
        detections = [item for item in self._load_watermark_evidence() if item.box is not None]
        duration_us = self._scan_plan_duration_us or self._integer_config("duration_us", 0, 1)
        groups = (
            [
                (
                    segment.timeline_start_us,
                    segment.timeline_end_us,
                    [item for item in detections if segment.contains_timeline(item.timestamp_us)],
                )
                for segment in self._scan_plan_segments
            ]
            if self._scan_plan_segments
            else [(0, duration_us, detections)]
        )
        result: list[dict[str, object]] = []
        for group_start_us, group_end_us, group_detections in groups:
            clusters: list[list[SubtitleEvidence]] = []
            for item in group_detections:
                box = item.box
                assert box is not None
                center = (box.x + box.width / 2, box.y + box.height / 2)
                target = next(
                    (
                        cluster
                        for cluster in clusters
                        if cluster[-1].box is not None
                        and abs(center[0] - (cluster[-1].box.x + cluster[-1].box.width / 2)) <= 0.15
                        and abs(center[1] - (cluster[-1].box.y + cluster[-1].box.height / 2))
                        <= 0.10
                    ),
                    None,
                )
                if target is None:
                    clusters.append([item])
                else:
                    target.append(item)
            for cluster in clusters:
                global_cluster = [item for item in cluster if item.evidence_kind == "global"]
                frame_ids = {item.frame_id for item in global_cluster}
                timestamps = sorted({item.timestamp_us for item in global_cluster})
                if len(frame_ids) < 3 or len(timestamps) < 3:
                    continue
                group_duration_us = group_end_us - group_start_us
                minimum_span_us = min(5_000_000, max(600_000, group_duration_us // 10))
                if timestamps[-1] - timestamps[0] < minimum_span_us:
                    continue
                boxes = [item.box for item in global_cluster if item.box is not None]
                if not boxes or not all(
                    box.x <= 0.12
                    or box.x + box.width >= 0.88
                    or box.y <= 0.16
                    or box.y + box.height >= 0.84
                    for box in boxes
                ):
                    continue
                left = max(0.0, min(box.x for box in boxes) - 0.008)
                top = max(0.0, min(box.y for box in boxes) - 0.006)
                right = min(1.0, max(box.x + box.width for box in boxes) + 0.008)
                bottom = min(1.0, max(box.y + box.height for box in boxes) + 0.006)
                result.append(
                    {
                        "id": f"ocr-watermark-{len(result) + 1}",
                        "start_us": group_start_us,
                        "end_us": group_end_us,
                        "box": [left, top, right - left, bottom - top],
                        "sample_count": len(frame_ids),
                        "source": "ocr_bilibili_anchor",
                    }
                )
        return result

    def _fast_candidates(self, duration_us: int, interval_us: int):
        evidence = tuple(
            item
            for item in self._load_evidence()
            if item.provider_id not in {"paddle-quality", "rapid-quality-fallback"}
        )
        if getattr(self, "_scan_plan_segments", ()):
            candidates = []
            rescue_windows: list[RescueWindow] = []
            for segment in self._scan_plan_segments:
                segment_evidence = self._segment_evidence(evidence, segment)
                by_band = {
                    band.name: group_visual_evidence(
                        segment_evidence,
                        band=band.name,
                        duration_us=duration_us,
                        sample_interval_us=interval_us,
                    )
                    for band in DEFAULT_SUBTITLE_BANDS
                }
                ids = iter(range(1, 1_000_000))
                merged = self._clamp_candidates_to_segment(
                    merge_band_candidates(
                        by_band.get("upper", ()),
                        by_band.get("lower", ()),
                        evidence=segment_evidence,
                        progress=self._finalization_progress,
                        id_factory=lambda segment_index=segment.index, id_values=ids: (
                            f"fast-segment-{segment_index}-merged-{next(id_values)}"
                        ),
                    ),
                    segment,
                )
                candidates.extend(merged)
                local_candidates = tuple(
                    replace(
                        item,
                        range=TimeRange(
                            item.range.start_us - segment.timeline_start_us,
                            item.range.end_us - segment.timeline_start_us,
                        ),
                    )
                    for item in merged
                )
                local_windows = coalesce_rescue_windows(
                    targeted_rescue_windows(
                        local_candidates,
                        duration_us=segment.timeline_duration_us,
                    )
                )
                rescue_windows.extend(
                    RescueWindow(
                        TimeRange(
                            item.range.start_us + segment.timeline_start_us,
                            item.range.end_us + segment.timeline_start_us,
                        ),
                        item.reasons,
                    )
                    for item in local_windows
                )
            return tuple(candidates), tuple(rescue_windows)
        by_band = {
            band.name: group_visual_evidence(
                evidence,
                band=band.name,
                duration_us=duration_us,
                sample_interval_us=interval_us,
            )
            for band in DEFAULT_SUBTITLE_BANDS
        }
        ids = iter(range(1, 1_000_000))
        merged = merge_band_candidates(
            by_band.get("upper", ()),
            by_band.get("lower", ()),
            evidence=evidence,
            progress=self._finalization_progress,
            id_factory=lambda: f"fast-merged-{next(ids)}",
        )
        return merged, coalesce_rescue_windows(
            targeted_rescue_windows(merged, duration_us=duration_us)
        )

    def _final_candidates(
        self,
        fast_candidates,
        rescue_windows,
        duration_us: int,
        interval_us: int,
    ):
        all_evidence = self._load_evidence()
        if getattr(self, "_scan_plan_segments", ()):
            result = []
            for segment in self._scan_plan_segments:
                segment_fast = tuple(
                    item
                    for item in fast_candidates
                    if item.range.start_us < segment.timeline_end_us
                    and segment.timeline_start_us < item.range.end_us
                )
                segment_windows = tuple(
                    item
                    for item in rescue_windows
                    if item.range.start_us < segment.timeline_end_us
                    and segment.timeline_start_us < item.range.end_us
                )
                segment_evidence = self._segment_evidence(all_evidence, segment)
                quality_evidence = tuple(
                    item
                    for item in segment_evidence
                    if item.provider_id in {"paddle-quality", "rapid-quality-fallback"}
                )
                by_band = {
                    band.name: group_visual_evidence(
                        quality_evidence,
                        band=band.name,
                        duration_us=duration_us,
                        sample_interval_us=interval_us,
                    )
                    for band in DEFAULT_SUBTITLE_BANDS
                }
                ids = iter(range(1, 1_000_000))
                quality = self._clamp_candidates_to_segment(
                    merge_band_candidates(
                        by_band.get("upper", ()),
                        by_band.get("lower", ()),
                        evidence=quality_evidence,
                        progress=self._finalization_progress,
                        id_factory=lambda segment_index=segment.index, id_values=ids: (
                            f"quality-segment-{segment_index}-merged-{next(id_values)}"
                        ),
                    ),
                    segment,
                )
                cross_pass = self._clamp_candidates_to_segment(
                    cross_pass_consensus_candidates(
                        segment_evidence,
                        video_duration_us=duration_us,
                        evidence_half_interval_us=max(1, interval_us // 2),
                        maximum_gap_us=max(400_000, interval_us),
                    ),
                    segment,
                )
                rescued = apply_quality_rescue(
                    segment_fast,
                    (*quality, *cross_pass),
                    segment_windows,
                )
                reinforced = reinforce_candidate_ranges(
                    rescued,
                    quality_evidence,
                    video_duration_us=duration_us,
                    evidence_half_interval_us=interval_us // 2,
                    maximum_distance_us=interval_us,
                )
                result.extend(self._clamp_candidates_to_segment(reinforced, segment))
            return tuple(sorted(result, key=lambda item: (item.range.start_us, item.range.end_us)))
        evidence = tuple(
            item
            for item in all_evidence
            if item.provider_id in {"paddle-quality", "rapid-quality-fallback"}
        )
        by_band = {
            band.name: group_visual_evidence(
                evidence,
                band=band.name,
                duration_us=duration_us,
                sample_interval_us=interval_us,
            )
            for band in DEFAULT_SUBTITLE_BANDS
        }
        ids = iter(range(1, 1_000_000))
        quality = merge_band_candidates(
            by_band.get("upper", ()),
            by_band.get("lower", ()),
            evidence=evidence,
            progress=self._finalization_progress,
            id_factory=lambda: f"quality-merged-{next(ids)}",
        )
        cross_pass = cross_pass_consensus_candidates(
            all_evidence,
            video_duration_us=duration_us,
            evidence_half_interval_us=max(1, interval_us // 2),
            maximum_gap_us=max(400_000, interval_us),
        )
        rescued = apply_quality_rescue(
            fast_candidates,
            (*quality, *cross_pass),
            rescue_windows,
        )
        return reinforce_candidate_ranges(
            rescued,
            evidence,
            video_duration_us=duration_us,
            evidence_half_interval_us=interval_us // 2,
            maximum_distance_us=interval_us,
        )

    def _configured_scan_windows(self, duration_us: int) -> tuple[RescueWindow, ...]:
        raw_windows = self.config.get("scan_windows")
        if not isinstance(raw_windows, list):
            return ()
        result: list[RescueWindow] = []
        for raw in raw_windows:
            if not isinstance(raw, dict):
                raise ValueError("invalid_scan_window")
            start_us = raw.get("start_us")
            end_us = raw.get("end_us")
            reasons = raw.get("reasons", ["targeted_rescan"])
            if (
                type(start_us) is not int
                or type(end_us) is not int
                or start_us < 0
                or end_us > duration_us
                or end_us <= start_us
                or not isinstance(reasons, list)
                or not all(isinstance(reason, str) and reason for reason in reasons)
            ):
                raise ValueError("invalid_scan_window")
            result.append(RescueWindow(TimeRange(start_us, end_us), tuple(reasons)))
        return tuple(result)

    def _finish_targeted_rescan(self, candidates) -> int:
        start_us = self._integer_config("rescan_cue_start_us", 0, 0)
        end_us = self._integer_config("rescan_cue_end_us", 0, 1)
        matching = [
            item
            for item in candidates
            if item.range.start_us < end_us and start_us < item.range.end_us
        ]
        evidence = self._load_evidence()
        matching = collapse_proven_ocr_candidates(
            tuple(matching), evidence=evidence,
            suppressed_evidence={
                item.evidence_id: item.winner_evidence_id
                for item in getattr(self, "_suppressed_readings", ())
            },
        )
        if not matching:
            self.send("failed", {"code": "ocr_rescan_no_consensus"})
            return 3
        winner = max(
            matching,
            key=lambda item: (
                min(item.range.end_us, end_us) - max(item.range.start_us, start_us),
                item.confidence,
            ),
        )
        if winner.review_reasons:
            self.send(
                "failed",
                {"code": "ocr_rescan_conflict", "review_count": len(winner.review_reasons)},
            )
            return 3
        self.send(
            "succeeded",
            {
                "status": "PASS",
                "rescan": {
                    "text": winner.text,
                    "confidence": winner.confidence,
                    "evidence_ids": list(winner.evidence_ids),
                    "source": winner.source + ":targeted-rescan",
                },
            },
        )
        return 0

    def _checkpoint(
        self,
        *,
        artifact_path: Path | None = None,
        artifact_kind: str = "ocr_evidence",
        checkpoint_key: str = "latest",
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        source = artifact_path
        if source is None:
            if not self.evidence_path.is_file():
                self.evidence_path.write_text("", encoding="utf-8")
            source = self.job_staging / f"checkpoint-{uuid4().hex}.ndjson.partial"
            shutil.copyfile(self.evidence_path, source)
        digest = _file_sha256(source)
        payload = {
            "stage_key": "ocr",
            "checkpoint_key": checkpoint_key,
            "schema_version": 1,
            "cursor_us": self.cursor_us,
            "payload": {
                "stage": self.stage,
                "cursor_us": self.cursor_us,
                "band_cursors_us": {band.name: self.cursor_us for band in DEFAULT_SUBTITLE_BANDS},
                "change_events_us": self._change_events,
                "scene_change_events_us": self._scene_change_events,
                "input_fingerprint": self.spec.get("input_fingerprint", ""),
                "settings_fingerprint": self.spec.get("settings_fingerprint", ""),
                "ocr_aggregation_version": OCR_AGGREGATION_VERSION,
                **(
                    {"scan_plan_fingerprint": self._scan_plan_fingerprint}
                    if self._scan_plan_fingerprint
                    else {}
                ),
                **(extra_payload or {}),
            },
            "artifact_path": str(source),
            "artifact_kind": artifact_kind,
            "artifact_state": "technical",
            "expected_sha256": digest,
        }
        self.send("checkpoint", payload)


def _probe_dialogue_stream(ffprobe: Path, media_path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,channels:stream_tags=language,title",
            "-of",
            "json",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if completed.returncode != 0:
        raise RuntimeError("whisper_media_probe_failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("whisper_media_probe_invalid") from exc
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    audio = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if not audio:
        raise RuntimeError("whisper_audio_track_missing")

    def score(stream: dict[str, Any]) -> tuple[int, int]:
        tags = stream.get("tags", {})
        language = str(tags.get("language", "")).casefold() if isinstance(tags, dict) else ""
        return int(language in {"chi", "zho", "cmn", "zh", "und", ""}), int(
            stream.get("channels") or 0
        )

    return max(audio, key=score)


def _ffmpeg_progress_us(line: str) -> int | None:
    key, _, value = line.strip().partition("=")
    try:
        if key in {"out_time_us", "out_time_ms"}:
            return max(0, int(value))
        if key == "out_time":
            hours, minutes, seconds = value.split(":")
            return max(
                0,
                int(round((int(hours) * 3_600 + int(minutes) * 60 + float(seconds)) * 1_000_000)),
            )
    except (TypeError, ValueError):
        return None
    return None


def _whisper_cues(
    raw_rows: list[dict[str, Any]],
    *,
    video_duration_us: int,
) -> tuple[tuple[SubtitleCue, ...], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    leading_punctuation = "。！？?!，,；;：:、"
    for source in sorted(
        raw_rows,
        key=lambda row: (int(row.get("start_us", 0)), int(row.get("end_us", 0))),
    ):
        start_us = source.get("start_us")
        end_us = source.get("end_us")
        text = str(source.get("text", "")).strip()
        if (
            type(start_us) is not int
            or type(end_us) is not int
            or not text
            or start_us < 0
            or end_us <= start_us
        ):
            warnings.append("whisper_invalid_segment_removed")
            continue
        start_us = min(video_duration_us - 1, start_us)
        end_us = min(video_duration_us, max(start_us + 1, end_us))
        leading = ""
        while text and text[0] in leading_punctuation:
            leading += text[0]
            text = text[1:].lstrip()
        if leading and rows:
            rows[-1]["text"] = str(rows[-1]["text"]).rstrip() + leading
            warnings.append("whisper_leading_punctuation_repaired")
        if not text:
            if rows:
                rows[-1]["end_us"] = max(int(rows[-1]["end_us"]), end_us)
            continue
        compact = "".join(character for character in text.casefold() if character.isalnum())
        if rows:
            previous = rows[-1]
            previous_compact = "".join(
                character for character in str(previous["text"]).casefold() if character.isalnum()
            )
            gap_us = start_us - int(previous["end_us"])
            if compact and compact == previous_compact and -100_000 <= gap_us <= 850_000:
                previous["end_us"] = max(int(previous["end_us"]), end_us)
                warnings.append("whisper_duplicate_segment_merged")
                continue
            if start_us < int(previous["end_us"]):
                boundary = (start_us + int(previous["end_us"])) // 2
                boundary = max(int(previous["start_us"]) + 1, min(end_us - 1, boundary))
                if boundary <= int(previous["start_us"]) or boundary >= end_us:
                    warnings.append("whisper_overlap_segment_removed")
                    continue
                previous["end_us"] = boundary
                start_us = boundary
                warnings.append("whisper_overlap_repaired")
        probability = source.get("avg_word_probability")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            log_probability = source.get("avg_logprob", -0.75)
            probability = (
                math.exp(float(log_probability))
                if isinstance(log_probability, (int, float))
                and not isinstance(log_probability, bool)
                else 0.5
            )
        rows.append(
            {
                **source,
                "start_us": start_us,
                "end_us": end_us,
                "text": text,
                "confidence": max(0.0, min(1.0, float(probability))),
            }
        )
    cues = tuple(
        SubtitleCue(
            id=f"whisper-{index:06d}",
            index=index,
            range=TimeRange(int(row["start_us"]), int(row["end_us"])),
            text=str(row["text"]),
            language="zh",
            source="whisper-large-v3",
            confidence=float(row["confidence"]),
        )
        for index, row in enumerate(rows, start=1)
    )
    return cues, list(dict.fromkeys(warnings))


def _scene_probe_from_row(raw: dict[str, Any]) -> SceneTextProbe:
    return SceneTextProbe(
        raw["timestamp_us"], NormalizedBox(*raw["region"]),
        str(raw.get("source_scope", "")) or (
            f"{raw.get('media_id', '')}:{raw.get('scan_segment_index', '')}"
            if "media_id" in raw or "scan_segment_index" in raw else ""
        ),
    )


def _evidence_from_row(raw: object) -> SubtitleEvidence:
    if not isinstance(raw, dict):
        raise TypeError("OCR evidence row must be an object")
    raw_box = raw.get("box")
    box = None
    if isinstance(raw_box, list) and len(raw_box) == 4:
        box = NormalizedBox(*(float(value) for value in raw_box))
    raw_panel_box = raw.get("panel_box")
    panel_box = None
    if isinstance(raw_panel_box, list) and len(raw_panel_box) == 4:
        panel_box = NormalizedBox(*(float(value) for value in raw_panel_box))
    return SubtitleEvidence(
        id=str(raw["id"]),
        frame_id=str(raw["frame_id"]),
        timestamp_us=int(raw["timestamp_us"]),
        band=str(raw["band"]),
        text=str(raw["text"]),
        confidence=float(raw["confidence"]),
        provider_id=str(raw["provider_id"]),
        model_id=str(raw["model_id"]),
        box=box,
        evidence_kind=str(raw.get("evidence_kind", "global")),
        orientation=str(raw.get("orientation", "horizontal")),
        style_hint=str(raw.get("style_hint", "standard")),
        panel_box=panel_box,
        background_color=str(raw.get("background_color", "")),
        scene_confidence=float(raw.get("scene_confidence", 0.0)),
        source_scope=str(raw.get("source_scope", ""))
        or (
            f"{raw.get('media_id', '')}:{raw.get('scan_segment_index', '')}"
            if "media_id" in raw or "scan_segment_index" in raw
            else ""
        ),
    )


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str, *, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding=encoding, newline="\n")
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DUCAI OCR worker")
    parser.add_argument("job_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    from ducai_studio.infrastructure.licensing_state import worker_activation_exit_code

    denied = worker_activation_exit_code()
    if denied:
        return denied
    return OcrWorker(arguments.job_id).run()


def exit_ocr_worker(exit_code: int) -> NoReturn:
    """Flush after host cleanup without finalizing the blocked command reader."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    exit_ocr_worker(main())
