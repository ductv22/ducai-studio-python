from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure.update_install import (
    launch_update_helper,
    prepare_update_handoff,
)
from ducai_studio.infrastructure.update_manifest import canonical_manifest_bytes

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows updater helper")


def _compile_fixture(csc: Path, source: Path, target: Path, *, mode: str) -> None:
    writes_health = "true" if mode == "healthy" else "false"
    source.write_text(
        rf'''
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

public static class Program
{{
    private static string Escape(string value)
    {{
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }}

    public static int Main(string[] args)
    {{
        if (args.Length > 0 && args[0] == "--verify-update-stage")
        {{
            File.WriteAllText(
                Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "stage-verified.txt"),
                "verified"
            );
            return 0;
        }}
        string healthPath = null;
        string nonce = "";
        string version = "";
        for (int index = 0; index + 1 < args.Length; index += 2)
        {{
            if (args[index] == "--update-health-path") healthPath = args[index + 1];
            if (args[index] == "--update-health-nonce") nonce = args[index + 1];
            if (args[index] == "--update-health-version") version = args[index + 1];
        }}
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        if (healthPath == null)
        {{
            File.WriteAllText(Path.Combine(root, "old-reopened.txt"), "ready");
            return 0;
        }}
        File.WriteAllText(Path.Combine(root, "new-attempted.txt"), "started");
        if (!{writes_health}) return 7;
        string body = "{{\"schema\":\"ducai-update-health-v1\",\"pid\":"
            + Process.GetCurrentProcess().Id
            + ",\"nonce\":\"" + Escape(nonce)
            + "\",\"version\":\"" + Escape(version)
            + "\",\"backend_ready\":true,\"install_root\":\"" + Escape(root) + "\"}}";
        string temporary = healthPath + ".tmp";
        File.WriteAllText(temporary, body);
        File.Move(temporary, healthPath);
        Thread.Sleep(2000);
        return 0;
    }}
}}
''',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(csc), "/nologo", "/target:exe", f"/out:{target}", str(source)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _manifest(path: Path, signature: Path, payload: Path, version: str) -> tuple[str, bytes]:
    files = []
    for relative in ("DucaiStudio2.exe", "app.dat"):
        source = payload / relative
        files.append(
            {
                "path": relative,
                "size": source.stat().st_size,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
    encoded = canonical_manifest_bytes(
        {
            "schema": 1,
            "product": "DUCAI STUDIO",
            "version": version,
            "channel": "testing",
            "platform": "win64",
            "archive": {
                "name": f"DUCAI-STUDIO-{version}-win64-update.zip",
                "size": 1,
                "sha256": "0" * 64,
            },
            "files": files,
        }
    )
    key = Ed25519PrivateKey.generate()
    path.write_bytes(encoded)
    signature.write_bytes(key.sign(encoded))
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return hashlib.sha256(encoded).hexdigest(), public


def _run_helper(
    tmp_path: Path, *, new_mode: str
) -> tuple[Path, Path, Path, subprocess.Popen[bytes]]:
    windows = Path(os.environ["WINDIR"])
    csc = windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not csc.is_file():
        pytest.skip(".NET Framework C# compiler is unavailable")

    install = tmp_path / "Install Root With Spaces"
    update_root = tmp_path / "Private Update State"
    candidate_root = update_root / "candidate-fixture"
    payload = candidate_root / "payload"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / "app.dat").write_bytes(b"old-app-data")
    (install / "keep-user-file.txt").write_bytes(b"do-not-touch")
    (install / "DUCAI_STUDIO_DATA").mkdir()
    (install / "DUCAI_STUDIO_DATA" / "cache.bin").write_bytes(b"user-cache")
    _compile_fixture(
        csc,
        tmp_path / "old-app.cs",
        install / "DucaiStudio2.exe",
        mode="rollback",
    )
    _compile_fixture(
        csc,
        tmp_path / "new-app.cs",
        payload / "DucaiStudio2.exe",
        mode=new_mode,
    )
    (payload / "app.dat").write_bytes(b"new-app-data")
    manifest = tmp_path / "update-manifest.json"
    signature = tmp_path / "update-manifest.sig"
    digest, public = _manifest(manifest, signature, payload, "2.0.0a118")
    handoff = prepare_update_handoff(
        install_root=install,
        payload_root=payload,
        manifest_path=manifest,
        signature_path=signature,
        update_root=update_root,
        version="2.0.0a118",
        expected_manifest_sha256=digest,
        public_key=public,
    )
    process = launch_update_helper(handoff, parent_pid=2_000_000_000)
    process.wait(timeout=20)
    return install, handoff.request_path.parent, candidate_root, process


def test_helper_installs_verified_payload_and_preserves_user_data(tmp_path: Path) -> None:
    install, handoff_root, candidate_root, process = _run_helper(
        tmp_path, new_mode="healthy"
    )

    assert process.returncode == 0
    assert (install / "app.dat").read_bytes() == b"new-app-data"
    assert (install / "keep-user-file.txt").read_bytes() == b"do-not-touch"
    assert (install / "DUCAI_STUDIO_DATA" / "cache.bin").read_bytes() == b"user-cache"
    assert (install / "stage-verified.txt").read_text(encoding="utf-8") == "verified"
    assert json.loads((handoff_root / "journal.json").read_text(encoding="utf-8-sig"))[
        "state"
    ] == "healthy"
    assert not (handoff_root / "backup").exists()
    assert not candidate_root.exists()
    time.sleep(2.1)


def test_helper_rolls_back_and_reopens_old_app_when_health_fails(tmp_path: Path) -> None:
    install, handoff_root, candidate_root, process = _run_helper(
        tmp_path, new_mode="rollback"
    )

    assert process.returncode == 1
    assert (install / "app.dat").read_bytes() == b"old-app-data"
    assert (install / "keep-user-file.txt").read_bytes() == b"do-not-touch"
    assert (install / "DUCAI_STUDIO_DATA" / "cache.bin").read_bytes() == b"user-cache"
    assert (install / "stage-verified.txt").read_text(encoding="utf-8") == "verified"
    assert json.loads((handoff_root / "journal.json").read_text(encoding="utf-8-sig"))[
        "state"
    ] == "rolled_back"
    assert not candidate_root.exists()
    deadline = time.monotonic() + 5
    while not (install / "old-reopened.txt").is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (install / "old-reopened.txt").is_file()
