from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ducai_studio.infrastructure.runtime_program import runtime_program_path
from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)

from .catalog import VoiceCatalog, discover_data_root
from .contracts import (
    TtsBatchResult,
    TtsCueArtifact,
    TtsCueFailure,
    TtsCueRequest,
    TtsRuntimeUnavailable,
)
from .host_client import EngineHostClient, EngineHostError


def _default_host_script() -> Path:
    source_path = Path(__file__).parents[2] / "engine_hosts" / "tts_engine_host.py"
    release_path = runtime_program_path().parent / "engine_hosts" / "tts_engine_host.py"
    return source_path if source_path.is_file() else release_path


class PiperTtsProvider:
    kind = ProviderKind.VOICE
    provider_id = "piper"
    model_id = "local-onnx-catalog"

    def __init__(
        self,
        *,
        runtime_python: Path | None = None,
        data_root: Path | None = None,
        host_script: Path | None = None,
        timeout_seconds: float = 300,
        host_factory: Callable[..., EngineHostClient] = EngineHostClient,
    ) -> None:
        self.data_root = (data_root or discover_data_root()).expanduser().resolve()
        self.runtime_python = (
            (
                runtime_python
                or self.data_root / "piper_runtime" / ".venv" / "Scripts" / "python.exe"
            )
            .expanduser()
            .resolve()
        )
        self.host_script = (host_script or _default_host_script()).expanduser().resolve()
        self.catalog = VoiceCatalog(self.data_root)
        self.timeout_seconds = float(timeout_seconds)
        self._host_factory = host_factory
        self._host: EngineHostClient | None = None
        self._loaded_voice = ""

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {
                "batch_transport": True,
                "clone": False,
                "offline": True,
                "preview": True,
                "persistent_model": True,
                "installed_voice_count": len(self.catalog.installed()),
            }
        )

    def health(self) -> ProviderHealth:
        runtime_ready = self.runtime_python.is_file()
        package_ready = (
            runtime_ready
            and (self.runtime_python.parents[1] / "Lib" / "site-packages" / "piper").is_dir()
        )
        host_ready = self.host_script.is_file()
        installed = len(self.catalog.installed())
        ready = package_ready and host_ready and installed > 0
        return ProviderHealth(
            self.provider_id,
            HealthState.READY if ready else HealthState.UNAVAILABLE,
            datetime.now(UTC).isoformat(),
            (
                f"Piper sẵn sàng với {installed} giọng cục bộ"
                if ready
                else "Runtime Piper, engine host hoặc model giọng chưa sẵn sàng"
            ),
            {
                "runtime_python": str(self.runtime_python),
                "host_ready": host_ready,
                "package_ready": package_ready,
                "installed_voice_count": installed,
            },
        )

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
            code = result.failures[0].code if result.failures else "piper_preview_empty"
            raise TtsRuntimeUnavailable(code)
        artifact = result.artifacts[0]
        if artifact.wav_path != output_path.expanduser().resolve():
            artifact.wav_path.replace(output_path.expanduser().resolve())
            artifact = TtsCueArtifact(
                artifact.cue_id,
                artifact.request_fingerprint,
                output_path,
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
        if not 0.55 <= float(speed) <= 1.85:
            raise ValueError("Piper synthesis speed must be between 0.55 and 1.85")
        entry = self.catalog.get(voice_id, require_installed=True)
        host = self._ensure_host()
        if self._loaded_voice != voice_id:
            host.request(
                "load",
                {
                    "model_path": entry.model_path,
                    "config_path": entry.config_path,
                    "model_fingerprint": entry.model_fingerprint,
                },
            )
            self._loaded_voice = voice_id
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
                {"jobs": jobs, "speed": float(speed), "batch_size": max(1, int(batch_size))},
            )
        except EngineHostError as exc:
            return TtsBatchResult(
                request_id,
                (),
                tuple(TtsCueFailure(cue.cue_id, exc.code, True) for cue in cues),
            )
        return _decode_batch_result(request_id, cues, raw, "piper-persistent")

    def close(self) -> None:
        if self._host is not None:
            self._host.close()
            self._host = None
        self._loaded_voice = ""

    def _ensure_host(self) -> EngineHostClient:
        if self.health().state is not HealthState.READY:
            raise TtsRuntimeUnavailable(self.health().summary)
        if self._host is None:
            self._host = self._host_factory(
                python_executable=self.runtime_python,
                host_script=self.host_script,
                engine="piper",
                timeout_seconds=self.timeout_seconds,
            )
            self._host.start()
        return self._host


def _decode_batch_result(
    request_id: str,
    cues: Sequence[TtsCueRequest],
    raw: dict,
    mode: str,
) -> TtsBatchResult:
    raw_items = raw.get("items", [])
    if not isinstance(raw_items, list):
        raise TtsRuntimeUnavailable("TTS host returned invalid item list")
    by_id = {cue.cue_id: cue for cue in cues}
    artifacts: list[TtsCueArtifact] = []
    failures: list[TtsCueFailure] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict) or not isinstance(item.get("cue_id"), str):
            raise TtsRuntimeUnavailable("TTS host returned an invalid cue item")
        cue_id = item["cue_id"]
        cue = by_id.get(cue_id)
        if cue is None or cue_id in seen:
            raise TtsRuntimeUnavailable("TTS host returned unexpected or duplicate cue id")
        seen.add(cue_id)
        if item.get("ok") is not True:
            failures.append(TtsCueFailure(cue_id, str(item.get("code", "tts_cue_failed")), True))
            continue
        artifacts.append(
            TtsCueArtifact(
                cue_id,
                cue.request_fingerprint,
                Path(str(item["output_path"])),
                int(item["sample_rate"]),
                int(item["duration_us"]),
                mode,
                {"provider_generated": True},
            )
        )
    for cue in cues:
        if cue.cue_id not in seen:
            failures.append(TtsCueFailure(cue.cue_id, "tts_host_missing_cue", True))
    return TtsBatchResult(
        request_id,
        tuple(artifacts),
        tuple(failures),
        {"engine_batch_size": int(raw.get("batch_size", 1) or 1)},
    )
