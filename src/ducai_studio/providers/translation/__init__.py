"""Replaceable translation transports for DUCAI STUDIO."""

from .codex_cli import CodexCliProvider
from .contracts import (
    AuthenticationRequired,
    ChallengeRequired,
    ProviderAccessBlocked,
    ProviderResponse,
    QuotaExhausted,
    ResponseFormatFailure,
    TransientProviderFailure,
    TranslationProvider,
    TranslationRequest,
)
from .openai_compatible import (
    OPENAI_COMPATIBLE_PRESETS,
    OPENAI_COMPATIBLE_PROVIDER_IDS,
    OpenAICompatiblePreset,
    OpenAICompatibleProvider,
    preset_for,
    validate_base_url,
)

__all__ = [
    "CodexCliProvider",
    "AuthenticationRequired",
    "ChallengeRequired",
    "ProviderAccessBlocked",
    "ProviderResponse",
    "QuotaExhausted",
    "ResponseFormatFailure",
    "TransientProviderFailure",
    "TranslationProvider",
    "TranslationRequest",
    "OPENAI_COMPATIBLE_PRESETS",
    "OPENAI_COMPATIBLE_PROVIDER_IDS",
    "OpenAICompatiblePreset",
    "OpenAICompatibleProvider",
    "preset_for",
    "validate_base_url",
]
