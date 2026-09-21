from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from ducai_studio.infrastructure.portable_runtime import portable_data_root


class VoiceRightsState(StrEnum):
    VERIFIED = "verified"
    RESTRICTED = "restricted"
    UNCLEAR = "unclear"
    PERMISSION_REQUIRED = "permission_required"


@dataclass(frozen=True, slots=True)
class VoiceCatalogEntry:
    voice_id: str
    provider_id: str
    name: str
    source: str
    description: str
    rights_label: str
    rights_state: VoiceRightsState
    installed: bool = False
    model_path: str = ""
    config_path: str = ""
    model_fingerprint: str = ""
    recommended: bool = False

    def __post_init__(self) -> None:
        required = (
            self.voice_id,
            self.provider_id,
            self.name,
            self.source,
            self.rights_label,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Voice catalog identity, source and rights are required")
        if self.installed and (
            not Path(self.model_path).is_file() or not Path(self.config_path).is_file()
        ):
            raise ValueError("Installed Piper catalog entry must own ONNX and JSON files")


_VOICE_SPECS = (
    (
        "vi_VN-vais1000-medium",
        "Vais1000 Medium",
        "Piper",
        "Rõ, cân bằng",
        "CC BY 4.0",
        VoiceRightsState.VERIFIED,
        True,
    ),
    (
        "vi_VN-25hours_single-low",
        "25 Hours Low",
        "Piper ngoài",
        "Nhẹ",
        "Giấy phép dữ liệu chưa rõ",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "vi_VN-vivos-x_low",
        "VIVOS X-Low",
        "Piper ngoài",
        "Siêu nhẹ",
        "Chỉ phi thương mại",
        VoiceRightsState.RESTRICTED,
        False,
    ),
    (
        "vi_VN-banmai-vntts",
        "Ban Mai",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-chieu-thanh",
        "Chiếu Thành",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-duy-onyx-moi",
        "Duy Onyx (mới)",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-duy-oryx",
        "Duy Oryx",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-lac-phi",
        "Lạc Phi",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-mai-phuong",
        "Mai Phương",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-minh-khang",
        "Minh Khang",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-minh-quang",
        "Minh Quang",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-manh-dung",
        "Mạnh Dũng",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-my-tam",
        "Mỹ Tâm",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
        False,
    ),
    (
        "nghitts-my-tam-real",
        "Mỹ Tâm Real",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
        False,
    ),
    (
        "nghitts-ngoc-huyen-moi",
        "Ngọc Huyền (mới)",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-ngoc-ngan",
        "Ngọc Ngạn",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
        False,
    ),
    (
        "nghitts-phuong-trang",
        "Phương Trang",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-thanh-phuong-viettel",
        "Thanh Phương Viettel",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-thien-tam",
        "Thiện Tâm",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-tran-thanh",
        "Trấn Thành",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
        False,
    ),
    (
        "nghitts-tai-an",
        "Tài An",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
    (
        "nghitts-viet-thao",
        "Việt Thảo",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
        False,
    ),
    (
        "nghitts-adam",
        "Adam",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
        False,
    ),
)


def discover_data_root(
    anchor: Path | None = None,
    *,
    executable_path: str | Path | None = None,
) -> Path:
    configured = os.environ.get("DUCAI_STUDIO_DATA_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    bundled = portable_data_root(executable_path or sys.executable)
    if bundled is not None:
        return bundled
    start = (anchor or Path(__file__)).resolve()
    for parent in (start.parent, *start.parents):
        if parent.name == "create-a-scheduled-task-called-weekday":
            return (parent / "outputs" / "DUCAI_STUDIO_DATA").resolve()
    return (Path.home() / "AppData" / "Local" / "DUCAI STUDIO" / "DUCAI_STUDIO_DATA").resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_piper_config(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sample_rate = int((payload.get("audio") or {}).get("sample_rate") or 0)
        return 8_000 <= sample_rate <= 192_000 and isinstance(payload.get("phoneme_id_map"), dict)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


class VoiceCatalog:
    def __init__(self, data_root: Path | None = None) -> None:
        self.data_root = (data_root or discover_data_root()).expanduser().resolve()
        self.voice_root = self.data_root / "piper_voices"

    def entries(self) -> tuple[VoiceCatalogEntry, ...]:
        result: list[VoiceCatalogEntry] = []
        for voice_id, name, source, description, rights, state, recommended in _VOICE_SPECS:
            entry = VoiceCatalogEntry(
                voice_id,
                "piper",
                name,
                source,
                description,
                rights,
                state,
                recommended=recommended,
            )
            model = self.voice_root / f"{voice_id}.onnx"
            config = model.with_suffix(model.suffix + ".json")
            if model.is_file() and config.is_file() and _validate_piper_config(config):
                stat = model.stat()
                identity = hashlib.sha256(
                    (
                        f"{model.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
                        f"{_sha256_file(config)}"
                    ).encode()
                ).hexdigest()
                entry = replace(
                    entry,
                    installed=True,
                    model_path=str(model.resolve()),
                    config_path=str(config.resolve()),
                    model_fingerprint=identity,
                )
            result.append(entry)
        return tuple(result)

    def get(self, voice_id: str, *, require_installed: bool = False) -> VoiceCatalogEntry:
        entry = next((item for item in self.entries() if item.voice_id == voice_id), None)
        if entry is None:
            raise LookupError(f"Unknown Piper voice: {voice_id}")
        if require_installed and not entry.installed:
            raise FileNotFoundError(f"Piper voice is not installed: {voice_id}")
        return entry

    def installed(self) -> tuple[VoiceCatalogEntry, ...]:
        return tuple(item for item in self.entries() if item.installed)
