from __future__ import annotations

import json
import zlib
from dataclasses import asdict
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    Overlay,
    OverlayKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineEditState,
    TimelineTrack,
    TrackKind,
    TranslationPreferences,
    TranslationStylePreset,
    VoiceConformState,
    normalize_legacy_subtitle_lanes,
)
from ducai_studio.domain.timebase import SpeedRatio, TimeRange
from ducai_studio.domain.voice_conform import decode_voice_conform_plan

from .database import Database
from .job_repository import utc_now_text

PROJECT_SNAPSHOT_ENCODING_JSON = "json-v1"
PROJECT_SNAPSHOT_ENCODING_ZLIB = "zlib-json-v1"
PROJECT_SNAPSHOT_ZLIB_LEVEL = 3
MAX_PROJECT_SNAPSHOT_BYTES = 256 * 1024 * 1024


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def encode_project_snapshot(snapshot: ProjectSnapshot) -> tuple[str, str]:
    payload = _json_value(asdict(snapshot))
    # Keep byte-for-byte compatibility for legacy projects that never had
    # timeline bookmarks.  The decoder treats a missing field as empty; only
    # an actual user mark needs to be written to the snapshot.
    if not snapshot.timeline_markers:
        payload.pop("timeline_markers", None)
    if not snapshot.translation_preferences.saved_styles:
        preferences = payload.get("translation_preferences")
        if isinstance(preferences, dict):
            preferences.pop("saved_styles", None)
    if not snapshot.translation_preferences.active_style_name:
        payload["translation_preferences"].pop("active_style_name", None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return encoded, sha256(encoded.encode("utf-8")).hexdigest()


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"Project {name} must be an object")
    return value


def _sequence(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"Project {name} must be an array")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Project {name} must be a string")
    return value


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"Project {name} must be an integer")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Project {name} must be a number")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"Project {name} must be a boolean")
    return value


def _range(value: Any, name: str) -> TimeRange:
    raw = _object(value, name)
    return TimeRange(
        _integer(raw.get("start_us"), f"{name}.start_us"),
        _integer(raw.get("end_us"), f"{name}.end_us"),
    )


def _optional_box(value: Any, name: str) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    result = tuple(
        _number(item, name)
        for item in _sequence(value, name)
    )
    if len(result) != 4:
        raise ValueError(f"Project {name} must contain four values")
    return result


def _cue(value: Any) -> SubtitleCue:
    raw = _object(value, "cue")
    confidence = raw.get("confidence")
    evidence = _sequence(raw.get("evidence_ids", []), "cue.evidence_ids")
    return SubtitleCue(
        id=_string(raw.get("id"), "cue.id"),
        index=_integer(raw.get("index"), "cue.index"),
        range=_range(raw.get("range"), "cue.range"),
        text=_string(raw.get("text"), "cue.text"),
        language=_string(raw.get("language"), "cue.language"),
        revision=_integer(raw.get("revision", 1), "cue.revision"),
        source=_string(raw.get("source", "manual"), "cue.source"),
        confidence=None if confidence is None else _number(confidence, "cue.confidence"),
        evidence_ids=tuple(_string(item, "cue.evidence_id") for item in evidence),
        visual_box=_optional_box(raw.get("visual_box"), "cue.visual_box"),
        visual_box_source=_string(
            raw.get("visual_box_source", ""), "cue.visual_box_source"
        ),
        role=_string(raw.get("role", "dialogue"), "cue.role"),
        orientation=_string(
            raw.get("orientation", "horizontal"), "cue.orientation"
        ),
        style_hint=_string(raw.get("style_hint", "standard"), "cue.style_hint"),
        panel_box=_optional_box(raw.get("panel_box"), "cue.panel_box"),
        background_color=_string(
            raw.get("background_color", ""), "cue.background_color"
        ),
        text_color=_string(raw.get("text_color", ""), "cue.text_color"),
        review_state=_string(
            raw.get("review_state", "accepted"), "cue.review_state"
        ),
        scene_font_scale=_number(
            raw.get("scene_font_scale", 1.0), "cue.scene_font_scale"
        ),
        text_font_size=_integer(
            raw.get("text_font_size", 12), "cue.text_font_size"
        ),
        subtitle_track_id=_string(
            raw.get("subtitle_track_id", ""), "cue.subtitle_track_id"
        ),
        background_padding_x=_integer(
            raw.get("background_padding_x", 20), "cue.background_padding_x"
        ),
        background_padding_y=_integer(
            raw.get("background_padding_y", 10), "cue.background_padding_y"
        ),
        background_style=_string(
            raw.get("background_style", "solid"), "cue.background_style"
        ),
        letter_spacing_percent=_integer(
            raw.get("letter_spacing_percent", 0), "cue.letter_spacing_percent"
        ),
        font_bold=_boolean(raw.get("font_bold", False), "cue.font_bold"),
        font_italic=_boolean(raw.get("font_italic", False), "cue.font_italic"),
        text_opacity_percent=_integer(
            raw.get("text_opacity_percent", 100), "cue.text_opacity_percent"
        ),
        background_opacity_percent=_integer(
            raw.get("background_opacity_percent", 90),
            "cue.background_opacity_percent",
        ),
        fade_enabled=_boolean(raw.get("fade_enabled", False), "cue.fade_enabled"),
        fade_duration_ms=_integer(
            raw.get("fade_duration_ms", 300), "cue.fade_duration_ms"
        ),
    )


