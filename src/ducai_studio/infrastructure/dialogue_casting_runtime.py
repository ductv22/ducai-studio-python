from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.domain.project import MediaKind, ProjectSnapshot
from ducai_studio.infrastructure.runtime_program import runtime_program_path

BLOCK_US = 60_000_000
CONTEXT_US = 5_000_000
MAX_CUE_US = 20_000_000


def engine_bundle_fingerprint(host: Path) -> str:
    """Bind checkpoints/results to the actual host, helpers, locks and policy."""
    helpers = host.parent / "dialogue_casting"
    if not helpers.is_dir():
        helpers = host.parents[3] / "scripts/dialogue_casting"
    identities = {"host": sha256(host.read_bytes()).hexdigest()}
    for name in ("__init__", "analysis", "assets", "inference", "ecapa", "srt_demo",
                 "corpus", "demo", "evaluation", "speaker_consistency", "asr_crosscheck", "moss"):
        path = helpers / f"{name}.py"
        if not path.is_file():
            path = path.with_suffix(".pyc")
        identities[name] = sha256(path.read_bytes()).hexdigest()
    for name in ("model-lock.json", "ecapa-lock.json", "moss-lock.json"):
        identities[name] = sha256((helpers / name).read_bytes()).hexdigest()
    return stable_fingerprint({"files": identities, "checkpoint_schema": 2,
                               "block_us": BLOCK_US, "context_us": CONTEXT_US,
                               "max_cue_us": MAX_CUE_US})


def _bundled_host() -> Path:
    source = Path(__file__).parents[1] / "engine_hosts/dialogue_casting_engine_host.py"
    if source.is_file():
        return source
    return runtime_program_path().parent / "engine_hosts/dialogue_casting_engine_host.pyc"


CASTING_ANALYZER_ID = "moss-srt-v3-" + engine_bundle_fingerprint(_bundled_host())[:24]


def media_identities(snapshot: ProjectSnapshot) -> dict[str, str]:
    identities = {}
    for media in snapshot.media:
        if media.kind is not MediaKind.VIDEO:
            continue
        path = Path(media.path).expanduser().resolve(strict=True)
        stamp = path.stat()
        identities[media.id] = stable_fingerprint({
            "path": str(path), "size": stamp.st_size, "mtime_ns": stamp.st_mtime_ns,
            "import_sha256": media.sha256,
        })
    return identities


def runtime_config(data_root: Path) -> dict[str, str]:
    runtime = data_root / "dialogue_casting_runtime"
    paths = {
        "diarization_python": runtime / ".venv/Scripts/python.exe",
        "classifier_python": data_root / "vieneu_3_8_runtime/.venv/Scripts/python.exe",
        "diarization_models": runtime / "models",
        "classifier_models": runtime / "ecapa-experiment",
    }
    moss_root = runtime / "moss"
    paths.update({"moss_python": moss_root / ".venv/Scripts/python.exe",
                  "moss_models": moss_root / "model", "moss_source": moss_root / "source"})
    for _name, path in paths.items():
        if not path.exists():
            raise FileNotFoundError(
                f"Thiếu bộ nhận diện phân vai: {path}. "
                "Chạy scripts/setup-dialogue-casting-runtime.ps1 để cài rõ ràng trước khi dùng."
            )
    lock_root = _bundled_host().parent / "dialogue_casting"
    if not lock_root.is_dir():
        lock_root = Path(__file__).parents[3] / "scripts/dialogue_casting"
    lock = json.loads((lock_root / "moss-lock.json").read_text(encoding="utf-8"))
    return {**{name: str(path.resolve()) for name, path in paths.items()},
            "moss_model_revision": lock["model_revision"],
            "moss_source_revision": lock["source_revision"]}
