from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ducai_studio.external_runtime import (
    isolated_python_environment,
    isolated_python_working_directory,
)
from ducai_studio.providers.export.ffmpeg import probe_ffmpeg_capabilities
from ducai_studio.providers.translation.gemini_web import _browser_executable
from ducai_studio.providers.tts import VoiceCatalog, discover_data_root
from ducai_studio.providers.tts.vieneu import VieNeuTtsProvider

from .runtime_program import runtime_program_path
from .whisper_runtime import (
    WHISPER_CTRANSLATE2_VERSION,
    WHISPER_RUNTIME_VERSION,
    whisper_model_path,
    whisper_runtime_python,
)


@dataclass(frozen=True, slots=True)
class RuntimeCheck:
    component: str
    status: str
    summary: str
    version: str = ""
    path: str = ""
    details: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuntimeDiagnosticReport:
    schema: str
    overall_status: str
    checks: tuple[RuntimeCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def run_runtime_diagnostics(
    *,
    data_root: str | Path | None = None,
    gui_python: str | Path | None = None,
    executable_resolver: Callable[[str], str | None] = shutil.which,
) -> RuntimeDiagnosticReport:
    root = (Path(data_root) if data_root else discover_data_root()).expanduser().resolve()
    checks = [
        _check_gui_python(Path(gui_python) if gui_python else runtime_program_path()),
        _check_ffmpeg(executable_resolver),
        _check_paddle_runtime(root),
        _check_whisper_runtime(root),
        _check_playwright(),
        _check_gemini_cli(executable_resolver),
        _check_piper(root),
        _check_vieneu(root),
    ]
    overall = (
        "blocked"
        if any(item.status == "blocked" for item in checks)
        else "degraded"
        if any(item.status in {"degraded", "missing"} for item in checks)
        else "ready"
    )
    return RuntimeDiagnosticReport("ducai-runtime-diagnostics-v1", overall, tuple(checks))


def _check_gui_python(path: Path) -> RuntimeCheck:
    resolved = path.expanduser().resolve()
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ready = sys.version_info[:2] == (3, 12) and resolved.is_file()
    return RuntimeCheck(
        "gui-python",
        "ready" if ready else "blocked",
        "Python GUI 3.12 sẵn sàng" if ready else "GUI phải chạy bằng Python 3.12",
        version,
        str(resolved),
    )


def _check_ffmpeg(resolver: Callable[[str], str | None]) -> RuntimeCheck:
    ffmpeg = resolver("ffmpeg")
    ffprobe = resolver("ffprobe")
    if not ffmpeg or not ffprobe:
        return RuntimeCheck(
            "ffmpeg",
            "blocked",
            "Thiếu FFmpeg hoặc ffprobe",
            details={"ffprobe_ready": bool(ffprobe)},
        )
    try:
        capabilities = probe_ffmpeg_capabilities(ffmpeg)
    except (OSError, subprocess.SubprocessError) as exc:
        return RuntimeCheck("ffmpeg", "blocked", f"FFmpeg probe lỗi: {type(exc).__name__}")
    status = "ready" if capabilities.ready else "blocked"
    return RuntimeCheck(
        "ffmpeg",
        status,
        (
            "FFmpeg/ASS/libx264 sẵn sàng; NVENC dùng được"
            if capabilities.h264_nvenc
            else "FFmpeg sẵn sàng; NVENC không dùng được nên sẽ dùng CPU"
        ),
        capabilities.version_line,
        capabilities.ffmpeg_path,
        {
            "ffprobe_path": str(Path(ffprobe).resolve()),
            "ass_filter": capabilities.ass_filter,
            "libx264": capabilities.libx264,
            "h264_nvenc": capabilities.h264_nvenc,
        },
    )


def _check_paddle_runtime(data_root: Path) -> RuntimeCheck:
    configured = os.environ.get("DUCAI_PADDLE_PYTHON", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        data_root / "paddle_runtime" / ".venv" / "Scripts" / "python.exe",
        data_root.parent / ".bench-paddle-rapid" / "venv" / "Scripts" / "python.exe",
    ]
    runtime = next((item.resolve() for item in candidates if item and item.is_file()), None)
    if runtime is None:
        return RuntimeCheck("paddle-ocr", "blocked", "Không tìm thấy runtime Paddle OCR")
    version = _python_package_version(runtime, "paddleocr")
    paddle = _python_package_version(runtime, "paddlepaddle-gpu") or _python_package_version(
        runtime, "paddlepaddle"
    )
    ready = bool(version and paddle)
    return RuntimeCheck(
        "paddle-ocr",
        "ready" if ready else "blocked",
        "Paddle OCR runtime sẵn sàng" if ready else "Runtime thiếu paddleocr/PaddlePaddle",
        f"paddleocr={version or '?'}; paddle={paddle or '?'}",
        str(runtime),
    )


def _check_whisper_runtime(data_root: Path) -> RuntimeCheck:
    runtime = whisper_runtime_python(data_root)
    model = whisper_model_path(data_root)
    if not runtime.is_file() and model is None:
        return RuntimeCheck(
            "whisper",
            "missing",
            "Whisper chưa cài; OCR khung hình vẫn hoạt động bình thường",
        )
    if not runtime.is_file():
        return RuntimeCheck(
            "whisper",
            "blocked",
            "Đã có model nhưng thiếu runtime Whisper",
            path=str(model or ""),
        )
    version = _python_package_version(runtime, "faster-whisper")
    ctranslate2 = _python_package_version(runtime, "ctranslate2")
    ready = (
        version == WHISPER_RUNTIME_VERSION
        and ctranslate2 == WHISPER_CTRANSLATE2_VERSION
        and model is not None
    )
    if ready:
        summary = "Whisper large-v3 sẵn sàng"
    elif model is None:
        summary = "Runtime Whisper đã có nhưng thiếu model large-v3"
    else:
        summary = (
            "Runtime Whisper phải dùng "
            f"faster-whisper {WHISPER_RUNTIME_VERSION} và "
            f"CTranslate2 {WHISPER_CTRANSLATE2_VERSION}"
        )
    return RuntimeCheck(
        "whisper",
        "ready" if ready else "blocked",
        summary,
        f"faster-whisper={version or '?'}; ctranslate2={ctranslate2 or '?'}",
        str(runtime.resolve()),
        {"model_path": str(model or ""), "model_ready": model is not None},
    )


def _check_playwright() -> RuntimeCheck:
    try:
        version = importlib.metadata.version("playwright")
        installed = True
    except importlib.metadata.PackageNotFoundError:
        version = ""
        installed = False
    browser = _browser_executable()
    ready = installed and browser is not None
    return RuntimeCheck(
        "gemini-web",
        "ready" if ready else "blocked",
        "Playwright và Chrome/Edge sẵn sàng" if ready else "Thiếu Playwright hoặc Chrome/Edge",
        version,
        str(browser or ""),
        {"temporary_chat_supported": True, "bounded_1095_recovery": True},
    )


def _check_gemini_cli(resolver: Callable[[str], str | None]) -> RuntimeCheck:
    executable = resolver("gemini")
    if not executable:
        return RuntimeCheck(
            "gemini-cli",
            "missing",
            "Gemini CLI chưa cài; Gemini Web/API vẫn có thể hoạt động",
        )
    return RuntimeCheck("gemini-cli", "ready", "Gemini CLI đã được tìm thấy", path=executable)


def _check_piper(data_root: Path) -> RuntimeCheck:
    runtime = data_root / "piper_runtime" / ".venv" / "Scripts" / "python.exe"
    metadata = data_root / "piper_runtime" / "ducai_runtime.json"
    version = _metadata_version(metadata)
    try:
        voices = VoiceCatalog(data_root).installed()
    except (OSError, TypeError, ValueError):
        voices = ()
    ready = runtime.is_file() and version == "1.6.0" and bool(voices)
    return RuntimeCheck(
        "piper",
        "ready" if ready else "blocked",
        (
            f"Piper 1.6 sẵn sàng với {len(voices)} giọng"
            if ready
            else "Thiếu runtime Piper 1.6 hoặc giọng ONNX hợp lệ"
        ),
        version,
        str(runtime.resolve()) if runtime.exists() else "",
        {"installed_voice_count": len(voices)},
    )


def _check_vieneu(data_root: Path) -> RuntimeCheck:
    provider = VieNeuTtsProvider(data_root=data_root)
    health = provider.health()
    ready = health.state.value == "ready"
    details = dict(health.details)
    return RuntimeCheck(
        "vieneu-3.3",
        "ready" if ready else "blocked",
        health.summary,
        str(details.get("runtime_version", "")),
        str(details.get("runtime_python", "")),
        details,
    )


def _python_package_version(runtime: Path, package: str) -> str:
    code = (
        "import importlib.metadata as m; "
        f"print(m.version({json.dumps(package)}), end='')"
    )
    try:
        result = subprocess.run(
            [str(runtime), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=isolated_python_working_directory(runtime),
            env=isolated_python_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _metadata_version(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return str(value.get("version", "")) if isinstance(value, dict) else ""
    except (OSError, TypeError, json.JSONDecodeError):
        return ""
