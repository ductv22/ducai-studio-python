from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
    encode_frame,
)

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
WORKER_MODULES = {
    "ocr": "ducai_studio.workers.ocr_worker",
    "translate": "ducai_studio.workers.translation_worker",
    "tts": "ducai_studio.workers.tts_worker",
    "export": "ducai_studio.workers.export_worker",
}


class ProductionRunner:
    def __init__(self, arguments: argparse.Namespace) -> None:
        self.arguments = arguments
        self.root = arguments.run_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "run-state.json"
        self.metrics_path = self.root / "metrics.jsonl"
        self.state = self._read_state()
        self.server_name = f"ducai-production-{uuid4().hex}"
        self.process: subprocess.Popen | None = None
        self.socket = QLocalSocket()

    def _read_state(self) -> dict[str, object]:
        if not self.state_path.is_file():
            return {}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def _write_state(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.state_path)

    def _metric(self, kind: str, payload: dict[str, object]) -> None:
        row = {
            "timestamp": time.time(),
            "kind": kind,
            **payload,
        }
        with self.metrics_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def open(self) -> None:
        environment = os.environ.copy()
        environment["DUCAI_STUDIO_DATA_ROOT"] = str(self.arguments.data_root)
        environment["DUCAI_PADDLE_PYTHON"] = str(self.arguments.paddle_python)
        command = [
            str(self.arguments.release_exe),
            "-m",
            "ducai_studio.job_manager",
            "--server-name",
            self.server_name,
            "--database",
            str(self.root / "state.db"),
            "--artifacts",
            str(self.root / "artifacts"),
        ]
        self.process = subprocess.Popen(
            command,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 15
        while (
            time.monotonic() < deadline
            and self.socket.state()
            != QLocalSocket.LocalSocketState.ConnectedState
        ):
            self.socket.abort()
            self.socket.connectToServer(self.server_name)
            self.socket.waitForConnected(250)
        if self.socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
            raise RuntimeError("production_job_manager_connect_timeout")
        report = self.request("runtime_diagnostics", {})
        if report.get("overall_status") != "ready":
            raise RuntimeError(f"runtime_not_ready:{report.get('overall_status')}")
        self._metric("runtime_ready", {"report": report})

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                self.request("shutdown", {}, timeout=15)
                process.wait(timeout=20)
            except Exception:
                process.kill()
                process.wait(timeout=10)
        self.socket.abort()

    def request(
        self, action: str, params: dict[str, object], *, timeout: float = 90
    ) -> object:
        request_id = uuid4().hex
        message = IpcMessage(
            IPC_PROTOCOL_VERSION,
            request_id,
            "request",
            {"action": action, "params": params},
        )
        self.socket.write(encode_frame(message))
        if not self.socket.waitForBytesWritten(2_000):
            raise RuntimeError(f"ipc_write_failed:{action}")
        decoder = FrameDecoder()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.socket.bytesAvailable() or self.socket.waitForReadyRead(500):
                for response in decoder.feed(bytes(self.socket.readAll())):
                    if (
                        response.kind == "response"
                        and response.payload.get("request_id") == request_id
                    ):
                        if response.payload.get("ok") is not True:
                            raise RuntimeError(
                                f"{action}:{response.payload.get('error', {})}"
                            )
                        return response.payload.get("result")
        raise RuntimeError(f"ipc_response_timeout:{action}")

    def ensure_project(self) -> dict[str, object]:
        project_id = str(self.state.get("project_id", ""))
        if project_id:
            try:
                project = self.request("get_project", {"project_id": project_id})
                if isinstance(project, dict):
                    return project
            except RuntimeError:
                pass
        project = self.request(
            "create_project",
            {
                "project_path": str(self.root / "production.ducai"),
                "name": self.arguments.video.stem,
            },
        )
        if not isinstance(project, dict):
            raise RuntimeError("create_project_invalid_result")
        project_id = str(project["id"])
        import_params: dict[str, object] = {
            "project_id": project_id,
            "media_path": str(self.arguments.video),
            "overlay_preset": {
                "blur": {
                    "x": 0.04,
                    "y": 0.82,
                    "width": 0.92,
                    "height": 0.15,
                    "enabled": True,
                    "blur_radius": 22,
                },
                "logo": {
                    "x": 0.82,
                    "y": 0.035,
                    "width": 0.15,
                    "height": 0.085,
                    "enabled": self.arguments.logo is not None,
                    "opacity": 0.9,
                },
            },
        }
        if self.arguments.logo is not None:
            import_params["default_logo_path"] = str(self.arguments.logo)
        imported = self.request("import_media", import_params)
        if not isinstance(imported, dict):
            raise RuntimeError("import_media_invalid_result")
        self.state["project_id"] = project_id
        self.state["video"] = str(self.arguments.video)
        self._write_state()
        self._metric(
            "media_imported",
            {
                "project_id": project_id,
                "duration_us": _video_duration(imported),
            },
        )
        return imported

    def latest_job(self, project_id: str, kind: str) -> dict[str, object] | None:
        jobs = self.request("list_jobs", {})
        if not isinstance(jobs, list):
            raise RuntimeError("list_jobs_invalid_result")
        return next(
            (
                item
                for item in jobs
                if isinstance(item, dict)
                and isinstance(item.get("spec"), dict)
                and item["spec"].get("project_id") == project_id
                and item["spec"].get("kind") == kind
            ),
            None,
        )

    def run_job(
        self,
        job: dict[str, object],
        *,
        kind: str,
        retry_transient: int = 0,
    ) -> dict[str, object]:
        job_id = str(job["id"])
        restarts = 0
        started_at = time.monotonic()
        after_sequence = 0
        while True:
            current = self.request("get_job", {"job_id": job_id})
            if not isinstance(current, dict):
                raise RuntimeError("get_job_invalid_result")
            state = str(current.get("state", ""))
            error_code = str(current.get("error_code", "") or "")
            if state == "queued":
                self._start_worker(job_id, kind)
            elif state == "interrupted" or (
                state == "failed"
                and error_code == "transient_provider_failure"
                and restarts < retry_transient
            ):
                restarts += 1
                self.request("retry_job", {"job_id": job_id})
                self._metric(
                    "job_restart",
                    {"job_id": job_id, "kind": kind, "attempt": restarts},
                )
                continue
            events = self.request(
                "list_events",
                {"job_id": job_id, "after_sequence": after_sequence},
            )
            if isinstance(events, list):
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    after_sequence = max(after_sequence, int(event.get("sequence", 0)))
                    self._print_event(kind, event)
            if state in TERMINAL_STATES:
                elapsed = round(time.monotonic() - started_at, 3)
                self._metric(
                    "job_terminal",
                    {
                        "job_id": job_id,
                        "kind": kind,
                        "state": state,
                        "error_code": error_code,
                        "elapsed_seconds": elapsed,
                        "restarts": restarts,
                        "result": current.get("result", {}),
                    },
                )
                print(
                    json.dumps(
                        {
                            "stage": kind,
                            "state": state,
                            "error_code": error_code,
                            "elapsed_seconds": elapsed,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if state != "succeeded":
                    raise RuntimeError(f"{kind}_job_{state}:{error_code}")
                return current
            time.sleep(1)

    def _start_worker(self, job_id: str, kind: str) -> None:
        self.request(
            "start_job",
            {
                "job_id": job_id,
                "program": str(self.arguments.release_exe),
                "arguments": ["-m", WORKER_MODULES[kind], job_id],
                "resume_stage_key": kind if kind != "translate" else "translation",
                "resume_checkpoint_key": "latest",
            },
        )

    @staticmethod
    def _print_event(kind: str, event: dict[str, object]) -> None:
        if event.get("kind") not in {"progress", "warning", "failed", "succeeded"}:
            return
        payload = event.get("payload", {})
        print(
            json.dumps(
                {"stage": kind, "event": event.get("kind"), "payload": payload},
                ensure_ascii=False,
            ),
            flush=True,
        )

    def create_ocr(self, project_id: str) -> dict[str, object]:
        job = self.latest_job(project_id, "ocr")
        if job is None:
            job = self.request(
                "create_ocr_job",
                {
                    "project_id": project_id,
                    "sample_interval_us": int(round(1_000_000 / self.arguments.ocr_fps)),
                    "engine_python": str(self.arguments.paddle_python),
                    "device": "gpu:0",
                },
            )
        if not isinstance(job, dict):
            raise RuntimeError("create_ocr_job_invalid_result")
        return job

    def create_translation(self, project_id: str) -> dict[str, object]:
        job = self.latest_job(project_id, "translate")
        if job is None:
            job = self.request(
                "create_translation_job",
                {
                    "project_id": project_id,
                    "provider_id": "gemini-web",
                    "model_id": self.arguments.gemini_model,
                    "provider_options": {
                        "profile_path": str(self.root / "gemini-web-profile"),
                        "headed": self.arguments.gemini_headed,
                        "guest": self.arguments.gemini_guest,
                        "temporary_chat": True,
                        "web_session_retries": 4,
                        "banned_latin_terms": ["Daozhang", "Renzong", "yamen"],
                    },
                },
            )
        if not isinstance(job, dict):
            raise RuntimeError("create_translation_job_invalid_result")
        return job

    def create_tts(self, project_id: str) -> dict[str, object]:
        job = self.latest_job(project_id, "tts")
        if job is None:
            job = self.request(
                "create_tts_job",
                {
                    "project_id": project_id,
                    "provider_id": "vieneu-3.3",
                    "voice_id": self.arguments.vieneu_profile.stem,
                    "provider_options": {
                        "purpose": "production",
                        "data_root": str(self.arguments.data_root),
                        "profile_path": str(self.arguments.vieneu_profile),
                        "batch_size": 32,
                    },
                },
            )
        if not isinstance(job, dict):
            raise RuntimeError("create_tts_job_invalid_result")
        return job

    def create_export(self, project_id: str) -> dict[str, object]:
        job = self.latest_job(project_id, "export")
        if job is None:
            job = self.request(
                "create_export_job",
                {
                    "project_id": project_id,
                    "output_path": str(self.arguments.output),
                    "export_options": {
                        "preset_id": "source-quality",
                        "backend": "auto",
                        "chunk_minutes": 10,
                        "overwrite": True,
                        "subtitle_style": {
                            "enabled": True,
                            "font_family": "Arial",
                            "font_size": 42,
                            "margin_bottom": 28,
                        },
                    },
                },
            )
        if not isinstance(job, dict):
            raise RuntimeError("create_export_job_invalid_result")
        return job

    def set_mix(self, project_id: str) -> None:
        for track_id, volume in (("A1", 0.18), ("D1", 1.0)):
            self.request(
                "timeline_command",
                {
                    "project_id": project_id,
                    "command": "track_volume",
                    "track_id": track_id,
                    "value": volume,
                },
            )

    def run(self) -> None:
        self.open()
        project = self.ensure_project()
        project_id = str(project["id"])
        if self.arguments.until == "import":
            return
        self.run_job(self.create_ocr(project_id), kind="ocr")
        if self.arguments.until == "ocr":
            return
        self.run_job(
            self.create_translation(project_id),
            kind="translate",
            retry_transient=3,
        )
        if self.arguments.until == "translate":
            return
        self.run_job(self.create_tts(project_id), kind="tts")
        self.set_mix(project_id)
        if self.arguments.until == "tts":
            return
        self.run_job(self.create_export(project_id), kind="export")


def _video_duration(project: dict[str, object]) -> int:
    media = project.get("media", [])
    if not isinstance(media, list):
        return 0
    for item in media:
        if isinstance(item, dict) and item.get("kind") == "video":
            return int(item.get("duration_us", 0))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a resumable DUCAI production E2E")
    parser.add_argument("--release-exe", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--paddle-python", type=Path, required=True)
    parser.add_argument("--vieneu-profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--logo", type=Path)
    parser.add_argument("--ocr-fps", type=float, default=1.25)
    parser.add_argument("--gemini-model", default="gemini-2.5-flash")
    parser.add_argument("--gemini-headed", action="store_true")
    parser.add_argument("--gemini-guest", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--until",
        choices=("import", "ocr", "translate", "tts", "export"),
        default="export",
    )
    arguments = parser.parse_args()
    for path in (
        arguments.release_exe,
        arguments.video,
        arguments.paddle_python,
        arguments.vieneu_profile,
    ):
        if not path.expanduser().is_file():
            parser.error(f"Missing required file: {path}")
    if arguments.logo is not None and not arguments.logo.expanduser().is_file():
        parser.error(f"Missing logo file: {arguments.logo}")
    if not 0.67 <= arguments.ocr_fps <= 2.5:
        parser.error("OCR FPS must be between 0.67 and 2.5")
    arguments.release_exe = arguments.release_exe.expanduser().resolve()
    arguments.video = arguments.video.expanduser().resolve()
    arguments.run_root = arguments.run_root.expanduser().resolve()
    arguments.data_root = arguments.data_root.expanduser().resolve()
    arguments.paddle_python = arguments.paddle_python.expanduser().resolve()
    arguments.vieneu_profile = arguments.vieneu_profile.expanduser().resolve()
    arguments.output = arguments.output.expanduser().resolve()
    arguments.logo = arguments.logo.expanduser().resolve() if arguments.logo else None
    return arguments


def main() -> int:
    QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    runner = ProductionRunner(parse_args())
    try:
        runner.run()
        return 0
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
