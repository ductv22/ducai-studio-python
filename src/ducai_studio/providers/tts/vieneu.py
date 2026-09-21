from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

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


class VieNeuTtsProvider:
    kind = ProviderKind.VOICE
    provider_id = "vieneu-3.3"
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
        batch_size: int = 64,
        host_factory: Callable[..., EngineHostClient] = EngineHostClient,
    ) -> None:
        self.data_root = (data_root or discover_data_root()).expanduser().resolve()
        self.runtime_root = self.data_root / "vieneu_3_3_runtime"
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
        self.batch_size = max(1, min(64, int(batch_size)))
        self._host_factory = host_factory
        self._host: EngineHostClient | None = None
        self._loaded_profile = ""
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
                "clone_speed_claim": "quality-first; not Piper-speed",
            }
        )

    def health(self) -> ProviderHealth:
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
        ready = (
            self.runtime_python.is_file()
            and self.host_script.is_file()
            and version == "3.3.0"
            and (profile_ready or reference_ready)
        )
        return ProviderHealth(
            self.provider_id,
            HealthState.READY if ready else HealthState.UNAVAILABLE,
            datetime.now(UTC).isoformat(),
            (
                "VieNeu 3.3 Turbo sẵn sàng với profile clone cục bộ"
                if ready
                else "Thiếu runtime VieNeu 3.3.0 hoặc profile clone hợp lệ"
            ),
            {
                "runtime_python": str(self.runtime_python),
                "runtime_version": version,
                "runtime_commit": commit,
                "profile_path": str(self.profile_path),
                "profile_ready": profile_ready,
                "reference_audio_ready": reference_ready,
            },
        )

    def inspect_reference_binding(self) -> dict[str, object]:
        host = self._ensure_host(load_model=False)
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
        if voice_id not in {"clone", self.profile_path.stem}:
            raise TtsReferenceBindingFailure("VieNeu request did not select the clone profile")
        if speed != 1.0:
            raise ValueError(
                "VieNeu inference speed stays 1.0; timeline fitting is application policy"
            )
        host = self._ensure_host(load_model=True)
        if not self.profile_path.is_file():
            if self.reference_audio_path is None or not self.reference_audio_path.is_file():
                raise TtsReferenceBindingFailure("VieNeu clone reference audio is missing")
            receipt = host.request(
                "enroll_profile",
                {
                    "reference_audio_path": str(self.reference_audio_path),
                    "profile_path": str(self.profile_path),
                    "denoise": self.reference_denoise,
                },
            )
            if receipt.get("binding") != "explicit_v330_speaker_embedding_192d_ref_codes":
                raise TtsReferenceBindingFailure("VieNeu reference enrollment was not explicit")
        if self._loaded_profile != str(self.profile_path):
            receipt = host.request(
                "bind_profile",
                {"profile_path": str(self.profile_path), "require_speaker_embedding_size": 192},
            )
            if receipt.get("binding") != "explicit_v330_speaker_embedding_192d_ref_codes":
                raise TtsReferenceBindingFailure("VieNeu did not confirm explicit clone binding")
            self._binding_receipt = receipt
            self._loaded_profile = str(self.profile_path)
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
        decoded = _decode_batch_result(request_id, cues, raw, "vieneu-v3.3-batch")
        return TtsBatchResult(
            decoded.request_id,
            decoded.artifacts,
            decoded.failures,
            {
                **dict(decoded.metadata),
                "clone_binding": "explicit_v330_speaker_embedding_192d_ref_codes",
            },
        )

    def close(self) -> None:
        if self._host is not None:
            self._host.close()
            self._host = None
        self._loaded_profile = ""
        self._binding_receipt = None

    def _ensure_host(self, *, load_model: bool) -> EngineHostClient:
        health = self.health()
        if health.state is not HealthState.READY:
            raise TtsRuntimeUnavailable(health.summary)
        if self._host is None:
            self._host = self._host_factory(
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
        if load_model:
            self._host.request(
                "load",
                {
                    "mode": "v3turbo",
                    "max_batch_size": self.batch_size,
                    "runtime_version": "3.3.0",
                    "model_cache": str(self.data_root / "model_cache"),
                },
            )
        return self._host

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
