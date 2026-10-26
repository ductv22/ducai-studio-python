"""Acoustic labels are separate from subtitle roles and selected TTS voices."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

CASTING_LABELS = frozenset({"male", "female", "unknown"})
NO_SPEECH_TTS_SKIP_REASON = "dialogue_casting_no_speech"


@dataclass(frozen=True, slots=True)
class CastingVoice:
    provider_id: str
    voice_id: str
    profile_path: str = ""
    reference_audio_path: str = ""
    reference_text: str = ""
    reference_denoise: bool = False

    def __post_init__(self) -> None:
        if self.provider_id not in {"piper", "vieneu-3.8"} or not self.voice_id.strip():
            raise ValueError("Giọng phân vai không hợp lệ")
        if any(not isinstance(getattr(self, name), str) for name in (
            "provider_id", "voice_id", "profile_path", "reference_audio_path", "reference_text"
        )) or type(self.reference_denoise) is not bool:
            raise ValueError("Cấu hình giọng phân vai không hợp lệ")


@dataclass(frozen=True, slots=True)
class CastingResult:
    cue_id: str
    audio_key: str
    label: str
    reason: str = ""
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.cue_id or not self.audio_key or self.label not in CASTING_LABELS:
            raise ValueError("Kết quả phân vai không hợp lệ")
        if not isinstance(self.confidence, (int, float)) or not 0 <= self.confidence <= 1:
            raise ValueError("Độ tin cậy phân vai không hợp lệ")


@dataclass(frozen=True, slots=True)
class CastingOverride:
    cue_id: str
    audio_key: str
    label: str

    def __post_init__(self) -> None:
        if not self.cue_id or not self.audio_key or self.label not in CASTING_LABELS:
            raise ValueError("Nhãn phân vai thủ công không hợp lệ")


@dataclass(frozen=True, slots=True)
class DialogueCastingState:
    enabled: bool = False
    male_voice: CastingVoice | None = None
    female_voice: CastingVoice | None = None
    fallback_voice: CastingVoice | None = None
    results: tuple[CastingResult, ...] = ()
    overrides: tuple[CastingOverride, ...] = ()
    analyzer_id: str = ""

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or not isinstance(self.analyzer_id, str):
            raise ValueError("Cấu hình phân vai không hợp lệ")
        for rows in (self.results, self.overrides):
            if len({row.cue_id for row in rows}) != len(rows):
                raise ValueError("Nhãn phân vai bị lặp mã câu")


def decode_casting(value: Any) -> DialogueCastingState | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("dialogue_casting phải là một đối tượng")
    known = {field.name for field in fields(DialogueCastingState)}
    if set(value) - known:
        raise ValueError("Trường phân vai không được hỗ trợ")
    raw = dict(value)
    for name in ("male_voice", "female_voice", "fallback_voice"):
        if raw.get(name) is not None:
            raw[name] = CastingVoice(**raw[name])
    for name, cls in (("results", CastingResult), ("overrides", CastingOverride)):
        if name in raw:
            if not isinstance(raw[name], list):
                raise ValueError("Danh sách phân vai không hợp lệ")
            raw[name] = tuple(cls(**item) for item in raw[name])
    return DialogueCastingState(**raw)
