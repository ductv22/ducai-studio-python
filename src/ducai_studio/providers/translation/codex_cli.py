from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)

from .contracts import (
    AuthenticationRequired,
    ProviderResponse,
    ResponseFormatFailure,
    TransientProviderFailure,
    TranslationProviderFailure,
    TranslationRequest,
)


def _prefer_native_codex(executable: str) -> str:
    path = Path(executable)
    if os.name != "nt" or path.suffix.casefold() not in {".bat", ".cmd"}:
        return executable
    machine = platform.machine().casefold()
    if machine in {"amd64", "x64", "x86_64"}:
        package, triple = "codex-win32-x64", "x86_64-pc-windows-msvc"
    elif machine in {"arm64", "aarch64"}:
        package, triple = "codex-win32-arm64", "aarch64-pc-windows-msvc"
    else:
        return executable
    package_root = path.parent / "node_modules" / "@openai" / "codex"
    candidates = (
        package_root
        / "node_modules"
        / "@openai"
        / package
        / "vendor"
        / triple
        / "bin"
        / "codex.exe",
        package_root / "vendor" / triple / "bin" / "codex.exe",
    )
    return str(next((candidate for candidate in candidates if candidate.is_file()), path))


def _launch_command(executable: str, arguments: list[str]) -> list[str]:
    command = [executable, *arguments]
    if os.name != "nt" or Path(executable).suffix.casefold() not in {".bat", ".cmd"}:
        return command
    interpreter = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    if not interpreter:
        raise FileNotFoundError("Không tìm thấy Windows command interpreter cho Codex CLI")
    return [interpreter, "/d", "/s", "/c", *command]


class CodexCliProvider:
    provider_id = "codex-cli"
    kind = ProviderKind.TRANSLATION
    provider_version = "ephemeral-local-access-v1"

    def __init__(
        self,
        *,
        model_id: str = "gpt-5.6-luna",
        executable: str = "codex",
        timeout_seconds: float = 300.0,
    ) -> None:
        if not model_id.strip() or not executable.strip():
            raise ValueError("Codex CLI model and executable are required")
        resolved = shutil.which(executable) or (
            str(Path(executable).expanduser().resolve())
            if Path(executable).expanduser().is_file()
            else ""
        )
        if not resolved:
            raise FileNotFoundError("Không tìm thấy Codex CLI trong PATH hoặc đường dẫn đã chọn")
        self.model_id = model_id.strip()
        self.executable = _prefer_native_codex(resolved)
        self.timeout_seconds = float(timeout_seconds)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {
                "headless": True,
                "ephemeral": True,
                "local_access": True,
                "structured_json_prompt": True,
            }
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_id,
            HealthState.READY,
            "not-probed",
            "Codex CLI sẵn sàng làm fallback cục bộ bằng phiên Codex hiện có.",
            {"executable": self.executable, "model_id": self.model_id},
        )

    def close(self) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def generate(self, request: TranslationRequest) -> ProviderResponse:
        with tempfile.TemporaryDirectory(prefix="ducai-codex-translation-") as raw_root:
            output_path = Path(raw_root) / "last-message.json"
            arguments = [
                "exec",
                "--model",
                self.model_id,
                "--ephemeral",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--disable",
                "plugins",
                "--disable",
                "remote_plugin",
                "--disable",
                "plugin_sharing",
                "--disable",
                "skill_search",
                "--disable",
                "shell_snapshot",
                "--disable",
                "multi_agent",
                "--config",
                'model_reasoning_effort="low"',
                "--output-last-message",
                str(output_path),
                "-",
            ]
            command = _launch_command(self.executable, arguments)
            with self._lock:
                try:
                    self._process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    _stdout, stderr = self._process.communicate(
                        request.prompt, timeout=self.timeout_seconds
                    )
                    code = self._process.returncode
                except subprocess.TimeoutExpired as exc:
                    if self._process is not None:
                        self._process.kill()
                        self._process.communicate()
                    raise TransientProviderFailure(
                        "Codex CLI quá thời gian; cache vẫn giữ nguyên đúng chunk."
                    ) from exc
                except OSError as exc:
                    raise TranslationProviderFailure(
                        "Không khởi chạy được Codex CLI; cache vẫn giữ nguyên đúng chunk."
                    ) from exc
                finally:
                    self._process = None
            combined = str(stderr or "").casefold()
            if code != 0:
                if any(token in combined for token in ("unauthorized", "authentication")):
                    raise AuthenticationRequired(
                        "Phiên Codex CLI không còn quyền dùng model fallback."
                    )
                raise TransientProviderFailure(
                    f"Codex CLI fallback dừng tạm thời (exit {code}); cache được giữ nguyên."
                )
            try:
                text = output_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ResponseFormatFailure(
                    "Codex CLI không tạo file phản hồi cuối."
                ) from exc
            if not text:
                raise ResponseFormatFailure("Codex CLI trả phản hồi rỗng.")
            return ProviderResponse(
                request.request_id,
                text,
                self.provider_id,
                self.model_id,
                {"transport": "codex-cli", "ephemeral": True},
            )
