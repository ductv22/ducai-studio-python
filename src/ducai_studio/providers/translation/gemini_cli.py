from __future__ import annotations

import json
import shutil
import subprocess
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
    ChallengeRequired,
    ProviderResponse,
    QuotaExhausted,
    ResponseFormatFailure,
    TransientProviderFailure,
    TranslationRequest,
)


class GeminiCliProvider:
    provider_id = "gemini-cli"
    kind = ProviderKind.TRANSLATION
    provider_version = "headless-json-v1"

    def __init__(
        self,
        *,
        model_id: str,
        executable: str = "gemini",
        timeout_seconds: float = 300.0,
    ) -> None:
        if not model_id.strip() or not executable.strip():
            raise ValueError("Gemini CLI model and executable are required")
        resolved = shutil.which(executable) or (
            str(Path(executable).expanduser().resolve())
            if Path(executable).expanduser().is_file()
            else ""
        )
        if not resolved:
            raise FileNotFoundError("Không tìm thấy Gemini CLI trong PATH hoặc đường dẫn đã chọn")
        self.model_id = model_id.strip()
        self.executable = resolved
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {"headless": True, "structured_outer_json": True, "oauth_user_managed": True}
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_id,
            HealthState.READY,
            "not-probed",
            "Gemini CLI sẵn sàng; OAuth do người dùng quản lý trong CLI.",
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
        command = [
            self.executable,
            "--model",
            self.model_id,
            "--output-format",
            "json",
        ]
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
                stdout, stderr = self._process.communicate(
                    request.prompt, timeout=self.timeout_seconds
                )
                code = self._process.returncode
            except subprocess.TimeoutExpired as exc:
                if self._process is not None:
                    self._process.kill()
                    self._process.communicate()
                raise TransientProviderFailure(
                    "Gemini CLI quá thời gian; sẽ thử lại đúng chunk từ cache."
                ) from exc
            finally:
                self._process = None
        combined = f"{stdout}\n{stderr}".casefold()
        if code != 0:
            self._raise_cli_failure(code, combined)
        try:
            payload = json.loads(stdout)
            text = payload["response"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ResponseFormatFailure("Gemini CLI không trả outer JSON hợp lệ.") from exc
        if not isinstance(text, str) or not text.strip():
            raise ResponseFormatFailure("Gemini CLI trả phản hồi rỗng.")
        usage = payload.get("stats", {})
        return ProviderResponse(
            request.request_id,
            text,
            self.provider_id,
            self.model_id,
            usage if isinstance(usage, dict) else {},
        )

    @staticmethod
    def _raise_cli_failure(code: int, text: str) -> None:
        if any(token in text for token in ("login", "sign in", "oauth", "unauthenticated")):
            raise AuthenticationRequired("Gemini CLI chưa đăng nhập OAuth hoặc phiên đã hết hạn.")
        if any(token in text for token in ("captcha", "verify it's you", "challenge")):
            raise ChallengeRequired("Gemini CLI yêu cầu xác minh thủ công.")
        if any(token in text for token in ("resource_exhausted", "quota exceeded")):
            raise QuotaExhausted("Gemini CLI xác nhận hết hạn mức.", verified=True)
        if code in {1, 53}:
            raise TransientProviderFailure(
                f"Gemini CLI dừng tạm thời (exit {code}); cache được giữ nguyên."
            )
        raise ResponseFormatFailure(f"Gemini CLI thất bại (exit {code}).")