def _clip(value: Any) -> TimelineClip:
    raw = _object(value, "clip")
    source_range = raw.get("source_range")
    speed = _object(raw.get("speed", {"numerator": 1, "denominator": 1}), "clip.speed")
    media_id = raw.get("media_id")
    link_group_id = raw.get("link_group_id")
    return TimelineClip(
        id=_string(raw.get("id"), "clip.id"),
        track_id=_string(raw.get("track_id"), "clip.track_id"),
        timeline_range=_range(raw.get("timeline_range"), "clip.timeline_range"),
        source_range=None
        if source_range is None
        else _range(source_range, "clip.source_range"),
        media_id=None if media_id is None else _string(media_id, "clip.media_id"),
        link_group_id=None
        if link_group_id is None
        else _string(link_group_id, "clip.link_group_id"),
        speed=SpeedRatio(
            _integer(speed.get("numerator"), "clip.speed.numerator"),
            _integer(speed.get("denominator"), "clip.speed.denominator"),
        ),
        volume=_number(raw.get("volume", 1.0), "clip.volume"),
        payload_revision=_integer(
            raw.get("payload_revision", 1), "clip.payload_revision"
        ),
        cue_id=None if raw.get("cue_id") is None else _string(raw.get("cue_id"), "clip.cue_id"),
        cue_index=None
        if raw.get("cue_index") is None
        else _integer(raw.get("cue_index"), "clip.cue_index"),
        cue_text=_string(raw.get("cue_text", ""), "clip.cue_text"),
        timing_slot=None
        if raw.get("timing_slot") is None
        else _range(raw.get("timing_slot"), "clip.timing_slot"),
        timing_unresolved=_boolean(
            raw.get("timing_unresolved", False), "clip.timing_unresolved"
        ),
        content_warning=_boolean(
            raw.get("content_warning", False), "clip.content_warning"
        ),
        issue_code=_string(raw.get("issue_code", ""), "clip.issue_code"),
        muted=_boolean(raw.get("muted", False), "clip.muted"),
    )


