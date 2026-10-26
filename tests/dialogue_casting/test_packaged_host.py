from __future__ import annotations

import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
CASTING_SOURCE = PROJECT_ROOT / "scripts" / "dialogue_casting"
PACKAGED_MODULES = (
    "__init__",
    "analysis",
    "asr_crosscheck",
    "assets",
    "corpus",
    "demo",
    "ecapa",
    "evaluation",
    "inference",
    "moss",
    "speaker_consistency",
    "srt_demo",
)


def test_compiled_casting_host_modules_import_without_python_sources(tmp_path):
    engine_hosts = tmp_path / "engine_hosts"
    package = engine_hosts / "dialogue_casting"
    package.mkdir(parents=True)
    for name in PACKAGED_MODULES:
        py_compile.compile(
            str(CASTING_SOURCE / f"{name}.py"),
            cfile=str(package / f"{name}.pyc"),
            dfile=f"engine_hosts/dialogue_casting/{name}.py",
            doraise=True,
        )
    for name in ("model-lock.json", "ecapa-lock.json", "moss-lock.json", "requirements.txt"):
        shutil.copy2(CASTING_SOURCE / name, package / name)
    host = (
        PROJECT_ROOT
        / "src"
        / "ducai_studio"
        / "engine_hosts"
        / "dialogue_casting_engine_host.py"
    )
    host_bytecode = engine_hosts / "dialogue_casting_engine_host.pyc"
    py_compile.compile(
        str(host),
        cfile=str(host_bytecode),
        dfile="engine_hosts/dialogue_casting_engine_host.py",
        doraise=True,
    )

    environment = {**os.environ, "PYTHONPATH": str(engine_hosts)}
    command = [
        sys.executable,
        "-c",
        "import dialogue_casting.srt_demo; "
        "import dialogue_casting.asr_crosscheck; "
        "import dialogue_casting.moss; "
        "from dialogue_casting.speaker_consistency import classify_consistent_cues; "
        "from importlib.machinery import SourcelessFileLoader; "
        f"SourcelessFileLoader('packaged_casting_host', r'{host_bytecode}').load_module()",
    ]
    result = subprocess.run(command, env=environment, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert not list(package.glob("*.py"))
