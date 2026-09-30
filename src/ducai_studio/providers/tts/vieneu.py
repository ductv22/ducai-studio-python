from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from ducai_studio.external_runtime import (
    isolated_python_environment,
    isolated_python_working_directory,
)
from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)

from .catalog import discover_data_root
from .contracts import (
    TtsBatchResult,
    TtsCueArtifact,
    TtsCueFailure,
    TtsCueRequest,
    TtsReferenceBindingFailure,
    TtsRuntimeUnavailable,
)
from .host_client import EngineHostClient, EngineHostError
from .piper import _decode_batch_result, _default_host_script
from .warm_host import WarmTtsHostClient


def _file_stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return 0, 0


def _runtime_package_stamp(runtime_python: Path) -> tuple[tuple[str, int, int], ...]:
    site_packages = runtime_python.parent.parent / "Lib" / "site-packages"
    candidates = []
    for pattern in ("vieneu-*.dist-info/METADATA", "torch-*.dist-info/METADATA"):
        for path in site_packages.glob(pattern):
            size, modified = _file_stamp(path)
            candidates.append((path.parent.name, size, modified))
    return tuple(sorted(candidates))


def _probe_runtime_and_profile(
    runtime_python: str,
    profile_path: str,
    runtime_stamp: tuple[int, int],
    profile_stamp: tuple[int, int],
    metadata_stamp: tuple[int, int],
    package_stamp: tuple[tuple[str, int, int], ...],
) -> dict[str, object]:
    try:
        return _cached_runtime_and_profile_probe(
            runtime_python, profile_path, runtime_stamp, profile_stamp,
            metadata_stamp, package_stamp,
        )
    except subprocess.TimeoutExpired:
        return {"probe_error": "timeout"}
    except (OSError, subprocess.SubprocessError):
        return {"probe_error": "process_failed"}
    except (ValueError, TypeError):
        return {"probe_error": "invalid_response"}


