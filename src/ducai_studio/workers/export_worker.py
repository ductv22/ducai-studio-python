from __future__ import annotations

import contextlib
import ctypes
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from ducai_studio.application.voice_pipeline import atomic_write_json, file_sha256
from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.providers.export import (
    chunk_ranges,
    decode_export_snapshot,
    materialize_export_chunk,
    probe_ffmpeg_capabilities,
)
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage

PROGRESS_INTERVAL_SECONDS = 1.0


def _hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


class WorkerStopped(RuntimeError):
    pass


class ExportWorker:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.commands: queue.Queue[WorkerMessage] = queue.Queue()
        self.acknowledgements: dict[str, WorkerMessage] = {}
        self.spec: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.snapshot = None
        self.capabilities = None
        self.ledger: dict[str, Any] = {}
        self._job_started = time.monotonic()
        self._last_progress_sent = 0.0
        self._rendered_this_run_us = 0
        staging_root = (
            Path(os.environ.get("DUCAI_ARTIFACT_STAGING_ROOT", Path.cwd() / ".ducai-staging"))
            .expanduser()
            .resolve()
        )
        self.job_staging = staging_root / job_id
        self.job_staging.mkdir(parents=True, exist_ok=True)
        self.chunk_work = self.job_staging / "chunks"
        self.chunk_work.mkdir(exist_ok=True)
        self.ledger_path = self.job_staging / "export-ledger-working.json"
        self._reader = threading.Thread(target=self._read_commands, daemon=True)

    def send(self, kind: str, payload: dict[str, Any]) -> WorkerMessage:
        message = WorkerMessage(PROTOCOL_VERSION, self.job_id, str(uuid4()), kind, payload)
        self._emit_message(message)
        return message

    @staticmethod
    def _emit_message(message: WorkerMessage) -> None:
        sys.stdout.buffer.write(message.to_json_line())
        sys.stdout.buffer.flush()

    def _replay_acknowledgement(self, command_id: object) -> bool:
        if not isinstance(command_id, str):
            return False
        cached = self.acknowledgements.get(command_id)
        if cached is None:
            return False
        self._emit_message(cached)
        return True

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
            self.send("failed", {"code": "export_start_timeout"})
            return 2
        if start.kind != "control" or start.payload.get("action") != "start":
            self.send("failed", {"code": "export_start_command_invalid"})
            return 2
        command_id = start.payload.get("command_id")
        if not isinstance(command_id, str):
            self.send("failed", {"code": "export_start_command_invalid"})
            return 2
        try:
            self._configure(dict(start.payload))
            self.acknowledge(command_id, "started", {"status": "running"})
            self._execute()
            return 0
        except WorkerStopped:
            return 0
        except Exception as exc:
            code = str(exc) if str(exc).replace("_", "").isalnum() else type(exc).__name__
            self.send("failed", {"code": (code[:80] or "export_worker_failed")})
            return 2

    def _configure(self, start_payload: dict[str, Any]) -> None:
        spec = start_payload.get("job")
        if not isinstance(spec, dict) or spec.get("kind") != "export":
            raise ValueError("export_job_spec_missing")
        config = spec.get("provider_config")
        if not isinstance(config, dict):
            raise ValueError("export_provider_config_missing")
        snapshot = decode_export_snapshot(config.get("snapshot"))
        output_path = Path(str(config.get("output_path", ""))).expanduser().resolve()
        if output_path.suffix.casefold() != ".mp4" or not output_path.name:
            raise ValueError("export_output_path_invalid")
        ffmpeg = Path(str(config.get("ffmpeg_path", ""))).expanduser().resolve(strict=True)
        ffprobe = Path(str(config.get("ffprobe_path", ""))).expanduser().resolve(strict=True)
        cache_root = Path(str(config.get("cache_root", ""))).expanduser().resolve()
        if not cache_root:
            raise ValueError("export_cache_root_missing")
        self.spec = spec
        self.config = config
        self.config["output_path"] = str(output_path)
        self.config["ffmpeg_path"] = str(ffmpeg)
        self.config["ffprobe_path"] = str(ffprobe)
        self.config["cache_root"] = str(cache_root)
        self.snapshot = snapshot
        self.capabilities = probe_ffmpeg_capabilities(ffmpeg)
        if not self.capabilities.ready:
            raise ValueError("export_ffmpeg_capabilities_missing")
        signature_input = {
            "schema": "ducai-export-ledger-v1",
            "snapshot": config["snapshot"],
            "ffmpeg": self.capabilities.version_line,
        }
        legacy_signature = stable_fingerprint(signature_input)
        previous_signature = stable_fingerprint(
            {**signature_input, "render_contract": "measured-glyph-v1"}
        )
        readable_signature = stable_fingerprint(
            {**signature_input, "render_contract": "readable-glyph-panel-v2"})
        from ducai_studio.providers.export.ffmpeg import _bundled_font_directory

        directory = _bundled_font_directory()
        font_assets = {path.name: file_sha256(path) for path in sorted(directory.iterdir())
                       if path.suffix.casefold() in {".ttf", ".otf"}} if directory else {}
        signature = stable_fingerprint({**signature_input,
            "render_contract": "subtitle-system-fonts-v4", "font_assets": font_assets})
        background_signature = stable_fingerprint({**signature_input,
            "render_contract": "subtitle-background-layers-v3", "font_assets": font_assets})
        restored = self._restore_checkpoint(
            start_payload.get("checkpoint"),
            signature,
            legacy_signature=legacy_signature,
            previous_signature=previous_signature,
            readable_signature=readable_signature,
            background_signature=background_signature,
        )
        self.ledger = restored or {
            "schema_version": 1,
            "signature": signature,
            "entries": {},
        }

    def _restore_checkpoint(
        self,
        checkpoint: object,
        signature: str,
        *,
        legacy_signature: str | None = None,
        previous_signature: str | None = None,
        readable_signature: str | None = None,
        background_signature: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(checkpoint, dict):
            return None
        artifact = checkpoint.get("artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("uri"), str):
            return None
        source = Path(str(artifact["uri"])).expanduser().resolve(strict=True)
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
        if (
            isinstance(raw, dict)
            and isinstance(raw.get("signature"), str)
            and raw["signature"]
            in {
                legacy_signature,
                previous_signature,
                readable_signature,
                background_signature,
            }
            - {None, signature}
        ):
            # The input is the same but its old chunks used different font/ink
            # layout. Re-render without deleting valid historical cache files.
            return None
        if not isinstance(raw, dict) or raw.get("signature") != signature:
            raise ValueError("export_checkpoint_signature_mismatch")
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            raise ValueError("export_checkpoint_entries_invalid")
        valid: dict[str, Any] = {}
        for key, entry in entries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            path = entry.get("path")
            digest = entry.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                continue
            candidate = Path(path).expanduser().resolve()
            if candidate.is_file() and file_sha256(candidate) == digest:
                valid[key] = entry
        raw["entries"] = valid
        return raw

    def _execute(self) -> None:
        assert self.snapshot is not None and self.capabilities is not None
        self._job_started = time.monotonic()
        self._rendered_this_run_us = 0
        ranges = chunk_ranges(self.snapshot)
        entries = self.ledger.get("entries", {})
        if not isinstance(entries, dict):
            raise ValueError("export_ledger_entries_invalid")
        for index, (start_us, end_us) in enumerate(ranges):
            self._control_between_chunks()
            key = str(index)
            if key in entries:
                self._send_progress("render", ranges)
                continue
            if self._restore_global_chunk(index, start_us, end_us):
                self._control_between_chunks()
                self._send_progress("render", ranges)
                continue
            raw_output = self.chunk_work / f"chunk-{index:05d}.partial.mp4"
            raw_output.unlink(missing_ok=True)
            try:
                plan = materialize_export_chunk(
                    self.snapshot,
                    index=index,
                    start_us=start_us,
                    end_us=end_us,
                    work_dir=self.chunk_work,
                    output_path=raw_output,
                    capabilities=self.capabilities,
                )
                self._run_ffmpeg(plan.arguments, plan.duration_us, ranges, cwd=self.chunk_work)
            finally:
                for temporary_path in self.chunk_work.glob(f"voice-chunk-{index:05d}-*.wav"):
                    temporary_path.unlink(missing_ok=True)
            qa = self._probe_media(raw_output, plan.duration_us)
            self._control_between_chunks()
            if qa["playable"] is not True:
                raise ValueError("export_chunk_probe_failed")
            cache_path = self._chunk_cache_path(index, start_us, end_us)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = cache_path.with_name(cache_path.name + f".{uuid4().hex}.tmp")
            shutil.copyfile(raw_output, temp)
            self._control_between_chunks()
            os.replace(temp, cache_path)
            cache_sha256 = file_sha256(cache_path)
            self._control_between_chunks()
            metadata = {
                "index": index,
                "start_us": start_us,
                "end_us": end_us,
                "path": str(cache_path),
                "sha256": cache_sha256,
                "backend": plan.backend,
                "qa": qa,
            }
            atomic_write_json(cache_path.with_suffix(".json"), metadata)
            entries[key] = metadata
            self._rendered_this_run_us += plan.duration_us
            self._checkpoint(ranges)
            self._control_between_chunks()
            self._send_progress("render", ranges, force=True)
        self._finalize(ranges)

    def _restore_global_chunk(self, index: int, start_us: int, end_us: int) -> bool:
        path = self._chunk_cache_path(index, start_us, end_us)
        metadata_path = path.with_suffix(".json")
        if not path.is_file() or not metadata_path.is_file():
            return False
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                raw.get("index") != index
                or raw.get("start_us") != start_us
                or raw.get("end_us") != end_us
                or raw.get("sha256") != file_sha256(path)
                or not isinstance(raw.get("qa"), dict)
                or raw["qa"].get("playable") is not True
            ):
                return False
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        self.ledger.setdefault("entries", {})[str(index)] = raw
        return True

    def _chunk_cache_path(self, index: int, start_us: int, end_us: int) -> Path:
        identity = stable_fingerprint(
            {
                "ledger": self.ledger.get("signature"),
                "index": index,
                "start_us": start_us,
                "end_us": end_us,
            }
        )
        return Path(str(self.config["cache_root"])) / identity[:2] / f"{identity}.mp4"

    def _run_ffmpeg(
        self,
        arguments: tuple[str, ...] | list[str],
        duration_us: int,
        ranges: tuple[tuple[int, int], ...],
        *,
        cwd: Path,
        phase: str = "render",
        failure_code: str = "export_ffmpeg_failed",
    ) -> None:
        phase_started = time.monotonic()
        stderr_path = self.job_staging / "ffmpeg-last.log"
        with stderr_path.open("wb") as stderr_stream:
            process = subprocess.Popen(
                list(arguments),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_stream,
                **_hidden_subprocess_kwargs(),
            )
            assert process.stdout is not None
            lines: queue.Queue[bytes | None] = queue.Queue()

            def read_progress() -> None:
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)

            thread = threading.Thread(target=read_progress, daemon=True)
            thread.start()
            out_time_us = 0
            try:
                while process.poll() is None:
                    self._control_running_process(process, ranges)
                    try:
                        line = lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        continue
                    decoded = line.decode("utf-8", errors="replace").strip()
                    key, separator, value = decoded.partition("=")
                    if separator and key in {"out_time_us", "out_time_ms"}:
                        with contextlib.suppress(ValueError):
                            out_time_us = min(duration_us, max(0, int(value)))
                        if phase == "render":
                            self._send_progress("render", ranges, current_chunk_us=out_time_us)
                        else:
                            self._send_phase_progress(
                                phase,
                                out_time_us,
                                duration_us,
                                ranges,
                                phase_started=phase_started,
                            )
                if process.returncode != 0:
                    raise ValueError(failure_code)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                raise
            finally:
                process.stdout.close()
                thread.join(timeout=1)
        if phase != "render":
            self._send_phase_progress(
                phase,
                duration_us,
                duration_us,
                ranges,
                phase_started=phase_started,
                force=True,
            )

    def _control_between_chunks(self) -> None:
        while True:
            try:
                message = self.commands.get_nowait()
            except queue.Empty:
                return
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if self._replay_acknowledgement(command_id):
                continue
            if action == "pause" and isinstance(command_id, str):
                self._checkpoint(chunk_ranges(self.snapshot))
                self.acknowledge(command_id, "paused", {"status": "paused"})
                self._wait_paused_without_process()
            elif action in {"cancel", "stop"}:
                self._stop_message(action, command_id)

    def _control_running_process(
        self, process: subprocess.Popen, ranges: tuple[tuple[int, int], ...]
    ) -> None:
        while True:
            try:
                message = self.commands.get_nowait()
            except queue.Empty:
                return
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if self._replay_acknowledgement(command_id):
                continue
            if action == "pause" and isinstance(command_id, str):
                _suspend_process(process.pid)
                try:
                    self._checkpoint(ranges)
                    self.acknowledge(command_id, "paused", {"status": "paused"})
                    self._wait_paused_process(process)
                except BaseException:
                    if process.poll() is None:
                        _resume_process(process.pid)
                    raise
            elif action in {"cancel", "stop"}:
                if process.poll() is None:
                    process.terminate()
                self._stop_message(action, command_id, ranges=ranges)

    def _wait_paused_process(self, process: subprocess.Popen) -> None:
        while True:
            message = self.commands.get()
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if self._replay_acknowledgement(command_id):
                continue
            if action == "resume" and isinstance(command_id, str):
                _resume_process(process.pid)
                self.acknowledge(command_id, "resumed", {"status": "running"})
                return
            if action in {"cancel", "stop"}:
                _resume_process(process.pid)
                process.terminate()
                self._stop_message(action, command_id)

    def _wait_paused_without_process(self) -> None:
        while True:
            message = self.commands.get()
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if self._replay_acknowledgement(command_id):
                continue
            if action == "resume" and isinstance(command_id, str):
                self.acknowledge(command_id, "resumed", {"status": "running"})
                return
            if action in {"cancel", "stop"}:
                self._stop_message(action, command_id)

    def _stop_message(
        self,
        action: object,
        command_id: object,
        *,
        ranges: tuple[tuple[int, int], ...] | None = None,
    ) -> None:
        self._checkpoint(ranges or chunk_ranges(self.snapshot))
        payload = {"status": str(action)}
        if isinstance(command_id, str):
            payload["command_id"] = command_id
        self.send("stopped", payload)
        raise WorkerStopped

    def _finalize(self, ranges: tuple[tuple[int, int], ...]) -> None:
        assert self.snapshot is not None
        self._control_between_chunks()
        entries = self.ledger.get("entries", {})
        if not isinstance(entries, dict) or len(entries) != len(ranges):
            raise ValueError("export_chunk_coverage_incomplete")
        ordered = [Path(str(entries[str(index)]["path"])) for index in range(len(ranges))]
        concat = self.job_staging / "concat.txt"
        concat.write_text(
            "".join(f"file '{_concat_path(path)}'\n" for path in ordered),
            encoding="utf-8",
            newline="\n",
        )
        output = Path(str(self.config["output_path"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        finalizing_marker = output.with_name(f".{output.name}.{self.job_id}.finalizing.json")
        if output.exists() and self.snapshot.settings.overwrite is not True:
            adopted = self._adopt_interrupted_finalization(output, finalizing_marker, ranges)
            if adopted:
                return
            raise ValueError("export_output_exists")
        partial = output.with_name(f".{output.stem}.{self.job_id}.partial.mp4")
        partial.unlink(missing_ok=True)
        arguments = [
            str(self.config["ffmpeg_path"]),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c",
            "copy",
            "-progress",
            "pipe:1",
            str(partial),
        ]
        self._send_phase_progress(
            "finalize_concat",
            0,
            self.snapshot.duration_us,
            ranges,
            phase_started=time.monotonic(),
            force=True,
        )
        self._run_ffmpeg(
            arguments,
            self.snapshot.duration_us,
            ranges,
            cwd=self.job_staging,
            phase="finalize_concat",
            failure_code="export_concat_failed",
        )
        self._control_between_chunks()
        self._send_phase_progress(
            "finalize_probe", 0, 1, ranges, phase_started=time.monotonic(), force=True
        )
        qa = self._probe_media(partial, self.snapshot.duration_us)
        self._control_between_chunks()
        if qa["playable"] is not True or qa["has_video"] is not True:
            raise ValueError("export_final_probe_failed")
        self._send_phase_progress(
            "finalize_probe", 1, 1, ranges, phase_started=time.monotonic(), force=True
        )
        output_sha256 = self._sha256_final_output(partial, ranges)
        self._control_between_chunks()
        publish_started = time.monotonic()
        self._send_phase_progress(
            "finalize_publish", 0, 1, ranges, phase_started=publish_started, force=True
        )
        atomic_write_json(
            finalizing_marker,
            {
                "schema_version": 1,
                "job_id": self.job_id,
                "ledger_signature": self.ledger.get("signature"),
                "output_sha256": output_sha256,
            },
        )
        self._control_between_chunks()
        os.replace(partial, output)
        qa.update(
            {
                "status": "PASS",
                "output_path": str(output),
                "output_sha256": output_sha256,
                "chunk_count": len(ranges),
            }
        )
        self._send_phase_progress(
            "finalize_publish", 1, 1, ranges, phase_started=publish_started, force=True
        )
        self._publish_final_success(output, qa, ranges)
        finalizing_marker.unlink(missing_ok=True)

    def _adopt_interrupted_finalization(
        self,
        output: Path,
        marker_path: Path,
        ranges: tuple[tuple[int, int], ...],
    ) -> bool:
        if not marker_path.is_file():
            return False
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            expected_hash = marker.get("output_sha256")
            if (
                marker.get("job_id") != self.job_id
                or marker.get("ledger_signature") != self.ledger.get("signature")
                or not isinstance(expected_hash, str)
                or self._sha256_final_output(output, ranges) != expected_hash
            ):
                return False
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        self._control_between_chunks()
        probe_started = time.monotonic()
        self._send_phase_progress(
            "finalize_probe", 0, 1, ranges, phase_started=probe_started, force=True
        )
        qa = self._probe_media(output, self.snapshot.duration_us)
        self._control_between_chunks()
        if qa.get("playable") is not True:
            return False
        self._send_phase_progress(
            "finalize_probe", 1, 1, ranges, phase_started=probe_started, force=True
        )
        qa.update(
            {
                "status": "PASS",
                "output_path": str(output),
                "output_sha256": expected_hash,
                "chunk_count": len(ranges),
                "recovered_finalization": True,
            }
        )
        publish_started = time.monotonic()
        self._send_phase_progress(
            "finalize_publish", 0, 1, ranges, phase_started=publish_started, force=True
        )
        self._send_phase_progress(
            "finalize_publish", 1, 1, ranges, phase_started=publish_started, force=True
        )
        self._publish_final_success(output, qa, ranges)
        marker_path.unlink(missing_ok=True)
        return True

    def _sha256_final_output(
        self,
        path: Path,
        ranges: tuple[tuple[int, int], ...],
    ) -> str:
        total_bytes = path.stat().st_size
        processed_bytes = 0
        phase_started = time.monotonic()
        digest = sha256()
        self._send_phase_progress(
            "finalize_checksum",
            0,
            total_bytes,
            ranges,
            phase_started=phase_started,
            force=True,
        )
        with path.open("rb") as stream:
            while True:
                self._control_between_chunks()
                block = stream.read(8 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                processed_bytes += len(block)
                self._send_phase_progress(
                    "finalize_checksum",
                    processed_bytes,
                    total_bytes,
                    ranges,
                    phase_started=phase_started,
                )
        self._control_between_chunks()
        self._send_phase_progress(
            "finalize_checksum",
            total_bytes,
            total_bytes,
            ranges,
            phase_started=phase_started,
            force=True,
        )
        return digest.hexdigest()

    def _publish_final_success(
        self,
        output: Path,
        qa: dict[str, Any],
        ranges: tuple[tuple[int, int], ...],
    ) -> None:
        qa_path = output.with_name(output.stem + "_QA.json")
        atomic_write_json(qa_path, qa)
        self.send(
            "succeeded",
            {
                "status": "PASS",
                "output_path": str(output),
                "qa_path": str(qa_path),
                "duration_us": self.snapshot.duration_us,
                "chunk_count": len(ranges),
                "output_sha256": qa["output_sha256"],
            },
        )

    def _probe_media(self, path: Path, expected_duration_us: int) -> dict[str, Any]:
        result = subprocess.run(
            [
                str(self.config["ffprobe_path"]),
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        try:
            raw = json.loads(result.stdout or "{}")
            duration_us = int(round(float(raw.get("format", {}).get("duration", 0)) * 1_000_000))
            kinds = {
                item.get("codec_type") for item in raw.get("streams", []) if isinstance(item, dict)
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            duration_us = 0
            kinds = set()
        tolerance = max(250_000, int(expected_duration_us * 0.015))
        playable = (
            result.returncode == 0
            and path.is_file()
            and path.stat().st_size > 0
            and "video" in kinds
            and abs(duration_us - expected_duration_us) <= tolerance
        )
        return {
            "playable": playable,
            "duration_us": duration_us,
            "expected_duration_us": expected_duration_us,
            "has_video": "video" in kinds,
            "has_audio": "audio" in kinds,
            "size_bytes": path.stat().st_size if path.is_file() else 0,
        }

    def _checkpoint(self, ranges: tuple[tuple[int, int], ...]) -> None:
        atomic_write_json(self.ledger_path, self.ledger)
        source = self.job_staging / f"ledger-{uuid4().hex}.json.partial"
        shutil.copyfile(self.ledger_path, source)
        digest = file_sha256(source)
        entries = self.ledger.get("entries", {})
        completed = len(entries) if isinstance(entries, dict) else 0
        cursor_us = ranges[completed - 1][1] if completed else self.snapshot.timeline_start_us
        self.send(
            "checkpoint",
            {
                "stage_key": "export",
                "checkpoint_key": "latest",
                "schema_version": 1,
                "cursor_us": cursor_us,
                "payload": {"stage": "export", "completed_chunks": completed},
                "artifact_path": str(source),
                "artifact_kind": "export_ledger",
                "artifact_state": "technical",
                "expected_sha256": digest,
            },
        )

    def _send_progress(
        self,
        phase: str,
        ranges: tuple[tuple[int, int], ...],
        *,
        current_chunk_us: int = 0,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress_sent < PROGRESS_INTERVAL_SECONDS:
            return
        self._last_progress_sent = now
        entries = self.ledger.get("entries", {})
        completed = len(entries) if isinstance(entries, dict) else 0
        completed_us = sum(end - start for start, end in ranges[:completed])
        current = min(self.snapshot.duration_us, completed_us + current_chunk_us)
        chunk_total = len(ranges)
        chunk_current = min(chunk_total, completed + (1 if completed < chunk_total else 0))
        elapsed_ms = max(1, int((now - self._job_started) * 1_000))
        measured_us = self._rendered_this_run_us + current_chunk_us
        rate = measured_us / max(1, elapsed_ms * 1_000)
        remaining_us = max(0, self.snapshot.duration_us - current)
        eta_ms = int(remaining_us / rate / 1_000) if rate > 0 else None
        self.send(
            "progress",
            {
                "phase": phase,
                "current": current,
                "total": self.snapshot.duration_us,
                "elapsed_ms": elapsed_ms,
                "eta_ms": eta_ms,
                "completed_chunks": completed,
                "total_chunks": chunk_total,
                "chunk_current": chunk_current,
                "chunk_total": chunk_total,
                "phase_current": current,
                "phase_total": self.snapshot.duration_us,
            },
        )

    def _send_phase_progress(
        self,
        phase: str,
        phase_current: int,
        phase_total: int,
        ranges: tuple[tuple[int, int], ...],
        *,
        phase_started: float,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress_sent < PROGRESS_INTERVAL_SECONDS:
            return
        self._last_progress_sent = now
        current = max(0, min(phase_total, phase_current))
        phase_elapsed_seconds = max(0.001, now - phase_started)
        phase_rate = current / phase_elapsed_seconds
        eta_ms = int(max(0, phase_total - current) / phase_rate * 1_000) if phase_rate > 0 else None
        elapsed_ms = max(1, int((now - self._job_started) * 1_000))
        chunk_total = len(ranges)
        self.send(
            "progress",
            {
                "phase": phase,
                "current": self.snapshot.duration_us,
                "total": self.snapshot.duration_us,
                "elapsed_ms": elapsed_ms,
                "eta_ms": eta_ms,
                "completed_chunks": chunk_total,
                "total_chunks": chunk_total,
                "chunk_current": chunk_total,
                "chunk_total": chunk_total,
                "phase_current": current,
                "phase_total": phase_total,
            },
        )


def _concat_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "'\\''")


def _suspend_process(pid: int) -> None:
    if sys.platform == "win32":
        _nt_process_call(pid, "NtSuspendProcess")
    else:
        os.kill(pid, signal.SIGSTOP)


def _resume_process(pid: int) -> None:
    if sys.platform == "win32":
        _nt_process_call(pid, "NtResumeProcess")
    else:
        os.kill(pid, signal.SIGCONT)


def _nt_process_call(pid: int, function_name: str) -> None:
    process_suspend_resume = 0x0800
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_suspend_resume, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Cannot open owned FFmpeg process")
    try:
        function = getattr(ntdll, function_name)
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_long
        status = function(handle)
        if status != 0:
            raise OSError(int(status), f"{function_name} failed")
    finally:
        kernel32.CloseHandle(handle)


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        return 2
    from ducai_studio.infrastructure.licensing_state import worker_activation_exit_code

    denied = worker_activation_exit_code()
    if denied:
        return denied
    return ExportWorker(sys.argv[1]).run()


if __name__ == "__main__":
    raise SystemExit(main())
