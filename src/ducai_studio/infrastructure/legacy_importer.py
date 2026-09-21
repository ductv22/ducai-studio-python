from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from ducai_studio.application.effects_commands import (
    DEFAULT_BLUR_ID,
    DEFAULT_LOGO_ID,
    EffectsEditor,
)
from ducai_studio.application.media_commands import MediaImportService
from ducai_studio.domain.jobs import redact_secret_fields
from ducai_studio.domain.project import ProjectSnapshot, SubtitleCue
from ducai_studio.domain.timebase import TimeRange

_MEDIA_EXTENSIONS = {
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
_SUBTITLE_EXTENSIONS = {".srt"}
_CACHE_EXTENSIONS = {".bin", ".json", ".mp4", ".npy", ".npz", ".png", ".srt", ".txt", ".wav"}
_CACHE_SECRET_TOKENS = {"api-key", "apikey", "cookie", "credential", "secret", "token"}
_SETTING_KEYS = {
    "content_speed_percent",
    "export_output",
    "export_preset",
    "font_file",
    "sub_color",
    "sub_font_size",
    "sub_margin_bottom",
    "sub_outline",
    "tts_auto_fit",
    "tts_borrow_gap_ms",
    "tts_end_pad_ms",
    "tts_fit_max_rate",
    "tts_parallel_workers",
    "tts_preserve_words",
    "tts_provider",
    "tts_rate",
    "tts_reduce_original",
    "tts_start_offset_ms",
    "tts_strict_video_sync",
    "tts_trim_silence",
    "tts_trim_to_srt",
    "tts_use_cache",
    "tts_voice",
    "tts_voice_lang",
    "tts_volume_percent",
}


@dataclass(frozen=True, slots=True)
class LegacyMigrationReport:
    source_path: str
    source_sha256: str
    source_unchanged: bool
    dry_run: bool
    imported_media: tuple[str, ...] = ()
    imported_subtitles: tuple[str, ...] = ()
    imported_caption_cues: int = 0
    imported_settings: dict[str, Any] | None = None
    skipped: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    cache_decisions: tuple[dict[str, str], ...] = ()
    imported_caches: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LegacyProjectImporter:
    """Read 0.1.99 JSON projects without mutating or trusting legacy state."""

    def __init__(self, media_imports: MediaImportService) -> None:
        self.media_imports = media_imports

    def inspect(self, source_path: str | Path) -> LegacyMigrationReport:
        path, payload, digest = self._read_source(source_path)
        media, subtitles = self._discover_paths(path, payload)
        captions = _caption_rows(payload.get("captions", []))
        settings = _safe_settings(payload)
        skipped = self._skipped_sections(payload)
        cache_decisions = self._cache_decisions(path, payload)
        return LegacyMigrationReport(
            str(path),
            digest,
            self._digest(path) == digest,
            True,
            tuple(str(item) for item in media),
            tuple(str(item) for item in subtitles),
            len(captions),
            settings,
            skipped,
            cache_decisions=cache_decisions,
        )

    def import_project(
        self,
        source_path: str | Path,
        snapshot: ProjectSnapshot,
        *,
        cache_import_root: str | Path | None = None,
    ) -> tuple[ProjectSnapshot, LegacyMigrationReport]:
        path, payload, digest = self._read_source(source_path)
        media, subtitles = self._discover_paths(path, payload)
        imported_media: list[str] = []
        imported_subtitles: list[str] = []
        warnings: list[str] = []
        result = snapshot
        for candidate in media:
            try:
                if _is_voice_item(payload, candidate):
                    result = self.media_imports.import_voice_media(result, candidate)
                else:
                    result = self.media_imports.import_media(result, candidate)
                imported_media.append(str(candidate))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                warnings.append(f"Không nhập được media {candidate.name}: {type(exc).__name__}")
        for candidate in subtitles:
            language = "vi" if _looks_vietnamese_filename(candidate) else "zh"
            try:
                result = self.media_imports.import_srt(result, candidate, language=language)
                imported_subtitles.append(str(candidate))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                warnings.append(f"Không nhập được SRT {candidate.name}: {type(exc).__name__}")

        captions = _caption_rows(payload.get("captions", []))
        if captions and not result.source_cues:
            result = _apply_caption_rows(result, captions)
        result = self._apply_overlays(result, payload, warnings)
        unchanged = self._digest(path) == digest
        if not unchanged:
            raise RuntimeError("Legacy source changed while it was being imported")
        cache_decisions = self._cache_decisions(path, payload)
        imported_caches: tuple[str, ...] = ()
        if cache_import_root is not None:
            imported_caches, cache_decisions = self._import_compatible_caches(
                path,
                payload,
                Path(cache_import_root).expanduser().resolve(),
            )
        report = LegacyMigrationReport(
            str(path),
            digest,
            True,
            False,
            tuple(imported_media),
            tuple(imported_subtitles),
            len(captions) if captions and not imported_subtitles else 0,
            _safe_settings(payload),
            self._skipped_sections(payload),
            tuple(warnings),
            cache_decisions,
            imported_caches,
        )
        return result, report

    @staticmethod
    def _read_source(source_path: str | Path) -> tuple[Path, dict[str, Any], str]:
        path = Path(source_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError(path)
        raw = path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Legacy project must be valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise TypeError("Legacy project root must be an object")
        return path, payload, sha256(raw).hexdigest()

    @staticmethod
    def _digest(path: Path) -> str:
        return sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _discover_paths(
        source_path: Path, payload: dict[str, Any]
    ) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        values: list[str] = []
        for section in ("media_items", "timeline", "audio_tracks"):
            values.extend(_path_values(payload.get(section, [])))
        media: list[Path] = []
        subtitles: list[Path] = []
        seen: set[str] = set()
        for value in values:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = source_path.parent / candidate
            candidate = candidate.resolve()
            key = str(candidate).casefold()
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            if candidate.suffix.casefold() in _SUBTITLE_EXTENSIONS:
                subtitles.append(candidate)
            elif candidate.suffix.casefold() in _MEDIA_EXTENSIONS:
                media.append(candidate)
        return tuple(media), tuple(subtitles)

    @staticmethod
    def _skipped_sections(payload: dict[str, Any]) -> tuple[str, ...]:
        skipped: list[str] = []
        for key in ("text_items", "media_workspaces"):
            if payload.get(key):
                skipped.append(f"{key}: không có hợp đồng tương thích trực tiếp")
        secret_redacted = redact_secret_fields(payload)
        if secret_redacted != payload:
            skipped.append("credentials/secrets: luôn bỏ qua; dùng Windows Credential Manager")
        return tuple(skipped)

    @staticmethod
    def _cache_decisions(
        source_path: Path, payload: dict[str, Any]
    ) -> tuple[dict[str, str], ...]:
        candidates = _path_values(payload.get("cache_paths", []))
        decisions: list[dict[str, str]] = []
        for raw in candidates:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = source_path.parent / path
            identity = path / "identity.json"
            reason = "bỏ qua: thiếu identity.json tương thích"
            if identity.is_file():
                try:
                    value = json.loads(identity.read_text(encoding="utf-8"))
                    required = {
                        "schema",
                        "kind",
                        "input_fingerprint",
                        "provider_id",
                        "provider_version",
                        "model_id",
                        "settings_fingerprint",
                    }
                    if (
                        isinstance(value, dict)
                        and value.get("schema") == "ducai-studio-2-cache-v1"
                        and required <= value.keys()
                    ):
                        reason = "tương thích: có thể nhập bằng cache manager"
                except (OSError, TypeError, json.JSONDecodeError):
                    pass
            decisions.append({"path": str(path.resolve()), "decision": reason})
        return tuple(decisions)

    @classmethod
    def _import_compatible_caches(
        cls,
        source_path: Path,
        payload: dict[str, Any],
        destination_root: Path,
    ) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
        imported: list[str] = []
        decisions: list[dict[str, str]] = []
        for raw in _path_values(payload.get("cache_paths", [])):
            source = Path(raw).expanduser()
            if not source.is_absolute():
                source = source_path.parent / source
            source = source.resolve()
            identity = cls._compatible_cache_identity(source)
            if identity is None:
                decisions.append(
                    {
                        "path": str(source),
                        "decision": "bỏ qua: thiếu identity.json tương thích",
                    }
                )
                continue
            unsafe = _unsafe_cache_entry(source)
            if unsafe is not None:
                decisions.append(
                    {
                        "path": str(source),
                        "decision": f"bỏ qua: cache chứa file không an toàn ({unsafe.name})",
                    }
                )
                continue
            kind = str(identity["kind"]).strip().casefold()
            cache_id = sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            target = destination_root / kind / cache_id
            target.parent.mkdir(parents=True, exist_ok=True)
            source_digest = _cache_tree_digest(source)
            if target.exists():
                if (
                    not target.is_dir()
                    or _unsafe_cache_entry(target) is not None
                    or _cache_tree_digest(target) != source_digest
                ):
                    decisions.append(
                        {
                            "path": str(source),
                            "decision": "bỏ qua: đích cache tồn tại nhưng không trùng khớp",
                        }
                    )
                    continue
                imported.append(str(target))
                decisions.append(
                    {
                        "path": str(source),
                        "decision": f"đã có cache trùng khớp: {target}",
                    }
                )
                continue
            staging = target.parent / f".{target.name}.{uuid4().hex}.tmp"
            try:
                shutil.copytree(source, staging)
                if (
                    _unsafe_cache_entry(staging) is not None
                    or _cache_tree_digest(staging) != source_digest
                ):
                    raise RuntimeError("Legacy cache staging verification failed")
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            imported.append(str(target))
            decisions.append(
                {
                    "path": str(source),
                    "decision": f"đã nhập một chiều: {target}",
                }
            )
        return tuple(imported), tuple(decisions)

    @staticmethod
    def _compatible_cache_identity(path: Path) -> dict[str, Any] | None:
        identity_path = path / "identity.json"
        if not identity_path.is_file():
            return None
        try:
            value = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            return None
        required = {
            "schema",
            "kind",
            "input_fingerprint",
            "provider_id",
            "provider_version",
            "model_id",
            "settings_fingerprint",
        }
        if (
            not isinstance(value, dict)
            or value.get("schema") != "ducai-studio-2-cache-v1"
            or not required <= value.keys()
            or not all(isinstance(value[key], str) and value[key].strip() for key in required)
            or not str(value["kind"]).replace("-", "").replace("_", "").isalnum()
        ):
            return None
        return value

    @staticmethod
    def _apply_overlays(
        snapshot: ProjectSnapshot,
        payload: dict[str, Any],
        warnings: list[str],
    ) -> ProjectSnapshot:
        if not snapshot.overlays:
            return snapshot
        editor = EffectsEditor(snapshot)
        mappings = (
            (DEFAULT_BLUR_ID, _first_object(payload.get("blur_regions"))),
            (DEFAULT_LOGO_ID, _first_object(payload.get("logo_items"))),
        )
        for overlay_id, raw in mappings:
            if raw is None:
                continue
            try:
                current = next(item for item in editor.snapshot.overlays if item.id == overlay_id)
                values = _normalized_geometry(raw)
                editor.update_overlay(
                    overlay_id,
                    x=values.get("x", current.x),
                    y=values.get("y", current.y),
                    width=values.get("width", current.width),
                    height=values.get("height", current.height),
                    enabled=bool(raw.get("enabled", current.enabled)),
                    opacity=float(raw.get("opacity", current.opacity)),
                    blur_radius=int(raw.get("blur_radius", current.blur_radius)),
                )
            except (LookupError, TypeError, ValueError):
                warnings.append(f"Overlay legacy {overlay_id} không hợp lệ và đã được bỏ qua")
        return editor.snapshot


def _path_values(value: object) -> list[str]:
    result: list[str] = []
    if isinstance(value, list):
        for item in value:
            result.extend(_path_values(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in {
                "path",
                "file",
                "file_path",
                "filepath",
                "media_path",
                "source",
                "src",
            } and isinstance(item, str):
                result.append(item)
            elif isinstance(item, (dict, list)):
                result.extend(_path_values(item))
    elif isinstance(value, str):
        result.append(value)
    return result


def _caption_rows(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _time_us(row: dict[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        value = row.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if name.endswith("_us"):
            return int(value)
        if name.endswith("_ms"):
            return int(round(value * 1_000))
        return int(round(value * 1_000_000))
    return None


def _apply_caption_rows(
    snapshot: ProjectSnapshot, rows: tuple[dict[str, Any], ...]
) -> ProjectSnapshot:
    source: list[SubtitleCue] = []
    translated: list[SubtitleCue] = []
    for row in rows:
        start = _time_us(row, ("start_us", "start_ms", "start", "start_time"))
        end = _time_us(row, ("end_us", "end_ms", "end", "end_time"))
        text = next(
            (
                str(row[key]).strip()
                for key in ("text", "content", "subtitle", "zh", "source_text")
                if isinstance(row.get(key), str) and str(row[key]).strip()
            ),
            "",
        )
        if start is None or end is None or end <= start or not text:
            continue
        cue_id = f"legacy-cue-{len(source) + 1}"
        cue = SubtitleCue(
            cue_id,
            len(source) + 1,
            TimeRange(start, end),
            text,
            "zh",
            source="legacy_caption",
        )
        source.append(cue)
        vi = next(
            (
                str(row[key]).strip()
                for key in ("translated_text", "translation", "vi", "vi_text")
                if isinstance(row.get(key), str) and str(row[key]).strip()
            ),
            "",
        )
        if vi:
            translated.append(replace(cue, text=vi, language="vi", source="legacy_translation"))
    if not source:
        return snapshot
    if translated and len(translated) != len(source):
        translated = []
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        source_cues=tuple(source),
        translated_cues=tuple(translated),
    )


def _safe_settings(payload: dict[str, Any]) -> dict[str, Any]:
    values = {key: payload[key] for key in sorted(_SETTING_KEYS & payload.keys())}
    redacted = redact_secret_fields(values)
    return redacted if isinstance(redacted, dict) else {}


def _safe_cache_file(path: Path) -> bool:
    name = path.name.casefold()
    if path.suffix.casefold() not in _CACHE_EXTENSIONS or any(
        token in name for token in _CACHE_SECRET_TOKENS
    ):
        return False
    if path.suffix.casefold() != ".json":
        return True
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return redact_secret_fields(value) == value


def _unsafe_cache_entry(root: Path) -> Path | None:
    return next(
        (
            item
            for item in root.rglob("*")
            if item.is_symlink() or (item.is_file() and not _safe_cache_file(item))
        ),
        None,
    )


def _cache_tree_digest(root: Path) -> str:
    digest = sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _is_voice_item(payload: dict[str, Any], path: Path) -> bool:
    name = path.name.casefold()
    if any(token in name for token in ("voiceover", "voice_clone", "_tts", "lồng tiếng")):
        return True
    audio_tracks = payload.get("audio_tracks", [])
    if not isinstance(audio_tracks, list):
        return False
    for item in audio_tracks:
        if not isinstance(item, dict) or str(path) not in _path_values(item):
            continue
        role = " ".join(str(item.get(key, "")) for key in ("kind", "role", "type")).casefold()
        return any(token in role for token in ("voice", "tts", "dub"))
    return False


def _looks_vietnamese_filename(path: Path) -> bool:
    name = path.stem.casefold()
    return any(token in name for token in ("_vi", "codex_vi", "vietnam", "dịch"))


def _first_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), None)
    return None


def _normalized_geometry(raw: dict[str, Any]) -> dict[str, float]:
    aliases = {
        "x": ("x", "left"),
        "y": ("y", "top"),
        "width": ("width", "w"),
        "height": ("height", "h"),
    }
    result: dict[str, float] = {}
    for target, names in aliases.items():
        value = next((raw.get(name) for name in names if name in raw), None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if not 0.0 <= number <= 1.0:
            raise ValueError("Legacy overlay geometry must already be normalized")
        result[target] = number
    return result