@lru_cache(maxsize=8)
def _cached_runtime_and_profile_probe(
    runtime_python: str,
    profile_path: str,
    runtime_stamp: tuple[int, int],
    profile_stamp: tuple[int, int],
    metadata_stamp: tuple[int, int],
    package_stamp: tuple[tuple[str, int, int], ...],
) -> dict[str, object]:
    del runtime_stamp, profile_stamp, metadata_stamp, package_stamp
    code = """
import importlib.metadata as metadata
import json
import sys
import torch

result = {
    "version": metadata.version("vieneu"),
    "cuda_available": torch.cuda.is_available(),
    "cuda_version": torch.version.cuda or "",
    "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "",
}
profile = sys.argv[1]
if profile:
    import numpy as np
    from vieneu_utils.core_utils import strip_encoder_pad_frame

    with np.load(profile, allow_pickle=False) as values:
        if set(values.files) != {"speaker_emb", "codes"}:
            raise ValueError("vieneu_profile_fields_invalid")
        speaker = np.asarray(values["speaker_emb"], dtype=np.float32).reshape(-1)
        codes = np.asarray(strip_encoder_pad_frame(values["codes"]), dtype=np.int64)
    result["speaker_embedding_size"] = int(speaker.size)
    result["reference_codes_shape"] = [int(value) for value in codes.shape]
print(json.dumps(result, separators=(",", ":")))
"""
    # Windows cold imports of CUDA torch can exceed ten seconds. Exceptions must
    # escape the cached function so a transient failure cannot poison later jobs.
    completed = subprocess.run(
        [runtime_python, "-c", code, profile_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=isolated_python_working_directory(runtime_python),
        env=isolated_python_environment(),
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict) or not payload.get("version"):
        raise ValueError("vieneu_probe_response_invalid")
    return payload


class VieNeuTtsProvider:
    kind = ProviderKind.VOICE
    provider_id = "vieneu-3.8"
    model_id = "v3turbo"

    def __init__(
        self,
        *,
        runtime_python: Path | None = None,
        data_root: Path | None = None,
        profile_path: Path | None = None,
        reference_audio_path: Path | None = None,
        reference_denoise: bool = False,
        host_script: Path | None = None,
        timeout_seconds: float = 1_800,
        batch_size: int = 32,
        host_factory: Callable[..., EngineHostClient] = EngineHostClient,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.data_root = (data_root or discover_data_root()).expanduser().resolve()
        self.runtime_root = self.data_root / "vieneu_3_8_runtime"
        self.runtime_python = (
            (runtime_python or self.runtime_root / ".venv" / "Scripts" / "python.exe")
            .expanduser()
            .resolve()
        )
        self.host_script = (host_script or _default_host_script()).expanduser().resolve()
        self.profile_path = (profile_path or self._default_profile_path()).expanduser().resolve()
        self.reference_audio_path = (
            reference_audio_path.expanduser().resolve()
            if reference_audio_path is not None
            else None
        )
        self.reference_denoise = bool(reference_denoise)
        self.timeout_seconds = float(timeout_seconds)
        self.batch_size = max(1, min(32, int(batch_size)))
        self._host_factory = host_factory
        self._progress_callback = progress_callback
        try:
            self._warm_host = (
                WarmTtsHostClient.from_environment(timeout_seconds=self.timeout_seconds)
                if host_factory is EngineHostClient
                else None
            )
        except EngineHostError as exc:
            raise TtsRuntimeUnavailable(exc.code) from exc
        self._host: EngineHostClient | WarmTtsHostClient | None = None
        self._model_loaded = False
        self._loaded_voice_id = ""
        self._binding_receipt: dict[str, object] | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {
                "batch_transport": True,
                "clone": True,
                "offline": True,
                "preview": True,
                "persistent_model": True,
                "max_batch_size": self.batch_size,
                "engine_path": "upstream fused CUDA Graph",
            }
        )

    def health(self) -> ProviderHealth:
        self._progress("runtime_check", "Đang kiểm tra runtime VieNeu và CUDA")
        metadata_path = self.runtime_root / "ducai_runtime.json"
        version = ""
        commit = ""
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            version = str(raw.get("version", ""))
            commit = str(raw.get("commit", ""))
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        profile_json = self.profile_path.with_suffix(".json")
        profile_ready = self.profile_path.is_file() and profile_json.is_file()
        reference_ready = (
            self.reference_audio_path is not None and self.reference_audio_path.is_file()
        )
        probe = (
            _probe_runtime_and_profile(
                str(self.runtime_python),
                str(self.profile_path) if profile_ready else "",
                _file_stamp(self.runtime_python),
                _file_stamp(self.profile_path),
                _file_stamp(metadata_path),
                _runtime_package_stamp(self.runtime_python),
            )
            if self.runtime_python.is_file()
            else {}
        )
        profile_valid = (
            not profile_ready
            or (
                probe.get("speaker_embedding_size") == 192
                and isinstance(probe.get("reference_codes_shape"), list)
                and len(probe["reference_codes_shape"]) == 2
                and all(int(value) > 0 for value in probe["reference_codes_shape"])
            )
        )
        runtime_ready = (
            self.runtime_python.is_file()
            and self.host_script.is_file()
            and version == "3.8.1"
            and probe.get("version") == "3.8.1"
            and probe.get("cuda_available") is True
            and probe.get("cuda_version") == "12.8"
            and bool(probe.get("device_name"))
        )
        ready = runtime_ready
        if ready and profile_ready and profile_valid:
            summary = "VieNeu 3 Turbo 3.8.1 sẵn sàng với profile clone cục bộ"
        elif ready and reference_ready:
            summary = "VieNeu 3 Turbo 3.8.1 sẵn sàng tạo profile từ file mẫu"
        elif ready:
            summary = "VieNeu 3 Turbo 3.8.1 đã cài; hãy chọn file mẫu để tạo giọng clone"
        elif probe.get("probe_error") == "timeout":
            summary = (
                "Kiểm tra VieNeu quá 90 giây khi nạp PyTorch/CUDA. "
                "Hãy chờ máy bớt tải rồi bấm Tiếp tục; chưa thể kết luận runtime hỏng."
            )
        elif probe.get("probe_error"):
            summary = (
                "Không chạy được kiểm tra VieNeu hoặc profile giọng. "
                "Hãy kiểm tra runtime và file giọng mẫu trong Chẩn đoán runtime."
            )
        else:
            summary = "Thiếu hoặc hỏng runtime VieNeu 3.8.1"
        return ProviderHealth(
            self.provider_id,
            HealthState.READY if ready else HealthState.UNAVAILABLE,
            datetime.now(UTC).isoformat(),
            summary,
            {
                "runtime_python": str(self.runtime_python),
                "runtime_version": version,
                "runtime_commit": commit,
                "runtime_ready": runtime_ready,
                "probe_error": str(probe.get("probe_error", "")),
                "cuda_available": probe.get("cuda_available") is True,
                "cuda_version": str(probe.get("cuda_version", "")),
                "device_name": str(probe.get("device_name", "")),
                "profile_path": str(self.profile_path),
                "profile_ready": profile_ready,
                "profile_valid": profile_valid,
                "reference_audio_ready": reference_ready,
            },
        )

    def inspect_reference_binding(self) -> dict[str, object]:
        host = self._ensure_host(load_model=False)
        self._progress("voice_binding", "Đang kiểm tra profile giọng VieNeu")
        try:
            receipt = host.request("inspect_profile", {"profile_path": str(self.profile_path)})
        except EngineHostError as exc:
            raise TtsReferenceBindingFailure(exc.code) from exc
        if (
            receipt.get("speaker_embedding_size") != 192
            or not isinstance(receipt.get("reference_codes_shape"), list)
            or not receipt.get("reference_codes_shape")
        ):
            raise TtsReferenceBindingFailure("VieNeu clone profile lacks 192D anchor or codes")
        self._binding_receipt = receipt
        return receipt

    def preview(
        self,
        *,
        text: str,
        voice_id: str,
        output_path: Path,
        speed: float = 1.0,
    ) -> TtsCueArtifact:
        request = TtsCueRequest("preview", 1, 0, 30_000_000, text, "preview")
        result = self.synthesize_batch(
            request_id="preview",
            cues=(request,),
            voice_id=voice_id,
            output_dir=output_path.expanduser().resolve().parent,
            speed=speed,
            batch_size=1,
        )
        if result.failures or not result.artifacts:
            code = result.failures[0].code if result.failures else "vieneu_preview_empty"
            raise TtsRuntimeUnavailable(code)
        artifact = result.artifacts[0]
        target = output_path.expanduser().resolve()
        if artifact.wav_path != target:
            artifact.wav_path.replace(target)
            artifact = TtsCueArtifact(
                artifact.cue_id,
                artifact.request_fingerprint,
                target,
                artifact.sample_rate,
                artifact.duration_us,
                artifact.synthesis_mode,
                artifact.qa,
            )
        return artifact

    def synthesize_batch(
        self,
        *,
        request_id: str,
        cues: Sequence[TtsCueRequest],
        voice_id: str,
        output_dir: Path,
        speed: float = 1.0,
        batch_size: int = 1,
    ) -> TtsBatchResult:
        if not cues:
            return TtsBatchResult(request_id, ())
        selected_voice = voice_id.strip()
        preset_name = selected_voice.removeprefix("preset:") if selected_voice.startswith(
            "preset:"
        ) else ""
        if not preset_name and selected_voice not in {"clone", self.profile_path.stem}:
            raise TtsReferenceBindingFailure("VieNeu request selected an unknown voice")
        if speed != 1.0:
            raise ValueError(
                "VieNeu inference speed stays 1.0; timeline fitting is application policy"
            )
        host = self._ensure_host(load_model=True)
        if preset_name:
            if self._loaded_voice_id != selected_voice:
                self._progress("voice_binding", "Đang gắn giọng preset VieNeu")
                receipt = host.request("bind_preset", {"preset_name": preset_name})
                if receipt.get("binding") != "vieneu_v381_builtin_preset":
                    raise TtsReferenceBindingFailure("VieNeu did not confirm preset binding")
                self._binding_receipt = receipt
                self._loaded_voice_id = selected_voice
        else:
            if not self.profile_path.is_file():
                if self.reference_audio_path is None or not self.reference_audio_path.is_file():
                    raise TtsReferenceBindingFailure("VieNeu clone reference audio is missing")
                self._progress("voice_binding", "Đang tạo profile giọng VieNeu")
                receipt = host.request(
                    "enroll_profile",
                    {
                        "reference_audio_path": str(self.reference_audio_path),
                        "profile_path": str(self.profile_path),
                        "denoise": self.reference_denoise,
                    },
                )
                if receipt.get("binding") != "explicit_v381_speaker_embedding_192d_ref_codes":
                    raise TtsReferenceBindingFailure("VieNeu reference enrollment was not explicit")
            if self._loaded_voice_id != selected_voice:
                self._progress("voice_binding", "Đang gắn profile giọng VieNeu")
                receipt = host.request(
                    "bind_profile",
                    {
                        "profile_path": str(self.profile_path),
                        "require_speaker_embedding_size": 192,
                    },
                )
                if receipt.get("binding") != "explicit_v381_speaker_embedding_192d_ref_codes":
                    raise TtsReferenceBindingFailure(
                        "VieNeu did not confirm explicit clone binding"
                    )
                self._binding_receipt = receipt
                self._loaded_voice_id = selected_voice
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        jobs = [
            {
                "cue_id": cue.cue_id,
                "text": cue.text,
                "request_fingerprint": cue.request_fingerprint,
                "output_path": str(
                    output_dir
                    / (
                        "preview.wav"
                        if cue.cue_id == "preview"
                        else f"{cue.index:06d}-{cue.request_fingerprint[:16]}.wav"
                    )
                ),
            }
            for cue in cues
        ]
        try:
            self._progress("synthesis", f"Đang tổng hợp {len(jobs)} câu thoại")
            raw = host.request(
                "synthesize_batch",
                {
                    "jobs": jobs,
                    "batch_size": max(1, min(self.batch_size, int(batch_size))),
                    "use_ref_codes": True,
                    "apply_watermark": True,
                },
            )
        except EngineHostError as exc:
            return TtsBatchResult(
                request_id,
                (),
                tuple(TtsCueFailure(cue.cue_id, exc.code, True) for cue in cues),
            )
        decoded = _decode_batch_result(request_id, cues, raw, "vieneu-v3.8-fused-batch")
        return TtsBatchResult(
            decoded.request_id,
            decoded.artifacts,
            decoded.failures,
            {
                **dict(decoded.metadata),
                "voice_binding": (
                    "vieneu_v381_builtin_preset"
                    if preset_name
                    else "explicit_v381_speaker_embedding_192d_ref_codes"
                ),
                "preset_name": preset_name,
            },
        )

    def close(self) -> None:
        if self._host is not None:
            self._host.close()
            self._host = None
        self._model_loaded = False
        self._loaded_voice_id = ""
        self._binding_receipt = None

    def _ensure_host(
        self, *, load_model: bool
    ) -> EngineHostClient | WarmTtsHostClient:
        if self._host is None:
            if self._warm_host is None:
                self._progress("runtime_check", "Đang kiểm tra bộ máy VieNeu và GPU")
                health = self.health()
                if health.state is not HealthState.READY:
                    raise TtsRuntimeUnavailable(health.summary)
            else:
                self._progress("runtime_check", "Đang kết nối runtime VieNeu được giữ ấm")
            self._host = self._warm_host or self._host_factory(
                    python_executable=self.runtime_python,
                    host_script=self.host_script,
                    engine="vieneu",
                    timeout_seconds=self.timeout_seconds,
                    environment={
                        "HF_HOME": str(self.data_root / "model_cache" / "huggingface"),
                        "HUGGINGFACE_HUB_CACHE": str(
                            self.data_root / "model_cache" / "huggingface" / "hub"
                        ),
                    },
            )
            self._host.start()
        if load_model and not self._model_loaded:
            self._progress("model_loading", "Đang nạp mô hình VieNeu 3.8.1")
            receipt = self._host.request(
                "load",
                {
                    "mode": "v3turbo",
                    "max_batch_size": self.batch_size,
                    "runtime_version": "3.8.1",
                    "model_cache": str(self.data_root / "model_cache"),
                },
            )
            self._model_loaded = True
            if receipt.get("reused") is True:
                self._progress("warm_ready", "VieNeu đã sẵn sàng từ lượt trước")
        return self._host

    def _progress(self, phase: str, detail: str) -> None:
        if self._progress_callback is not None:
            self._progress_callback(phase, detail)

    def _default_profile_path(self) -> Path:
        profile_root = self.data_root / "vieneu_profiles"
        candidates = sorted(profile_root.glob("*.npz"))
        return candidates[0] if candidates else profile_root / "clone-profile.npz"


def profile_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for candidate in (path, path.with_suffix(".json")):
        with candidate.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()
