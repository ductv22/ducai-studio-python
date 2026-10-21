"""Assemble/verify the isolated customer test without editing its source runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path, PurePosixPath

MODEL_NAMES = ("PP-OCRv6_tiny_det", "PP-OCRv6_small_rec",
               "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")
SKIP_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".git",
                              "tests", "test", "selftest", "docs", "examples", "benchmarks"})
PRIVATE_NAMES = frozenset({"ducai-key-issuer.exe", "license-issuer.exe", "license-issuer.py",
                           "key-info.json", "issued-licenses.json", "license-v1.txt",
                           "sitecustomize.py", ".env"})
PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?:ENCRYPTED |ED25519 |RSA |EC )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{20}")
LICENSE_TOKEN = re.compile(
    rb"(?:DUCAI-L1\.[A-Za-z0-9_-]{200,}|DUCAI-L2\.[A-Za-z0-9_-]{119}|"
    rb"DUCAI-L3\.[A-Za-z0-9_-]{130})")
MANIFEST_NAME = "rtx50-test-manifest.json"
WHEEL_URL = ("https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu/"
             "paddlepaddle_gpu-3.2.2-cp311-cp311-win_amd64.whl")


def checked_path(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if (not parts or "\\" in name or ":" in name or ".." in parts
            or PurePosixPath(name).is_absolute()):
        raise ValueError("Invalid relative package path")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Package path escapes its root")
    return path


def native_python_file(name: str) -> bool:
    name = name.casefold()
    return name in {"python.exe", "pythonw.exe"} or (
        name.endswith(".dll") and name.startswith(("python", "vcruntime")))


def runtime_file_allowed(relative: Path) -> bool:
    parts = tuple(part.casefold() for part in relative.parts)
    name = parts[-1]
    if (any(part in SKIP_DIRECTORIES for part in parts[:-1])
            or relative.suffix.casefold() in {".pyc", ".pyo", ".pdb", ".whl"}
            or name == "direct_url.json"):
        return False
    if parts[:1] == (".venv",):
        parts = parts[1:]
    if parts[:1] == ("scripts",):
        return len(parts) == 2 and native_python_file(name)
    return True


def copy_filtered_tree(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    for directory, directories, files in os.walk(source):
        directories[:] = [name for name in directories if name.casefold() not in SKIP_DIRECTORIES]
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(source)
            if not runtime_file_allowed(relative):
                continue
            if not path.resolve().is_relative_to(source):
                raise ValueError("Runtime input escapes its root")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def file_digest(path: Path, *, audit: bool = False) -> str:
    if audit and (path.name.casefold() in PRIVATE_NAMES
                  or path.name.casefold().startswith(".env.")
                  or path.suffix.casefold() in {".key", ".ducai-license", ".whl"}):
        raise ValueError("Owner/test/activation filename in test package: " + path.name)
    digest = hashlib.sha256()
    previous = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            if audit:
                block = previous + chunk
                if PRIVATE_KEY.search(block) or LICENSE_TOKEN.search(block):
                    raise ValueError("Private key or activation material in: " + path.name)
                previous = block[-8192:]
    return digest.hexdigest()


def prepare(gui: Path, source_data: Path, destination: Path, expected_version: str) -> dict:
    gui, source_data = gui.resolve(strict=True), source_data.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Use a new test-package directory; existing data is never removed")
    if any(destination.is_relative_to(root) for root in (gui, source_data)):
        raise ValueError("Test package must be outside both source trees")
    app_manifest = json.loads((gui / "manifest.json").read_text(encoding="utf-8-sig"))
    if app_manifest["version"] != expected_version:
        raise ValueError("GUI version does not match the test package")
    wheel_metadata = source_data / (
        "paddle_runtime/.venv/Lib/site-packages/paddlepaddle_gpu-3.2.2.dist-info/direct_url.json")
    if json.loads(wheel_metadata.read_text(encoding="utf-8"))["url"] != WHEEL_URL:
        raise ValueError("Unexpected test runtime provenance")
    destination.mkdir(parents=True)
    for relative, expected in app_manifest["files_sha256"].items():
        source, target = checked_path(gui, relative), checked_path(destination, relative)
        if file_digest(source) != expected:
            raise ValueError("Application source hash mismatch: " + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(gui / "manifest.json", destination / "manifest.json")
    for source in (source_data.parent / "bin").iterdir():
        if source.is_file():
            target = destination / "bin" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    data = destination / "DUCAI_STUDIO_DATA"
    for name in ("paddle_runtime", "python_runtime_311"):
        copy_filtered_tree(source_data / name, data / name)
    for name in MODEL_NAMES:
        relative = Path("paddle_model_cache") / "official_models" / name
        copy_filtered_tree(source_data / relative, data / relative)
    config = data / "paddle_runtime/.venv/pyvenv.cfg"
    config.write_text("home = __DUCAI_PORTABLE_PYTHON_HOME__\nimplementation = CPython\n"
                      "version_info = 3.11.4\ninclude-system-site-packages = false\n",
                      encoding="utf-8")
    assets = Path(__file__).parent / "test-package-files"
    for name in ("Mo-ban-thu-RTX50.vbs", "HUONG-DAN-THU-RTX50.txt"):
        shutil.copy2(assets / name, destination / name)
    inventory = {}
    payload_bytes = 0
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            inventory[path.relative_to(destination).as_posix()] = file_digest(path, audit=True)
            payload_bytes += path.stat().st_size
    manifest = {
        "schema": "ducai-rtx50-ocr-test-v1", "version": expected_version,
        "distribution": "isolated-customer-test-not-general-update",
        "platform": "windows-x86_64", "launcher": "Mo-ban-thu-RTX50.vbs",
        "project_state": "RTX50_TEST_STATE", "tts_included": False, "whisper_included": False,
        "runtime": {"paddle": "3.2.2", "paddleocr": "3.7.0", "paddlex": "3.7.2",
                    "cuda": "12.9", "cudnn": "9.9", "wheel_source": WHEEL_URL},
        "ocr_models": list(MODEL_NAMES),
        "installation_constraints": {"ascii_install_path_required": True,
                                     "maximum_cuda_loader_path_characters": 240,
                                     "recommended_directory": r"E:\DUCAI-RTX50"},
        "quality_limit": "RTX 3060 local verification only; one 5m-fixture cue ends 0.6s later",
        "first_launch_mutable_files": ["DUCAI_STUDIO_DATA/paddle_runtime/.venv/pyvenv.cfg"],
        "payload_files": len(inventory), "payload_bytes": payload_bytes,
        "files_sha256": inventory,
    }
    (destination / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {key: manifest[key] for key in ("version", "payload_files", "payload_bytes")}


def verify(root: Path) -> dict:
    root = root.resolve(strict=True)
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest["schema"] != "ducai-rtx50-ocr-test-v1":
        raise ValueError("Unexpected test manifest schema")
    inventory = manifest["files_sha256"]
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(inventory) | {MANIFEST_NAME}:
        raise ValueError("Test package has missing or unexpected files")
    payload_bytes = 0
    for relative, expected in inventory.items():
        path = checked_path(root, relative)
        if file_digest(path, audit=True) != expected:
            raise ValueError("Test-package hash mismatch: " + relative)
        payload_bytes += path.stat().st_size
    if len(inventory) != manifest["payload_files"] or payload_bytes != manifest["payload_bytes"]:
        raise ValueError("Test-package size/count mismatch")
    return {"status": "PASS", "version": manifest["version"], "files": len(inventory),
            "payload_bytes": payload_bytes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--gui", type=Path)
    parser.add_argument("--source-data", type=Path)
    parser.add_argument("--expected-version", default="2.0.0a153")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        result = verify(args.destination)
    else:
        if args.gui is None or args.source_data is None:
            parser.error("Assembly requires --gui and --source-data")
        result = prepare(args.gui, args.source_data, args.destination, args.expected_version)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
