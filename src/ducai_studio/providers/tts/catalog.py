from __future__ import annotations

import hashlib
import json
import os
import sys
import unicodedata
import wave
from array import array
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
class VoiceDownloadFile:
    filename: str
    url: str
    size_bytes: int
    md5: str


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
    preset_name: str = ""
    preview_path: str = ""
    preview_text: str = ""
    installable: bool = False
    download_files: tuple[VoiceDownloadFile, ...] = ()

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
        if (
            self.installed
            and self.provider_id == "piper"
            and (not Path(self.model_path).is_file() or not Path(self.config_path).is_file())
        ):
            raise ValueError("Installed Piper catalog entry must own ONNX and JSON files")
        if self.provider_id == "vieneu-3.8" and not self.preset_name:
            raise ValueError("VieNeu catalog entry must identify its preset")

    @property
    def preview_available(self) -> bool:
        return bool(self.preview_path and Path(self.preview_path).is_file())

    @property
    def download_bytes(self) -> int:
        return sum(item.size_bytes for item in self.download_files)


_PIPER_REVISION = "320d5f7f7751a17ef6512d5c23863056c6a11c0f"
_PIPER_BASE = f"https://huggingface.co/rhasspy/piper-voices/resolve/{_PIPER_REVISION}"


@dataclass(frozen=True, slots=True)
class _PiperVoiceSpec:
    voice_id: str
    name: str
    source: str
    description: str
    rights_label: str
    rights_state: VoiceRightsState
    recommended: bool
    preview_text: str
    remote_directory: str
    model_size: int
    model_md5: str
    config_size: int
    config_md5: str


_PIPER_SPECS = (
    _PiperVoiceSpec(
        "vi_VN-vais1000-medium",
        "Vais1000 Medium",
        "Piper chính thức",
        "Giọng Việt rõ, cân bằng; model mặc định gọn nhẹ",
        "CC BY 4.0",
        VoiceRightsState.VERIFIED,
        True,
        "Một bản thu tốt cần rõ lời, tự nhiên và giữ được nhịp kể chuyện ổn định, "
        "để người nghe luôn tập trung vào nội dung.",
        "vi/vi_VN/vais1000/medium",
        63_201_294,
        "5e42428c4f6131f75557cf156c9c1526",
        4_860,
        "5ec9669253541d64ba97f60e422f1ad0",
    ),
    _PiperVoiceSpec(
        "vi_VN-vivos-x_low",
        "VIVOS X-Low",
        "Piper chính thức",
        "Model rất nhẹ, phù hợp máy yếu",
        "CC BY-NC-SA 4.0, chỉ phi thương mại",
        VoiceRightsState.RESTRICTED,
        False,
        "Buổi sáng trên bến cảng, từng con thuyền chậm rãi rời bờ trong làn sương mỏng.",
        "vi/vi_VN/vivos/x_low",
        27_789_413,
        "d5880d32e340f57489dcb9d4f1f7aa04",
        5_592,
        "708356072a75e33cff652f1b5d455795",
    ),
)

