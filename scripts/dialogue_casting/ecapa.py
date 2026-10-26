"""Pinned experimental CPU classifier; no network or app-runtime installation at inference."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import shutil
import urllib.request
from pathlib import Path

from .assets import checked_path, file_sha256, read_json, verify_file

LOCK = Path(__file__).with_name("ecapa-lock.json")


def verify_model(root: Path) -> dict:
    lock = read_json(LOCK)
    for entry in lock["files"]:
        verify_file(checked_path(root, entry["path"]), entry)
    if read_json(root / "config.json") != {"C": 1024}:
        raise ValueError("Unexpected ECAPA model architecture")
    return lock


def download_model(root: Path, cache: Path, *, offline: bool = False) -> dict:
    """Only the explicit download command may access the network."""
    lock = read_json(LOCK)
    root.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    for entry in lock["files"]:
        path = checked_path(root, entry["path"])
        if path.is_file():
            verify_file(path, entry)
            continue
        cached = checked_path(cache, entry["path"])
        if not cached.exists():
            if offline:
                raise RuntimeError(f"Offline ECAPA cache is incomplete: {cached}")
            cached.parent.mkdir(parents=True, exist_ok=True)
            cache_partial = cached.with_suffix(cached.suffix + ".partial")
            try:
                with (
                    urllib.request.urlopen(entry["url"], timeout=60) as source,
                    cache_partial.open("wb") as target,
                ):
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                verify_file(cache_partial, entry)
                os.replace(cache_partial, cached)
            finally:
                cache_partial.unlink(missing_ok=True)
        verify_file(cached, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".partial")
        try:
            shutil.copyfile(cached, partial)
            verify_file(partial, entry)
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)
        verify_file(path, entry)
    verify_model(root)
    return {
        "model_id": lock["model_id"],
        "revision": lock["revision"],
        "lock_sha256": file_sha256(LOCK),
    }


def verify_runtime() -> dict[str, str]:
    lock = read_json(LOCK)
    versions = {}
    for name, expected in lock["runtime"].items():
        actual = importlib.metadata.version(name)
        if actual != expected:
            raise RuntimeError(f"ECAPA requires {name}=={expected}; found {actual}")
        versions[name] = actual
    return versions


class EcapaClassifier:
    def __init__(self, root: Path, *, threads: int = 2):
        verify_model(root)
        if type(threads) is not int or not 1 <= threads <= 8:
            raise ValueError("ECAPA thread budget must be between one and eight")
        self.versions = verify_runtime()
        import numpy as np
        import torch
        from safetensors.torch import load_file

        torch.set_num_threads(threads)
        self.np, self.torch = np, torch
        # Exact source is reviewed and checksum-pinned before importing. Do not use remote code
        # loaders or from_pretrained, which could download code/weights during inference.
        spec = importlib.util.spec_from_file_location("ducai_pinned_ecapa", root / "model.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.model = module.ECAPA_gender(C=1024).to("cpu")
        self.model.load_state_dict(load_file(root / "model.safetensors", device="cpu"), strict=True)
        self.model.eval()

    def female_probability(self, samples) -> float:
        np, torch = self.np, self.torch
        if samples.ndim != 1 or not 16_000 <= samples.size <= 80_000:
            raise ValueError("ECAPA reference must be mono, one to five seconds at 16 kHz")
        if not np.isfinite(samples).all():
            raise ValueError("Non-finite original audio")
        # Upstream frontend performs pre-emphasis, log-mel extraction and mean subtraction.
        # Use original PCM amplitude; wav2vec's variance normalization does not belong here.
        with torch.inference_mode():
            logits = self.model(torch.from_numpy(np.array(samples, dtype=np.float32))[None, :])
            if tuple(logits.shape) != (1, 2) or not torch.isfinite(logits).all():
                raise RuntimeError("Invalid ECAPA inference result")
            probability = float(torch.softmax(logits, dim=1)[0, 1])
        return probability
