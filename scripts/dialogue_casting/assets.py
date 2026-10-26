from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "model-lock.json"


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def checked_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes output directory: {relative}")
    return path


def verify_file(path: Path, entry: dict) -> None:
    if not path.is_file() or path.stat().st_size != entry["size"]:
        raise RuntimeError(f"Missing/incomplete casting model: {path}. Run the setup script.")
    if file_sha256(path) != entry["sha256"]:
        raise RuntimeError(f"Casting SHA-256 mismatch: {path}. Reinstall the recognition pack.")


def model_files(root: Path) -> dict[str, Path]:
    lock = read_json(LOCK_PATH)
    result = {}
    for entry in lock["artifacts"]:
        if "members" in entry:
            for member in entry["members"]:
                path = checked_path(root, member["path"])
                verify_file(path, member)
                if member["path"].endswith(".onnx"):
                    result[entry["id"]] = path
        else:
            path = checked_path(root, entry["path"])
            verify_file(path, entry)
            result[entry["id"]] = path
    return result


def runtime_versions() -> dict[str, str]:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Casting requires its isolated Python 3.12 runtime.")
    # requirements.txt pins every dependency; verify the installed environment as well.
    versions = {}
    for line in (HERE / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        package, version = line.split(" ", 1)[0].split("==", 1)
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"Missing casting dependency {package}; run setup.") from exc
        if actual != version:
            raise RuntimeError(f"Casting requires {package}=={version}; found {actual}.")
        versions[package] = actual
    return versions


def install_models(root: Path, cache: Path, *, offline: bool = False) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    lock = read_json(LOCK_PATH)
    for entry in lock["artifacts"]:
        destination = checked_path(cache, entry["filename"])
        if not destination.exists():
            if offline:
                raise RuntimeError(f"Offline installer cache is incomplete: {destination}")
            partial = destination.with_suffix(destination.suffix + ".partial")
            try:
                print(f"Downloading {entry['filename']} ({entry['size']} bytes)", flush=True)
                with (
                    urllib.request.urlopen(entry["url"], timeout=60) as source,
                    partial.open("wb") as output,
                ):
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                verify_file(partial, entry)
                os.replace(partial, destination)
            finally:
                partial.unlink(missing_ok=True)
        verify_file(destination, entry)
        if "members" in entry:
            with tarfile.open(destination, "r:bz2") as archive:
                for member in entry["members"]:
                    target = checked_path(root, member["path"])
                    info = archive.getmember(member["member"])
                    if not info.isfile() or info.size != member["size"]:
                        raise RuntimeError(f"Invalid casting archive member: {info.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    verify_file(target, member)
        else:
            target = checked_path(root, entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(destination, target)
    model_files(root)
    receipt = {
        "schema": "ducai-dialogue-casting-runtime-v1",
        "model_lock_sha256": file_sha256(LOCK_PATH),
        "requirements_sha256": file_sha256(HERE / "requirements.txt"),
        "packages": runtime_versions(),
        "python": sys.version,
        "product_gate": "NOT_EVALUATED",
    }
    write_json(root.parent / "ducai_runtime.json", receipt)
    return receipt
