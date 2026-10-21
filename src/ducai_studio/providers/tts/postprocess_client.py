"""Bounded hidden CPU hosts; the calling TTS owner alone publishes results."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from ducai_studio.infrastructure.process_tree import ProcessTreeGuard
from ducai_studio.infrastructure.runtime_program import runtime_program_path
from ducai_studio.providers.tts.wav_preparation import PreparedVoice, resolved_wav_path


class WavHostError(RuntimeError):
    pass


class _WavHost:
    def __init__(self, staging_root: Path, program: Path) -> None:
        self.tree = ProcessTreeGuard()
        self.process: subprocess.Popen[str] | None = None
        self.responses: queue.Queue[dict | BaseException] = queue.Queue()
        self.readers: list[threading.Thread] = []
        self.closed = False
        environment = os.environ.copy()
        environment["DUCAI_WORKER_PROGRAM"] = str(program)
        environment["DUCAI_WORKER_ARGUMENTS_JSON"] = json.dumps([
            "-m", "ducai_studio.workers.tts_postprocess", str(staging_root),
        ])
        try:
            self.process = subprocess.Popen(
                [str(program), "-m", "ducai_studio.workers.preowned_launcher"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.tree.assign_pid(self.process.pid)
            assert self.process.stdin is not None
            if os.write(self.process.stdin.fileno(), b"DUCAI_LAUNCH\n") != 13:
                raise WavHostError("tts_wav_launch_failed")
            for target in (self._read_stdout, self._read_stderr):
                reader = threading.Thread(target=target, daemon=True)
                self.readers.append(reader)
                reader.start()
        except BaseException:
            self.close(force=True)
            raise

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                response = json.loads(line)
                if not isinstance(response, dict) or response.get("protocol_version") != 1:
                    raise WavHostError("tts_wav_protocol_invalid")
                self.responses.put(response)
        except (ValueError, OSError, WavHostError) as exc:
            self.responses.put(exc)
        finally:
            self.responses.put(WavHostError("tts_wav_host_exited"))

    def _read_stderr(self) -> None:
        process = self.process
        if process is not None and process.stderr is not None:
            for _ in process.stderr:
                pass

    def prepare(self, payload: dict) -> PreparedVoice:
        process = self.process
        if self.closed or process is None or process.stdin is None or process.poll() is not None:
            raise WavHostError("tts_wav_host_unavailable")
        request_id = uuid4().hex
        try:
            process.stdin.write(json.dumps({
                "protocol_version": 1, "id": request_id, "action": "prepare", "payload": payload,
            }, ensure_ascii=False) + "\n")
            process.stdin.flush()
            response = self.responses.get(timeout=660)
        except (OSError, queue.Empty) as exc:
            raise WavHostError("tts_wav_host_unresponsive") from exc
        if isinstance(response, BaseException):
            raise WavHostError("tts_wav_host_failed") from response
        if response.get("id") != request_id or response.get("ok") is not True:
            error_type = response.get("error")
            # Only exception class identifiers, never raw provider messages.
            suffix = "unknown"
            if isinstance(error_type, str) and error_type.isidentifier():
                suffix = error_type
            site = response.get("site", "")
            if isinstance(site, str) and len(site) < 100 and all(
                char.isalnum() or char in "_.:" for char in site
            ):
                suffix += f":{site}"
            raise WavHostError(f"tts_wav_task_failed:{suffix}")
        result = response.get("result", {})
        if (
            result.get("cue_id") != payload["cue"]["cue_id"]
            or result.get("request_fingerprint") != payload["cue"]["request_fingerprint"]
        ):
            raise WavHostError("tts_wav_identity_mismatch")
        prepared = PreparedVoice.from_json(result["prepared"])
        directory = resolved_wav_path(Path(payload["directory"]))
        cache_root = resolved_wav_path(Path(payload["cache_root"]))
        if (
            prepared.base_path is not None
            and resolved_wav_path(prepared.base_path) != directory / "base.wav"
        ):
            raise WavHostError("tts_wav_base_path_mismatch")
        if prepared.variant_path is not None and resolved_wav_path(prepared.variant_path) not in {
            directory / "variant.wav", cache_root / f"{prepared.variant_fingerprint}.wav",
        }:
            raise WavHostError("tts_wav_variant_path_mismatch")
        return prepared

    def close(self, *, force: bool = False) -> None:
        self.closed = True
        process = self.process
        self.responses.put(WavHostError("tts_wav_host_closed"))
        try:
            if process is not None and process.poll() is None and not force:
                with suppress(OSError, ValueError, subprocess.TimeoutExpired):
                    assert process.stdin is not None
                    process.stdin.write(json.dumps({
                        "protocol_version": 1, "action": "shutdown",
                    }) + "\n")
                    process.stdin.flush()
                    process.wait(timeout=0.5)
        finally:
            self.tree.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for reader in self.readers:
                    reader.join(timeout=1)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        with suppress(OSError):
                            stream.close()


class WavPreparationPool:
    def __init__(
        self, staging_root: Path, *, workers: int = 3, program: Path | None = None,
    ) -> None:
        if not 1 <= workers <= 3:
            raise ValueError("tts_wav_worker_count_invalid")
        self.hosts: list[_WavHost] = []
        self.available: queue.Queue[_WavHost] = queue.Queue()
        self.closed = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wav-host-io")
        try:
            for _ in range(workers):
                host = _WavHost(staging_root, program or runtime_program_path())
                self.hosts.append(host)
                self.available.put(host)
        except BaseException:
            self.close(force=True)
            raise

    @property
    def pids(self) -> tuple[int, ...]:
        return tuple(host.process.pid for host in self.hosts if host.process is not None)

    def submit(self, payload: dict) -> Future[PreparedVoice]:
        if self.closed.is_set():
            raise WavHostError("tts_wav_pool_closed")
        return self.executor.submit(self._prepare, payload)

    def _prepare(self, payload: dict) -> PreparedVoice:
        if self.closed.is_set():
            raise WavHostError("tts_wav_pool_closed")
        host = self.available.get()
        try:
            if self.closed.is_set():
                raise WavHostError("tts_wav_pool_closed")
            return host.prepare(payload)
        finally:
            self.available.put(host)

    def close(self, *, force: bool = False) -> None:
        self.closed.set()
        failure: BaseException | None = None
        for host in self.hosts:
            try:
                host.close(force=force)
            except BaseException as exc:
                if failure is None:
                    failure = exc
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        except BaseException as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            raise failure
