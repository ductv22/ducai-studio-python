from __future__ import annotations

import json
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
    TimelineTrack,
    TrackKind,
    VoiceConformState,
)
from ducai_studio.domain.timebase import SpeedRatio, TimeRange
from ducai_studio.domain.voice_conform import decode_voice_conform_plan

from .database import Database
from .job_repository import utc_now_text


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
    return ProjectSnapshot(
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
        voice_conform=voice_conform,
        schema_version=_integer(raw.get("schema_version", 1), "schema_version"),
    )


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(self, *, project_path: str | Path, snapshot: ProjectSnapshot) -> str:
        path = str(Path(project_path).expanduser().resolve())
        encoded, digest = encode_project_snapshot(snapshot)
        now = utc_now_text()
        with self.database.write_transaction() as connection:
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
                    project_id, revision, snapshot_json, snapshot_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot.id, snapshot.revision, encoded, digest, now),
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
        return digest

    def load_json(self, project_id: str, revision: int | None = None) -> dict[str, Any]:
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
                SELECT snapshot_json FROM project_revisions
                WHERE project_id = ? AND revision = ?
                """,
                (project_id, target_revision),
            ).fetchone()
        if row is None:
            raise LookupError(f"Unknown project revision: {project_id}/{target_revision}")
        return json.loads(row["snapshot_json"])

    def load(self, project_id: str, revision: int | None = None) -> ProjectSnapshot:
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
        return job_ids
