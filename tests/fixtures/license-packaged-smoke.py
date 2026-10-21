"""Exercise customer/owner EXEs with real IPC and disposable activation state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

from ducai_studio import __version__
from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
    encode_frame,
)
from ducai_studio.infrastructure.machine_identity import request_code
from ducai_studio.infrastructure.offline_license import verify_license


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--issuer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    release = arguments.release.resolve(strict=True)
    issuer = arguments.issuer.resolve(strict=True)
    root = arguments.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    executable = release / "DucaiStudio2.exe"
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["version"] == __version__
    assert manifest["activation"] == "offline-device-license-v1"
    assert manifest["license_token_formats"] == ["DUCAI-L1", "DUCAI-L2", "DUCAI-L3"]
    for relative, expected in manifest["files_sha256"].items():
        path = (release / relative).resolve(strict=True)
        assert path.is_relative_to(release)
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == expected, relative
    results: dict[str, object] = {"manifest_and_hashes": True}
    processes: list[dict] = []
    owned: list[tuple[subprocess.Popen, dict]] = []
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "DUCAI_", "QT_"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    environment.update(
        LOCALAPPDATA=str(root / "localappdata"),
        DUCAI_APP_DATA_ROOT=str(root / "projectdata"),
        QT_QPA_PLATFORM="offscreen",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    license_file = root / "localappdata/DUCAI STUDIO/Licensing/license-v1.txt"
    owner_state = root / "issuer-state"
    QCoreApplication.instance() or QCoreApplication([])

    def checkpoint() -> None:
        (root / "result.json").write_text(
            json.dumps({"checks": results, "processes": processes}, indent=2), encoding="utf-8"
        )

    def spawn(name: str, command: list[str], overrides: dict | None = None):
        process = subprocess.Popen(
            command, cwd=release, env={**environment, **(overrides or {})},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        entry = {"name": name, "pid": process.pid, "port": None,
                 "executable": command[0], "cwd": str(release), "state": "running"}
        processes.append(entry)
        owned.append((process, entry))
        checkpoint()
        return process, entry

    def collect(process, entry, timeout=30, expected=0) -> bytes:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=20, check=False)
                process.communicate(timeout=10)
            entry["state"] = "timed-out"
            checkpoint()
            raise
        entry.update(state="exited", exit_code=process.returncode)
        checkpoint()
        assert process.returncode == expected, (entry["name"], process.returncode,
                                                stderr.decode("utf-8", "replace")[-1500:])
        assert b"QQmlApplicationEngine failed" not in stderr
        assert b"Traceback" not in stderr
        assert b"ReferenceError" not in stderr and b"TypeError:" not in stderr
        return stdout

    def gui(name: str) -> None:
        health = root / f"{name}-health.json"
        nonce = uuid4().hex
        process, entry = spawn(name, [str(executable)], {
            "DUCAI_SMOKE_SIZE": "1180x720", "DUCAI_SMOKE_EXIT_MS": "18000",
            "DUCAI_SMOKE_WORKSPACE": "settings", "DUCAI_SMOKE_SCREENSHOT_DELAY_MS": "8000",
            "DUCAI_SMOKE_SCREENSHOT": str(root / f"{name}.png"),
            "DUCAI_UPDATE_HEALTH_PATH": str(health), "DUCAI_UPDATE_HEALTH_NONCE": nonce,
            "DUCAI_UPDATE_HEALTH_VERSION": manifest["version"],
        })
        collect(process, entry, timeout=45)
        receipt = json.loads(health.read_text(encoding="utf-8"))
        assert receipt["nonce"] == nonce and receipt["version"] == manifest["version"]
        assert receipt["backend_ready"] and (root / f"{name}.png").is_file()
        results[name] = True
        checkpoint()

    @contextmanager
    def manager(name: str):
        server = f"ducai-license-smoke-{uuid4().hex}"
        (root / "manager").mkdir(exist_ok=True)
        process, entry = spawn(name, [str(executable), "-m", "ducai_studio.job_manager",
            "--server-name", server, "--database", str(root / "manager/state.db"),
            "--artifacts", str(root / "manager/artifacts")])
        socket = QLocalSocket()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            socket.connectToServer(server)
            if socket.waitForConnected(100):
                break
            socket.abort()
            assert process.poll() is None, "Packaged manager exited before IPC readiness"
            time.sleep(0.05)
        assert socket.state() == QLocalSocket.LocalSocketState.ConnectedState
        decoder = FrameDecoder()

        def request(action: str, params: dict | None = None) -> dict:
            identifier = uuid4().hex
            message = IpcMessage(IPC_PROTOCOL_VERSION, identifier, "request",
                                 {"action": action, "params": params or {}})
            socket.write(encode_frame(message))
            socket.flush()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not socket.bytesAvailable():
                    socket.waitForReadyRead(100)
                for response in decoder.feed(bytes(socket.readAll())):
                    if response.payload.get("request_id") == identifier:
                        return dict(response.payload)
            raise AssertionError(f"Packaged IPC request timed out: {action}")

        try:
            yield request
        finally:
            try:
                if process.poll() is None:
                    request("shutdown")
            finally:
                socket.abort()
                collect(process, entry, timeout=20)

    def owner_issue(
        code: str,
        filename: str,
        *,
        legacy: bool = False,
        duration_hours: int | None = None,
    ) -> str:
        destination = root / filename
        command = [str(issuer), "--state-root", str(owner_state), "issue",
                   "--request-code", code, "--output", str(destination)]
        if legacy:
            command.append("--legacy")
        if duration_hours is not None:
            command.extend(["--duration-hours", str(duration_hours)])
        label = "owner-issue-legacy" if legacy else (
            "owner-issue-timed" if duration_hours is not None else "owner-issue"
        )
        process, entry = spawn(label, command)
        collect(process, entry)
        token = destination.read_text(encoding="utf-8")
        destination.unlink()
        return token

    try:
        assert not license_file.exists(), "Use a fresh smoke output directory"
        gui("unlicensed-gui")
        for module in ("ocr_worker", "translation_worker", "tts_worker", "export_worker",
                       "preowned_launcher"):
            process, entry = spawn(module, [str(executable), "-m",
                "ducai_studio.workers." + module, "unlicensed-probe"])
            collect(process, entry, expected=3)
        results["direct_workers_denied"] = True
        with manager("unlicensed-manager") as request:
            status = request("get_license_status")["result"]
            assert not status["active"] and status["request_code"]
            for action in ("list_projects", "list_jobs", "create_project", "create_ocr_job",
                           "create_translation_job", "create_tts_job", "create_export_job",
                           "subtitle_command", "timeline_command", "multi_start", "multi_resume",
                           "list_translation_models", "store_credential"):
                rejected = request(action)
                assert not rejected["ok"] and rejected["error"]["code"] == "ActivationRequired"
            assert request("get_update_install_eligibility")["ok"]
            results["ipc_work_denied_recovery_allowed"] = True
            wrong = owner_issue(request_code(uuid4().hex + uuid4().hex), "wrong.ducai-license")
            assert wrong.startswith("DUCAI-L2.") and len(wrong) == 128
            rejected = request("activate_license", {"token": wrong})
            assert not rejected["ok"] and not license_file.exists()
            results["wrong_machine_denied"] = True
            token = owner_issue(
                status["request_code"], "valid-legacy.ducai-license", legacy=True
            )
            assert token.startswith("DUCAI-L1.")
            device = status["request_code"][len("DUCAI-M1-"):len("DUCAI-M1-") + 64]
            verify_license(token, device_id=device)
            assert request("activate_license", {"token": token})["result"]["active"]
            legacy_bytes = license_file.read_bytes()
            assert not request("activate_license", {"token": token[:-5]})["ok"]
            assert license_file.read_bytes() == legacy_bytes
            assert request("get_license_status")["result"]["active"]
            results["legacy_owner_exe_activation_invalid_replacement_preserved"] = True
            created = request("create_project", {"project_path": str(root / "test.ducai"),
                                                   "name": "Packaged license smoke"})
            assert created["ok"]
            assert request("list_projects")["result"]
            results["licensed_work_unlocked"] = True
        with manager("restarted-manager") as request:
            assert request("get_license_status")["result"]["active"]
            assert license_file.read_bytes() == legacy_bytes
            assert request("list_projects")["result"]
            status = request("get_license_status")["result"]
            compact = owner_issue(status["request_code"], "valid-compact.ducai-license")
            assert compact.startswith("DUCAI-L2.") and len(compact) == 128
            assert request("activate_license", {"token": compact})["result"]["active"]
            timed = owner_issue(
                status["request_code"], "valid-timed.ducai-license", duration_hours=24
            )
            assert timed.startswith("DUCAI-L3.") and len(timed) == 139
            timed_status = request("activate_license", {"token": timed})["result"]
            assert timed_status["active"] and timed_status["expires_at"]
            assert request("activate_license", {"token": compact})["result"]["active"]
            compact_bytes = license_file.read_bytes()
            assert compact_bytes != legacy_bytes
            assert not request("activate_license", {"token": compact[:-1]})["ok"]
            assert license_file.read_bytes() == compact_bytes
            results["legacy_restart_and_compact_activation"] = True
            reservation = request("reserve_update_install")["result"]
            assert reservation["reserved"]
            assert request("release_update_install", {"token": reservation["token"]})["ok"]
            assert license_file.read_bytes() == compact_bytes
            results["restart_and_update_reservation_preserve_activation"] = True
        with manager("compact-restarted-manager") as request:
            assert request("get_license_status")["result"]["active"]
            assert request("list_projects")["result"]
            assert license_file.read_bytes() == compact_bytes
            results["compact_restart_persistence"] = True
        gui("licensed-gui")
        results["status"] = "PASS"
    finally:
        for process, entry in owned:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=20, check=False)
                    process.communicate(timeout=10)
                entry.update(state="cleaned-up", exit_code=process.returncode)
            elif entry["state"] == "running":
                process.communicate(timeout=10)
                entry.update(state="exited", exit_code=process.returncode)
        # No real machine-bound activation or owner ledger remains in test reports.
        license_file.unlink(missing_ok=True)
        for filename in ("issued-licenses.json", "key-info.json", "issuer.lock"):
            (owner_state / filename).unlink(missing_ok=True)
        for path in root.glob("*.ducai-license"):
            path.unlink(missing_ok=True)
        checkpoint()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
