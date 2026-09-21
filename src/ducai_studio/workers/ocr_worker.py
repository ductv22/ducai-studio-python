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
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from ducai_studio.application.ocr_pipeline import (
    apply_quality_rescue,
    coalesce_rescue_windows,
    group_visual_evidence,
    measure_hard_sub_coverage,
    merge_band_candidates,
    reinforce_candidate_ranges,
    targeted_rescue_windows,
)
from ducai_studio.application.subtitle_finalizer import (
    FinalizationStatus,
    finalize_ocr_candidates,
    publish_final_srt,
    render_srt,
)
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.subtitles import NormalizedBox, RescueWindow, SubtitleEvidence
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.external_runtime import (
    isolated_python_environment,
    isolated_python_working_directory,
)
from ducai_studio.infrastructure.portable_runtime import paddle_cache_home_for_runtime
from ducai_studio.providers.ocr import DEFAULT_SUBTITLE_BANDS
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


class EngineError(RuntimeError):
    pass


class WorkerStopped(RuntimeError):
    pass


class WhisperRestart(RuntimeError):
    pass


def _sampling_intervals(configured_interval_us: int) -> tuple[int, int]:
    return max(400_000, configured_interval_us // 2), configured_interval_us


class EngineHostClient:
    def __init__(self, python_path: Path, host_path: Path) -> None:
        self.python_path = python_path
        self.host_path = host_path
        self.process: subprocess.Popen[str] | None = None
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
        if process is None:
            return
        if process.poll() is None and process.stdin is not None and process.stdout is not None:
            try:
                request_id = str(uuid4())
                process.stdin.write(
                    json.dumps(
                        {
                            "protocol_version": 1,
                            "id": request_id,
                            "action": "shutdown",
                            "payload": {},
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                process.wait(timeout=5)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.kill()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


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
        self._whisper_force_cpu = False
        self._whisper_runtime: dict[str, Any] = {}
        self._whisper_segment_total = 0
        self._reader = threading.Thread(target=self._read_commands, daemon=True)

    def send(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> WorkerMessage:
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
        self._restore_checkpoint(start_payload.get("checkpoint"))

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
        uri = artifact.get("uri")
        if not isinstance(uri, str):
            return
        source = Path(uri).expanduser().resolve(strict=True)
        shutil.copyfile(source, self.evidence_path)
        self.stage = str(payload.get("stage", "fast"))
        raw_cursor = payload.get("cursor_us", checkpoint.get("cursor_us", 0))
        self.cursor_us = int(raw_cursor) if type(raw_cursor) is int else 0
        if self.stage == "fast":
            interval = self._integer_config("sample_interval_us", 800_000, 400_000)
            rewind = max(0, self.cursor_us - interval * 2)
            self._filter_working_evidence(
                lambda row: not (
                    row.get("provider_id") in {"paddle-fast", "rapid-compatibility"}
                    and int(row.get("timestamp_us", 0)) >= rewind
                )
            )
            self.cursor_us = rewind
        elif self.stage == "quality" and self.cursor_us > 0:
            fast_interval = self._integer_config("sample_interval_us", 800_000, 400_000)
            quality_interval = max(400_000, fast_interval // 2)
            rewind = max(0, self.cursor_us - quality_interval * 2)
            self._filter_working_evidence(
                lambda row: not (
                    row.get("provider_id")
                    in {"paddle-quality", "rapid-quality-fallback"}
                    and int(row.get("timestamp_us", 0)) >= rewind
                )
            )
            self.cursor_us = rewind

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
        with self.evidence_path.open("r", encoding="utf-8") as source, temporary.open(
            "w", encoding="utf-8", newline="\n"
        ) as destination:
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
            }
            for band in DEFAULT_SUBTITLE_BANDS
        ]

    def _open_engine(self, mode: str) -> None:
        if self.engine is not None:
            self.engine.close()
        python_path = (
            Path(str(self.config.get("engine_python", sys.executable)))
            .expanduser()
            .resolve()
        )
        runtime_host = os.environ.get("DUCAI_OCR_ENGINE_HOST_PATH", "").strip()
        host_path = Path(
            runtime_host
            or str(
                self.config.get(
                    "engine_host_path",
                    Path(__file__).parents[1] / "engine_hosts" / "ocr_engine_host.py",
                )
            )
        ).expanduser().resolve()
        runtime_host_hash = os.environ.get("DUCAI_OCR_ENGINE_HOST_SHA256", "").strip()
        if runtime_host_hash and sha256(host_path.read_bytes()).hexdigest() != runtime_host_hash:
            raise RuntimeError("ocr_engine_host_runtime_changed")
        self.engine = EngineHostClient(python_path, host_path)
        self.engine.request(
            "open",
            {
                "mode": mode,
                "media_path": str(self.config["media_path"]),
                "device": str(self.config.get("device", "gpu:0")),
                "batch_size": self._integer_config("engine_batch_size", 12, 1),
            },
        )

    def _execute(self) -> int:
        if self.config.get("extraction_method") == "whisper_audio":
            return self._execute_whisper()
        duration_us = self._integer_config("duration_us", 0, 1)
        interval_us = self._integer_config("sample_interval_us", 800_000, 400_000)
        fast_interval_us, quality_interval_us = _sampling_intervals(interval_us)
        batch_frames = self._integer_config("window_frame_count", 12, 2)
        bands = self._bands_payload()
        configured_windows = self._configured_scan_windows(duration_us)
        if self.stage == "fast":
            self._scan_fast(
                duration_us,
                fast_interval_us,
                batch_frames,
                bands,
                configured_windows,
            )
            self.stage = "quality"
            self.cursor_us = 0
            self._checkpoint()
        fast_candidates, rescue_windows = self._fast_candidates(
            duration_us, fast_interval_us
        )
        if configured_windows:
            rescue_windows = configured_windows
        if rescue_windows:
            self._scan_quality(
                duration_us,
                quality_interval_us,
                batch_frames,
                bands,
                rescue_windows,
            )
        self._checkpoint()
        final_candidates = self._final_candidates(
            fast_candidates,
            rescue_windows,
            duration_us,
            quality_interval_us,
        )
        rescan_cue_id = self.config.get("rescan_cue_id")
        if isinstance(rescan_cue_id, str):
            return self._finish_targeted_rescan(final_candidates)
        result = finalize_ocr_candidates(final_candidates, video_duration_us=duration_us)
        coverage = measure_hard_sub_coverage(
            self._load_evidence(),
            result.cues,
            minimum_coverage=0.95,
            ignored_overlay_tokens=result.overlay_tokens,
            video_duration_us=duration_us,
        )
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
        output_path = Path(
            str(
                self.config.get(
                    "staging_output_path",
                    self.config.get(
                        "output_path",
                        Path(str(self.config["media_path"])).with_suffix("").with_name(
                            Path(str(self.config["media_path"])).stem + "_ZH_FINAL.srt"
                        ),
                    ),
                )
            )
        ).expanduser().resolve()
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
    ) -> dict[str, Any]:
        output_stat = output_path.stat()
        return {
            "schema_version": 2,
            "kind": "ocr_hard_sub_coverage",
            "status": status,
            "job_id": self.job_id,
            "input_fingerprint": self.spec.get("input_fingerprint", ""),
            "settings_fingerprint": self.spec.get("settings_fingerprint", ""),
            "coverage_threshold": coverage["minimum_coverage"],
            "coverage_ratio": coverage["coverage_ratio"],
            "subtitle_positive_timestamps": coverage[
                "subtitle_positive_timestamps"
            ],
            "covered_timestamps": coverage["covered_timestamps"],
            "continuity_gap_limit_us": coverage["continuity_gap_limit_us"],
            "maximum_positive_gap_us": coverage["maximum_positive_gap_us"],
            "timeline_continuity_passes": coverage["timeline_continuity_passes"],
            "unmatched_samples": coverage["unmatched_samples"],
            "cue_count": cue_count,
            "blockers": blockers,
            "warnings": warnings,
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
            device = (
                "cpu"
                if self._whisper_force_cpu or configured_device == "cpu"
                else "cuda"
            )
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

        cues, warnings = _whisper_cues(
            self._load_whisper_segments(), video_duration_us=duration_us
        )
        if not cues:
            raise RuntimeError("whisper_no_speech_detected")
        output_path = Path(
            str(
                self.config.get(
                    "staging_output_path",
                    self.config.get("output_path", ""),
                )
            )
        ).expanduser().resolve()
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
        host_path = Path(
            runtime_host
            or str(
                self.config.get(
                    "engine_host_path",
                    Path(__file__).parents[1]
                    / "engine_hosts"
                    / "whisper_engine_host.py",
                )
            )
        ).expanduser().resolve()
        runtime_hash = os.environ.get("DUCAI_OCR_ENGINE_HOST_SHA256", "").strip()
        if runtime_hash and sha256(host_path.read_bytes()).hexdigest() != runtime_hash:
            raise RuntimeError("whisper_engine_host_runtime_changed")
        self.engine = WhisperEngineClient(python_path, host_path)

    def _ensure_whisper_audio(self, duration_us: int) -> None:
        while not (
            self.whisper_audio_path.is_file()
            and self.whisper_audio_path.stat().st_size >= 4_096
        ):
            if not self._extract_whisper_audio_once(duration_us):
                return

    def _extract_whisper_audio_once(self, duration_us: int) -> bool:
        media_path = Path(str(self.config.get("media_path", ""))).expanduser().resolve(
            strict=True
        )
        ffprobe = Path(str(self.config.get("ffprobe_path", ""))).expanduser().resolve(
            strict=True
        )
        ffmpeg = Path(str(self.config.get("ffmpeg_path", ""))).expanduser().resolve(
            strict=True
        )
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
    ) -> None:
        mode = "fast"
        try:
            self._open_engine(mode)
        except EngineError:
            mode = "compatibility"
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
                    for value in range(
                        max(self.cursor_us, window.range.start_us),
                        window.range.end_us,
                        interval_us,
                    )
                }
            )
        else:
            timestamps = list(range(self.cursor_us, duration_us, interval_us))
        started = time.monotonic()
        completed = 0
        for offset in range(0, len(timestamps), batch_frames):
            batch = timestamps[offset : offset + batch_frames]
            if not batch:
                continue
            if self._handle_controls(stage="fast", cursor_us=self.cursor_us):
                raise WorkerStopped
            try:
                result = self.engine.request(
                    "recognize", {"timestamps_us": batch, "bands": bands}
                )
            except EngineError:
                if mode == "compatibility":
                    raise
                mode = "compatibility"
                self.send(
                    "progress",
                    {
                        "phase": "rapid_fallback",
                        "current": completed,
                        "total": len(timestamps),
                    },
                )
                self._open_engine(mode)
                result = self.engine.request(
                    "recognize", {"timestamps_us": batch, "bands": bands}
                )
            self._append_evidence(result.get("evidence", []))
            self.cursor_us = batch[-1]
            completed += len(batch)
            self._send_progress("fast", completed, len(timestamps), started)
            if completed % (batch_frames * 5) == 0:
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
                for value in range(
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
                result = self.engine.request(
                    "recognize", {"timestamps_us": batch, "bands": bands}
                )
            except EngineError:
                if compatibility_mode:
                    raise
                self._open_engine("compatibility")
                compatibility_fallback = True
                compatibility_mode = True
                result = self.engine.request(
                    "recognize", {"timestamps_us": batch, "bands": bands}
                )
            rows = result.get("evidence", [])
            if compatibility_fallback and isinstance(rows, list):
                rows = [
                    {**row, "provider_id": "rapid-quality-fallback"}
                    if isinstance(row, dict)
                    else row
                    for row in rows
                ]
            self._append_evidence(rows)
            self.cursor_us = batch[-1]
            self._send_progress(
                "quality",
                min(offset + len(batch), len(timestamps)),
                len(timestamps),
                started,
            )
            if (offset // batch_frames + 1) % 5 == 0:
                self._checkpoint()

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
                _evidence_from_row(row)
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _load_evidence(self) -> tuple[SubtitleEvidence, ...]:
        if not self.evidence_path.is_file():
            return ()
        values: dict[str, SubtitleEvidence] = {}
        with self.evidence_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    raw = json.loads(line)
                    item = _evidence_from_row(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                values[item.id] = item
        return tuple(
            sorted(
                values.values(), key=lambda item: (item.timestamp_us, item.band, item.id)
            )
        )

    def _fast_candidates(self, duration_us: int, interval_us: int):
        evidence = tuple(
            item
            for item in self._load_evidence()
            if item.provider_id not in {"paddle-quality", "rapid-quality-fallback"}
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
        merged = merge_band_candidates(
            by_band.get("upper", ()),
            by_band.get("lower", ()),
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
        evidence = tuple(
            item
            for item in self._load_evidence()
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
            id_factory=lambda: f"quality-merged-{next(ids)}",
        )
        rescued = apply_quality_rescue(fast_candidates, quality, rescue_windows)
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
        digest = sha256(source.read_bytes()).hexdigest()
        payload = {
            "stage_key": "ocr",
            "checkpoint_key": checkpoint_key,
            "schema_version": 1,
            "cursor_us": self.cursor_us,
            "payload": {
                "stage": self.stage,
                "cursor_us": self.cursor_us,
                "band_cursors_us": {
                    band.name: self.cursor_us for band in DEFAULT_SUBTITLE_BANDS
                },
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
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "audio"
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
                int(
                    round(
                        (int(hours) * 3_600 + int(minutes) * 60 + float(seconds))
                        * 1_000_000
                    )
                ),
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
                character
                for character in str(previous["text"]).casefold()
                if character.isalnum()
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


def _evidence_from_row(raw: object) -> SubtitleEvidence:
    if not isinstance(raw, dict):
        raise TypeError("OCR evidence row must be an object")
    raw_box = raw.get("box")
    box = None
    if isinstance(raw_box, list) and len(raw_box) == 4:
        box = NormalizedBox(*(float(value) for value in raw_box))
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
    return OcrWorker(arguments.job_id).run()


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
