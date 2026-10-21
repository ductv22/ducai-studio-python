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
from time import monotonic

from ducai_studio.external_runtime import (
    isolated_python_environment,
    isolated_python_working_directory,
)
from ducai_studio.providers.export.ffmpeg import probe_ffmpeg_capabilities
from ducai_studio.providers.translation.gemini_web import _browser_executable
from ducai_studio.providers.tts import VoiceCatalog, discover_data_root
from ducai_studio.providers.tts.vieneu import VieNeuTtsProvider

from .process_tree import ProcessTreeGuard
from .runtime_program import runtime_program_path
from .whisper_runtime import (
    WHISPER_CTRANSLATE2_VERSION,
    WHISPER_RUNTIME_VERSION,
    whisper_model_path,
    whisper_runtime_python,
)

_PADDLE_CUDA_PROBE_TIMEOUT_SECONDS = 45
_PROBE_LAUNCH_GATE = b"DUCAI_LAUNCH\n"
_PADDLE_CUDA_PROBE = """
import json
import runpy
import sys
result = {"state": "cuda_failed"}
support = {}
try:
    support = runpy.run_path(sys.argv[1])
    import paddle
    result.update(cuda_compiled=paddle.is_compiled_with_cuda(),
                  cuda_version=str(paddle.version.cuda() or ""),
                  cudnn_version=str(paddle.version.cudnn() or ""))
    result["device_count"] = paddle.device.cuda.device_count() if result["cuda_compiled"] else 0
    if not result["cuda_compiled"]:
        result["state"] = "cpu_only"
    elif not result["device_count"]:
        result["state"] = "cuda_unavailable"
    else:
        result["gpu_name"] = paddle.device.cuda.get_device_name(0)
        result["compute_capability"] = list(paddle.device.cuda.get_device_capability(0))
        support["_validate_paddle_gpu"](paddle, "gpu:0")
        paddle.set_device("gpu:0")
        image = paddle.ones([1, 1, 8, 8], dtype="float32")
        weight = paddle.ones([1, 1, 3, 3], dtype="float32")
        convolution = paddle.nn.functional.conv2d(image, weight)
        matrix = paddle.ones([4, 4], dtype="float32")
        multiplied = paddle.matmul(matrix, matrix)
        if (not image.place.is_gpu_place()
                or float(convolution.sum().item()) != 324.0
                or float(multiplied.sum().item()) != 64.0):
            raise RuntimeError("cuda_probe_mismatch")
        result["state"] = "cuda_ready"
except Exception as error:
    result["error_type"] = type(error).__name__
    if "_error_code" in support:
        result["error_code"] = support["_error_code"](error)
print("DUCAI_CUDA_PROBE=" + json.dumps(result))
"""


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


