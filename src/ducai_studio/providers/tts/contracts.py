from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ducai_studio.domain.jobs import JsonValue, freeze_json
from ducai_studio.providers.contracts import Provider


@dataclass(frozen=True, slots=True)
class TtsCueRequest:
    cue_id: str
    index: int
    start_us: int
    end_us: int
    text: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        if not self.cue_id.strip() or not self.request_fingerprint.strip():
            raise ValueError("TTS cue identity and fingerprint are required")
        if type(self.index) is not int or self.index <= 0:
            raise ValueError("TTS cue index must be a positive integer")
        if (
            type(self.start_us) is not int
            or type(self.end_us) is not int
            or self.start_us < 0
            or self.end_us <= self.start_us
        ):
            raise ValueError("TTS cue must have an ordered positive time range")
        if not self.text.strip():
            raise ValueError("TTS cue text cannot be empty")

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us


@dataclass(frozen=True, slots=True)
class TtsCueArtifact:
    cue_id: str
    request_fingerprint: str
    wav_path: Path
    sample_rate: int
    duration_us: int
    synthesis_mode: str
    qa: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        path = self.wav_path.expanduser().resolve()
        if not self.cue_id.strip() or not self.request_fingerprint.strip():
            raise ValueError("TTS artifact identity and fingerprint are required")
        if type(self.sample_rate) is not int or not 8_000 <= self.sample_rate <= 192_000:
            raise ValueError("TTS artifact sample rate is invalid")
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("TTS artifact duration must be positive")
        if not self.synthesis_mode.strip():
            raise ValueError("TTS artifact synthesis mode is required")
        object.__setattr__(self, "wav_path", path)
        object.__setattr__(self, "qa", freeze_json(dict(self.qa)))


@dataclass(frozen=True, slots=True)
class TtsCueFailure:
    cue_id: str
    code: str
    retryable: bool
    details: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.cue_id.strip() or not self.code.strip():
            raise ValueError("TTS failure cue id and code are required")
        object.__setattr__(self, "details", freeze_json(dict(self.details)))


@dataclass(frozen=True, slots=True)
class TtsBatchResult:
    request_id: str
    artifacts: tuple[TtsCueArtifact, ...]
    failures: tuple[TtsCueFailure, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("TTS batch request id is required")
        ids = [item.cue_id for item in (*self.artifacts, *self.failures)]
        if len(ids) != len(set(ids)):
            raise ValueError("TTS batch cannot return duplicate cue ids")
        object.__setattr__(self, "metadata", freeze_json(dict(self.metadata)))


class TtsProviderFailure(RuntimeError):
    code = "tts_provider_failure"
    retryable = False

    def __init__(self, message: str, *, cue_id: str | None = None) -> None:
        super().__init__(message)
        self.cue_id = cue_id


class TtsRuntimeUnavailable(TtsProviderFailure):
    code = "tts_runtime_unavailable"


class TtsReferenceBindingFailure(TtsProviderFailure):
    code = "tts_reference_binding_failed"


class TtsTransientFailure(TtsProviderFailure):
    code = "tts_transient_failure"
    retryable = True


@runtime_checkable
class TtsProvider(Provider, Protocol):
    model_id: str

    def preview(
        self,
        *,
        text: str,
        voice_id: str,
        output_path: Path,
        speed: float = 1.0,
    ) -> TtsCueArtifact: ...

    def synthesize_batch(
        self,
        *,
        request_id: str,
        cues: Sequence[TtsCueRequest],
        voice_id: str,
        output_dir: Path,
        speed: float = 1.0,
        batch_size: int = 1,
    ) -> TtsBatchResult: ...

    def close(self) -> None: ...
