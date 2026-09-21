from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

_PROGRESS_EMIT_INTERVAL_SECONDS = 1.0


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


def _atempo_filter_chain(speed: float) -> str:
    """Build a pitch-preserving FFmpeg chain for total speeds up to 3x."""

    remaining = float(speed)
    factors: list[float] = []
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(f"atempo={factor:.8f}" for factor in factors)


class FfmpegAudioProcessor:
    def __init__(self, executable: str = "ffmpeg") -> None:
        resolved = shutil.which(executable) if not Path(executable).is_file() else executable
        if not resolved:
            raise FileNotFoundError("FFmpeg executable is unavailable")
        self.executable = str(Path(resolved).resolve())

    def time_stretch(
        self,
        source: Path,
        target: Path,
        *,
        speed: float,
        sample_rate: int = 48_000,
    ) -> None:
        if not 0.5 <= speed <= 3.0:
            raise ValueError("FFmpeg atempo speed must be between 0.5 and 3.0")
        source = source.expanduser().resolve(strict=True)
        target = target.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp.wav")
        command = [
            self.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            _atempo_filter_chain(speed),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(temp),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        if result.returncode != 0 or not temp.is_file():
            temp.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg_time_stretch_failed")
        os.replace(temp, target)
    def master_ebu_r128(
        self,
        source: Path,
        target: Path,
        *,
        target_lufs: float = -18.0,
        true_peak_db: float = -1.5,
        loudness_range: float = 7.0,
        progress: Callable[[str, int, int, int | None], None] | None = None,
    ) -> dict[str, float | str]:
        source = source.expanduser().resolve(strict=True)
        target = target.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        measured = self.measure_ebu_r128(
            source,
            target_lufs=target_lufs,
            true_peak_db=true_peak_db,
            loudness_range=loudness_range,
            progress=progress,
        )
        return self.apply_ebu_r128(
            source,
            target,
            measured=measured,
            target_lufs=target_lufs,
            true_peak_db=true_peak_db,
            loudness_range=loudness_range,
            progress=progress,
        )

    def measure_ebu_r128(
        self,
        source: Path,
        *,
        target_lufs: float = -18.0,
        true_peak_db: float = -1.5,
        loudness_range: float = 7.0,
        progress: Callable[[str, int, int, int | None], None] | None = None,
    ) -> dict[str, float | str]:
        source = source.expanduser().resolve(strict=True)
        duration_us = _wav_duration_us(source)
        first_filter = (
            f"loudnorm=I={target_lufs}:TP={true_peak_db}:LRA={loudness_range}:print_format=json"
        )
        if progress is not None:
            progress("measure", 0, duration_us, None)
        first_returncode, first_stderr = _run_ffmpeg_with_progress(
            [
                self.executable,
                "-hide_banner",
                "-nostats",
                "-progress",
                "pipe:1",
                "-i",
                str(source),
                "-af",
                first_filter,
                "-f",
                "null",
                "-",
            ],
            stage="measure",
            duration_us=duration_us,
            progress=progress,
            timeout_seconds=1_800,
        )
        if first_returncode != 0:
            raise RuntimeError("ffmpeg_loudnorm_measure_failed")
        return _extract_loudnorm(first_stderr)

    def open_pcm16_ebu_r128_measure(
        self,
        *,
        sample_rate: int,
        duration_us: int,
        target_lufs: float = -18.0,
        true_peak_db: float = -1.5,
        loudness_range: float = 7.0,
        progress: Callable[[str, int, int, int | None], None] | None = None,
    ) -> Pcm16LoudnessMeasureSession:
        return Pcm16LoudnessMeasureSession(
            self.executable,
            sample_rate=sample_rate,
            duration_us=duration_us,
            target_lufs=target_lufs,
            true_peak_db=true_peak_db,
            loudness_range=loudness_range,
            progress=progress,
        )

    def apply_ebu_r128(
        self,
        source: Path,
        target: Path,
        *,
        measured: dict[str, float | str],
        target_lufs: float = -18.0,
        true_peak_db: float = -1.5,
        loudness_range: float = 7.0,
        progress: Callable[[str, int, int, int | None], None] | None = None,
    ) -> dict[str, float | str]:
        source = source.expanduser().resolve(strict=True)
        target = target.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        duration_us = _wav_duration_us(source)
        second_filter = (
            f"loudnorm=I={target_lufs}:TP={true_peak_db}:LRA={loudness_range}:"
            f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:"
            f"measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:linear=true:print_format=json"
        )
        temp = target.with_name(target.name + ".tmp.wav")
        if progress is not None:
            progress("apply", 0, duration_us, None)
        second_returncode, _second_stderr = _run_ffmpeg_with_progress(
            [
                self.executable,
                "-hide_banner",
                "-nostats",
                "-progress",
                "pipe:1",
                "-y",
                "-i",
                str(source),
                "-af",
                second_filter,
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(temp),
            ],
            stage="apply",
            duration_us=duration_us,
            progress=progress,
            timeout_seconds=1_800,
        )
        if second_returncode != 0 or not temp.is_file():
            temp.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg_loudnorm_apply_failed")
        os.replace(temp, target)
        return measured


class Pcm16LoudnessMeasureSession:
    def __init__(
        self,
        executable: str,
        *,
        sample_rate: int,
        duration_us: int,
        target_lufs: float,
        true_peak_db: float,
        loudness_range: float,
        progress: Callable[[str, int, int, int | None], None] | None,
        timeout_seconds: float = 1_800,
    ) -> None:
        if sample_rate <= 0 or duration_us <= 0:
            raise ValueError("pcm16_loudness_measure_input_invalid")
        loudness_filter = (
            f"loudnorm=I={target_lufs}:TP={true_peak_db}:LRA={loudness_range}:"
            "print_format=json"
        )
        self._command = [
            executable,
            "-hide_banner",
            "-nostats",
            "-progress",
            "pipe:1",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-i",
            "pipe:0",
            "-af",
            loudness_filter,
            "-f",
            "null",
            "-",
        ]
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            **_hidden_subprocess_kwargs(),
        )
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._duration_us = duration_us
        self._progress = progress
        self._started = time.monotonic()
        self._deadline = self._started + timeout_seconds
        self._last_current_us = -1
        self._last_emitted_at = self._started
        self._progress_lines: queue.Queue[bytes | None] = queue.Queue()
        self._stderr_chunks: list[bytes] = []
        self._stdout_finished = False
        self._result: dict[str, float | str] | None = None
        self._stdout_reader = threading.Thread(target=self._read_progress, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_reader.start()
        self._stderr_reader.start()
        if progress is not None:
            progress("measure", 0, duration_us, None)

    def write(self, raw: bytes) -> None:
        if self._result is not None or self._process.stdin.closed:
            raise RuntimeError("ffmpeg_loudnorm_measure_session_closed")
        if len(raw) % 2:
            raise ValueError("pcm16_loudness_measure_frame_incomplete")
        self._drain_progress()
        try:
            self._process.stdin.write(raw)
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("ffmpeg_loudnorm_measure_failed") from exc
        self._drain_progress()

    def finish(self) -> dict[str, float | str]:
        if self._result is not None:
            return dict(self._result)
        if not self._process.stdin.closed:
            self._process.stdin.close()
        while not self._stdout_finished or self._process.poll() is None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                self.abort()
                raise subprocess.TimeoutExpired(self._command, 1_800)
            self._drain_progress(timeout=min(0.2, remaining))
        self._stdout_reader.join(timeout=2)
        self._stderr_reader.join(timeout=2)
        returncode = self._process.wait()
        if returncode != 0:
            raise RuntimeError("ffmpeg_loudnorm_measure_failed")
        if self._progress is not None and self._last_current_us < self._duration_us:
            self._progress("measure", self._duration_us, self._duration_us, 0)
        stderr = b"".join(self._stderr_chunks).decode("utf-8", "replace")
        self._result = _extract_loudnorm(stderr)
        return dict(self._result)

    def abort(self) -> None:
        if self._process.poll() is None:
            if not self._process.stdin.closed:
                self._process.stdin.close()
            self._process.kill()
            self._process.wait()
        self._stdout_reader.join(timeout=2)
        self._stderr_reader.join(timeout=2)

    def _read_progress(self) -> None:
        try:
            for line in self._process.stdout:
                self._progress_lines.put(line)
        finally:
            self._progress_lines.put(None)

    def _read_stderr(self) -> None:
        self._stderr_chunks.extend(iter(lambda: self._process.stderr.read(65_536), b""))

    def _drain_progress(self, *, timeout: float = 0.0) -> None:
        first = True
        while True:
            try:
                line = self._progress_lines.get(timeout=timeout if first else 0)
            except queue.Empty:
                return
            first = False
            if line is None:
                self._stdout_finished = True
                return
            self._consume_progress_line(line.decode("utf-8", "replace"))

    def _consume_progress_line(self, line: str) -> None:
        key, separator, raw_value = line.strip().partition("=")
        if not separator or key not in {"out_time_us", "out_time_ms"}:
            return
        try:
            current_us = min(self._duration_us, max(0, int(raw_value)))
        except ValueError:
            return
        now = time.monotonic()
        if current_us <= self._last_current_us or (
            current_us < self._duration_us
            and now - self._last_emitted_at < _PROGRESS_EMIT_INTERVAL_SECONDS
        ):
            return
        elapsed = max(0.001, now - self._started)
        rate = current_us / elapsed
        eta_ms = (
            int(max(0, self._duration_us - current_us) / rate * 1_000)
            if rate > 0
            else None
        )
        if self._progress is not None:
            self._progress("measure", current_us, self._duration_us, eta_ms)
        self._last_current_us = current_us
        self._last_emitted_at = now


def _wav_duration_us(source: Path) -> int:
    with wave.open(str(source), "rb") as stream:
        sample_rate = stream.getframerate()
        if sample_rate <= 0:
            raise ValueError("wav_sample_rate_invalid")
        return int(round(stream.getnframes() * 1_000_000 / sample_rate))


def _run_ffmpeg_with_progress(
    command: list[str],
    *,
    stage: str,
    duration_us: int,
    progress: Callable[[str, int, int, int | None], None] | None,
    timeout_seconds: float,
) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_subprocess_kwargs(),
    )
    assert process.stdout is not None
    assert process.stderr is not None
    progress_lines: queue.Queue[str | None] = queue.Queue()
    stderr_lines: list[str] = []

    def read_progress() -> None:
        try:
            for line in process.stdout:
                progress_lines.put(line)
        finally:
            progress_lines.put(None)

    def read_stderr() -> None:
        stderr_lines.extend(process.stderr.readlines())

    stdout_reader = threading.Thread(target=read_progress, daemon=True)
    stderr_reader = threading.Thread(target=read_stderr, daemon=True)
    stdout_reader.start()
    stderr_reader.start()
    started = time.monotonic()
    deadline = started + timeout_seconds
    stdout_finished = False
    last_current_us = -1
    last_emitted_at = started
    try:
        while not stdout_finished or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                line = progress_lines.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                stdout_finished = True
                continue
            key, separator, raw_value = line.strip().partition("=")
            if not separator or key not in {"out_time_us", "out_time_ms"}:
                continue
            try:
                current_us = min(duration_us, max(0, int(raw_value)))
            except ValueError:
                continue
            now = time.monotonic()
            if current_us <= last_current_us or (
                current_us < duration_us
                and now - last_emitted_at < _PROGRESS_EMIT_INTERVAL_SECONDS
            ):
                continue
            elapsed = max(0.001, now - started)
            rate = current_us / elapsed
            eta_ms = (
                int(max(0, duration_us - current_us) / rate * 1_000)
                if rate > 0
                else None
            )
            if progress is not None:
                progress(stage, current_us, duration_us, eta_ms)
            last_current_us = current_us
            last_emitted_at = now
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        stdout_reader.join(timeout=2)
        stderr_reader.join(timeout=2)
    returncode = process.wait()
    if returncode == 0 and progress is not None and last_current_us < duration_us:
        progress(stage, duration_us, duration_us, 0)
    return returncode, "".join(stderr_lines)


def _extract_loudnorm(stderr: str) -> dict[str, float | str]:
    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
    if not matches:
        raise RuntimeError("ffmpeg_loudnorm_report_missing")
    raw = json.loads(matches[-1])
    required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if any(key not in raw for key in required):
        raise RuntimeError("ffmpeg_loudnorm_report_incomplete")
    return {key: float(raw[key]) for key in required}