def isolated_runtime_diagnostics(
    *, data_root: str | None, cancelled: Callable[[], bool],
) -> dict[str, object] | None:
    """Keep slow native probes outside the manager and own their entire lifetime."""
    if cancelled():
        return None
    with ProcessTreeGuard() as guard:
        process = subprocess.Popen(
            [str(runtime_program_path()), "-m", __name__, "--report", data_root or ""],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=isolated_python_environment(),
        )
        try:
            guard.assign_pid(process.pid)
            deadline = monotonic() + 300
            launch_gate: bytes | None = _PROBE_LAUNCH_GATE
            while not cancelled():
                if monotonic() >= deadline:
                    raise TimeoutError("Chẩn đoán quá thời gian. Hãy thử lại khi máy rảnh.")
                try:
                    stdout, _stderr = process.communicate(launch_gate, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    launch_gate = None
            else:
                return None
            if process.returncode != 0:
                raise RuntimeError("Tiến trình chẩn đoán không hoàn tất. Hãy thử lại.")
            line = next((line for line in reversed(stdout.decode("utf-8", "replace").splitlines())
                         if line.startswith("DUCAI_DIAGNOSTICS=")), "")
            value = json.loads(line.removeprefix("DUCAI_DIAGNOSTICS="))
            if not isinstance(value, dict) or value.get("schema") != "ducai-runtime-diagnostics-v1":
                raise ValueError("Không đọc được kết quả chẩn đoán.")
            return value
        finally:
            guard.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


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
    cuda = _paddle_cuda_probe(runtime) if ready else {"state": "probe_unavailable"}
    state = cuda["state"]
    if not ready:
        summary = "Runtime thiếu paddleocr/PaddlePaddle"
    elif state == "cuda_ready":
        summary = "Paddle CUDA sẵn sàng: " + str(cuda.get("gpu_name", "GPU"))
    elif state == "cpu_only":
        summary = "Paddle chỉ có CPU; runtime này không thể dùng GPU OCR"
    elif state == "cuda_unavailable":
        summary = "Paddle chưa thấy GPU CUDA; kiểm tra driver NVIDIA/runtime"
    elif state == "cuda_failed":
        summary = "Paddle đã cài nhưng phép thử CUDA lỗi; OCR có thể chuyển sang CPU"
        if cuda.get("error_code") == "ocr_cuda_blackwell_runtime_incompatible":
            summary = "RTX 50 cần runtime Paddle CUDA 12.9 trở lên; bản hiện tại không phù hợp"
        elif cuda.get("error_code") == "ocr_cuda_driver_incompatible":
            summary = "Driver NVIDIA chưa phù hợp với runtime CUDA của Paddle"
        elif cuda.get("error_code") == "ocr_cuda_library_missing":
            summary = "Paddle không nạp được thư viện CUDA/cuDNN; kiểm tra gói runtime"
    else:
        summary = "Paddle đã cài; chưa xác minh được CUDA, không thể kết luận GPU sẵn sàng"
        reason = cuda.get("probe_reason")
        if reason == "probe_timeout":
            seconds = cuda.get("timeout_seconds", _PADDLE_CUDA_PROBE_TIMEOUT_SECONDS)
            summary = f"Kiểm tra CUDA quá {seconds} giây; chưa thể xác nhận GPU sẵn sàng"
        elif reason == "probe_process_failed":
            summary = "Không chạy được tiến trình kiểm tra CUDA; chưa thể xác nhận GPU sẵn sàng"
        elif reason == "probe_exited":
            code = cuda.get("return_code")
            suffix = f" (mã 0x{code & 0xFFFFFFFF:08X})" if type(code) is int else ""
            summary = "Tiến trình kiểm tra CUDA thoát lỗi" + suffix
        elif reason == "probe_invalid_output":
            summary = "Không đọc được kết quả kiểm tra CUDA; chưa thể xác nhận GPU sẵn sàng"
    return RuntimeCheck(
        "paddle-ocr",
        "ready" if ready and state == "cuda_ready" else "degraded" if ready else "blocked",
        summary,
        f"paddleocr={version or '?'}; paddle={paddle or '?'}",
        str(runtime),
        cuda,
    )


def _paddle_cuda_probe(runtime: Path) -> dict[str, object]:
    unavailable: dict[str, object] = {
        "state": "probe_unavailable", "probe_reason": "probe_invalid_output",
    }
    try:
        with ProcessTreeGuard() as guard:
            process = subprocess.Popen(
                [str(runtime_program_path()), "-m",
                 "ducai_studio.infrastructure.runtime_diagnostics", str(runtime)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                cwd=isolated_python_working_directory(runtime),
                env=isolated_python_environment(),
            )
            try:
                # Own the venv launcher and real interpreter before the probe
                # can start. A timeout must not leave its CUDA child running.
                guard.assign_pid(process.pid)
                stdout, _stderr = process.communicate(
                    _PROBE_LAUNCH_GATE, timeout=_PADDLE_CUDA_PROBE_TIMEOUT_SECONDS,
                )
            finally:
                guard.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        if process.returncode != 0:
            return {"state": "probe_unavailable", "probe_reason": "probe_exited",
                    "return_code": process.returncode}
        line = next((line for line in reversed(stdout.decode("utf-8", "replace").splitlines())
                     if line.startswith("DUCAI_CUDA_PROBE=")), "")
        value = json.loads(line.removeprefix("DUCAI_CUDA_PROBE="))
        if not isinstance(value, dict) or value.get("state") not in {
            "cuda_ready", "cuda_failed", "cpu_only", "cuda_unavailable",
        }:
            return unavailable
        allowed = {"state", "cuda_compiled", "cuda_version", "cudnn_version", "device_count",
                   "gpu_name", "compute_capability", "error_type", "error_code"}
        return {key: item for key, item in value.items() if key in allowed}
    except subprocess.TimeoutExpired:
        return {"state": "probe_unavailable", "probe_reason": "probe_timeout",
                "timeout_seconds": _PADDLE_CUDA_PROBE_TIMEOUT_SECONDS}
    except (OSError, subprocess.SubprocessError):
        return {"state": "probe_unavailable", "probe_reason": "probe_process_failed"}
    except ValueError:
        return unavailable


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
        "vieneu-3.8",
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


def main(arguments: list[str] | None = None) -> int:
    """Launch only fixed diagnostics, also before app activation."""
    arguments = sys.argv[1:] if arguments is None else arguments
    report_mode = len(arguments) == 2 and arguments[0] == "--report"
    if not report_mode and (len(arguments) != 1 or not Path(arguments[0]).is_file()):
        return 64
    # The parent owns our process tree before releasing this exact byte gate.
    # This route never accepts arbitrary Python code or job commands.
    gate = b"".join(os.read(sys.stdin.fileno(), 1) for _ in range(len(_PROBE_LAUNCH_GATE)))
    if gate != _PROBE_LAUNCH_GATE:
        return 65
    if report_mode:
        result = run_runtime_diagnostics(data_root=arguments[1] or None).to_dict()
        print("DUCAI_DIAGNOSTICS=" + json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    source_host = Path(__file__).parents[1] / "engine_hosts" / "ocr_engine_host.py"
    host = (source_host if source_host.is_file() else
            runtime_program_path().parent / "engine_hosts" / "ocr_engine_host.pyc")
    child = subprocess.Popen(
        [arguments[0], "-c", _PADDLE_CUDA_PROBE, str(host)],
        stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer,
        env=isolated_python_environment(), close_fds=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return child.wait()


def _metadata_version(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return str(value.get("version", "")) if isinstance(value, dict) else ""
    except (OSError, TypeError, json.JSONDecodeError):
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