def decode_project_snapshot(value: Any) -> ProjectSnapshot:
    raw = _object(value, "snapshot")
    media = tuple(
        MediaAsset(
            id=_string(item.get("id"), "media.id"),
            path=_string(item.get("path"), "media.path"),
            kind=MediaKind(_string(item.get("kind"), "media.kind")),
            duration_us=_integer(item.get("duration_us"), "media.duration_us"),
            sha256=None
            if item.get("sha256") is None
            else _string(item.get("sha256"), "media.sha256"),
        )
        for item in (
            _object(entry, "media item")
            for entry in _sequence(raw.get("media", []), "media")
        )
    )
    tracks = tuple(
        TimelineTrack(
            id=_string(item.get("id"), "track.id"),
            kind=TrackKind(_string(item.get("kind"), "track.kind")),
            name=_string(item.get("name"), "track.name"),
            clips=tuple(
                _clip(clip) for clip in _sequence(item.get("clips", []), "track.clips")
            ),
            locked=_boolean(item.get("locked", False), "track.locked"),
            visible=_boolean(item.get("visible", True), "track.visible"),
            muted=_boolean(item.get("muted", False), "track.muted"),
        )
        for item in (
            _object(entry, "track item")
            for entry in _sequence(raw.get("tracks", []), "tracks")
        )
    )
    overlays = tuple(
        Overlay(
            id=_string(item.get("id"), "overlay.id"),
            kind=OverlayKind(_string(item.get("kind"), "overlay.kind")),
            range=_range(item.get("range"), "overlay.range"),
            x=_number(item.get("x"), "overlay.x"),
            y=_number(item.get("y"), "overlay.y"),
            width=_number(item.get("width"), "overlay.width"),
            height=_number(item.get("height"), "overlay.height"),
            asset_id=None
            if item.get("asset_id") is None
            else _string(item.get("asset_id"), "overlay.asset_id"),
            enabled=_boolean(item.get("enabled", True), "overlay.enabled"),
            opacity=_number(item.get("opacity", 1.0), "overlay.opacity"),
            blur_radius=_integer(item.get("blur_radius", 18), "overlay.blur_radius"),
            tracking_mode=_string(
                item.get("tracking_mode", "fixed"), "overlay.tracking_mode"
            ),
            padding_x=_number(item.get("padding_x", 0.012), "overlay.padding_x"),
            padding_y=_number(item.get("padding_y", 0.008), "overlay.padding_y"),
            concealment_mode=_string(
                item.get("concealment_mode", "blur"), "overlay.concealment_mode"
            ),
        )
        for item in (
            _object(entry, "overlay item")
            for entry in _sequence(raw.get("overlays", []), "overlays")
        )
    )
    raw_voice_conform = raw.get("voice_conform")
    voice_conform = None
    if raw_voice_conform is not None:
        conform = _object(raw_voice_conform, "voice_conform")
        voice_conform = VoiceConformState(
            plan=decode_voice_conform_plan(conform.get("plan")),
            base_voice_media_id=_string(
                conform.get("base_voice_media_id"), "voice_conform.base_voice_media_id"
            ),
            conformed_voice_media_id=_string(
                conform.get("conformed_voice_media_id"),
                "voice_conform.conformed_voice_media_id",
            ),
            parent_tts_job_id=_string(
                conform.get("parent_tts_job_id"), "voice_conform.parent_tts_job_id"
            ),
        )
    preferences = _object(
        raw.get("translation_preferences", {}), "translation_preferences"
    )
    raw_saved_styles = preferences.get("saved_styles", [])
    if isinstance(raw_saved_styles, dict):
        # Accept the short-lived map shape used by early development builds.
        raw_saved_styles = [
            {"name": name, **(_object(value, "translation_preferences.saved_styles")
                                if isinstance(value, dict) else {})}
            for name, value in raw_saved_styles.items()
        ]
    saved_styles = tuple(
        TranslationStylePreset(
            name=_string(item.get("name"), "translation_preferences.saved_styles[].name"),
            preset=_string(
                item.get("preset", "auto"),
                "translation_preferences.saved_styles[].preset",
            ),
            custom_rules=_string(
                item.get("custom_rules", ""),
                "translation_preferences.saved_styles[].custom_rules",
            ),
        )
        for item in (
            _object(entry, "translation_preferences.saved_styles[]")
            for entry in _sequence(raw_saved_styles, "translation_preferences.saved_styles")
        )
    )
    snapshot = ProjectSnapshot(
        id=_string(raw.get("id"), "id"),
        name=_string(raw.get("name"), "name"),
        revision=_integer(raw.get("revision"), "revision"),
        media=media,
        tracks=tracks,
        source_cues=tuple(
            _cue(cue) for cue in _sequence(raw.get("source_cues", []), "source_cues")
        ),
        translated_cues=tuple(
            _cue(cue)
            for cue in _sequence(raw.get("translated_cues", []), "translated_cues")
        ),
        overlays=overlays,
        title_cues=tuple(
            _cue(cue) for cue in _sequence(raw.get("title_cues", []), "title_cues")
        ),
        voice_conform=voice_conform,
        timeline_edit=(
            TimelineEditState(
                retained_ranges=tuple(
                    TimeRange(_integer(item.get("start_us"), "cut.start_us"),
                              _integer(item.get("end_us"), "cut.end_us"))
                    for item in _sequence(
                        _object(raw["timeline_edit"], "timeline_edit").get("retained_ranges"),
                        "timeline_edit.retained_ranges",
                    )
                ),
                schema_version=_integer(raw["timeline_edit"].get("schema_version", 1),
                                        "timeline_edit.schema_version"),
            ) if raw.get("timeline_edit") is not None else None
        ),
        timeline_markers=tuple(
            sorted(
                {
                    _integer(item, "timeline_markers[]")
                    for item in _sequence(
                        raw.get("timeline_markers", []), "timeline_markers"
                    )
                }
            )
        ),
        translation_preferences=TranslationPreferences(
            preset=_string(
                preferences.get("preset", "auto"),
                "translation_preferences.preset",
            ),
            custom_rules=_string(
                preferences.get("custom_rules", ""),
                "translation_preferences.custom_rules",
            ),
            saved_styles=saved_styles,
            active_style_name=_string(
                preferences.get("active_style_name", ""),
                "translation_preferences.active_style_name",
            ),
        ),
        ocr_dialogue_region=_optional_box(
            raw.get("ocr_dialogue_region"), "ocr_dialogue_region"
        ),
        ocr_exclusion_regions=tuple(
            box
            for index, item in enumerate(
                _sequence(raw.get("ocr_exclusion_regions", []), "ocr_exclusion_regions")
            )
            for box in (
                _optional_box(item, f"ocr_exclusion_regions[{index}]"),
            )
            if box is not None
        ),
        schema_version=_integer(raw.get("schema_version", 1), "schema_version"),
    )
    return normalize_legacy_subtitle_lanes(snapshot)


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        # Editor drafts belong to the single Job Manager, not SQLite or QML.
        # Historical job revisions remain durable and immutable.
        self._edit_drafts: dict[str, tuple[int, str, ProjectSnapshot]] = {}

    def has_pending_edits(self, project_id: str) -> bool:
        return project_id in self._edit_drafts

    def stage_edits(self, *, project_path: str | Path, snapshot: ProjectSnapshot) -> None:
        draft = self._edit_drafts.get(snapshot.id)
        base = draft[0] if draft else self.persisted_head_revision(snapshot.id)
        if self.persisted_head_revision(snapshot.id) != base:
            raise RuntimeError("Dự án đã được lưu ở phiên khác; bản đang chỉnh vẫn được giữ")
        current = draft[2].revision if draft else base
        if snapshot.revision <= current:
            raise ValueError("Bản đang chỉnh phải tiếp nối revision hiện tại")
        self._edit_drafts[snapshot.id] = (base, str(project_path), snapshot)

    def flush_edits(self, project_id: str) -> int:
        draft = self._edit_drafts.get(project_id)
        if draft:
            self.save(project_path=draft[1], snapshot=draft[2])
        return self.persisted_head_revision(project_id)

    def flush_all_edits(self) -> None:
        for project_id in tuple(self._edit_drafts):
            self.flush_edits(project_id)

    def save(self, *, project_path: str | Path, snapshot: ProjectSnapshot) -> str:
        draft = self._edit_drafts.get(snapshot.id)
        if draft and (snapshot.revision < draft[2].revision or (
                snapshot.revision == draft[2].revision and snapshot != draft[2])):
            raise RuntimeError("Không thể ghi đè bản đang chỉnh bằng snapshot cũ")
        path = str(Path(project_path).expanduser().resolve())
        encoded, digest = encode_project_snapshot(snapshot)
        raw_bytes = encoded.encode("utf-8")
        compressed = zlib.compress(raw_bytes, level=PROJECT_SNAPSHOT_ZLIB_LEVEL)
        now = utc_now_text()
        with self.database.write_transaction() as connection:
            if draft:
                head = connection.execute("SELECT head_revision FROM projects WHERE id = ?",
                                          (snapshot.id,)).fetchone()
                if head is None or int(head[0]) != draft[0]:
                    raise RuntimeError(
                        "Dự án đã thay đổi ở phiên khác; chưa lưu, bản chỉnh vẫn được giữ")
            connection.execute(
                """
                INSERT INTO projects(id, path, name, schema_version, head_revision, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path = excluded.path,
                    name = excluded.name,
                    schema_version = excluded.schema_version,
                    head_revision = MAX(projects.head_revision, excluded.head_revision),
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot.id,
                    path,
                    snapshot.name,
                    snapshot.schema_version,
                    snapshot.revision,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO project_revisions(
                    project_id, revision, snapshot_json, snapshot_sha256, created_at,
                    snapshot_encoding, snapshot_blob, uncompressed_size, compressed_size
                ) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    snapshot.revision,
                    digest,
                    now,
                    PROJECT_SNAPSHOT_ENCODING_ZLIB,
                    compressed,
                    len(raw_bytes),
                    len(compressed),
                ),
            )
            row = connection.execute(
                """
                SELECT snapshot_sha256 FROM project_revisions
                WHERE project_id = ? AND revision = ?
                """,
                (snapshot.id, snapshot.revision),
            ).fetchone()
            if row is None or row["snapshot_sha256"] != digest:
                raise RuntimeError("Project revision is immutable and its content does not match")
        self._edit_drafts.pop(snapshot.id, None)
        return digest

    def load_json(self, project_id: str, revision: int | None = None) -> dict[str, Any]:
        draft = self._edit_drafts.get(project_id)
        if draft and (revision is None or revision == draft[2].revision):
            return _json_value(asdict(draft[2]))
        with self.database.connect(read_only=True) as connection:
            target_revision = revision
            if target_revision is None:
                row = connection.execute(
                    "SELECT head_revision FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if row is None:
                    raise LookupError(f"Unknown project: {project_id}")
                target_revision = int(row["head_revision"])
            row = connection.execute(
                """
                SELECT snapshot_json, snapshot_sha256, snapshot_encoding,
                       snapshot_blob, uncompressed_size
                FROM project_revisions
                WHERE project_id = ? AND revision = ?
                """,
                (project_id, target_revision),
            ).fetchone()
        if row is None:
            raise LookupError(f"Unknown project revision: {project_id}/{target_revision}")
        encoding = str(row["snapshot_encoding"] or PROJECT_SNAPSHOT_ENCODING_JSON)
        if encoding == PROJECT_SNAPSHOT_ENCODING_JSON:
            encoded = str(row["snapshot_json"])
            raw_bytes = encoded.encode("utf-8")
        elif encoding == PROJECT_SNAPSHOT_ENCODING_ZLIB:
            blob = row["snapshot_blob"]
            if not isinstance(blob, bytes):
                raise RuntimeError("Compressed project revision has no payload")
            expected_size = int(row["uncompressed_size"] or 0)
            if expected_size <= 0 or expected_size > MAX_PROJECT_SNAPSHOT_BYTES:
                raise RuntimeError("Compressed project revision size is invalid")
            try:
                raw_bytes = zlib.decompress(blob)
            except zlib.error as exc:
                raise RuntimeError("Compressed project revision is damaged") from exc
            if len(raw_bytes) != expected_size:
                raise RuntimeError("Compressed project revision size does not match")
            try:
                encoded = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("Compressed project revision is not UTF-8") from exc
        else:
            raise RuntimeError(f"Unsupported project revision encoding: {encoding}")
        if len(raw_bytes) > MAX_PROJECT_SNAPSHOT_BYTES:
            raise RuntimeError("Project revision exceeds the safe decode limit")
        if sha256(raw_bytes).hexdigest() != str(row["snapshot_sha256"]):
            raise RuntimeError("Project revision checksum does not match")
        return json.loads(encoded)

    def head_revision(self, project_id: str) -> int:
        draft = self._edit_drafts.get(project_id)
        return draft[2].revision if draft else self.persisted_head_revision(project_id)

    def persisted_head_revision(self, project_id: str) -> int:
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT head_revision FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise LookupError(f"Unknown project: {project_id}")
        return int(row["head_revision"])

    def load(self, project_id: str, revision: int | None = None) -> ProjectSnapshot:
        draft = self._edit_drafts.get(project_id)
        if draft and (revision is None or revision == draft[2].revision):
            return draft[2]
        return decode_project_snapshot(self.load_json(project_id, revision))

    def list_summaries(self, *, limit: int | None = 100) -> tuple[dict[str, Any], ...]:
        if limit is not None and limit <= 0:
            raise ValueError("Project list limit must be positive")
        with self.database.connect(read_only=True) as connection:
            query = """
                SELECT id, path, name, schema_version, head_revision, updated_at
                FROM projects
                ORDER BY updated_at DESC, id
                """
            parameters: tuple[object, ...] = ()
            if limit is not None:
                query += " LIMIT ?"
                parameters = (limit,)
            rows = connection.execute(query, parameters).fetchall()
        return tuple(dict(row) for row in rows)

    def get_summary(self, project_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT id, path, name, schema_version, head_revision, updated_at
                FROM projects WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"Unknown project: {project_id}")
        return dict(row)

    @staticmethod
    def _deletable_job_ids(connection, project_id: str) -> tuple[str, ...]:
        active_states = {
            "queued",
            "start_requested",
            "running",
            "pause_requested",
            "paused",
            "resume_requested",
            "cancel_requested",
        }
        project = connection.execute(
            "SELECT id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise LookupError(f"Unknown project: {project_id}")
        jobs = connection.execute(
            "SELECT id, state FROM jobs WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        active = [str(row["id"]) for row in jobs if row["state"] in active_states]
        if active:
            raise ValueError(
                "Không thể xóa dự án khi còn tác vụ đang hoạt động; "
                "hãy dừng hoặc hủy tác vụ trước."
            )
        return tuple(str(row["id"]) for row in jobs)

    def deletable_job_ids(self, project_id: str) -> tuple[str, ...]:
        """Validate deletion without changing records, for artifact preflight."""
        with self.database.connect(read_only=True) as connection:
            return self._deletable_job_ids(connection, project_id)

    def delete_internal(self, project_id: str) -> tuple[str, ...]:
        """Delete only DUCAI database records and return the owned job IDs."""
        with self.database.write_transaction() as connection:
            job_ids = self._deletable_job_ids(connection, project_id)
            connection.execute("DELETE FROM jobs WHERE project_id = ?", (project_id,))
            deleted = connection.execute(
                "DELETE FROM projects WHERE id = ?", (project_id,)
            )
            if deleted.rowcount != 1:
                raise RuntimeError("Project deletion did not remove exactly one project")
        self._edit_drafts.pop(project_id, None)
        return job_ids
