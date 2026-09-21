from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def portable_data_root(executable_path: str | Path | None = None) -> Path | None:
    """Return the data directory shipped beside a standalone executable."""

    executable = Path(executable_path or sys.executable).expanduser().resolve()
    candidate = executable.parent / "DUCAI_STUDIO_DATA"
    return candidate.resolve() if candidate.is_dir() else None


def configure_portable_environment(
    executable_path: str | Path | None = None,
) -> Path | None:
    """Activate bundled tools and repair relocatable managed Python runtimes."""

    executable = Path(executable_path or sys.executable).expanduser().resolve()
    install_root = executable.parent
    tool_root = install_root / "bin"
    if (tool_root / "ffmpeg.exe").is_file() and (tool_root / "ffprobe.exe").is_file():
        current_path = os.environ.get("PATH", "")
        path_parts = [item for item in current_path.split(os.pathsep) if item]
        if not any(_same_path(item, tool_root) for item in path_parts):
            suffix = os.pathsep + current_path if current_path else ""
            os.environ["PATH"] = str(tool_root) + suffix

    data_root = portable_data_root(executable)
    if data_root is None:
        return None
    os.environ.setdefault("DUCAI_STUDIO_DATA_ROOT", str(data_root))
    paddle_cache = data_root / "paddle_model_cache"
    if paddle_cache.is_dir():
        os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(paddle_cache))
    _repair_managed_venv_homes(data_root)
    return data_root


def paddle_cache_home_for_runtime(python_path: str | Path) -> Path | None:
    """Locate a data-root PaddleX cache from its managed Python executable."""

    runtime = Path(python_path).expanduser().resolve()
    parents = runtime.parents
    if len(parents) < 4 or parents[1].name != ".venv" or parents[2].name != "paddle_runtime":
        return None
    candidate = parents[3] / "paddle_model_cache"
    return candidate.resolve() if candidate.is_dir() else None


def _repair_managed_venv_homes(data_root: Path) -> None:
    mappings = (
        (data_root / "paddle_runtime" / ".venv", data_root / "python_runtime_311"),
        (data_root / "piper_runtime" / ".venv", data_root / "python_runtime_312"),
        (data_root / "vieneu_3_3_runtime" / ".venv", data_root / "python_runtime_312"),
        (data_root / "whisper_runtime" / ".venv", data_root / "python_runtime_312"),
    )
    for venv_root, python_home in mappings:
        config = venv_root / "pyvenv.cfg"
        if not config.is_file() or not (python_home / "python.exe").is_file():
            continue
        try:
            original = config.read_text(encoding="utf-8")
            lines = original.splitlines()
            replacement = f"home = {python_home.resolve()}"
            found = False
            for index, line in enumerate(lines):
                if line.partition("=")[0].strip().casefold() == "home":
                    lines[index] = replacement
                    found = True
                    break
            if not found:
                lines.insert(0, replacement)
            updated = "\n".join(lines) + "\n"
            if updated != original:
                config.write_text(updated, encoding="utf-8")
            scripts = venv_root / "Scripts"
            runtime_files = [python_home / "python.exe", python_home / "pythonw.exe"]
            runtime_files.extend(sorted(python_home.glob("python*.dll")))
            runtime_files.extend(sorted(python_home.glob("vcruntime*.dll")))
            for source in runtime_files:
                target = scripts / source.name
                if not source.is_file():
                    continue
                if target.is_file() and target.read_bytes() == source.read_bytes():
                    continue
                scripts.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        except OSError:
            # A read-only install will be reported by the runtime health check.
            continue


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return False