# Keep the historic IDs loadable for old jobs/projects, but do not publish them
# as installable UI choices until their redistribution and voice-personality
# rights have been verified. This is a compatibility contract, not a download list.
_LEGACY_PIPER_SPECS = (
    (
        "vi_VN-25hours_single-low",
        "25 Hours Low",
        "Piper ngoài",
        "Nhẹ",
        "Giấy phép dữ liệu chưa rõ",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "vi_VN-banmai-vntts",
        "Ban Mai",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-chieu-thanh",
        "Chiếu Thành",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-duy-onyx-moi",
        "Duy Onyx (mới)",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-duy-oryx",
        "Duy Oryx",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-lac-phi",
        "Lạc Phi",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-mai-phuong",
        "Mai Phương",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-minh-khang",
        "Minh Khang",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-minh-quang",
        "Minh Quang",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-manh-dung",
        "Mạnh Dũng",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-my-tam",
        "Mỹ Tâm",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
    ),
    (
        "nghitts-my-tam-real",
        "Mỹ Tâm Real",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
    ),
    (
        "nghitts-ngoc-huyen-moi",
        "Ngọc Huyền (mới)",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-ngoc-ngan",
        "Ngọc Ngạn",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
    ),
    (
        "nghitts-phuong-trang",
        "Phương Trang",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-thanh-phuong-viettel",
        "Thanh Phương Viettel",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-thien-tam",
        "Thiện Tâm",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-tran-thanh",
        "Trấn Thành",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
    ),
    (
        "nghitts-tai-an",
        "Tài An",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
    (
        "nghitts-viet-thao",
        "Việt Thảo",
        "NGHI-TTS",
        "Tên người thật",
        "Cần quyền sử dụng giọng",
        VoiceRightsState.PERMISSION_REQUIRED,
    ),
    (
        "nghitts-adam",
        "Adam",
        "NGHI-TTS",
        "Giọng cộng đồng",
        "Kiểm tra quyền giọng",
        VoiceRightsState.UNCLEAR,
    ),
)

_VIENEU_PREVIEW_TEXTS = {
    "Adam bựa": (
        "Hôm nay trời mát, chúng ta cứ thong thả kể một câu chuyện vui bên tách cà phê, "
        "rồi ngắm phố lên đèn ngoài khung cửa."
    ),
    "Trúc Ly": (
        "Mỗi sáng, khu vườn nhỏ lại đón nắng qua những tán lá xanh còn đọng sương, "
        "tiếng chim gọi nhau từ phía hàng rào."
    ),
    "Anh Khôi": (
        "Con thuyền rời bến lúc bình minh, mang theo hy vọng về một hành trình bình yên, "
        "phía xa mặt biển sáng dần dưới mây."
    ),
    "Mai Anh": (
        "Bản tin chiều nay ghi nhận giao thông ổn định và thời tiết thuận lợi trên cả nước, "
        "nhiệt độ giảm nhẹ vào lúc tối."
    ),
    "Minh Quân Pro": (
        "Một giọng đọc tự nhiên cần rõ chữ, đúng nhịp và giữ cảm xúc vừa đủ, "
        "để người nghe theo dõi thật dễ dàng."
    ),
    "Thùy Dung": (
        "Thị trường sáng nay có nhiều tín hiệu tích cực, "
        "trong khi giá năng lượng tiếp tục ổn định và sức mua đang phục hồi."
    ),
    "Thiền Tâm Đức": "Người lữ khách dừng chân bên suối, lắng nghe tiếng rừng đêm thở thật khẽ.",
    "Ngọc Huyền": (
        "Tôi thích những buổi chiều yên tĩnh, khi thành phố chậm lại sau một ngày dài, "
        "và mọi con đường dần lên ánh đèn."
    ),
    "Quang Sơn": (
        "Gió miền Trung thổi qua triền cát, mang theo vị mặn của biển và nắng, "
        "gợi nhớ những mùa hè xa quê."
    ),
    "Ngọc Trân": (
        "Ngoài hiên, tiếng mưa đều đặn làm căn phòng trở nên ấm áp và gần gũi, "
        "bên cạnh một tách trà vừa pha."
    ),
    "Minh Đức": (
        "Chương trình hôm nay cập nhật các thông tin quan trọng về kinh tế và đời sống, "
        "cùng những thay đổi đáng chú ý trong tuần."
    ),
    "Phạm Tuyên": (
        "Bạn có thể bắt đầu từ điều nhỏ nhất, rồi kiên trì tiến thêm mỗi ngày, "
        "kết quả tốt sẽ đến vào đúng lúc."
    ),
    "Thái Sơn": (
        "Đêm xuống rất nhanh, nhưng ngọn đèn trong căn nhà cuối phố vẫn sáng, "
        "như đang chờ một người trở về."
    ),
    "Xuân Vĩnh": (
        "Một chuyến đi đáng nhớ không chỉ nằm ở đích đến, mà còn ở bạn đồng hành "
        "và những điều bất ngờ nhỏ trên đường."
    ),
    "Thanh Bình": (
        "Ông lão mở cuốn sổ cũ, chậm rãi kể lại mùa hè của nhiều năm trước, "
        "khi cả làng còn chưa có điện."
    ),
    "Ngọc Linh": (
        "Cánh cửa gỗ khẽ mở, để lộ căn phòng đầy sách và mùi trà thơm, "
        "nơi người chủ vẫn ngồi chờ khách."
    ),
    "Đoan Trang": (
        "Cuối tuần này, cả nhà sẽ về quê thăm bà và cùng nấu một bữa cơm, "
        "rồi ngồi kể nhau nghe chuyện cũ."
    ),
    "Thục Đoan": (
        "Cô bé lần theo dấu chân nhỏ, đi sâu vào khu rừng phủ đầy ánh nắng, "
        "không biết ai đang đợi phía trước."
    ),
    "Minh Triết": (
        "Dự báo cho thấy nhiệt độ giảm nhẹ, người dân nên chuẩn bị áo ấm khi ra đường, "
        "đặc biệt trong khoảng sáng sớm mai."
    ),
    "Mỹ Duyên": "Từ trên đồi cao, cô nhìn thấy dòng sông uốn quanh những cánh đồng xanh.",
    "Quỳnh Anh": (
        "Ngày xưa, bên bờ hồ có một ngôi làng nhỏ luôn rộn ràng tiếng hát, "
        "và những mái nhà phủ đầy hoa trắng."
    ),
    "Đức Trí": (
        "Anh giữ tấm bản đồ trong tay, quyết định đi theo con đường ít người biết, "
        "dù trời đã bắt đầu chuyển tối."
    ),
    "Kim Thanh": "Bà khép cửa sổ, rồi ngồi bên bếp lửa kể chuyện cho lũ trẻ nghe.",
    "Adam": (
        "Buổi sớm miền Nam trong trẻo, tiếng xe hòa cùng tiếng chim trên hàng cây, "
        "người bán hàng vui vẻ chào khách quen."
    ),
    "Mạnh Dũng": (
        "Sau nhiều lần thử sức, anh hiểu rằng sự bình tĩnh luôn tạo nên khác biệt, "
        "nhất là khi mọi người đều vội vàng."
    ),
}


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


def discover_voice_preview_root(module_path: Path | None = None) -> Path:
    source = (module_path or Path(__file__)).resolve()
    for parent in (source.parent, *source.parents):
        candidate = parent / "ui" / "assets" / "voice-previews"
        if candidate.is_dir():
            return candidate
    return Path(sys.executable).resolve().parent / "ui" / "assets" / "voice-previews"


def voice_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    ascii_text = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return "-".join("".join(ch if ch.isalnum() else " " for ch in ascii_text).split())


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


def inspect_voice_preview(path: Path) -> dict[str, int | float | bool]:
    """Inspect a PCM audition without loading the whole file into memory."""

    candidate = path.expanduser().resolve()
    try:
        with wave.open(str(candidate), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frame_count = stream.getnframes()
            if (
                stream.getcomptype() != "NONE"
                or channels < 1
                or sample_width != 2
                or not 8_000 <= sample_rate <= 192_000
                or frame_count < 1
            ):
                return {"valid": False, "non_silent": False}
            non_silent = False
            remaining = frame_count
            bytes_per_frame = channels * sample_width
            while remaining > 0:
                chunk_frames = min(remaining, sample_rate)
                samples = array("h")
                raw = stream.readframes(chunk_frames)
                # ``wave`` can return a short chunk for a truncated file
                # without raising.  Do not offer a partially playable sample
                # as an immediate audition; it must fall back to managed TTS.
                if len(raw) != chunk_frames * bytes_per_frame:
                    return {"valid": False, "non_silent": False}
                samples.frombytes(raw)
                non_silent = non_silent or any(sample != 0 for sample in samples)
                remaining -= chunk_frames
    except (OSError, EOFError, wave.Error, ValueError):
        return {"valid": False, "non_silent": False}
    return {
        "valid": non_silent,
        "non_silent": non_silent,
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "frame_count": frame_count,
        "duration_seconds": frame_count / sample_rate,
    }


def valid_voice_preview(path: Path) -> bool:
    return inspect_voice_preview(path).get("valid") is True


class VoiceCatalog:
    def __init__(
        self,
        data_root: Path | None = None,
        *,
        preview_root: Path | None = None,
    ) -> None:
        self.data_root = (data_root or discover_data_root()).expanduser().resolve()
        self.voice_root = self.data_root / "piper_voices"
        self.preview_root = (preview_root or discover_voice_preview_root()).expanduser().resolve()

    def vieneu_runtime_installed(self) -> bool:
        """Return whether the immutable VieNeu 3.8.1 assets are present.

        The voice library needs an immediate installation state, not a CUDA
        health diagnosis.  A full provider health probe starts a managed
        Python process and imports torch, which is both slow on the Qt thread
        and can fail transiently while a portable venv is being relocated.
        Runtime health is still validated when synthesis actually starts.
        """

        runtime_root = self.data_root / "vieneu_3_8_runtime"
        metadata_path = runtime_root / "ducai_runtime.json"
        preset_path = (
            runtime_root
            / ".venv"
            / "Lib"
            / "site-packages"
            / "vieneu"
            / "assets"
            / "voices_v3_turbo.json"
        )
        runtime_python = runtime_root / ".venv" / "Scripts" / "python.exe"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            presets = json.loads(preset_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return False
        return (
            runtime_python.is_file()
            and str(metadata.get("version", "")) == "3.8.1"
            and isinstance(presets.get("presets"), dict)
            and bool(presets["presets"])
        )

    def entries(self) -> tuple[VoiceCatalogEntry, ...]:
        result: list[VoiceCatalogEntry] = []
        for spec in _PIPER_SPECS:
            filename = f"{spec.voice_id}.onnx"
            downloads = (
                VoiceDownloadFile(
                    filename,
                    f"{_PIPER_BASE}/{spec.remote_directory}/{filename}?download=true",
                    spec.model_size,
                    spec.model_md5,
                ),
                VoiceDownloadFile(
                    f"{filename}.json",
                    f"{_PIPER_BASE}/{spec.remote_directory}/{filename}.json?download=true",
                    spec.config_size,
                    spec.config_md5,
                ),
            )
            entry = VoiceCatalogEntry(
                spec.voice_id,
                "piper",
                spec.name,
                spec.source,
                spec.description,
                spec.rights_label,
                spec.rights_state,
                recommended=spec.recommended,
                preview_path=str(self.preview_root / f"{spec.voice_id}.wav"),
                preview_text=spec.preview_text,
                installable=True,
                download_files=downloads,
            )
            model = self.voice_root / filename
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
        for voice_id, name, source, description, rights, state in _LEGACY_PIPER_SPECS:
            entry = VoiceCatalogEntry(
                voice_id,
                "piper",
                name,
                source,
                description,
                rights,
                state,
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

    def vieneu_presets(self, *, installed: bool) -> tuple[VoiceCatalogEntry, ...]:
        preset_file = (
            self.data_root
            / "vieneu_3_8_runtime"
            / ".venv"
            / "Lib"
            / "site-packages"
            / "vieneu"
            / "assets"
            / "voices_v3_turbo.json"
        )
        try:
            payload = json.loads(preset_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        presets = payload.get("presets")
        if not isinstance(presets, dict):
            return ()
        result: list[VoiceCatalogEntry] = []
        for order, (name, raw) in enumerate(presets.items()):
            if not isinstance(raw, dict):
                continue
            description = str(raw.get("description") or "Giọng dựng sẵn VieNeu")
            preview_text = str(
                raw.get("preview_text")
                or _VIENEU_PREVIEW_TEXTS.get(
                    name,
                    f"Đây là đoạn nghe thử ngắn để bạn nhận biết chất giọng {name}.",
                )
            )
            result.append(
                VoiceCatalogEntry(
                    voice_id=f"preset:{name}",
                    provider_id="vieneu-3.8",
                    name=name,
                    source="VieNeu-TTS v3 Turbo 3.8.1",
                    description=description,
                    rights_label="CC BY-NC 4.0, chỉ phi thương mại",
                    rights_state=VoiceRightsState.RESTRICTED,
                    installed=installed,
                    model_fingerprint=f"vieneu-3.8.1-preset-{voice_slug(name)}",
                    recommended=bool(raw.get("featured")) or order < 4,
                    preset_name=name,
                    preview_path=str(self.preview_root / f"vieneu-{voice_slug(name)}.wav"),
                    preview_text=preview_text,
                )
            )
        return tuple(result)

    def vieneu_profiles(self) -> tuple[Path, ...]:
        """Return locally complete clone profiles in stable identity order."""

        root = self.data_root / "vieneu_profiles"
        return tuple(
            profile.resolve()
            for profile in sorted(root.glob("*.npz"), key=lambda item: item.name.casefold())
            if profile.with_suffix(".json").is_file()
        )

    def get(self, voice_id: str, *, require_installed: bool = False) -> VoiceCatalogEntry:
        entry = next((item for item in self.entries() if item.voice_id == voice_id), None)
        if entry is None:
            raise LookupError(f"Unknown Piper voice: {voice_id}")
        if require_installed and not entry.installed:
            raise FileNotFoundError(f"Piper voice is not installed: {voice_id}")
        return entry

    def installed(self) -> tuple[VoiceCatalogEntry, ...]:
        return tuple(item for item in self.entries() if item.installed)
