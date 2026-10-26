"""Pinned offline MOSS inference; anonymous speakers never supply gender labels."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from urllib.request import urlopen

from .assets import checked_path, read_json, verify_file

LOCK_PATH = Path(__file__).with_name("moss-lock.json")
MAX_WINDOW_US = 90_000_000
MAX_NEW_TOKENS = 4096


def verify_moss_assets(root: Path, source: Path) -> dict:
    lock = read_json(LOCK_PATH)
    for name, directory in (("model_files", root), ("source_files", source)):
        for entry in lock[name]:
            verify_file(checked_path(directory, entry["path"]), entry)
    return lock


def install_moss_assets(
    root: Path,
    source: Path,
    cache: Path,
    *,
    offline: bool = False,
    legacy_root: Path | None = None,
) -> dict:
    """Only the explicit installer is permitted to download model/source files."""
    lock = read_json(LOCK_PATH)
    legacy_directories: dict[str, Path] = {}
    if legacy_root is not None and legacy_root.exists():
        legacy_directories = {
            "model_files": legacy_root / "model",
            "source_files": legacy_root / "source",
        }
        # Validate the complete legacy snapshot before copying any part of it.
        # The migration is copy-only so the audited experiment remains intact.
        verify_moss_assets(
            legacy_directories["model_files"],
            legacy_directories["source_files"],
        )
    for group, target_root in (("model_files", root), ("source_files", source)):
        for entry in lock[group]:
            target = checked_path(target_root, entry["path"])
            if target.is_file():
                verify_file(target, entry)
                continue
            legacy = (
                checked_path(legacy_directories[group], entry["path"])
                if group in legacy_directories
                else None
            )
            if legacy is not None:
                verify_file(legacy, entry)
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_suffix(target.suffix + ".partial")
                try:
                    print(f"Migrating verified MOSS {entry['path']}", flush=True)
                    shutil.copyfile(legacy, partial)
                    verify_file(partial, entry)
                    os.replace(partial, target)
                finally:
                    partial.unlink(missing_ok=True)
                continue
            cached = checked_path(cache / group, entry["path"])
            cached.parent.mkdir(parents=True, exist_ok=True)
            if not cached.is_file():
                partial = cached.with_suffix(cached.suffix + ".partial")
                try:
                    if offline:
                        raise FileNotFoundError(f"Thiếu cache cài MOSS offline: {cached}")
                    print(f"Downloading MOSS {entry['path']}", flush=True)
                    with (urlopen(entry["url"], timeout=120) as response,
                          partial.open("wb") as stream):
                        shutil.copyfileobj(response, stream, length=1024 * 1024)
                    verify_file(partial, entry)
                    os.replace(partial, cached)
                finally:
                    partial.unlink(missing_ok=True)
            verify_file(cached, entry)
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".partial")
            try:
                shutil.copyfile(cached, partial)
                verify_file(partial, entry)
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
    return verify_moss_assets(root, source)


def verify_moss_runtime() -> dict[str, str]:
    versions = {}
    for name, expected in read_json(LOCK_PATH)["runtime"].items():
        actual = importlib.metadata.version(name)
        if actual != expected:
            raise RuntimeError(f"MOSS cần {name}=={expected}; đang có {actual}. Cài lại bộ MOSS.")
        versions[name] = actual
    return versions


def normalize_turns(segments: list, start_us: int, end_us: int) -> list[dict]:
    if (type(start_us) is not int or type(end_us) is not int
            or not 0 <= start_us < end_us or end_us - start_us > MAX_WINDOW_US):
        raise ValueError("Khoảng âm thanh MOSS không hợp lệ hoặc quá 90 giây")
    turns = []
    for segment in segments:
        a, b = segment.start, segment.end
        if (isinstance(a, bool) or isinstance(b, bool)
                or not isinstance(a, (int, float)) or not isinstance(b, (int, float))
                or not math.isfinite(a) or not math.isfinite(b) or not 0 <= a < b):
            raise ValueError("MOSS trả về mốc thời gian không hợp lệ")
        first, last = start_us + round(a * 1_000_000), start_us + round(b * 1_000_000)
        # A rounded final timestamp may exceed the decoded duration by <=20ms.
        if first >= end_us or last > end_us + 20_000:
            raise ValueError("MOSS trả về lời thoại nằm ngoài khối âm thanh")
        if not isinstance(segment.speaker, str) or not segment.speaker:
            raise ValueError("MOSS trả về mã người nói không hợp lệ")
        turns.append({"start_us": first, "end_us": min(last, end_us),
                      "speaker": segment.speaker, "text": segment.text})
    return turns


def _asset_paths(lock: dict, root: Path, source: Path) -> list[Path]:
    paths = []
    for name, directory in (("model_files", root), ("source_files", source)):
        paths.extend(checked_path(directory, entry["path"]) for entry in lock.get(name, []))
    return paths


def _stat_signature(paths: list[Path]) -> tuple[tuple[str, int, int], ...]:
    try:
        return tuple(
            (str(path), path.stat().st_size, path.stat().st_mtime_ns)
            for path in paths
        )
    except OSError as exc:
        raise RuntimeError("Tệp MOSS đã đổi hoặc bị xóa; hãy tạo tác vụ mới") from exc


class MossTranscriber:
    """Keep the pinned MOSS model resident while processing bounded windows."""

    seed = 20261024

    def __init__(self, request: dict) -> None:
        self.root = Path(request["moss_models"]).resolve()
        self.source = Path(request["moss_source"]).resolve()
        self.lock = verify_moss_assets(self.root, self.source)
        verify_moss_runtime()
        os.environ.update({
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
        })
        import numpy as np
        import torch

        self.np, self.torch = np, torch
        source_path = str(self.source)
        if source_path not in sys.path:
            sys.path.insert(0, source_path)
        # Loading this reviewed file directly avoids importing upstream's web app.
        runner_path = self.source / "moss_transcribe_diarize/app/model_runner.py"
        spec = importlib.util.spec_from_file_location("ducai_pinned_moss_runner", runner_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Không thể nạp bộ chạy MOSS đã khóa phiên bản")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from moss_transcribe_diarize.transcript_parser import parse_transcript

        self.parse_transcript = parse_transcript
        self._reset_determinism()
        torch.set_num_threads(2)
        torch.use_deterministic_algorithms(True, warn_only=True)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.dtype = (
            "bf16" if torch.cuda.is_bf16_supported() else "fp16"
        ) if self.device != "cpu" else "fp32"
        # The pinned implementation tries SDPA first when flash_attn is absent.
        # Select it before model construction so optional packages cannot change the run.
        module._attention_candidates = lambda _device: ("sdpa",)
        self.runner = module.ModelRunner(self.root, device=self.device, dtype=self.dtype)
        self.asset_paths = _asset_paths(self.lock, self.root, self.source)
        self.asset_signature = _stat_signature(self.asset_paths)

    def _reset_determinism(self) -> None:
        random.seed(self.seed)
        self.np.random.seed(self.seed)
        self.torch.manual_seed(self.seed)

    def _verify_identity(self, request: dict) -> None:
        if (Path(request["moss_models"]).resolve() != self.root
                or Path(request["moss_source"]).resolve() != self.source):
            raise ValueError("Không được đổi bộ MOSS trong cùng một tiến trình phân vai")
        if _stat_signature(self.asset_paths) != self.asset_signature:
            raise RuntimeError("Tệp MOSS đã đổi trong lúc chạy; hãy tạo tác vụ mới")

    def transcribe(self, request: dict) -> dict:
        self._verify_identity(request)
        start, end = request["start_us"], request["end_us"]
        normalize_turns([], start, end)
        self._reset_determinism()
        with tempfile.TemporaryDirectory(
            prefix="ducai-moss-", dir=request.get("scratch_root")
        ) as temp:
            audio = Path(temp) / "source.wav"
            subprocess.run(
                [request["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-ss", f"{start / 1_000_000:.6f}", "-i", request["media_path"],
                 "-t", f"{(end - start) / 1_000_000:.6f}", "-vn", "-ac", "1",
                 "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(audio)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with wave.open(str(audio), "rb") as stream:
                if stream.getnframes() <= 0:
                    raise ValueError("MOSS không nhận được âm thanh gốc hợp lệ")
            result = self.runner.transcribe(
                audio, max_new_tokens=MAX_NEW_TOKENS, decoding="greedy"
            )
        if result.generated_tokens >= MAX_NEW_TOKENS:
            raise RuntimeError("MOSS chạm giới hạn đầu ra; không dùng bản phân vai bị cắt dở")
        segments = self.parse_transcript(result.text)
        if result.text.strip() and not segments:
            raise RuntimeError("Không đọc được mốc người nói từ kết quả MOSS")
        starts = re.findall(r"\[\d+(?:\.\d+)?\]\s*\[S\d+\]", result.text)
        if len(starts) != len(segments) or (
            segments and not re.search(r"\[\d+(?:\.\d+)?\]\s*$", result.text)
        ):
            raise RuntimeError("MOSS trả về đoạn lời thoại chưa hoàn chỉnh; hãy chạy lại")
        turns = normalize_turns(segments, start, end)
        return {
            "id": request["window_id"],
            "start_us": start,
            "end_us": end,
            "turns": turns,
            "transcript": result.text,
            "provenance": {
                "model_revision": self.lock["model_revision"],
                "source_revision": self.lock["source_revision"],
                "device": self.device,
                "dtype": self.dtype,
                "attention": "sdpa",
                "seed": self.seed,
                "generated_tokens": result.generated_tokens,
            },
        }


def transcribe(request: dict) -> dict:
    """Run one bounded window while preserving the one-shot adapter contract."""
    return MossTranscriber(request).transcribe(request)
