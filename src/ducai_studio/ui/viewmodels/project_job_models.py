from __future__ import annotations

from bisect import bisect_left, bisect_right
from copy import deepcopy
from math import ceil
from pathlib import Path
from time import monotonic

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)

from ducai_studio.domain.multi_video import stage_local_progress
from ducai_studio.domain.project import (
    TranslationPreferences,
    TranslationStylePreset,
    grow_dialogue_panel_for_font,
    subtitle_track_number,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.domain.timeline import (
    VOICE_TIMING_OVERFLOW,
    classify_voice_timing,
)
from ducai_studio.domain.timeline_edit import (
    base_to_timeline_us,
    project_json_for_timeline,
    timeline_to_base_us,
)
from ducai_studio.domain.voice_conform import VoiceConformPlan, decode_voice_conform_plan
from ducai_studio.ui.multi_thumbnail_provider import multi_thumbnail_url

_INVALID_INDEX = QModelIndex()


def _replace_visible_rows(model, items: list[dict], identity: str) -> None:
    """Reuse a bounded bank of delegates; positioning comes from row geometry.

    Row order is deliberately not a time index. The complete track index owns
    time queries; rows only paint x/y derived from each clip's own timestamps.
    """
    old = model._items
    if old == items:
        return
    old_count = len(old)
    common_count = min(old_count, len(items))
    remaining = {item[identity]: item for item in items}
    replacements = [None] * common_count
    for row in range(common_count):
        replacements[row] = remaining.pop(old[row][identity], None)
    pending = iter(remaining.values())
    changed = []
    for row in range(common_count):
        value = replacements[row]
        if value is None:
            value = next(pending)
        if old[row] != value:
            old[row] = value
            changed.append(row)
    if changed:
        first = previous = changed[0]
        for row in [*changed[1:], -1]:
            if row == previous + 1:
                previous = row
                continue
            model.dataChanged.emit(model.index(first, 0), model.index(previous, 0), [])
            first = previous = row
    if len(items) < old_count:
        model.beginRemoveRows(_INVALID_INDEX, len(items), old_count - 1)
        del old[len(items) :]
        model.endRemoveRows()
    elif len(items) > old_count:
        model.beginInsertRows(_INVALID_INDEX, old_count, len(items) - 1)
        old.extend(pending)
        model.endInsertRows()
    if len(old) != old_count:
        model.countChanged.emit()


def _substantial_overlap_ids(clips: list[dict], threshold: float = 2 / 3) -> set[str]:
    """Return subtitle clip ids whose same-lane overlap is visibly dangerous.

    This is a display-only warning.  It deliberately does not reject or
    rewrite imported/OCR cues.  The active-window scan is bounded so a
    malformed project containing thousands of identical intervals cannot
    freeze Timeline construction.
    """

    # ``replace`` already sorts every track once.  Avoid sorting a second time
    # for the common 10k+ cue case; retain a defensive sort for direct callers
    # and older models that hand us an unsorted list.
    ordered = [
        clip
        for clip in clips
        if isinstance(clip, dict)
        and isinstance(clip.get("id"), str)
        and isinstance(clip.get("timeline_range"), dict)
        and type(clip["timeline_range"].get("start_us")) is int
        and type(clip["timeline_range"].get("end_us")) is int
    ]
    if any(
        left["timeline_range"]["start_us"] > right["timeline_range"]["start_us"]
        for left, right in zip(ordered, ordered[1:], strict=False)
    ):
        ordered.sort(
            key=lambda item: (
                item["timeline_range"]["start_us"],
                item["timeline_range"]["end_us"],
                item["id"],
            )
        )
    active: list[dict] = []
    warned: set[str] = set()
    for current in ordered:
        current_range = current["timeline_range"]
        current_start = current_range["start_us"]
        current_end = current_range["end_us"]
        current_duration = current_end - current_start
        if current_duration <= 0:
            continue
        active = [
            previous
            for previous in active
            if previous["timeline_range"]["end_us"] > current_start
        ]
        # Normal OCR output has at most a few neighbouring candidates.  Stop
        # after a bounded number of oldest active rows to protect the editor
        # from adversarial all-overlap input while still checking the newest
        # rows first (the rows users normally see stacked together).
        for previous in reversed(active[-64:]):
            previous_range = previous["timeline_range"]
            overlap = min(previous_range["end_us"], current_end) - max(
                previous_range["start_us"], current_start
            )
            previous_duration = previous_range["end_us"] - previous_range["start_us"]
            if overlap <= 0 or previous_duration <= 0:
                continue
            if overlap / min(current_duration, previous_duration) >= threshold:
                warned.add(str(previous["id"]))
                warned.add(str(current["id"]))
                break
        active.append(current)
    return warned


def _range_from_mapping(raw: object) -> TimeRange | None:
    if not isinstance(raw, dict):
        return None
    start_us = raw.get("start_us")
    end_us = raw.get("end_us")
    if type(start_us) is not int or type(end_us) is not int or start_us < 0 or end_us <= start_us:
        return None
    return TimeRange(start_us, end_us)


def _voice_timing_metrics(item: dict) -> dict[str, object]:
    timeline_range = _range_from_mapping(item.get("timeline_range"))
    timing_slot = _range_from_mapping(item.get("timing_slot"))
    if timeline_range is None:
        return {
            "state": "unknown",
            "duration_delta_us": 0,
            "start_delta_us": 0,
            "required_speed": 1.0,
        }
    state = classify_voice_timing(timeline_range, timing_slot)
    if timing_slot is None:
        return {
            "state": state,
            "duration_delta_us": 0,
            "start_delta_us": 0,
            "required_speed": 1.0,
        }
    source_range = _range_from_mapping(item.get("source_range"))
    required_speed = (
        source_range.duration_us / timing_slot.duration_us if source_range is not None else 1.0
    )
    return {
        "state": state,
        "duration_delta_us": timeline_range.duration_us - timing_slot.duration_us,
        "start_delta_us": timeline_range.start_us - timing_slot.start_us,
        "required_speed": required_speed,
    }


class ProjectListModel(QAbstractListModel):
    countChanged = Signal()

    IdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = Qt.ItemDataRole.UserRole + 2
    PathRole = Qt.ItemDataRole.UserRole + 3
    UpdatedAtRole = Qt.ItemDataRole.UserRole + 4

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._row_by_cue_index: dict[int, int] = {}

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        values = {
            self.IdRole: item.get("id", ""),
            self.NameRole: item.get("name", ""),
            self.PathRole: item.get("path", ""),
            self.UpdatedAtRole: item.get("updated_at", ""),
            Qt.ItemDataRole.DisplayRole: item.get("name", ""),
        }
        return values.get(role)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.IdRole: b"projectId",
            self.NameRole: b"name",
            self.PathRole: b"path",
            self.UpdatedAtRole: b"updatedAt",
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    def replace(self, raw_items: object) -> None:
        items = [dict(item) for item in raw_items] if isinstance(raw_items, list) else []
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()


class MultiVideoListModel(QAbstractListModel):
    """Small card-oriented model for the Multi video workspace."""

    countChanged = Signal()

    ProjectIdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = Qt.ItemDataRole.UserRole + 2
    VideoUrlRole = Qt.ItemDataRole.UserRole + 3
    DurationUsRole = Qt.ItemDataRole.UserRole + 4
    DurationTextRole = Qt.ItemDataRole.UserRole + 5
    SelectedRole = Qt.ItemDataRole.UserRole + 6
    StateRole = Qt.ItemDataRole.UserRole + 7
    StageRole = Qt.ItemDataRole.UserRole + 8
    ProgressRole = Qt.ItemDataRole.UserRole + 9
    ErrorCodeRole = Qt.ItemDataRole.UserRole + 10
    HasSourceSubtitlesRole = Qt.ItemDataRole.UserRole + 11
    HasTranslatedSubtitlesRole = Qt.ItemDataRole.UserRole + 12
    HasVoiceTrackRole = Qt.ItemDataRole.UserRole + 13
    ProjectPathRole = Qt.ItemDataRole.UserRole + 14
    SubtitleCuesRole = Qt.ItemDataRole.UserRole + 15
    OriginalAudioUrlRole = Qt.ItemDataRole.UserRole + 16
    VoiceAudioUrlRole = Qt.ItemDataRole.UserRole + 17
    HasOriginalAudioRole = Qt.ItemDataRole.UserRole + 18
    StatusTextRole = Qt.ItemDataRole.UserRole + 19
    ErrorMessageRole = Qt.ItemDataRole.UserRole + 20
    ThumbnailUrlRole = Qt.ItemDataRole.UserRole + 21
    StageProgressRole = Qt.ItemDataRole.UserRole + 22

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    @staticmethod
    def _duration_text(value: object) -> str:
        try:
            total = max(0, int(value)) // 1_000_000
        except (TypeError, ValueError):
            total = 0
        hours, remainder = divmod(total, 3_600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _uses_project_status(item: dict) -> bool:
        # The batch-owned child is authoritative for the current card. A
        # concurrent manual job must not replace its stage/progress in the UI.
        return (
            item.get("project_status_override") is True and item.get("is_batch_current") is not True
        )

    @staticmethod
    def _effective_state(item: dict) -> str:
        state = str(item.get("state", "waiting"))
        batch_state = str(item.get("batch_state", "idle"))
        child_state = str(item.get("child_job_state", ""))
        is_current = item.get("is_batch_current") is True
        # A manual CJK review is a live projection over the durable failed
        # translation row.  Show the card as ready/green immediately, while
        # the coordinator still retains the failed job for cache-only resume.
        if item.get("manual_review_ready") is True:
            return "completed"
        if state in {"running", "retrying"} or is_current:
            if batch_state == "pause_requested":
                return "pause_requested"
            if batch_state == "paused":
                # A stale/optimistic batch response must not claim that the
                # card is already paused while the durable child still says it
                # is starting or running.
                if child_state in {
                    "queued",
                    "start_requested",
                    "running",
                    "pause_requested",
                    "resume_requested",
                    "cancel_requested",
                }:
                    return "pause_requested"
                return "paused"
            if batch_state == "interrupted":
                return "interrupted"
        # A catalog card that is not currently owned by a Multi child reflects
        # the latest manual project job (or durable artifact state).
        if MultiVideoListModel._uses_project_status(item):
            return str(item.get("project_status_state", "idle"))
        if (
            state in {"waiting", "idle"}
            and batch_state not in {"running", "pause_requested"}
            and "artifact_status_state" in item
        ):
            return str(item.get("artifact_status_state", "idle"))
        return state

    @staticmethod
    def _stage_progress(item: dict) -> float:
        if MultiVideoListModel._uses_project_status(item):
            return max(
                0.0,
                min(1.0, float(item.get("project_status_progress", 0.0))),
            )
        if (
            str(item.get("state", "waiting")) in {"waiting", "idle"}
            and str(item.get("batch_state", "idle")) not in {"running", "pause_requested"}
            and "artifact_status_progress" in item
        ):
            return max(
                0.0,
                min(1.0, float(item.get("artifact_status_progress", 0.0))),
            )
        return stage_local_progress(
            str(item.get("stage", "ocr")),
            float(item.get("progress", 0.0)),
        )

    @staticmethod
    def _state_text(item: dict) -> str:
        if item.get("error_code") == "membership_removal_pending":
            return "Đang dừng an toàn để xóa khỏi Multi…"
        if item.get("manual_review_ready") is True:
            return "Đã sửa xong chữ Trung · sẵn sàng lồng tiếng"
        state = MultiVideoListModel._effective_state(item)
        if MultiVideoListModel._uses_project_status(item):
            return str(item.get("project_status_text") or "Chưa xử lý")
        if (
            str(item.get("state", "waiting")) in {"waiting", "idle"}
            and str(item.get("batch_state", "idle")) not in {"running", "pause_requested"}
            and item.get("artifact_status_text")
        ):
            return str(item.get("artifact_status_text"))
        stage = str(item.get("stage", "ocr"))
        progress = round(MultiVideoListModel._stage_progress(item) * 100)
        labels = {"ocr": "trích xuất SRT", "translate": "dịch", "tts": "lồng tiếng"}
        child_state = str(item.get("child_job_state", ""))
        child_phase = str(item.get("child_phase", ""))
        batch_state = str(item.get("batch_state", "idle"))
        whisper_phases = {
            "extract_audio": "Đang tách audio cho Whisper",
            "load_model": "Đang nạp Whisper large-v3",
            "transcribe": "Whisper đang nhận dạng",
            "cpu_fallback": "Đang chuyển Whisper sang CPU",
            "finalize": "Đang kiểm tra SRT Whisper",
        }
        if stage == "ocr" and child_phase in whisper_phases and state in {"running", "retrying"}:
            return f"{whisper_phases[child_phase]} · {progress}%"
        if child_phase in {"cache_restore", "conform_cache_restore"} and state in {
            "running",
            "retrying",
        }:
            return f"Đang kiểm tra cache · {labels.get(stage, stage)} {progress}%"
        if stage == "tts" and state in {"running", "retrying"}:
            tts_phases = {
                "model_loading": "Đang nạp model",
                "warmup": "Đang làm nóng",
                "synthesis": "Đang tạo giọng",
            }
            if child_phase in tts_phases:
                return f"{tts_phases[child_phase]} · lồng tiếng {progress}%"
        if child_state == "start_requested" and state in {"running", "retrying"}:
            suffix = " từ cache" if progress > 0 else ""
            return f"Đang khởi động bộ máy{suffix} · {labels.get(stage, stage)} {progress}%"
        if child_state == "resume_requested" and state in {"running", "retrying"}:
            return f"Đang khôi phục cache · {labels.get(stage, stage)} {progress}%"
        if batch_state == "interrupted" and child_state in {
            "queued",
            "start_requested",
            "running",
            "pause_requested",
            "resume_requested",
            "cancel_requested",
        }:
            return f"Đang xác nhận worker đã dừng · {labels.get(stage, stage)} {progress}%"
        if item.get("error_code") == "ocr_review_required" and state in {
            "deferred",
            "failed",
        }:
            payload = item.get("error_payload", {})
            payload = payload if isinstance(payload, dict) else {}
            blocker_count = max(0, int(payload.get("blocker_count", 0) or 0))
            review_text = f"{blocker_count} mục cần xem lại" if blocker_count else "cần xem lại QA"
            suffix = "chờ cuối lượt" if state == "deferred" else "đã bỏ qua"
            return f"OCR đã xong · {review_text} · {suffix}"
        if item.get("error_code") == "translation_review_required" and state in {
            "deferred",
            "failed",
        }:
            payload = item.get("error_payload", {})
            payload = payload if isinstance(payload, dict) else {}
            cjk_count = max(0, int(payload.get("cjk_blocker_count", 0) or 0))
            if cjk_count:
                suffix = "chờ cuối lượt" if state == "deferred" else "đã bỏ qua"
                return f"Còn chữ Trung · {cjk_count} cue · {suffix}"
            if payload.get("warnings_only") is True:
                return "Đã dịch xong · còn cảnh báo · tiếp tục lồng tiếng"
        if state == "completed":
            return "Đã hoàn thành 100%"
        if state == "failed":
            labels = {"ocr": "trích xuất SRT", "translate": "dịch", "tts": "ghép voice"}
            return f"Lỗi {labels.get(stage, stage)} {progress}% · đã bỏ qua"
        if state == "deferred":
            labels = {"ocr": "trích xuất SRT", "translate": "dịch", "tts": "ghép voice"}
            return f"Lỗi {labels.get(stage, stage)} {progress}% · sẽ thử lại cuối lượt"
        if state == "retrying":
            labels = {"ocr": "trích xuất", "translate": "dịch", "tts": "ghép voice"}
            if item.get("final_pass") is True:
                return f"Đang thử lại {labels.get(stage, stage)} cuối lượt · {progress}%"
            retry = max(1, min(5, int(item.get("retry_count", 1) or 1)))
            return f"Đang thử lại {labels.get(stage, stage)} {retry}/5 · {progress}%"
        if state == "running":
            return f"Đang {labels.get(stage, stage)} {progress}%"
        if state == "pause_requested":
            return f"Đang tạm dừng · {labels.get(stage, stage)} {progress}%"
        if state in {"paused", "interrupted"}:
            prefix = "Đã tạm dừng" if state == "paused" else "Đã gián đoạn"
            cache_note = " · cache vẫn được giữ" if state == "interrupted" else ""
            return f"{prefix} · {labels.get(stage, stage)} {progress}%{cache_note}"
        return "Hàng chờ…"

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        duration = item.get("duration_us", 0)
        status_text = self._state_text(item)
        subtitle_cues = item.get("subtitle_cues", [])
        error_payload = item.get("error_payload", {})
        error_payload = error_payload if isinstance(error_payload, dict) else {}
        if MultiVideoListModel._uses_project_status(item):
            error_message = str(item.get("project_error_message") or "")
        else:
            reason = str(error_payload.get("message") or error_payload.get("reason") or "")
            action = str(error_payload.get("action") or "")
            error_message = " ".join(value.strip() for value in (reason, action) if value.strip())
        values = {
            self.ProjectIdRole: item.get("project_id", ""),
            self.NameRole: item.get("name", ""),
            self.VideoUrlRole: QUrl.fromLocalFile(str(item.get("video_path", ""))),
            self.DurationUsRole: duration,
            self.DurationTextRole: self._duration_text(duration),
            self.SelectedRole: bool(item.get("selected", False)),
            self.StateRole: self._effective_state(item),
            self.StageRole: item.get("stage", "ocr"),
            self.ProgressRole: float(
                item.get("project_status_progress", item.get("progress", 0.0))
                if (MultiVideoListModel._uses_project_status(item))
                else (
                    item.get("artifact_status_progress", item.get("progress", 0.0))
                    if (
                        str(item.get("state", "waiting")) in {"waiting", "idle"}
                        and str(item.get("batch_state", "idle"))
                        not in {"running", "pause_requested"}
                        and "artifact_status_progress" in item
                    )
                    else item.get("progress", 0.0)
                )
            ),
            self.ErrorCodeRole: item.get("error_code", ""),
            self.HasSourceSubtitlesRole: bool(item.get("has_source_subtitles", False)),
            self.HasTranslatedSubtitlesRole: bool(item.get("has_translated_subtitles", False)),
            self.HasVoiceTrackRole: bool(item.get("has_voice_track", False)),
            self.ProjectPathRole: item.get("project_path", ""),
            self.SubtitleCuesRole: (
                [dict(cue) for cue in subtitle_cues if isinstance(cue, dict)]
                if isinstance(subtitle_cues, list)
                else []
            ),
            self.OriginalAudioUrlRole: QUrl.fromLocalFile(str(item.get("original_audio_path", ""))),
            self.VoiceAudioUrlRole: QUrl.fromLocalFile(str(item.get("voice_audio_path", ""))),
            self.HasOriginalAudioRole: bool(item.get("has_original_audio", False)),
            self.StatusTextRole: status_text,
            self.ErrorMessageRole: error_message,
            self.ThumbnailUrlRole: multi_thumbnail_url(item.get("video_path", ""), duration),
            self.StageProgressRole: self._stage_progress(item),
            Qt.ItemDataRole.DisplayRole: status_text,
        }
        return values.get(role)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.ProjectIdRole: b"projectId",
            self.NameRole: b"name",
            self.VideoUrlRole: b"videoUrl",
            self.DurationUsRole: b"durationUs",
            self.DurationTextRole: b"durationText",
            self.SelectedRole: b"selected",
            self.StateRole: b"state",
            self.StageRole: b"stage",
            self.ProgressRole: b"progress",
            self.ErrorCodeRole: b"errorCode",
            self.HasSourceSubtitlesRole: b"hasSourceSubtitles",
            self.HasTranslatedSubtitlesRole: b"hasTranslatedSubtitles",
            self.HasVoiceTrackRole: b"hasVoiceTrack",
            self.ProjectPathRole: b"projectPath",
            self.SubtitleCuesRole: b"subtitleCues",
            self.OriginalAudioUrlRole: b"originalAudioUrl",
            self.VoiceAudioUrlRole: b"voiceAudioUrl",
            self.HasOriginalAudioRole: b"hasOriginalAudio",
            self.StatusTextRole: b"statusText",
            self.ErrorMessageRole: b"errorMessage",
            self.ThumbnailUrlRole: b"thumbnailUrl",
            self.StageProgressRole: b"stageProgress",
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    def replace(self, raw_items: object) -> None:
        items = [dict(item) for item in raw_items] if isinstance(raw_items, list) else []
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()

    def merge(self, raw_items: object) -> None:
        """Patch lightweight status updates without resetting GridView delegates."""

        updates = [dict(item) for item in raw_items] if isinstance(raw_items, list) else []
        existing_ids = [str(item.get("project_id", "")) for item in self._items]
        update_ids = [str(item.get("project_id", "")) for item in updates]
        if existing_ids != update_ids:
            self.replace(updates)
            return
        for row, update in enumerate(updates):
            merged = {**self._items[row], **update}
            if merged == self._items[row]:
                continue
            self._items[row] = merged
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [])

    def item(self, project_id: str) -> dict:
        return next(
            (dict(item) for item in self._items if item.get("project_id") == project_id),
            {},
        )


class JobListModel(QAbstractListModel):
    countChanged = Signal()

    IdRole = Qt.ItemDataRole.UserRole + 1
    KindRole = Qt.ItemDataRole.UserRole + 2
    StateRole = Qt.ItemDataRole.UserRole + 3
    ProjectIdRole = Qt.ItemDataRole.UserRole + 4
    CreatedAtRole = Qt.ItemDataRole.UserRole + 5
    StartedAtRole = Qt.ItemDataRole.UserRole + 6
    PhaseRole = Qt.ItemDataRole.UserRole + 7
    CurrentRole = Qt.ItemDataRole.UserRole + 8
    TotalRole = Qt.ItemDataRole.UserRole + 9
    ProgressPercentRole = Qt.ItemDataRole.UserRole + 10
    ElapsedMsRole = Qt.ItemDataRole.UserRole + 11
    EtaMsRole = Qt.ItemDataRole.UserRole + 12
    ErrorCodeRole = Qt.ItemDataRole.UserRole + 13
    TranslatedCuesRole = Qt.ItemDataRole.UserRole + 14
    QaCuesRole = Qt.ItemDataRole.UserRole + 15
    CueTotalRole = Qt.ItemDataRole.UserRole + 16
    PhaseCurrentRole = Qt.ItemDataRole.UserRole + 17
    PhaseTotalRole = Qt.ItemDataRole.UserRole + 18
    CacheRestoredCuesRole = Qt.ItemDataRole.UserRole + 19
    ReusedProcessedCuesRole = Qt.ItemDataRole.UserRole + 20
    ReprocessedRawCuesRole = Qt.ItemDataRole.UserRole + 21
    PendingSynthesisCuesRole = Qt.ItemDataRole.UserRole + 22
    ChunkCurrentRole = Qt.ItemDataRole.UserRole + 23
    ChunkTotalRole = Qt.ItemDataRole.UserRole + 24
    PhaseDetailRole = Qt.ItemDataRole.UserRole + 25
    ProgressAgeMsRole = Qt.ItemDataRole.UserRole + 26

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        accepted_kinds: frozenset[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._accepted_kinds = accepted_kinds
        self._source_items: list[dict] = []
        self._project_filter = ""
        self._project_filter_initialized = False
        self._progress_observed_at: dict[str, float] = {}
        self._progress_signatures: dict[str, tuple[object, ...]] = {}
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1_000)
        self._clock_timer.timeout.connect(self._tick_active_jobs)
        self._clock_timer.start()

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        spec = item.get("spec", {})
        progress = item.get("progress", {})
        state = item.get("state", "")
        current = progress.get("current", 0) if isinstance(progress, dict) else 0
        total = progress.get("total", 0) if isinstance(progress, dict) else 0
        percent = 100.0 * current / total if isinstance(total, int) and total > 0 else 0.0
        job_id = str(item.get("id", ""))
        locally_active = state in {
            "start_requested",
            "running",
            "pause_requested",
            "resume_requested",
            "cancel_requested",
        }
        observed_at = self._progress_observed_at.get(job_id)
        observation_age_ms = (
            max(0, int((monotonic() - observed_at) * 1_000))
            if locally_active and observed_at is not None
            else 0
        )
        base_elapsed_ms = progress.get("elapsed_ms", 0) if isinstance(progress, dict) else 0
        elapsed_ms = (
            max(0, int(base_elapsed_ms)) + observation_age_ms
            if isinstance(base_elapsed_ms, (int, float)) and not isinstance(base_elapsed_ms, bool)
            else observation_age_ms
        )
        base_eta_ms = progress.get("eta_ms") if isinstance(progress, dict) else None
        eta_ms = (
            max(0, int(base_eta_ms) - observation_age_ms)
            if locally_active
            and isinstance(base_eta_ms, (int, float))
            and not isinstance(base_eta_ms, bool)
            else None
        )
        values = {
            self.IdRole: item.get("id", ""),
            self.KindRole: spec.get("kind", ""),
            self.StateRole: state,
            self.ProjectIdRole: spec.get("project_id", ""),
            self.CreatedAtRole: item.get("created_at", ""),
            self.StartedAtRole: item.get("started_at", "") or "",
            self.PhaseRole: progress.get("phase", "") if isinstance(progress, dict) else "",
            self.CurrentRole: current,
            self.TotalRole: total,
            self.ProgressPercentRole: percent,
            self.ElapsedMsRole: elapsed_ms,
            self.EtaMsRole: eta_ms,
            self.ErrorCodeRole: item.get("error_code", "") or "",
            self.TranslatedCuesRole: progress.get("translated_cues", 0)
            if isinstance(progress, dict)
            else 0,
            self.QaCuesRole: progress.get("qa_cues", 0) if isinstance(progress, dict) else 0,
            self.CueTotalRole: progress.get("cue_total", 0) if isinstance(progress, dict) else 0,
            self.PhaseCurrentRole: progress.get("phase_current", current)
            if isinstance(progress, dict)
            else current,
            self.PhaseTotalRole: progress.get("phase_total", total)
            if isinstance(progress, dict)
            else total,
            self.CacheRestoredCuesRole: progress.get("cache_restored_cues", 0)
            if isinstance(progress, dict)
            else 0,
            self.ReusedProcessedCuesRole: progress.get("reused_processed_cues", 0)
            if isinstance(progress, dict)
            else 0,
            self.ReprocessedRawCuesRole: progress.get("reprocessed_raw_cues", 0)
            if isinstance(progress, dict)
            else 0,
            self.PendingSynthesisCuesRole: progress.get("pending_synthesis_cues", 0)
            if isinstance(progress, dict)
            else 0,
            self.ChunkCurrentRole: progress.get("chunk_current", 0)
            if isinstance(progress, dict)
            else 0,
            self.ChunkTotalRole: progress.get("chunk_total", 0)
            if isinstance(progress, dict)
            else 0,
            self.PhaseDetailRole: progress.get("phase_detail", "")
            if isinstance(progress, dict)
            else "",
            self.ProgressAgeMsRole: observation_age_ms,
            Qt.ItemDataRole.DisplayRole: item.get("id", ""),
        }
        return values.get(role)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.IdRole: b"jobId",
            self.KindRole: b"kind",
            self.StateRole: b"state",
            self.ProjectIdRole: b"projectId",
            self.CreatedAtRole: b"createdAt",
            self.StartedAtRole: b"startedAt",
            self.PhaseRole: b"phase",
            self.CurrentRole: b"current",
            self.TotalRole: b"total",
            self.ProgressPercentRole: b"progressPercent",
            self.ElapsedMsRole: b"elapsedMs",
            self.EtaMsRole: b"etaMs",
            self.ErrorCodeRole: b"errorCode",
            self.TranslatedCuesRole: b"translatedCues",
            self.QaCuesRole: b"qaCues",
            self.CueTotalRole: b"cueTotal",
            self.PhaseCurrentRole: b"phaseCurrent",
            self.PhaseTotalRole: b"phaseTotal",
            self.CacheRestoredCuesRole: b"cacheRestoredCues",
            self.ReusedProcessedCuesRole: b"reusedProcessedCues",
            self.ReprocessedRawCuesRole: b"reprocessedRawCues",
            self.PendingSynthesisCuesRole: b"pendingSynthesisCues",
            self.ChunkCurrentRole: b"chunkCurrent",
            self.ChunkTotalRole: b"chunkTotal",
            self.PhaseDetailRole: b"phaseDetail",
            self.ProgressAgeMsRole: b"progressAgeMs",
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    @Slot(str)
    def setProjectFilter(self, project_id: str) -> None:
        value = project_id.strip()
        if value == self._project_filter and self._project_filter_initialized:
            return
        self._project_filter = value
        self._project_filter_initialized = True
        self._replace_items(self._source_items)

    def replace(self, raw_items: object) -> None:
        self._source_items = (
            [dict(item) for item in raw_items] if isinstance(raw_items, list) else []
        )
        current_ids = {str(item.get("id", "")) for item in self._source_items}
        self._progress_observed_at = {
            key: value for key, value in self._progress_observed_at.items() if key in current_ids
        }
        self._progress_signatures = {
            key: value for key, value in self._progress_signatures.items() if key in current_ids
        }
        self._replace_items(self._source_items)

    def _replace_items(self, raw_items: list[dict]) -> None:
        items = list(raw_items)
        if self._accepted_kinds is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("spec"), dict)
                and item["spec"].get("kind") in self._accepted_kinds
            ]
        if self._project_filter_initialized:
            items = [
                item
                for item in items
                if isinstance(item.get("spec"), dict)
                and item["spec"].get("project_id") == self._project_filter
            ]
        observed_at = monotonic()
        for item in items:
            job_id = str(item.get("id", ""))
            progress = item.get("progress", {})
            signature = (
                (
                    progress.get("phase"),
                    progress.get("current"),
                    progress.get("elapsed_ms"),
                    progress.get("eta_ms"),
                )
                if isinstance(progress, dict)
                else ()
            )
            if self._progress_signatures.get(job_id) != signature:
                self._progress_signatures[job_id] = signature
                self._progress_observed_at[job_id] = observed_at
        if items == self._items:
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()

    @Slot()
    def _tick_active_jobs(self) -> None:
        active_states = {
            "start_requested",
            "running",
            "pause_requested",
            "resume_requested",
            "cancel_requested",
        }
        roles = [self.ElapsedMsRole, self.EtaMsRole, self.ProgressAgeMsRole]
        for row, item in enumerate(self._items):
            if item.get("state") not in active_states:
                continue
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, roles)

    def active_job_id(self, project_id: str, kind: str) -> str:
        active_states = {
            "queued",
            "start_requested",
            "running",
            "pause_requested",
            "paused",
            "resume_requested",
            "cancel_requested",
        }
        for item in self._items:
            spec = item.get("spec", {})
            if (
                isinstance(spec, dict)
                and spec.get("project_id") == project_id
                and spec.get("kind") == kind
                and item.get("state") in active_states
            ):
                return str(item.get("id", ""))
        return ""


class TimelineTrackListModel(QAbstractListModel):
    countChanged = Signal()

    IdRole = Qt.ItemDataRole.UserRole + 1
    KindRole = Qt.ItemDataRole.UserRole + 2
    NameRole = Qt.ItemDataRole.UserRole + 3
    ClipCountRole = Qt.ItemDataRole.UserRole + 4
    MutedRole = Qt.ItemDataRole.UserRole + 5
    LockedRole = Qt.ItemDataRole.UserRole + 6
    VisibleRole = Qt.ItemDataRole.UserRole + 7
    DisplayLabelRole = Qt.ItemDataRole.UserRole + 8

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        return {
            self.IdRole: item["id"],
            self.KindRole: item["kind"],
            self.NameRole: item["name"],
            self.ClipCountRole: len(item["clips"]),
            self.MutedRole: item.get("muted", False),
            self.LockedRole: item.get("locked", False),
            self.VisibleRole: item.get("visible", True),
            self.DisplayLabelRole: item.get("display_label", item["id"]),
            Qt.ItemDataRole.DisplayRole: item["name"],
        }.get(role)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.IdRole: b"trackId",
            self.KindRole: b"kind",
            self.NameRole: b"name",
            self.ClipCountRole: b"clipCount",
            self.MutedRole: b"muted",
            self.LockedRole: b"locked",
            self.VisibleRole: b"trackVisible",
            self.DisplayLabelRole: b"displayLabel",
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    def replace(self, raw_tracks: object) -> None:
        items = [dict(item) for item in raw_tracks] if isinstance(raw_tracks, list) else []
        counters: dict[str, int] = {}
        prefixes = {
            "video": "V",
            "original_audio": "A",
            "background_music": "A",
            "subtitle": "S",
            "voice": "D",
            "logo": "L",
            "blur": "B",
        }
        for item in items:
            kind = str(item.get("kind", ""))
            prefix = prefixes.get(kind, "T")
            counters[prefix] = counters.get(prefix, 0) + 1
            track_id = str(item.get("id", ""))
            item["display_label"] = (
                track_id
                if kind == "subtitle" and subtitle_track_number(track_id, fallback=0) > 0
                else f"{prefix}{counters[prefix]}"
            )
        if items == self._items:
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()


class TimelineClipListModel(QAbstractListModel):
    countChanged = Signal()

    IdRole = Qt.ItemDataRole.UserRole + 1
    TrackIdRole = Qt.ItemDataRole.UserRole + 2
    TrackKindRole = Qt.ItemDataRole.UserRole + 3
    StartUsRole = Qt.ItemDataRole.UserRole + 4
    EndUsRole = Qt.ItemDataRole.UserRole + 5
    DurationUsRole = Qt.ItemDataRole.UserRole + 6
    VolumeRole = Qt.ItemDataRole.UserRole + 7
    SpeedPercentRole = Qt.ItemDataRole.UserRole + 8
    TrackIndexRole = Qt.ItemDataRole.UserRole + 9
    TrackNameRole = Qt.ItemDataRole.UserRole + 10
    AggregateRole = Qt.ItemDataRole.UserRole + 11
    AggregateCountRole = Qt.ItemDataRole.UserRole + 12
    CueIdRole = Qt.ItemDataRole.UserRole + 13
    CueIndexRole = Qt.ItemDataRole.UserRole + 14
    CueTextRole = Qt.ItemDataRole.UserRole + 15
    SlotStartUsRole = Qt.ItemDataRole.UserRole + 16
    SlotEndUsRole = Qt.ItemDataRole.UserRole + 17
    TimingUnresolvedRole = Qt.ItemDataRole.UserRole + 18
    ContentWarningRole = Qt.ItemDataRole.UserRole + 19
    IssueCodeRole = Qt.ItemDataRole.UserRole + 20
    TimingStateRole = Qt.ItemDataRole.UserRole + 21
    TimingDeltaUsRole = Qt.ItemDataRole.UserRole + 22
    TimingRequiredSpeedRole = Qt.ItemDataRole.UserRole + 23
    LinkGroupIdRole = Qt.ItemDataRole.UserRole + 24
    TrackLockedRole = Qt.ItemDataRole.UserRole + 25
    MutedRole = Qt.ItemDataRole.UserRole + 26
    OverlapWarningRole = Qt.ItemDataRole.UserRole + 27

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._track_indexes: list[tuple[list[dict], list[int], list[int]]] = []
        self._viewport_start_us = 0
        self._viewport_end_us = 60_000_000
        self._prefetch_us = 10_000_000
        self._track_view_start = 0
        self._track_view_end = 2_147_483_647
        self._track_prefetch = 0
        self._materialized_start_us: int | None = None
        self._materialized_end_us: int | None = None
        self._materialized_first_track: int | None = None
        self._materialized_last_track: int | None = None
        self._max_delegates = 480
        self._max_sparse_track_clips = 120
        self._role_data_cache: dict[int, tuple[dict, dict]] = {}

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        cached = self._role_data_cache.get(id(item))
        if cached is not None and cached[0] is item:
            return cached[1].get(role)
        time_range = item["timeline_range"]
        speed = item.get("speed", {"numerator": 1, "denominator": 1})
        timing = _voice_timing_metrics(item)
        values = {
            self.IdRole: item["id"],
            self.TrackIdRole: item["track_id"],
            self.TrackKindRole: item["track_kind"],
            self.StartUsRole: time_range["start_us"],
            self.EndUsRole: time_range["end_us"],
            self.DurationUsRole: time_range["end_us"] - time_range["start_us"],
            self.VolumeRole: item.get("volume", 1.0),
            self.SpeedPercentRole: speed["numerator"] * 100 / speed["denominator"],
            self.TrackIndexRole: item["track_index"],
            self.TrackNameRole: item["track_name"],
            self.AggregateRole: item.get("is_aggregate", False),
            self.AggregateCountRole: item.get("aggregate_count", 1),
            self.CueIdRole: item.get("cue_id", ""),
            self.CueIndexRole: item.get("cue_index"),
            self.CueTextRole: item.get("cue_text", ""),
            self.SlotStartUsRole: (
                item.get("timing_slot", {}).get("start_us")
                if isinstance(item.get("timing_slot"), dict)
                else -1
            ),
            self.SlotEndUsRole: (
                item.get("timing_slot", {}).get("end_us")
                if isinstance(item.get("timing_slot"), dict)
                else -1
            ),
            self.TimingUnresolvedRole: timing["state"] == VOICE_TIMING_OVERFLOW,
            self.ContentWarningRole: item.get("content_warning", False),
            self.IssueCodeRole: item.get("issue_code", ""),
            self.TimingStateRole: timing["state"],
            self.TimingDeltaUsRole: timing["duration_delta_us"],
            self.TimingRequiredSpeedRole: timing["required_speed"],
            self.LinkGroupIdRole: item.get("link_group_id", "") or "",
            self.TrackLockedRole: item.get("track_locked", False),
            self.MutedRole: item.get("muted", False),
            self.OverlapWarningRole: item.get("overlap_warning", False),
            Qt.ItemDataRole.DisplayRole: item["id"],
        }
        self._role_data_cache[id(item)] = (item, values)
        return values.get(role)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.IdRole: b"clipId",
            self.TrackIdRole: b"trackId",
            self.TrackKindRole: b"trackKind",
            self.StartUsRole: b"startUs",
            self.EndUsRole: b"endUs",
            self.DurationUsRole: b"durationUs",
            self.VolumeRole: b"volume",
            self.SpeedPercentRole: b"speedPercent",
            self.TrackIndexRole: b"trackIndex",
            self.TrackNameRole: b"trackName",
            self.AggregateRole: b"isAggregate",
            self.AggregateCountRole: b"aggregateCount",
            self.CueIdRole: b"cueId",
            self.CueIndexRole: b"cueIndex",
            self.CueTextRole: b"cueText",
            self.SlotStartUsRole: b"slotStartUs",
            self.SlotEndUsRole: b"slotEndUs",
            self.TimingUnresolvedRole: b"timingUnresolved",
            self.ContentWarningRole: b"contentWarning",
            self.IssueCodeRole: b"issueCode",
            self.TimingStateRole: b"timingState",
            self.TimingDeltaUsRole: b"timingDeltaUs",
            self.TimingRequiredSpeedRole: b"timingRequiredSpeed",
            self.LinkGroupIdRole: b"linkGroupId",
            self.TrackLockedRole: b"trackLocked",
            self.MutedRole: b"muted",
            self.OverlapWarningRole: b"overlapWarning",
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    def replace(self, raw_tracks: object) -> None:
        self._track_indexes = []
        if isinstance(raw_tracks, list):
            for track_index, track in enumerate(raw_tracks):
                if not isinstance(track, dict):
                    continue
                indexed_clips: list[dict] = []
                for clip in track.get("clips", []):
                    time_range = clip.get("timeline_range") if isinstance(clip, dict) else None
                    if not isinstance(time_range, dict):
                        continue
                    start_us = time_range.get("start_us")
                    end_us = time_range.get("end_us")
                    if type(start_us) is not int or type(end_us) is not int:
                        continue
                    indexed_clips.append(
                        {
                            **clip,
                            # The owning track is the identity authority.  Old
                            # snapshots did not always repeat it on every clip,
                            # while compact patches do.  Normalizing here keeps
                            # every role readable without weakening patch
                            # validation.
                            "track_id": track.get("id", ""),
                            "track_kind": track.get("kind", ""),
                            "track_name": track.get("name", ""),
                            "track_index": track_index,
                            "track_locked": track.get("locked", False),
                        }
                    )
                indexed_clips.sort(key=lambda item: item["timeline_range"]["start_us"])
                overlap_ids = (
                    _substantial_overlap_ids(indexed_clips)
                    if track.get("kind") == "subtitle"
                    else set()
                )
                indexed_clips = [
                    {**item, "overlap_warning": item["id"] in overlap_ids}
                    for item in indexed_clips
                ]
                starts = [item["timeline_range"]["start_us"] for item in indexed_clips]
                prefix_max: list[int] = []
                maximum = -1
                for item in indexed_clips:
                    maximum = max(maximum, item["timeline_range"]["end_us"])
                    prefix_max.append(maximum)
                self._track_indexes.append((indexed_clips, starts, prefix_max))
        self._refresh(force=True)

    @Slot(float, float, float)
    def setViewport(self, start_us: float, end_us: float, prefetch_us: float) -> None:
        start = max(0, int(start_us))
        end = max(start + 1, int(end_us))
        prefetch = max(0, int(prefetch_us))
        if (
            start == self._viewport_start_us
            and end == self._viewport_end_us
            and prefetch == self._prefetch_us
        ):
            return
        self._viewport_start_us = start
        self._viewport_end_us = end
        self._prefetch_us = prefetch
        if self._time_viewport_needs_refresh():
            self._refresh()

    @Slot(float, float, float)
    def setTrackViewport(
        self,
        first_track: float,
        last_track: float,
        prefetch_tracks: float,
    ) -> None:
        first = max(0, int(first_track))
        last = max(first, int(last_track))
        prefetch = max(0, int(prefetch_tracks))
        if (
            first == self._track_view_start
            and last == self._track_view_end
            and prefetch == self._track_prefetch
        ):
            return
        self._track_view_start = first
        self._track_view_end = last
        self._track_prefetch = prefetch
        if self._track_viewport_needs_refresh():
            self._refresh()

    def _time_viewport_needs_refresh(self) -> bool:
        materialized_start = self._materialized_start_us
        materialized_end = self._materialized_end_us
        if materialized_start is None or materialized_end is None:
            return True
        if self._viewport_start_us < materialized_start or self._viewport_end_us > materialized_end:
            return True

        # A wide materialization window is useful while panning, but after a
        # substantial zoom-in it would keep dense aggregate blocks alive even
        # when individual cues can be edited again.  Rebuild only at geometric
        # thresholds instead of resetting the QML delegates on every wheel tick.
        desired_start = max(0, self._viewport_start_us - self._prefetch_us)
        desired_end = self._viewport_end_us + self._prefetch_us
        desired_span = max(1, desired_end - desired_start)
        materialized_span = max(1, materialized_end - materialized_start)
        return materialized_span > desired_span * 2

    def _track_viewport_needs_refresh(self) -> bool:
        materialized_first = self._materialized_first_track
        materialized_last = self._materialized_last_track
        if materialized_first is None or materialized_last is None:
            return True
        if self._track_view_start < materialized_first or self._track_view_end > materialized_last:
            return True
        desired_first = max(0, self._track_view_start - self._track_prefetch)
        desired_last = self._track_view_end + self._track_prefetch
        desired_count = max(1, desired_last - desired_first + 1)
        materialized_count = max(1, materialized_last - materialized_first + 1)
        return materialized_count > desired_count * 2

    def _refresh(self, *, force: bool = False) -> None:
        query_start = max(0, self._viewport_start_us - self._prefetch_us)
        query_end = self._viewport_end_us + self._prefetch_us
        first_track = max(0, self._track_view_start - self._track_prefetch)
        last_track = self._track_view_end + self._track_prefetch
        if (
            not force
            and not self._time_viewport_needs_refresh()
            and not self._track_viewport_needs_refresh()
        ):
            return
        self._materialized_start_us = query_start
        self._materialized_end_us = query_end
        self._materialized_first_track = first_track
        self._materialized_last_track = last_track
        tracks_in_view: list[list[dict]] = []
        for track_index, (clips, starts, prefix_max) in enumerate(self._track_indexes):
            if track_index < first_track or track_index > last_track:
                tracks_in_view.append([])
                continue
            lower = bisect_right(prefix_max, query_start)
            upper = bisect_left(starts, query_end)
            tracks_in_view.append(
                [
                    item
                    for item in clips[lower:upper]
                    if item["timeline_range"]["start_us"] < query_end
                    and item["timeline_range"]["end_us"] > query_start
                ]
            )
        total = sum(len(items) for items in tracks_in_view)
        items: list[dict] = []
        if total <= self._max_delegates:
            items = [item for track_items in tracks_in_view for item in track_items]
        else:
            # Keep genuinely sparse tracks (normally S2/S3 scene text) as
            # individual editable cues. A very dense S1 track must not consume
            # the proportional budget and collapse 75 scene labels into a few
            # unusable "15 clip" blocks.
            sparse_budget = self._max_delegates // 2
            sparse_track_indexes: set[int] = set()
            sparse_count = 0
            sparse_candidates = sorted(
                (
                    (len(track_items), track_index)
                    for track_index, track_items in enumerate(tracks_in_view)
                    if 0 < len(track_items) <= self._max_sparse_track_clips
                ),
                key=lambda value: (value[0], value[1]),
            )
            for track_count, track_index in sparse_candidates:
                if sparse_count + track_count > sparse_budget:
                    continue
                sparse_track_indexes.add(track_index)
                sparse_count += track_count

            dense_total = sum(
                len(track_items)
                for track_index, track_items in enumerate(tracks_in_view)
                if track_index not in sparse_track_indexes
            )
            dense_budget = max(1, self._max_delegates - sparse_count)
            for track_index, track_items in enumerate(tracks_in_view):
                if not track_items:
                    continue
                if track_index in sparse_track_indexes:
                    items.extend(track_items)
                    continue
                target = max(1, int(dense_budget * len(track_items) / dense_total))
                chunk_size = max(1, ceil(len(track_items) / target))
                for offset in range(0, len(track_items), chunk_size):
                    chunk = track_items[offset : offset + chunk_size]
                    if len(chunk) == 1:
                        items.append(chunk[0])
                        continue
                    first = chunk[0]
                    items.append(
                        {
                            **first,
                            "id": f"aggregate-{first['track_index']}-{offset}",
                            "timeline_range": {
                                "start_us": first["timeline_range"]["start_us"],
                                "end_us": max(item["timeline_range"]["end_us"] for item in chunk),
                            },
                            "is_aggregate": True,
                            "aggregate_count": len(chunk),
                        }
                    )
        items.sort(
            key=lambda item: (
                item["track_index"],
                item["timeline_range"]["start_us"],
            )
        )
        self._role_data_cache.clear()
        _replace_visible_rows(self, items, "id")


class VoiceConformCueListModel(QAbstractListModel):
    """Virtualized per-cue D1 timing decisions for the visible timeline range."""

    countChanged = Signal()
    totalCountChanged = Signal()

    CueIdRole = Qt.ItemDataRole.UserRole + 1
    CueIndexRole = Qt.ItemDataRole.UserRole + 2
    SourceStartUsRole = Qt.ItemDataRole.UserRole + 3
    SourceEndUsRole = Qt.ItemDataRole.UserRole + 4
    SlotStartUsRole = Qt.ItemDataRole.UserRole + 5
    SlotEndUsRole = Qt.ItemDataRole.UserRole + 6
    VoiceStartUsRole = Qt.ItemDataRole.UserRole + 7
    VoiceEndUsRole = Qt.ItemDataRole.UserRole + 8
    AudioDurationUsRole = Qt.ItemDataRole.UserRole + 9
    RenderedAudioDurationUsRole = Qt.ItemDataRole.UserRole + 10
    AudioSpeedRole = Qt.ItemDataRole.UserRole + 11
    AdjustedRole = Qt.ItemDataRole.UserRole + 12
    UnresolvedRole = Qt.ItemDataRole.UserRole + 13
    IssueCodeRole = Qt.ItemDataRole.UserRole + 14
    AggregateRole = Qt.ItemDataRole.UserRole + 15
    AggregateCountRole = Qt.ItemDataRole.UserRole + 16

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._all: list[dict] = []
        self._starts: list[int] = []
        self._prefix_max: list[int] = []
        self._items: list[dict] = []
        self._viewport_start_us = 0
        self._viewport_end_us = 60_000_000
        self._prefetch_us = 10_000_000
        self._materialized_start_us: int | None = None
        self._materialized_end_us: int | None = None
        self._max_delegates = 480

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        values = {
            self.CueIdRole: item["cue_id"],
            self.CueIndexRole: item["index"],
            self.SourceStartUsRole: float(item["source_start_us"]),
            self.SourceEndUsRole: float(item["source_end_us"]),
            self.SlotStartUsRole: float(item["output_start_us"]),
            self.SlotEndUsRole: float(item["output_end_us"]),
            self.VoiceStartUsRole: float(item["voice_start_us"]),
            self.VoiceEndUsRole: float(item["voice_end_us"]),
            self.AudioDurationUsRole: float(item["audio_duration_us"]),
            self.RenderedAudioDurationUsRole: float(item["rendered_audio_duration_us"]),
            self.AudioSpeedRole: item["audio_speed"],
            self.AdjustedRole: item["adjusted"],
            self.UnresolvedRole: item["unresolved"],
            self.IssueCodeRole: item["issue_code"],
            self.AggregateRole: item.get("is_aggregate", False),
            self.AggregateCountRole: item.get("aggregate_count", 1),
            Qt.ItemDataRole.DisplayRole: item["cue_id"],
        }
        return values.get(role)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.CueIdRole: b"cueId",
            self.CueIndexRole: b"cueIndex",
            self.SourceStartUsRole: b"sourceStartUs",
            self.SourceEndUsRole: b"sourceEndUs",
            self.SlotStartUsRole: b"slotStartUs",
            self.SlotEndUsRole: b"slotEndUs",
            self.VoiceStartUsRole: b"voiceStartUs",
            self.VoiceEndUsRole: b"voiceEndUs",
            self.AudioDurationUsRole: b"audioDurationUs",
            self.RenderedAudioDurationUsRole: b"renderedAudioDurationUs",
            self.AudioSpeedRole: b"audioSpeed",
            self.AdjustedRole: b"adjusted",
            self.UnresolvedRole: b"unresolved",
            self.IssueCodeRole: b"issueCode",
            self.AggregateRole: b"isAggregate",
            self.AggregateCountRole: b"aggregateCount",
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    @Property(int, notify=totalCountChanged)
    def totalCount(self) -> int:
        return len(self._all)

    def replace(self, plan: VoiceConformPlan | None) -> None:
        items: list[dict] = []
        if plan is not None:
            items = [
                {
                    "cue_id": item.cue_id,
                    "index": item.index,
                    "source_start_us": item.source_start_us,
                    "source_end_us": item.source_end_us,
                    "output_start_us": item.output_start_us,
                    "output_end_us": item.output_end_us,
                    "voice_start_us": item.voice_start_us,
                    "voice_end_us": item.voice_end_us,
                    "audio_duration_us": item.audio_duration_us,
                    "rendered_audio_duration_us": item.rendered_audio_duration_us,
                    "audio_speed": item.audio_speed,
                    "adjusted": item.adjusted,
                    "unresolved": item.unresolved,
                    "issue_code": item.issue_code,
                }
                for item in sorted(
                    plan.cues,
                    key=lambda decision: (
                        decision.voice_start_us,
                        decision.voice_end_us,
                        decision.index,
                    ),
                )
            ]
        self._replace_all(items)

    def replace_timing_rows(self, raw_rows: object) -> None:
        """Render measured base/review TTS rows before conform is applied."""

        rows = raw_rows if isinstance(raw_rows, list) else []
        items: list[dict] = []
        integer_keys = (
            "index",
            "source_start_us",
            "source_end_us",
            "output_start_us",
            "output_end_us",
            "voice_start_us",
            "voice_end_us",
            "audio_duration_us",
            "rendered_audio_duration_us",
        )
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            cue_id = raw.get("cue_id")
            if not isinstance(cue_id, str) or not cue_id:
                continue
            if any(type(raw.get(key)) is not int for key in integer_keys):
                continue
            if (
                int(raw["source_end_us"]) <= int(raw["source_start_us"])
                or int(raw["output_end_us"]) <= int(raw["output_start_us"])
                or int(raw["voice_end_us"]) <= int(raw["voice_start_us"])
                or int(raw["audio_duration_us"]) <= 0
                or int(raw["rendered_audio_duration_us"]) <= 0
            ):
                continue
            speed = raw.get("audio_speed", 1.0)
            if isinstance(speed, bool) or not isinstance(speed, (int, float)):
                speed = 1.0
            items.append(
                {
                    "cue_id": cue_id,
                    "index": int(raw["index"]),
                    "source_start_us": int(raw["source_start_us"]),
                    "source_end_us": int(raw["source_end_us"]),
                    "output_start_us": int(raw["output_start_us"]),
                    "output_end_us": int(raw["output_end_us"]),
                    "voice_start_us": int(raw["voice_start_us"]),
                    "voice_end_us": int(raw["voice_end_us"]),
                    "audio_duration_us": int(raw["audio_duration_us"]),
                    "rendered_audio_duration_us": int(raw["rendered_audio_duration_us"]),
                    "audio_speed": max(1.0, float(speed)),
                    "adjusted": raw.get("adjusted") is True,
                    "unresolved": raw.get("unresolved") is True,
                    "issue_code": str(raw.get("issue_code", "")),
                }
            )
        items.sort(
            key=lambda item: (
                item["voice_start_us"],
                item["voice_end_us"],
                item["index"],
            )
        )
        self._replace_all(items)

    def _replace_all(self, items: list[dict]) -> None:
        total_changed = len(items) != len(self._all)
        self._all = items
        self._starts = [item["voice_start_us"] for item in self._all]
        self._prefix_max = []
        maximum = -1
        for item in self._all:
            maximum = max(maximum, item["voice_end_us"])
            self._prefix_max.append(maximum)
        self._refresh(force=True)
        if total_changed:
            self.totalCountChanged.emit()

    @Slot(float, float, float)
    def setViewport(self, start_us: float, end_us: float, prefetch_us: float) -> None:
        start = max(0, int(start_us))
        end = max(start + 1, int(end_us))
        prefetch = max(0, int(prefetch_us))
        if (
            start == self._viewport_start_us
            and end == self._viewport_end_us
            and prefetch == self._prefetch_us
        ):
            return
        self._viewport_start_us = start
        self._viewport_end_us = end
        self._prefetch_us = prefetch
        if self._viewport_needs_refresh():
            self._refresh()

    def _viewport_needs_refresh(self) -> bool:
        materialized_start = self._materialized_start_us
        materialized_end = self._materialized_end_us
        if materialized_start is None or materialized_end is None:
            return True
        if self._viewport_start_us < materialized_start or self._viewport_end_us > materialized_end:
            return True
        desired_start = max(0, self._viewport_start_us - self._prefetch_us)
        desired_end = self._viewport_end_us + self._prefetch_us
        desired_span = max(1, desired_end - desired_start)
        materialized_span = max(1, materialized_end - materialized_start)
        return materialized_span > desired_span * 2

    def _refresh(self, *, force: bool = False) -> None:
        query_start = max(0, self._viewport_start_us - self._prefetch_us)
        query_end = self._viewport_end_us + self._prefetch_us
        if not force and not self._viewport_needs_refresh():
            return
        self._materialized_start_us = query_start
        self._materialized_end_us = query_end
        lower = bisect_right(self._prefix_max, query_start)
        upper = bisect_left(self._starts, query_end)
        visible = [
            item
            for item in self._all[lower:upper]
            if item["voice_start_us"] < query_end and item["voice_end_us"] > query_start
        ]
        if len(visible) <= self._max_delegates:
            items = visible
        else:
            chunk_size = max(1, ceil(len(visible) / self._max_delegates))
            items = []
            for offset in range(0, len(visible), chunk_size):
                chunk = visible[offset : offset + chunk_size]
                first = chunk[0]
                if len(chunk) == 1:
                    items.append(first)
                    continue
                items.append(
                    {
                        **first,
                        "voice_end_us": max(item["voice_end_us"] for item in chunk),
                        "output_end_us": max(item["output_end_us"] for item in chunk),
                        "rendered_audio_duration_us": sum(
                            item["rendered_audio_duration_us"] for item in chunk
                        ),
                        "audio_speed": max(item["audio_speed"] for item in chunk),
                        "adjusted": any(item["adjusted"] for item in chunk),
                        "unresolved": any(item["unresolved"] for item in chunk),
                        "issue_code": ", ".join(
                            dict.fromkeys(
                                item["issue_code"] for item in chunk if item["issue_code"]
                            )
                        ),
                        "is_aggregate": True,
                        "aggregate_count": len(chunk),
                    }
                )
        if items == self._items:
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()


def _timecode_from_us(value: object) -> str:
    microseconds = value if type(value) is int and value >= 0 else 0
    total_milliseconds = microseconds // 1_000
    total_seconds, milliseconds = divmod(total_milliseconds, 1_000)
    total_minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


class SubtitleCueListModel(QAbstractListModel):
    countChanged = Signal()

    CueIdRole = Qt.ItemDataRole.UserRole + 1
    CueIndexRole = Qt.ItemDataRole.UserRole + 2
    StartUsRole = Qt.ItemDataRole.UserRole + 3
    EndUsRole = Qt.ItemDataRole.UserRole + 4
    StartTextRole = Qt.ItemDataRole.UserRole + 5
    EndTextRole = Qt.ItemDataRole.UserRole + 6
    SourceTextRole = Qt.ItemDataRole.UserRole + 7
    TranslatedTextRole = Qt.ItemDataRole.UserRole + 8
    StatusRole = Qt.ItemDataRole.UserRole + 9
    ConfidenceRole = Qt.ItemDataRole.UserRole + 10
    ProvenanceRole = Qt.ItemDataRole.UserRole + 11
    RevisionRole = Qt.ItemDataRole.UserRole + 12
    TranslationCueIdRole = Qt.ItemDataRole.UserRole + 13
    QaIssueCodeRole = Qt.ItemDataRole.UserRole + 14
    QaIssueMessageRole = Qt.ItemDataRole.UserRole + 15
    TimingTooLongRole = Qt.ItemDataRole.UserRole + 16
    CueRoleRole = Qt.ItemDataRole.UserRole + 17
    OrientationRole = Qt.ItemDataRole.UserRole + 18
    StyleHintRole = Qt.ItemDataRole.UserRole + 19
    PanelBoxRole = Qt.ItemDataRole.UserRole + 20
    BackgroundColorRole = Qt.ItemDataRole.UserRole + 21
    ReviewStateRole = Qt.ItemDataRole.UserRole + 22
    SceneFontScaleRole = Qt.ItemDataRole.UserRole + 23
    TextFontSizeRole = Qt.ItemDataRole.UserRole + 24
    SubtitleTrackIdRole = Qt.ItemDataRole.UserRole + 25
    TextColorRole = Qt.ItemDataRole.UserRole + 26
    BackgroundPaddingXRole = Qt.ItemDataRole.UserRole + 27
    BackgroundPaddingYRole = Qt.ItemDataRole.UserRole + 28
    BackgroundStyleRole = Qt.ItemDataRole.UserRole + 29
    LetterSpacingPercentRole = Qt.ItemDataRole.UserRole + 30
    FontBoldRole = Qt.ItemDataRole.UserRole + 31
    FontItalicRole = Qt.ItemDataRole.UserRole + 32
    TextOpacityPercentRole = Qt.ItemDataRole.UserRole + 33
    BackgroundOpacityPercentRole = Qt.ItemDataRole.UserRole + 34
    FadeEnabledRole = Qt.ItemDataRole.UserRole + 35
    FadeDurationMsRole = Qt.ItemDataRole.UserRole + 36

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._source_cues: list[dict] = []
        self._translated_cues: list[dict] = []
        self._row_by_cue_id: dict[str, int] = {}
        self._row_by_cue_index: dict[int, int] = {}
        self._start_us: list[int] = []
        self._max_end_prefix: list[int] = []
        self._scene_timing_previews: dict[str, tuple[int, int]] = {}
        # QML asks for many roles separately for every visible row.  Keep the
        # immutable role projection for the current item objects and invalidate
        # it on every model/data notification.  Mutations in this model are
        # always followed by one of those notifications, so this cannot expose
        # stale text/style values while avoiding repeated dict construction.
        self._role_data_cache: dict[int, tuple[dict, dict]] = {}
        self.dataChanged.connect(self._invalidate_role_data_cache)
        self.modelReset.connect(self._invalidate_role_data_cache)
        self.rowsInserted.connect(self._invalidate_role_data_cache)
        self.rowsRemoved.connect(self._invalidate_role_data_cache)
        # Shared inspector commands can resolve independent titles without
        # adding them to subtitle editor rows, QA indices or translation data.
        self.extra_text_model: SubtitleCueListModel | None = None

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def _invalidate_role_data_cache(self, *_args) -> None:
        self._role_data_cache.clear()

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        base_item = self._items[index.row()]
        preview = self._scene_timing_previews.get(str(base_item.get("cue_id", "")))
        if preview is None:
            cached = self._role_data_cache.get(id(base_item))
            if cached is not None and cached[0] is base_item:
                return cached[1].get(role)
            item = base_item
        else:
            item = self._with_scene_timing_preview(base_item)
        values = {
            self.CueIdRole: item["cue_id"],
            self.CueIndexRole: item["index"],
            self.StartUsRole: float(item["start_us"]),
            self.EndUsRole: float(item["end_us"]),
            self.StartTextRole: item["start_text"],
            self.EndTextRole: item["end_text"],
            self.SourceTextRole: item["source_text"],
            self.TranslatedTextRole: item["translated_text"],
            self.StatusRole: item["status"],
            self.ConfidenceRole: item["confidence"],
            self.ProvenanceRole: item["provenance"],
            self.RevisionRole: item["revision"],
            self.TranslationCueIdRole: item["translation_cue_id"],
            self.QaIssueCodeRole: item.get("qa_issue_code", ""),
            self.QaIssueMessageRole: item.get("qa_issue_message", ""),
            self.TimingTooLongRole: item.get("timing_too_long", False),
            self.CueRoleRole: item.get("cue_role", "dialogue"),
            self.OrientationRole: item.get("orientation", "horizontal"),
            self.StyleHintRole: item.get("style_hint", "standard"),
            self.PanelBoxRole: item.get("panel_box"),
            self.BackgroundColorRole: item.get("background_color", ""),
            self.ReviewStateRole: item.get("review_state", "accepted"),
            self.SceneFontScaleRole: item.get("scene_font_scale", 1.0),
            self.TextFontSizeRole: item.get("text_font_size", 12),
            self.SubtitleTrackIdRole: item.get(
                "subtitle_track_id",
                "S1" if item.get("cue_role", "dialogue") == "dialogue" else "S2",
            ),
            self.TextColorRole: item.get("text_color", ""),
            self.BackgroundPaddingXRole: item.get("background_padding_x", 20),
            self.BackgroundPaddingYRole: item.get("background_padding_y", 10),
            self.BackgroundStyleRole: item.get("background_style", "solid"),
            self.LetterSpacingPercentRole: item.get("letter_spacing_percent", 0),
            self.FontBoldRole: item.get("font_bold", False),
            self.FontItalicRole: item.get("font_italic", False),
            self.TextOpacityPercentRole: item.get("text_opacity_percent", 100),
            self.BackgroundOpacityPercentRole: item.get("background_opacity_percent", 90),
            self.FadeEnabledRole: item.get("fade_enabled", False),
            self.FadeDurationMsRole: item.get("fade_duration_ms", 300),
            Qt.ItemDataRole.DisplayRole: item["source_text"],
        }
        if preview is None:
            self._role_data_cache[id(base_item)] = (base_item, values)
        return values.get(role)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.CueIdRole: b"cueId",
            self.CueIndexRole: b"cueIndex",
            self.StartUsRole: b"startUs",
            self.EndUsRole: b"endUs",
            self.StartTextRole: b"startText",
            self.EndTextRole: b"endText",
            self.SourceTextRole: b"sourceText",
            self.TranslatedTextRole: b"translatedText",
            self.StatusRole: b"status",
            self.ConfidenceRole: b"confidence",
            self.ProvenanceRole: b"provenance",
            self.RevisionRole: b"revision",
            self.TranslationCueIdRole: b"translationCueId",
            self.QaIssueCodeRole: b"qaIssueCode",
            self.QaIssueMessageRole: b"qaIssueMessage",
            self.TimingTooLongRole: b"timingTooLong",
            self.CueRoleRole: b"cueRole",
            self.OrientationRole: b"orientation",
            self.StyleHintRole: b"styleHint",
            self.PanelBoxRole: b"panelBox",
            self.BackgroundColorRole: b"backgroundColor",
            self.ReviewStateRole: b"reviewState",
            self.SceneFontScaleRole: b"sceneFontScale",
            self.TextFontSizeRole: b"textFontSize",
            self.SubtitleTrackIdRole: b"subtitleTrackId",
            self.TextColorRole: b"textColor",
            self.BackgroundPaddingXRole: b"backgroundPaddingX",
            self.BackgroundPaddingYRole: b"backgroundPaddingY",
            self.BackgroundStyleRole: b"backgroundStyle",
            self.LetterSpacingPercentRole: b"letterSpacingPercent",
            self.FontBoldRole: b"fontBold",
            self.FontItalicRole: b"fontItalic",
            self.TextOpacityPercentRole: b"textOpacityPercent",
            self.BackgroundOpacityPercentRole: b"backgroundOpacityPercent",
            self.FadeEnabledRole: b"fadeEnabled",
            self.FadeDurationMsRole: b"fadeDurationMs",
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> dict:
        return (
            self._with_scene_timing_preview(self._items[row]) if 0 <= row < len(self._items) else {}
        )

    def find(self, cue_id: str) -> dict:
        row = self._row_by_cue_id.get(cue_id, -1)
        if not 0 <= row < len(self._items):
            return self.extra_text_model.find(cue_id) if self.extra_text_model else {}
        item = self._items[row]
        return self._with_scene_timing_preview(item) if item["cue_id"] == cue_id else {}

    @Slot(str, result=int)
    def rowForCueId(self, cue_id: str) -> int:
        return self._row_by_cue_id.get(cue_id, -1)

    def _with_scene_timing_preview(self, item: dict) -> dict:
        preview = self._scene_timing_previews.get(str(item.get("cue_id", "")))
        if preview is None:
            return dict(item)
        start_us, end_us = preview
        return {
            **item,
            "start_us": start_us,
            "end_us": end_us,
            "start_text": _timecode_from_us(start_us),
            "end_text": _timecode_from_us(end_us),
        }

    def preview_scene_text_timing(self, cue_id: str, start_us: int, end_us: int) -> bool:
        if self.extra_text_model and self.extra_text_model.find(cue_id):
            return self.extra_text_model.preview_scene_text_timing(cue_id, start_us, end_us)
        for row, item in enumerate(self._items):
            if item.get("cue_id") != cue_id:
                continue
            current = self._scene_timing_previews.get(
                cue_id,
                (int(item["start_us"]), int(item["end_us"])),
            )
            requested = (start_us, end_us)
            if current == requested:
                return False
            base = (int(item["start_us"]), int(item["end_us"]))
            if requested == base:
                self._scene_timing_previews.pop(cue_id, None)
            else:
                self._scene_timing_previews[cue_id] = requested
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, 0),
                [
                    self.StartUsRole,
                    self.EndUsRole,
                    self.StartTextRole,
                    self.EndTextRole,
                ],
            )
            return True
        return False

    def scene_text_base_timing(self, cue_id: str) -> tuple[int, int] | None:
        if self.extra_text_model and self.extra_text_model.find(cue_id):
            return self.extra_text_model.scene_text_base_timing(cue_id)
        """Return persisted timing, excluding any pointer-move preview."""

        for item in self._items:
            if item.get("cue_id") == cue_id:
                return int(item["start_us"]), int(item["end_us"])
        return None

    def preview_scene_text_delta(
        self,
        cue_ids: set[str],
        delta_us: int,
    ) -> list[str]:
        """Preview a total delta from each cue's persisted timing."""

        changed: list[str] = []
        for item in self._items:
            cue_id = str(item.get("cue_id", ""))
            if cue_id not in cue_ids:
                continue
            start_us = max(0, int(item["start_us"]) + delta_us)
            duration_us = max(100_000, int(item["end_us"]) - int(item["start_us"]))
            if self.preview_scene_text_timing(
                cue_id,
                start_us,
                start_us + duration_us,
            ):
                changed.append(cue_id)
        return changed

    def preview_scene_text_edge_delta(
        self,
        cue_ids: set[str],
        edge: str,
        delta_us: int,
    ) -> list[str]:
        """Preview one shared left/right edge delta from persisted timing."""

        if edge not in {"left", "right"}:
            return []
        changed: list[str] = []
        for item in self._items:
            cue_id = str(item.get("cue_id", ""))
            if cue_id not in cue_ids:
                continue
            start_us = int(item["start_us"])
            end_us = int(item["end_us"])
            if edge == "left":
                start_us += delta_us
            else:
                end_us += delta_us
            if end_us - start_us < 100_000 or start_us < 0:
                continue
            if self.preview_scene_text_timing(cue_id, start_us, end_us):
                changed.append(cue_id)
        return changed

    def match_summary(self, find: str, *, language: str = "translated") -> dict[str, int]:
        key = "source_text" if language == "source" else "translated_text"
        counts = [str(item.get(key, "")).count(find) for item in self._items] if find else []
        return {
            "match_count": sum(counts),
            "cue_count": sum(count > 0 for count in counts),
        }

    def optimistic_update_text(
        self,
        cue_id: str,
        text: str,
        *,
        language: str,
    ) -> list[dict[str, str]]:
        if self.extra_text_model and self.extra_text_model.find(cue_id):
            return self.extra_text_model.optimistic_update_text(cue_id, text, language=language)
        id_key = "cue_id" if language == "source" else "translation_cue_id"
        text_key = "source_text" if language == "source" else "translated_text"
        role = self.SourceTextRole if language == "source" else self.TranslatedTextRole
        for row, item in enumerate(self._items):
            if item.get(id_key) != cue_id or str(item.get(text_key, "")) == text:
                continue
            previous = str(item.get(text_key, ""))
            item[text_key] = text
            self.dataChanged.emit(self.index(row, 0), self.index(row, 0), [role])
            return [
                {
                    "cue_id": cue_id,
                    "language": language,
                    "previous": previous,
                    "applied": text,
                }
            ]
        return []

    def optimistic_update_scene_font_scale(self, cue_id: str, scale: float) -> bool:
        if self.extra_text_model and self.extra_text_model.find(cue_id):
            return self.extra_text_model.optimistic_update_scene_font_scale(cue_id, scale)
        normalized = round(max(0.5, min(2.0, float(scale))), 2)
        for row, item in enumerate(self._items):
            if (item.get("cue_id") != cue_id
                    or item.get("cue_role") not in {"scene_text", "title_text"}):
                continue
            if float(item.get("scene_font_scale", 1.0)) == normalized:
                return False
            item["scene_font_scale"] = normalized
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, 0),
                [self.SceneFontScaleRole],
            )
            return True
        return False

    def optimistic_update_text_format(
        self,
        cue_id: str,
        *,
        font_size: int,
        orientation: str,
        background_color: str,
        text_color: str = "",
        style_hint: str = "standard",
        background_padding_x: int = 20,
        background_padding_y: int = 10,
        background_style: str = "solid",
        letter_spacing_percent: int = 0,
        font_bold: bool = False,
        font_italic: bool = False,
        text_opacity_percent: int = 100,
        background_opacity_percent: int = 90,
        _notify: bool = True,
    ) -> list[dict[str, object]]:
        matched_row = self._row_by_cue_id.get(cue_id, -1)
        for row in (matched_row,) if matched_row >= 0 else ():
            item = self._items[row]
            if item.get("cue_id") != cue_id:
                continue
            previous = {
                "font_size": int(item.get("text_font_size", 12)),
                "panel_box": item.get("panel_box"),
                "orientation": str(item.get("orientation", "horizontal")),
                "background_color": str(item.get("background_color", "")),
                "text_color": str(item.get("text_color", "")),
                "style_hint": str(item.get("style_hint", "standard")),
                "background_padding_x": int(item.get("background_padding_x", 20)),
                "background_padding_y": int(item.get("background_padding_y", 10)),
                "background_style": str(item.get("background_style", "solid")),
                "letter_spacing_percent": int(item.get("letter_spacing_percent", 0)),
                "font_bold": bool(item.get("font_bold", False)),
                "font_italic": bool(item.get("font_italic", False)),
                "text_opacity_percent": int(item.get("text_opacity_percent", 100)),
                "background_opacity_percent": int(item.get("background_opacity_percent", 90)),
            }
            applied = {
                "font_size": int(font_size),
                "panel_box": grow_dialogue_panel_for_font(
                    previous["panel_box"], previous["font_size"], int(font_size))
                    if item.get("cue_role", "dialogue") == "dialogue" else previous["panel_box"],
                "orientation": orientation,
                "background_color": background_color,
                "text_color": text_color,
                "style_hint": style_hint,
                "background_padding_x": int(background_padding_x),
                "background_padding_y": int(background_padding_y),
                "background_style": background_style,
                "letter_spacing_percent": int(letter_spacing_percent),
                "font_bold": bool(font_bold),
                "font_italic": bool(font_italic),
                "text_opacity_percent": int(text_opacity_percent),
                "background_opacity_percent": int(background_opacity_percent),
            }
            if previous == applied:
                return []
            item["text_font_size"] = applied["font_size"]
            item["panel_box"] = applied["panel_box"]
            item["orientation"] = applied["orientation"]
            item["background_color"] = applied["background_color"]
            item["text_color"] = applied["text_color"]
            item["style_hint"] = applied["style_hint"]
            item["background_padding_x"] = applied["background_padding_x"]
            item["background_padding_y"] = applied["background_padding_y"]
            item["background_style"] = applied["background_style"]
            item["letter_spacing_percent"] = applied["letter_spacing_percent"]
            item["font_bold"] = applied["font_bold"]
            item["font_italic"] = applied["font_italic"]
            item["text_opacity_percent"] = applied["text_opacity_percent"]
            item["background_opacity_percent"] = applied["background_opacity_percent"]
            if _notify:
                self._emit_changed_ranges([row], self._text_format_roles())
            return [
                {
                    "kind": "text_format",
                    "cue_id": cue_id,
                    "previous": previous,
                    "applied": applied,
                }
            ]
        return []

    def optimistic_update_text_format_many(
        self, cue_ids: list[str], **values
    ) -> list[dict[str, object]]:
        if self.extra_text_model:
            titles = [cue_id for cue_id in cue_ids if self.extra_text_model.find(cue_id)]
            if titles:
                return (self.extra_text_model.optimistic_update_text_format_many(titles, **values)
                        + self.optimistic_update_text_format_many(
                            [cue_id for cue_id in cue_ids if cue_id not in titles], **values))
        changes: list[dict[str, object]] = []
        rows: list[int] = []
        for cue_id in dict.fromkeys(cue_ids):
            changed = self.optimistic_update_text_format(cue_id, _notify=False, **values)
            if changed:
                changes.extend(changed)
                rows.append(self._row_by_cue_id[cue_id])
        if rows:
            self.dataChanged.emit(
                self.index(min(rows), 0), self.index(max(rows), 0), self._text_format_roles()
            )
        return changes

    def _text_format_roles(self) -> list[int]:
        return [
            self.TextFontSizeRole,
            self.PanelBoxRole,
            self.OrientationRole,
            self.BackgroundColorRole,
            self.TextColorRole,
            self.StyleHintRole,
            self.BackgroundPaddingXRole,
            self.BackgroundPaddingYRole,
            self.BackgroundStyleRole,
            self.LetterSpacingPercentRole,
            self.FontBoldRole,
            self.FontItalicRole,
            self.TextOpacityPercentRole,
            self.BackgroundOpacityPercentRole,
        ]

    def optimistic_update_scene_fade(
        self,
        cue_id: str,
        *,
        enabled: bool,
        duration_ms: int,
    ) -> list[dict[str, object]]:
        if self.extra_text_model and self.extra_text_model.find(cue_id):
            return self.extra_text_model.optimistic_update_scene_fade(
                cue_id, enabled=enabled, duration_ms=duration_ms)
        for row, item in enumerate(self._items):
            if (item.get("cue_id") != cue_id
                    or item.get("cue_role") not in {"scene_text", "title_text"}):
                continue
            previous = {
                "enabled": bool(item.get("fade_enabled", False)),
                "duration_ms": int(item.get("fade_duration_ms", 300)),
            }
            applied = {"enabled": bool(enabled), "duration_ms": int(duration_ms)}
            if previous == applied:
                return []
            item["fade_enabled"] = applied["enabled"]
            item["fade_duration_ms"] = applied["duration_ms"]
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, 0),
                [self.FadeEnabledRole, self.FadeDurationMsRole],
            )
            return [
                {
                    "kind": "scene_fade",
                    "cue_id": cue_id,
                    "previous": previous,
                    "applied": applied,
                }
            ]
        return []

    def optimistic_find_replace(
        self,
        find: str,
        replacement: str,
        *,
        language: str = "translated",
    ) -> list[dict[str, str]]:
        id_key = "cue_id" if language == "source" else "translation_cue_id"
        text_key = "source_text" if language == "source" else "translated_text"
        role = self.SourceTextRole if language == "source" else self.TranslatedTextRole
        changes: list[dict[str, str]] = []
        changed_rows: list[int] = []
        for row, item in enumerate(self._items):
            previous = str(item.get(text_key, ""))
            applied = previous.replace(find, replacement)
            cue_id = str(item.get(id_key, ""))
            if not cue_id or applied == previous:
                continue
            item[text_key] = applied
            changes.append(
                {
                    "cue_id": cue_id,
                    "language": language,
                    "previous": previous,
                    "applied": applied,
                }
            )
            changed_rows.append(row)
        self._emit_changed_ranges(changed_rows, [role])
        return changes

    def rollback_text_changes(self, raw_changes: object) -> None:
        if self.extra_text_model and isinstance(raw_changes, list):
            title_changes = [change for change in raw_changes if isinstance(change, dict)
                             and self.extra_text_model.find(str(change.get("cue_id", "")))]
            self.extra_text_model.rollback_text_changes(title_changes)
            raw_changes = [change for change in raw_changes if change not in title_changes]
        changes = raw_changes if isinstance(raw_changes, list) else []
        changed_by_role: dict[int, list[int]] = {
            self.SourceTextRole: [],
            self.TranslatedTextRole: [],
        }
        for change in changes:
            if not isinstance(change, dict):
                continue
            if change.get("kind") == "scene_font_scale":
                cue_id = str(change.get("cue_id", ""))
                previous = change.get("previous", 1.0)
                applied = change.get("applied", 1.0)
                if (
                    isinstance(previous, (int, float))
                    and not isinstance(previous, bool)
                    and isinstance(applied, (int, float))
                    and not isinstance(applied, bool)
                ):
                    for row, item in enumerate(self._items):
                        if item.get("cue_id") != cue_id:
                            continue
                        if abs(float(item.get("scene_font_scale", 1.0)) - float(applied)) > 0.001:
                            break
                        item["scene_font_scale"] = float(previous)
                        self.dataChanged.emit(
                            self.index(row, 0),
                            self.index(row, 0),
                            [self.SceneFontScaleRole],
                        )
                        break
                continue
            if change.get("kind") == "scene_timing":
                cue_id = str(change.get("cue_id", ""))
                for row, item in enumerate(self._items):
                    if item.get("cue_id") != cue_id:
                        continue
                    if cue_id in self._scene_timing_previews:
                        self._scene_timing_previews.pop(cue_id, None)
                        self.dataChanged.emit(
                            self.index(row, 0),
                            self.index(row, 0),
                            [
                                self.StartUsRole,
                                self.EndUsRole,
                                self.StartTextRole,
                                self.EndTextRole,
                            ],
                        )
                    break
                continue
            if change.get("kind") == "scene_fade":
                cue_id = str(change.get("cue_id", ""))
                previous = change.get("previous")
                applied = change.get("applied")
                if not isinstance(previous, dict) or not isinstance(applied, dict):
                    continue
                for row, item in enumerate(self._items):
                    if item.get("cue_id") != cue_id:
                        continue
                    if bool(item.get("fade_enabled", False)) != bool(
                        applied.get("enabled", False)
                    ) or int(item.get("fade_duration_ms", 300)) != int(
                        applied.get("duration_ms", 300)
                    ):
                        break
                    item["fade_enabled"] = bool(previous.get("enabled", False))
                    item["fade_duration_ms"] = int(previous.get("duration_ms", 300))
                    self.dataChanged.emit(
                        self.index(row, 0),
                        self.index(row, 0),
                        [self.FadeEnabledRole, self.FadeDurationMsRole],
                    )
                    break
                continue
            if change.get("kind") == "text_format":
                cue_id = str(change.get("cue_id", ""))
                previous = change.get("previous")
                applied = change.get("applied")
                if not isinstance(previous, dict) or not isinstance(applied, dict):
                    continue
                for row, item in enumerate(self._items):
                    if item.get("cue_id") != cue_id:
                        continue
                    if (
                        int(item.get("text_font_size", 12)) != int(applied.get("font_size", 12))
                        or str(item.get("orientation", "horizontal"))
                        != str(applied.get("orientation", "horizontal"))
                        or str(item.get("background_color", ""))
                        != str(applied.get("background_color", ""))
                        or str(item.get("text_color", "")) != str(applied.get("text_color", ""))
                        or str(item.get("style_hint", "standard"))
                        != str(applied.get("style_hint", "standard"))
                        or int(item.get("background_padding_x", 20))
                        != int(applied.get("background_padding_x", 20))
                        or int(item.get("background_padding_y", 10))
                        != int(applied.get("background_padding_y", 10))
                        or str(item.get("background_style", "solid"))
                        != str(applied.get("background_style", "solid"))
                        or int(item.get("letter_spacing_percent", 0))
                        != int(applied.get("letter_spacing_percent", 0))
                        or bool(item.get("font_bold", False))
                        != bool(applied.get("font_bold", False))
                        or bool(item.get("font_italic", False))
                        != bool(applied.get("font_italic", False))
                        or int(item.get("text_opacity_percent", 100))
                        != int(applied.get("text_opacity_percent", 100))
                        or int(item.get("background_opacity_percent", 90))
                        != int(applied.get("background_opacity_percent", 90))
                    ):
                        break
                    item["text_font_size"] = int(previous.get("font_size", 12))
                    if "panel_box" in previous:
                        item["panel_box"] = previous["panel_box"]
                    item["orientation"] = str(previous.get("orientation", "horizontal"))
                    item["background_color"] = str(previous.get("background_color", ""))
                    item["text_color"] = str(previous.get("text_color", ""))
                    item["style_hint"] = str(previous.get("style_hint", "standard"))
                    item["background_padding_x"] = int(previous.get("background_padding_x", 20))
                    item["background_padding_y"] = int(previous.get("background_padding_y", 10))
                    item["background_style"] = str(previous.get("background_style", "solid"))
                    item["letter_spacing_percent"] = int(previous.get("letter_spacing_percent", 0))
                    item["font_bold"] = bool(previous.get("font_bold", False))
                    item["font_italic"] = bool(previous.get("font_italic", False))
                    item["text_opacity_percent"] = int(previous.get("text_opacity_percent", 100))
                    item["background_opacity_percent"] = int(
                        previous.get("background_opacity_percent", 90)
                    )
                    self.dataChanged.emit(
                        self.index(row, 0),
                        self.index(row, 0),
                        [
                            self.TextFontSizeRole,
                            self.PanelBoxRole,
                            self.OrientationRole,
                            self.BackgroundColorRole,
                            self.TextColorRole,
                            self.StyleHintRole,
                            self.BackgroundPaddingXRole,
                            self.BackgroundPaddingYRole,
                            self.BackgroundStyleRole,
                            self.LetterSpacingPercentRole,
                            self.FontBoldRole,
                            self.FontItalicRole,
                            self.TextOpacityPercentRole,
                            self.BackgroundOpacityPercentRole,
                        ],
                    )
                    break
                continue
            language = str(change.get("language", ""))
            id_key = "cue_id" if language == "source" else "translation_cue_id"
            text_key = "source_text" if language == "source" else "translated_text"
            role = self.SourceTextRole if language == "source" else self.TranslatedTextRole
            cue_id = str(change.get("cue_id", ""))
            applied = str(change.get("applied", ""))
            previous = str(change.get("previous", ""))
            for row, item in enumerate(self._items):
                if item.get(id_key) == cue_id and str(item.get(text_key, "")) == applied:
                    item[text_key] = previous
                    changed_by_role[role].append(row)
                    break
        for role, rows in changed_by_role.items():
            self._emit_changed_ranges(rows, [role])

    def at_time(self, timeline_us: int) -> dict:
        values = [
            item
            for item in self._items_at_time(max(0, timeline_us))
            if item.get("cue_role", "dialogue") == "dialogue"
        ]
        return dict(values[-1]) if values else {}

    @Slot(float, result="QVariantList")
    def regionsAtTime(self, timeline_us: float) -> list[dict]:
        return [
            dict(item)
            for item in self._items_at_time(max(0, int(timeline_us)))
            if item.get("cue_role", "dialogue") == "dialogue"
        ]

    @Slot(float, result="QVariantList")
    def sceneTextsAtTime(self, timeline_us: float) -> list[dict]:
        position_us = max(0, int(timeline_us))
        return [
            item
            for item in self._preview_items_at_time(position_us)
            if item.get("cue_role") == "scene_text"
        ]

    def _preview_items_at_time(self, timeline_us: int | float) -> list[dict]:
        active = self._items_at_time(timeline_us)
        if not self._scene_timing_previews:
            return [dict(item) for item in active]
        # A dragged cue can leave its persisted interval or enter from a
        # distant one. Query the stable index plus all temporary overrides,
        # retaining original row order rather than sorting by preview time.
        rows = {self._row_by_cue_id[item["cue_id"]] for item in active}
        rows.update(self._row_by_cue_id[cue_id] for cue_id in self._scene_timing_previews
                    if cue_id in self._row_by_cue_id)
        return [timed for row in sorted(rows)
                for timed in (self._with_scene_timing_preview(self._items[row]),)
                if timed["start_us"] <= timeline_us < timed["end_us"]]

    def _items_at_time(self, timeline_us: int) -> list[dict]:
        row = bisect_right(self._start_us, timeline_us) - 1
        active: list[dict] = []
        while row >= 0:
            if self._max_end_prefix[row] <= timeline_us:
                break
            item = self._items[row]
            if int(item["start_us"]) <= timeline_us < int(item["end_us"]):
                active.append(item)
            row -= 1
        active.reverse()
        return active

    def cue_ids_in_range(
        self,
        lower_us: int,
        upper_us: int,
        track_ids: set[str] | None = None,
    ) -> list[str]:
        """Return intersecting cue IDs without scanning the whole model.

        Subtitle rows are kept sorted by start time and carry a prefix maximum
        of their end times.  Use those indexes to skip rows that cannot
        intersect the requested interval, while retaining the model's stable
        row order and the existing half-open range semantics.
        """

        lower = max(0, int(lower_us))
        upper = max(lower, int(upper_us))
        if upper <= lower or not self._items:
            return []
        first = bisect_right(self._max_end_prefix, lower)
        stop = bisect_left(self._start_us, upper)
        result: list[str] = []
        for item in self._items[first:stop]:
            if track_ids is not None and str(item.get("subtitle_track_id", "")) not in track_ids:
                continue
            if int(item.get("end_us", 0)) <= lower:
                continue
            cue_id = str(item.get("cue_id", ""))
            if cue_id:
                result.append(cue_id)
        return result

    def _emit_changed_ranges(self, rows: list[int], roles: list[int]) -> None:
        """Notify QML in contiguous ranges instead of once for every cue."""

        if not rows:
            return
        ordered = sorted(set(rows))
        range_start = previous = ordered[0]
        for row in (*ordered[1:], -1):
            if row == previous + 1:
                previous = row
                continue
            self.dataChanged.emit(
                self.index(range_start, 0),
                self.index(previous, 0),
                roles,
            )
            range_start = previous = row

    def replace(
        self,
        raw_source: object,
        raw_translated: object,
        *,
        preserve_live_translations: bool = False,
    ) -> None:
        self._role_data_cache.clear()
        self._scene_timing_previews.clear()
        source = [dict(item) for item in raw_source] if isinstance(raw_source, list) else []
        translated = (
            [dict(item) for item in raw_translated] if isinstance(raw_translated, list) else []
        )
        self._source_cues = [dict(item) for item in source]
        self._translated_cues = [dict(item) for item in translated]
        live_by_index = {
            item["index"]: dict(item)
            for item in self._items
            if preserve_live_translations
            and type(item.get("index")) is int
            and item.get("status") in {"Đang dịch", "Đã lưu cache"}
            and isinstance(item.get("translated_text"), str)
            and bool(item["translated_text"].strip())
        }
        translated_by_index = {
            item.get("index"): item for item in translated if type(item.get("index")) is int
        }
        translated_by_id = {str(item.get("id", "")): item for item in translated}
        source_ids = {str(item.get("id", "")) for item in source}
        rows: list[dict] = []
        base = source if source else translated
        for fallback_index, cue in enumerate(base, start=1):
            index = cue.get("index", fallback_index)
            timing = cue.get("range", {})
            translation = (translated_by_id.get(str(cue.get("id", "")))
                           or translated_by_index.get(index, {})) if source else cue
            if (source and translation.get("id") in source_ids
                    and translation.get("id") != cue.get("id")):
                translation = {}
            if translation and _range_from_mapping(translation.get("range")) is not None:
                timing = translation["range"]
            live = live_by_index.get(index, {}) if source and not translation else {}
            if live and not (
                live.get("cue_id") == str(cue.get("id", ""))
                and live.get("start_us") == timing.get("start_us", 0)
                and live.get("end_us") == timing.get("end_us", 0)
                and live.get("source_text") == str(cue.get("text", ""))
            ):
                live = {}
            confidence = cue.get("confidence")
            rows.append(
                {
                    "cue_id": str(cue.get("id", "")),
                    "index": index if type(index) is int else fallback_index,
                    "start_us": timing.get("start_us", 0),
                    "end_us": timing.get("end_us", 0),
                    "start_text": _timecode_from_us(timing.get("start_us")),
                    "end_text": _timecode_from_us(timing.get("end_us")),
                    "source_text": str(cue.get("text", "")) if source else "",
                    "translated_text": (
                        str(translation.get("text", ""))
                        if translation
                        else str(live.get("translated_text", ""))
                    ),
                    "status": ("Đã dịch" if translation else str(live.get("status", "Nguồn"))),
                    "confidence": float(confidence)
                    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                    else -1.0,
                    "provenance": (
                        str(live.get("provenance", ""))
                        if live
                        else str(cue.get("source", "manual"))
                    ),
                    "revision": cue.get("revision", 1),
                    "visual_box": cue.get("visual_box"),
                    "visual_box_source": str(cue.get("visual_box_source", "")),
                    "cue_role": str(cue.get("role", "dialogue")),
                    "orientation": str(cue.get("orientation", "horizontal")),
                    "style_hint": str(cue.get("style_hint", "standard")),
                    "panel_box": cue.get("panel_box"),
                    "background_color": str(cue.get("background_color", "")),
                    "text_color": str(cue.get("text_color", "")),
                    "review_state": str(cue.get("review_state", "accepted")),
                    "scene_font_scale": float(cue.get("scene_font_scale", 1.0)),
                    "text_font_size": int(cue.get("text_font_size", 12)),
                    "background_padding_x": int(cue.get("background_padding_x", 20)),
                    "background_padding_y": int(cue.get("background_padding_y", 10)),
                    "background_style": str(cue.get("background_style", "solid")),
                    "letter_spacing_percent": int(cue.get("letter_spacing_percent", 0)),
                    "font_bold": bool(cue.get("font_bold", False)),
                    "font_italic": bool(cue.get("font_italic", False)),
                    "text_opacity_percent": int(cue.get("text_opacity_percent", 100)),
                    "background_opacity_percent": int(cue.get("background_opacity_percent", 90)),
                    "fade_enabled": bool(cue.get("fade_enabled", False)),
                    "fade_duration_ms": int(cue.get("fade_duration_ms", 300)),
                    "subtitle_track_id": str(
                        cue.get("subtitle_track_id", "")
                        or ("S1" if str(cue.get("role", "dialogue")) == "dialogue" else "S2")
                    ),
                    "translation_cue_id": (
                        str(translation.get("id", ""))
                        if translation
                        else str(live.get("translation_cue_id", ""))
                    ),
                    "qa_issue_code": "",
                    "qa_issue_message": "",
                    "timing_too_long": False,
                }
            )
            if translation:
                for key in (
                    "visual_box",
                    "visual_box_source",
                    "orientation",
                    "style_hint",
                    "panel_box",
                    "background_color",
                    "text_color",
                    "review_state",
                    "scene_font_scale",
                    "text_font_size",
                    "background_padding_x",
                    "background_padding_y",
                    "background_style",
                    "letter_spacing_percent",
                    "font_bold",
                    "font_italic",
                    "text_opacity_percent",
                    "background_opacity_percent",
                    "fade_enabled",
                    "fade_duration_ms",
                    "subtitle_track_id",
                ):
                    if key in translation:
                        rows[-1][key] = translation[key]
        self._patch_rows(rows)

    def apply_cue_patch(
        self,
        source_upserts: object,
        removed_source_ids: object,
        translated_upserts: object,
        removed_translated_ids: object,
    ) -> None:
        """Apply a compact project cue delta while preserving stable model rows."""

        def patched(current: list[dict], upserts: object, removed_ids: object) -> list[dict]:
            removed = (
                {str(value) for value in removed_ids if isinstance(value, str) and value}
                if isinstance(removed_ids, list)
                else set()
            )
            by_id = {
                str(item.get("id", "")): dict(item)
                for item in current
                if str(item.get("id", "")) and str(item.get("id", "")) not in removed
            }
            if isinstance(upserts, list):
                for raw in upserts:
                    if not isinstance(raw, dict):
                        continue
                    cue_id = str(raw.get("id", ""))
                    if cue_id:
                        by_id[cue_id] = dict(raw)
            return sorted(
                by_id.values(),
                key=lambda item: (
                    int(item.get("index", 0) or 0),
                    int((item.get("range") or {}).get("start_us", 0))
                    if isinstance(item.get("range"), dict)
                    else 0,
                    str(item.get("id", "")),
                ),
            )

        source = patched(self._source_cues, source_upserts, removed_source_ids)
        translated = patched(self._translated_cues, translated_upserts, removed_translated_ids)
        self.replace(source, translated)

    def set_qa_issues(self, raw_issues: object) -> int:
        issues = (
            [dict(item) for item in raw_issues if isinstance(item, dict)]
            if isinstance(raw_issues, list)
            else []
        )
        by_cue_id = {
            str(item.get("cue_id", "")): item
            for item in issues
            if str(item.get("cue_id", "")).strip()
        }
        by_index = {int(item["id"]): item for item in issues if type(item.get("id")) is int}
        changed = 0
        roles = [
            self.QaIssueCodeRole,
            self.QaIssueMessageRole,
            self.TimingTooLongRole,
        ]
        changed_rows: list[int] = []
        for row, model_item in enumerate(self._items):
            issue = by_cue_id.get(model_item["cue_id"]) or by_index.get(model_item["index"])
            code = str(issue.get("code", "")) if issue else ""
            message = str(issue.get("message", "")) if issue else ""
            timing_too_long = "CUE_TOO_LONG" in {
                value.strip() for value in code.replace(",", " ").split() if value.strip()
            }
            next_values = (code, message, timing_too_long)
            current_values = (
                model_item.get("qa_issue_code", ""),
                model_item.get("qa_issue_message", ""),
                model_item.get("timing_too_long", False),
            )
            if next_values == current_values:
                continue
            model_item["qa_issue_code"] = code
            model_item["qa_issue_message"] = message
            model_item["timing_too_long"] = timing_too_long
            changed_rows.append(row)
            changed += 1
        self._emit_changed_ranges(changed_rows, roles)
        return changed

    @Slot(object, result=int)
    def apply_translation_delta(self, raw_values: object) -> int:
        values = raw_values if isinstance(raw_values, list) else []
        changed_rows: list[int] = []
        for raw in values:
            if not isinstance(raw, dict):
                continue
            cue_index = raw.get("index")
            text = raw.get("text")
            if type(cue_index) is not int or cue_index <= 0:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            row = self._row_by_cue_index.get(cue_index)
            if row is None or not 0 <= row < len(self._items):
                continue
            item = self._items[row]
            if item["status"] == "Đã dịch":
                continue
            cue_id = raw.get("id")
            if isinstance(cue_id, str) and cue_id.strip() and cue_id != item["cue_id"]:
                continue
            normalized = text.strip()
            # Resume snapshots and QA repair retries can replay an identical
            # value. Keep the current terminal/live-cache state in that case;
            # emitting another dataChanged would make the visible editor
            # flicker and can reset an actively inspected row.
            if item["translated_text"] == normalized:
                continue
            item["translated_text"] = normalized
            item["status"] = "Đang dịch"
            item["provenance"] = "gemini_translation_live"
            item["translation_cue_id"] = item["cue_id"]
            changed_rows.append(row)
        roles = [
            self.TranslatedTextRole,
            self.StatusRole,
            self.ProvenanceRole,
            self.TranslationCueIdRole,
        ]
        self._emit_changed_ranges(changed_rows, roles)
        return len(changed_rows)

    @Slot(result=int)
    def finalize_translation_preview(self) -> int:
        changed_rows: list[int] = []
        for row, item in enumerate(self._items):
            if item["status"] != "Đang dịch":
                continue
            item["status"] = "Đã lưu cache"
            changed_rows.append(row)
        self._emit_changed_ranges(changed_rows, [self.StatusRole])
        return len(changed_rows)

    def _patch_rows(self, rows: list[dict]) -> None:
        # OCR stores dialogue (S1) before scene text (S2), so cue indices are
        # not necessarily chronological across tracks.  Preview lookup uses a
        # binary search over ``_start_us`` and therefore requires one globally
        # ordered timeline, regardless of the source track/index ordering.
        rows.sort(
            key=lambda item: (
                int(item["start_us"]),
                int(item["end_us"]),
                int(item["index"]),
                str(item["cue_id"]),
            )
        )
        old_ids = [item["cue_id"] for item in self._items]
        new_ids = [item["cue_id"] for item in rows]
        prefix = 0
        while prefix < min(len(old_ids), len(new_ids)) and old_ids[prefix] == new_ids[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < len(old_ids) - prefix
            and suffix < len(new_ids) - prefix
            and old_ids[-1 - suffix] == new_ids[-1 - suffix]
        ):
            suffix += 1
        old_middle = len(old_ids) - prefix - suffix
        new_middle = len(new_ids) - prefix - suffix
        changed_rows = [index for index in range(prefix) if self._items[index] != rows[index]]
        changed_rows.extend(
            len(rows) - offset
            for offset in range(suffix, 0, -1)
            if self._items[-offset] != rows[-offset]
        )
        # Qt notifications can synchronously query Preview and cue selection.
        # Publish coherent rows and lookup indexes before each notification,
        # including the intermediate remove-before-insert state. Use the final
        # retained values so retimed prefix/suffix rows remain time-ordered.
        if old_middle:
            self.beginRemoveRows(_INVALID_INDEX, prefix, prefix + old_middle - 1)
            self._items[:] = rows[:prefix] + (rows[-suffix:] if suffix else [])
            self._rebuild_lookup_indexes()
            self.endRemoveRows()
        if new_middle:
            self.beginInsertRows(_INVALID_INDEX, prefix, prefix + new_middle - 1)
            self._items[:] = rows
            self._rebuild_lookup_indexes()
            self.endInsertRows()
        if not old_middle and not new_middle:
            self._items[:] = rows
            self._rebuild_lookup_indexes()
        self._emit_changed_ranges(changed_rows, list(self.roleNames()))
        if len(old_ids) != len(new_ids):
            self.countChanged.emit()

    def _rebuild_lookup_indexes(self) -> None:
        self._row_by_cue_index = {
            int(item["index"]): row
            for row, item in enumerate(self._items)
            if type(item.get("index")) is int
        }
        self._row_by_cue_id = {
            str(item["cue_id"]): row
            for row, item in enumerate(self._items)
            if str(item.get("cue_id", ""))
        }
        self._start_us = [int(item["start_us"]) for item in self._items]
        maximum = 0
        self._max_end_prefix = []
        for item in self._items:
            maximum = max(maximum, int(item["end_us"]))
            self._max_end_prefix.append(maximum)


class OverlayListModel(QAbstractListModel):
    countChanged = Signal()
    OverlayRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        if role in {self.OverlayRole, Qt.ItemDataRole.DisplayRole}:
            return dict(self._items[index.row()])
        return None

    def roleNames(self) -> dict[int, bytes]:
        return {self.OverlayRole: b"overlay"}

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    def replace(self, raw_items: object) -> None:
        items = [dict(item) for item in raw_items] if isinstance(raw_items, list) else []
        if items == self._items:
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()

    def apply_patch(self, upserts: object, removed_ids: object) -> None:
        removed = (
            {str(value) for value in removed_ids if isinstance(value, str) and value}
            if isinstance(removed_ids, list)
            else set()
        )
        for row in range(len(self._items) - 1, -1, -1):
            if str(self._items[row].get("id", "")) not in removed:
                continue
            self.beginRemoveRows(_INVALID_INDEX, row, row)
            self._items.pop(row)
            self.endRemoveRows()
            self.countChanged.emit()
        values = (
            [dict(item) for item in upserts if isinstance(item, dict)]
            if isinstance(upserts, list)
            else []
        )
        for value in values:
            overlay_id = str(value.get("id", ""))
            row = next(
                (index for index, item in enumerate(self._items) if item.get("id") == overlay_id),
                -1,
            )
            if row >= 0:
                if self._items[row] == value:
                    continue
                self._items[row] = value
                self.dataChanged.emit(
                    self.index(row, 0),
                    self.index(row, 0),
                    [self.OverlayRole],
                )
                continue
            row = len(self._items)
            self.beginInsertRows(_INVALID_INDEX, row, row)
            self._items.append(value)
            self.endInsertRows()
            self.countChanged.emit()


class ProjectStateModel(QObject):
    changed = Signal()
    subtitleVisualsChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tracks = TimelineTrackListModel(self)
        self.clips = TimelineClipListModel(self)
        self.voice_conform_cues = VoiceConformCueListModel(self)
        self.subtitle_cues = SubtitleCueListModel(self)
        self.title_cues = SubtitleCueListModel(self)
        self.subtitle_cues.extra_text_model = self.title_cues
        self.title_cues.dataChanged.connect(self._bump_subtitle_visual_revision)
        self.title_cues.modelReset.connect(self._bump_subtitle_visual_revision)
        self.title_cues.rowsInserted.connect(self._bump_subtitle_visual_revision)
        self.title_cues.rowsRemoved.connect(self._bump_subtitle_visual_revision)
        self.subtitle_cues.dataChanged.connect(self._bump_subtitle_visual_revision)
        self.subtitle_cues.modelReset.connect(self._bump_subtitle_visual_revision)
        self.subtitle_cues.rowsInserted.connect(self._bump_subtitle_visual_revision)
        self.subtitle_cues.rowsRemoved.connect(self._bump_subtitle_visual_revision)
        self.overlay_model = OverlayListModel(self)
        self._project_id = ""
        self._name = ""
        self._revision = 0
        self._content_revision = 0
        self._duration_us = 0
        self._video_source = QUrl()
        self._clip_by_id: dict[str, dict] = {}
        self._track_by_id: dict[str, dict] = {}
        self._video_clips: list[dict] = []
        self._video_starts: list[int] = []
        self._voice_clips: list[dict] = []
        self._voice_starts: list[int] = []
        self._voice_source = QUrl()
        self._media_path_by_id: dict[str, str] = {}
        self._linked_audio_by_group: dict[str, dict] = {}
        self._overlays: list[dict] = []
        self._primary_video_path = ""
        self._has_translated_subtitles = False
        self._voice_conform_plan: VoiceConformPlan | None = None
        self._voice_track_index = -1
        self._translation_style_preset = "auto"
        self._translation_custom_rules = ""
        self._translation_style_presets: list[dict[str, str]] = []
        self._translation_active_style_name = ""
        self._translation_preferences_revision = 0
        self._ocr_dialogue_region: list[float] = []
        self._ocr_exclusion_regions: list[list[float]] = []
        self._timeline_markers: list[int] = []
        self._source_region_ocr_count = 0
        self._source_region_total = 0
        self._subtitle_visual_revision = 0
        # Timeline cuts are non-destructive.  Models render a projected copy,
        # while every later compact patch is applied to this canonical source
        # snapshot before it is projected again.
        self._canonical_snapshot: dict = {}

    def _bump_subtitle_visual_revision(self, *_args) -> None:
        self._subtitle_visual_revision += 1
        self.subtitleVisualsChanged.emit()

    @Property(bool, notify=changed)
    def hasProject(self) -> bool:
        return bool(self._project_id)

    @Property(str, notify=changed)
    def projectId(self) -> str:
        return self._project_id

    @Property(str, notify=changed)
    def name(self) -> str:
        return self._name

    @Property(int, notify=changed)
    def revision(self) -> int:
        return self._revision

    @property
    def content_revision(self) -> int:
        """Last acknowledged non-style snapshot, excluding disjoint style ACKs."""

        return self._content_revision

    def content_patch_snapshot(self) -> dict:
        return {**self._canonical_snapshot, "revision": self._content_revision}

    @Property(int, notify=subtitleVisualsChanged)
    def subtitleVisualRevision(self) -> int:
        return self._subtitle_visual_revision

    @Property(float, notify=changed)
    def durationUs(self) -> float:
        return float(self._duration_us)

    @Property(str, notify=changed)
    def durationText(self) -> str:
        total_seconds = self._duration_us // 1_000_000
        hours, remainder = divmod(total_seconds, 3_600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @Property(bool, notify=changed)
    def hasVideo(self) -> bool:
        return bool(self._video_clips) and not self._video_source.isEmpty()

    @Property(QUrl, notify=changed)
    def videoSource(self) -> QUrl:
        return self._video_source

    @Property(str, notify=changed)
    def primaryVideoPath(self) -> str:
        return self._primary_video_path

    @Property(bool, notify=changed)
    def hasTranslatedSubtitles(self) -> bool:
        return self._has_translated_subtitles

    @Property(str, notify=changed)
    def translationStylePreset(self) -> str:
        return self._translation_style_preset

    @Property(str, notify=changed)
    def translationCustomRules(self) -> str:
        return self._translation_custom_rules

    @Property("QVariantList", notify=changed)
    def translationStylePresets(self) -> list[dict[str, str]]:
        return [dict(item) for item in self._translation_style_presets]

    @Property(str, notify=changed)
    def translationActiveStyleName(self) -> str:
        return self._translation_active_style_name

    @Property("QVariantList", notify=changed)
    def ocrDialogueRegion(self) -> list[float]:
        return list(self._ocr_dialogue_region)

    @Property("QVariantList", notify=changed)
    def ocrExclusionRegions(self) -> list[list[float]]:
        return [list(item) for item in self._ocr_exclusion_regions]

    @Property("QVariantList", notify=changed)
    def timelineMarkers(self) -> list[int]:
        return list(self._timeline_markers)

    @Property(str, notify=changed)
    def sourceSubtitleRegionStatus(self) -> str:
        if self._source_region_total <= 0:
            return "Chưa có cue nguồn"
        if self._source_region_ocr_count == self._source_region_total:
            return f"Theo OCR · {self._source_region_ocr_count} cue"
        if self._source_region_ocr_count > 0:
            estimated = self._source_region_total - self._source_region_ocr_count
            return f"Theo OCR {self._source_region_ocr_count} · Ước lượng {estimated}"
        return f"Ước lượng · {self._source_region_total} cue"

    @Property(bool, notify=changed)
    def hasVoiceTrack(self) -> bool:
        return bool(self._voice_clips)

    @Property(bool, notify=changed)
    def voiceClipsAreCueScoped(self) -> bool:
        """Whether the published D1 track has one clip per SRT cue."""
        return bool(self._voice_clips) and all(
            isinstance(clip.get("cue_id"), str) and bool(clip.get("cue_id"))
            for clip in self._voice_clips
        )

    @Property(bool, notify=changed)
    def voiceConformApplied(self) -> bool:
        return self._voice_conform_plan is not None

    @Property(int, notify=changed)
    def voiceTrackIndex(self) -> int:
        return self._voice_track_index

    @Property(QUrl, notify=changed)
    def voiceSource(self) -> QUrl:
        return self._voice_source

    @Property("QVariantList", notify=changed)
    def overlays(self) -> list[dict]:
        return self._overlays

    @Property(QObject, constant=True)
    def overlayModel(self) -> QObject:
        return self.overlay_model

    @Slot(str, result="QVariantMap")
    def overlay(self, overlay_id: str) -> dict:
        return next((item for item in self._overlays if item.get("id") == overlay_id), {})

    @Slot(str, result=QUrl)
    def mediaSource(self, media_id: str) -> QUrl:
        path = self._media_path_by_id.get(media_id, "")
        return QUrl.fromLocalFile(path) if path else QUrl()

    @Slot(str, result=str)
    def mediaName(self, media_id: str) -> str:
        path = self._media_path_by_id.get(media_id, "")
        return Path(path).name if path else ""

    @Property(QObject, constant=True)
    def trackModel(self) -> QObject:
        return self.tracks

    @Property(QObject, constant=True)
    def clipModel(self) -> QObject:
        return self.clips

    @Property(QObject, constant=True)
    def voiceConformCueModel(self) -> QObject:
        return self.voice_conform_cues

    @Property(QObject, constant=True)
    def subtitleCueModel(self) -> QObject:
        return self.subtitle_cues

    @Slot(float, result="QVariantMap")
    def subtitleAt(self, timeline_us: float) -> dict:
        position_us = max(0, int(timeline_us))
        return self.subtitle_cues.at_time(position_us)

    @Slot(float, result="QVariantList")
    def sourceSubtitleRegionsAt(self, timeline_us: float) -> list[dict]:
        position_us = max(0, int(timeline_us))
        return self.subtitle_cues.regionsAtTime(position_us)

    @Slot(float, result="QVariantList")
    def sceneTextsAt(self, timeline_us: float) -> list[dict]:
        position_us = max(0, int(timeline_us))
        return self.subtitle_cues.sceneTextsAtTime(position_us)

    @Slot(float, result="QVariantList")
    def titleTextsAt(self, timeline_us: float) -> list[dict]:
        if any(track.get("kind") == "text" and not track.get("visible", True)
               for track in self.tracks._items):
            return []
        return self.title_cues._preview_items_at_time(timeline_us)

    @Slot(result="QVariantList")
    def titleCueIds(self) -> list[str]:
        return [cue["cue_id"] for cue in self.title_cues._items]

    def text_cue_model(self, cue_id: str) -> SubtitleCueListModel:
        return self.title_cues if self.title_cues.find(cue_id) else self.subtitle_cues

    @Slot(float, float, int, int, result="QVariantList")
    def subtitleCueIdsInRange(
        self,
        start_us: float,
        end_us: float,
        first_track_index: int,
        last_track_index: int,
    ) -> list[str]:
        lower_time = max(0, int(min(start_us, end_us)))
        upper_time = max(lower_time, int(max(start_us, end_us)))
        lower_track = min(first_track_index, last_track_index)
        upper_track = max(first_track_index, last_track_index)
        track_positions = {
            str(item.get("id", "")): index for index, item in enumerate(self.tracks._items)
        }
        visible_track_ids = {
            track_id
            for track_id, track_index in track_positions.items()
            if track_id and lower_track <= track_index <= upper_track
        }
        # Each subtitle model already maintains a sorted start-time index and
        # prefix maximum of end times.  Query those indexes separately to keep
        # the historical subtitle-then-title ordering without scanning every
        # cue on every marquee release.
        return [
            *self.subtitle_cues.cue_ids_in_range(
                lower_time,
                upper_time,
                visible_track_ids,
            ),
            *self.title_cues.cue_ids_in_range(
                lower_time,
                upper_time,
                visible_track_ids,
            ),
        ]

    @Slot(str, result=int)
    def subtitleTrackIndexForCueId(self, cue_id: str) -> int:
        cue = self.text_cue_model(cue_id).find(str(cue_id or ""))
        track_id = str(cue.get("subtitle_track_id", ""))
        if not track_id:
            return -1
        return next(
            (
                index
                for index, item in enumerate(self.tracks._items)
                if str(item.get("id", "")) == track_id
            ),
            -1,
        )

    @Slot(str, result=bool)
    def isSceneTextCue(self, cue_id: str) -> bool:
        cue = self.text_cue_model(cue_id).find(str(cue_id or ""))
        return cue.get("cue_role") in {"scene_text", "title_text"}

    @Slot(float, result=float)
    def sourceToOutputUs(self, source_us: float) -> float:
        mapped = base_to_timeline_us(
            self._canonical_snapshot,
            max(0, int(source_us)),
            endpoint="start",
        )
        if (
            mapped is None
            and not self._canonical_snapshot.get("timeline_edit")
            and self._voice_conform_plan is not None
        ):
            mapped = self._voice_conform_plan.source_to_output_us(int(source_us))
        return -1.0 if mapped is None else float(mapped)

    @Slot(float, result=float)
    def outputToSourceUs(self, output_us: float) -> float:
        try:
            mapped = timeline_to_base_us(
                self._canonical_snapshot,
                max(0, int(output_us)),
                endpoint="start",
            )
        except ValueError:
            if self._canonical_snapshot.get("timeline_edit") or self._voice_conform_plan is None:
                raise
            mapped = self._voice_conform_plan.output_to_source_us(int(output_us))
        return float(mapped)

    def clip_info(self, clip_id: str) -> dict | None:
        return self._clip_by_id.get(clip_id)

    @Slot(str, float, float, float, result="QVariantList")
    def timelineMediaTiles(self, clip_id: str, start_us: float, end_us: float,
                           width: float) -> list[dict]:
        from ducai_studio.application.timeline_previews import visible_media_tiles
        from ducai_studio.ui.timeline_media_provider import timeline_tile_url

        clip = self._clip_by_id.get(clip_id, {})
        kind = self._track_by_id.get(clip.get("track_id"), {}).get("kind")
        path = self._media_path_by_id.get(clip.get("media_id"), "")
        timeline = clip.get("timeline_range", {})
        source = clip.get("source_range")
        if kind not in {"video", "original_audio", "voice", "background_music"} or not path:
            return []
        if not isinstance(source, dict) or end_us <= start_us or width < 24:
            return []
        begin, end = int(timeline.get("start_us", 0)), int(timeline.get("end_us", 0))
        speed = clip.get("speed", {})
        numerator, denominator = speed.get("numerator", 1), speed.get("denominator", 1)
        if min(numerator, denominator) <= 0 or end <= begin:
            return []

        def source_time(position):
            return min(int(source["end_us"]), int(source["start_us"])
                       + (position - begin) * numerator // denominator)

        segments = []
        plan = self._voice_conform_plan if kind in {"video", "original_audio"} else None
        if plan is not None:
            for segment in plan.segments:
                left, right = max(begin, segment.source_start_us), min(end, segment.source_end_us)
                if right > left:
                    segments.append({"start_us": plan.source_to_output_us(left),
                                     "end_us": plan.source_to_output_us(right),
                                     "source_start_us": source_time(left),
                                     "source_end_us": source_time(right)})
        else:
            segments = [{"start_us": begin, "end_us": end,
                         "source_start_us": source_time(begin), "source_end_us": source_time(end)}]
        result = visible_media_tiles(segments, int(start_us), int(end_us),
                                     width / (end_us - start_us), waveform=kind != "video")
        return [{**tile, "url": timeline_tile_url(path, tile)} for tile in result]

    def clip_timing_info(self, clip_id: str) -> dict[str, object]:
        clip = self._clip_by_id.get(clip_id)
        return (
            _voice_timing_metrics(clip)
            if clip is not None
            else {
                "state": "unknown",
                "duration_delta_us": 0,
                "start_delta_us": 0,
                "required_speed": 1.0,
            }
        )

    def track_info(self, track_id: str) -> dict | None:
        return self._track_by_id.get(track_id)

    def track_is_persisted(self, track_id: str) -> bool:
        return any(track.get("id") == track_id
                   for track in self._canonical_snapshot.get("tracks", []))

    @Slot(str, result="QVariantMap")
    def track(self, track_id: str) -> dict:
        return self._track_by_id.get(track_id, {})

    @Slot(str, result=float)
    def trackVolumePercent(self, track_id: str) -> float:
        track = self._track_by_id.get(track_id, {})
        clips = track.get("clips", []) if isinstance(track, dict) else []
        if not clips or not isinstance(clips[0], dict):
            return 100.0
        volume = clips[0].get("volume", 1.0)
        if isinstance(volume, bool) or not isinstance(volume, (int, float)):
            return 100.0
        return max(0.0, min(200.0, float(volume) * 100.0))

    @Slot(float, result="QVariantMap")
    def previewDescriptor(self, timeline_us: float) -> dict:
        position_us = max(0, int(timeline_us))
        if self._voice_conform_plan is not None:
            return self._conformed_preview_descriptor(position_us)
        index = bisect_right(self._video_starts, position_us) - 1
        clip: dict | None = None
        while index >= 0:
            candidate = self._video_clips[index]
            timeline_range = candidate["timeline_range"]
            if timeline_range["start_us"] <= position_us < timeline_range["end_us"]:
                clip = candidate
                break
            index -= 1
        if clip is None:
            next_index = bisect_left(self._video_starts, position_us)
            next_start = (
                self._video_starts[next_index] if next_index < len(self._video_starts) else -1
            )
            return {"available": False, "nextTimelineStartUs": next_start}

        media_id = clip.get("media_id")
        media_path = self._media_path_by_id.get(media_id, "")
        source_range = clip.get("source_range")
        speed = clip.get("speed", {"numerator": 1, "denominator": 1})
        numerator = speed.get("numerator", 1)
        denominator = speed.get("denominator", 1)
        if (
            not media_path
            or not isinstance(source_range, dict)
            or type(numerator) is not int
            or type(denominator) is not int
            or numerator <= 0
            or denominator <= 0
        ):
            return {"available": False, "nextTimelineStartUs": -1}
        timeline_range = clip["timeline_range"]
        timeline_offset = position_us - timeline_range["start_us"]
        source_position_us = min(
            source_range["end_us"],
            source_range["start_us"] + timeline_offset * numerator // denominator,
        )
        audio_volume = clip.get("volume", 1.0)
        link_group_id = clip.get("link_group_id")
        linked_audio = (
            self._linked_audio_by_group.get(link_group_id)
            if isinstance(link_group_id, str)
            else None
        )
        if linked_audio is not None:
            audio_volume = linked_audio.get("volume", 1.0)
            if linked_audio.get("track_muted") is True or linked_audio.get("muted") is True:
                audio_volume = 0.0
        if clip.get("track_muted") is True or clip.get("muted") is True:
            audio_volume = 0.0
        if isinstance(audio_volume, bool) or not isinstance(audio_volume, (int, float)):
            audio_volume = 1.0
        voice_descriptor: dict[str, object] = {"voiceAvailable": False}
        voice_index = bisect_right(self._voice_starts, position_us) - 1
        if 0 <= voice_index < len(self._voice_clips):
            voice_clip = self._voice_clips[voice_index]
            voice_range = voice_clip["timeline_range"]
            if voice_range["start_us"] <= position_us < voice_range["end_us"]:
                voice_source = voice_clip.get("source_range")
                voice_media_path = self._media_path_by_id.get(voice_clip.get("media_id"), "")
                voice_speed = voice_clip.get("speed", {"numerator": 1, "denominator": 1})
                voice_numerator = voice_speed.get("numerator", 1)
                voice_denominator = voice_speed.get("denominator", 1)
                if (
                    voice_media_path
                    and isinstance(voice_source, dict)
                    and type(voice_numerator) is int
                    and type(voice_denominator) is int
                    and voice_numerator > 0
                    and voice_denominator > 0
                ):
                    voice_offset = position_us - voice_range["start_us"]
                    voice_position_us = min(
                        voice_source["end_us"],
                        voice_source["start_us"]
                        + voice_offset * voice_numerator // voice_denominator,
                    )
                    voice_volume = (
                        0.0
                        if voice_clip.get("track_muted") is True or voice_clip.get("muted") is True
                        else voice_clip.get("volume", 1.0)
                    )
                    if isinstance(voice_volume, bool) or not isinstance(voice_volume, (int, float)):
                        voice_volume = 1.0
                    voice_descriptor = {
                        "voiceAvailable": True,
                        "voiceSourceUrl": QUrl.fromLocalFile(voice_media_path).toString(),
                        "voicePositionMs": voice_position_us / 1_000,
                        "voicePlaybackRate": voice_numerator / voice_denominator,
                        "voiceVolume": max(0.0, min(2.0, float(voice_volume))),
                    }
        return {
            "available": True,
            "clipId": clip["id"],
            "sourceUrl": QUrl.fromLocalFile(media_path).toString(),
            "sourcePositionMs": source_position_us / 1_000,
            "sourceStartMs": source_range["start_us"] / 1_000,
            "sourceEndMs": source_range["end_us"] / 1_000,
            "timelineStartUs": timeline_range["start_us"],
            "timelineEndUs": timeline_range["end_us"],
            "speedRate": numerator / denominator,
            "audioVolume": max(0.0, min(2.0, float(audio_volume))),
            **voice_descriptor,
        }

    def _conformed_preview_descriptor(self, position_us: int) -> dict:
        plan = self._voice_conform_plan
        if plan is None or not self._video_clips or position_us >= plan.output_duration_us:
            return {"available": False, "nextTimelineStartUs": -1}
        segment = plan.segments[bisect_right(
            plan.segments, position_us, key=lambda item: item.output_end_us
        )]
        clip = self._video_clips[0]
        media_path = self._media_path_by_id.get(clip.get("media_id"), "")
        if not media_path:
            return {"available": False, "nextTimelineStartUs": -1}
        audio_volume = clip.get("volume", 1.0)
        link_group_id = clip.get("link_group_id")
        linked_audio = (
            self._linked_audio_by_group.get(link_group_id)
            if isinstance(link_group_id, str)
            else None
        )
        if linked_audio is not None:
            audio_volume = linked_audio.get("volume", 1.0)
            if linked_audio.get("track_muted") is True or linked_audio.get("muted") is True:
                audio_volume = 0.0
        if clip.get("track_muted") is True or clip.get("muted") is True:
            audio_volume = 0.0
        if isinstance(audio_volume, bool) or not isinstance(audio_volume, (int, float)):
            audio_volume = 1.0
        source_position_us = plan.output_to_source_us(position_us)
        voice_descriptor: dict[str, object] = {"voiceAvailable": False}
        voice_index = bisect_right(self._voice_starts, position_us) - 1
        if 0 <= voice_index < len(self._voice_clips):
            voice_clip = self._voice_clips[voice_index]
            voice_range = voice_clip["timeline_range"]
            if voice_range["start_us"] <= position_us < voice_range["end_us"]:
                voice_media_path = self._media_path_by_id.get(voice_clip.get("media_id"), "")
                if voice_media_path:
                    voice_volume = (
                        0.0
                        if voice_clip.get("track_muted") is True
                        else voice_clip.get("volume", 1.0)
                    )
                    if isinstance(voice_volume, bool) or not isinstance(voice_volume, (int, float)):
                        voice_volume = 1.0
                    voice_descriptor = {
                        "voiceAvailable": True,
                        "voiceSourceUrl": QUrl.fromLocalFile(voice_media_path).toString(),
                        "voicePositionMs": position_us / 1_000,
                        "voicePlaybackRate": 1.0,
                        "voiceVolume": max(0.0, min(2.0, float(voice_volume))),
                    }
        return {
            "available": True,
            "clipId": clip["id"],
            "sourceUrl": QUrl.fromLocalFile(media_path).toString(),
            "sourcePositionMs": source_position_us / 1_000,
            "sourceStartMs": segment.source_start_us / 1_000,
            "sourceEndMs": segment.source_end_us / 1_000,
            "timelineStartUs": segment.output_start_us,
            "timelineEndUs": segment.output_end_us,
            "speedRate": segment.video_speed,
            "audioVolume": max(0.0, min(2.0, float(audio_volume))),
            **voice_descriptor,
        }

    def replace(
        self,
        raw_snapshot: object,
        *,
        preserve_live_translations: bool = False,
        take_ownership: bool = False,
    ) -> None:
        # IPC decoding already created an owned object graph. Transferring it
        # avoids a second O(project-size) Python deepcopy on the QML thread.
        # Normal callers retain the original defensive-copy contract.
        canonical = (
            raw_snapshot if take_ownership else deepcopy(raw_snapshot)
        ) if isinstance(raw_snapshot, dict) else {}
        self._replace_canonical_snapshot(
            canonical,
            preserve_live_translations=preserve_live_translations,
        )

    def _replace_canonical_snapshot(
        self,
        canonical: dict,
        *,
        preserve_live_translations: bool = False,
    ) -> None:
        projected = (
            project_json_for_timeline(canonical)
            if canonical.get("timeline_edit")
            else canonical
        )
        self._canonical_snapshot = canonical
        revision = canonical.get("revision", 0)
        self._translation_preferences_revision = revision if type(revision) is int else 0
        self._content_revision = self._translation_preferences_revision
        self._replace_projected_snapshot(
            projected,
            preserve_live_translations=preserve_live_translations,
        )

    def _replace_projected_snapshot(
        self,
        snapshot: dict,
        *,
        preserve_live_translations: bool = False,
    ) -> None:
        raw_conform = snapshot.get("voice_conform")
        self._voice_conform_plan = None
        if isinstance(raw_conform, dict):
            try:
                self._voice_conform_plan = decode_voice_conform_plan(raw_conform.get("plan"))
            except (KeyError, TypeError, ValueError):
                self._voice_conform_plan = None
        source_cues = self._map_cue_ranges(snapshot.get("source_cues", []))
        self._source_region_total = len(source_cues)
        self._source_region_ocr_count = sum(
            1
            for cue in source_cues
            if cue.get("visual_box_source") == "ocr"
            and isinstance(cue.get("visual_box"), (list, tuple))
            and len(cue["visual_box"]) == 4
        )
        translated_cues = self._map_cue_ranges(snapshot.get("translated_cues", []))
        title_cues = self._map_cue_ranges(snapshot.get("title_cues", []))
        raw_tracks = self._restore_legacy_voice_cue_metadata(
            snapshot.get("tracks", []),
            source_cues,
            translated_cues,
        )
        raw_tracks = self._restore_legacy_subtitle_cue_metadata(
            raw_tracks,
            source_cues,
            translated_cues,
        )
        # The empty Text line is an affordance, not a persisted fake clip.
        if snapshot.get("id"):
            text_tracks = [track for track in raw_tracks if track.get("kind") == "text"]
            raw_tracks = (text_tracks or [{"id": "T1", "kind": "text", "name": "Text",
                                          "clips": []}]) + [
                track for track in raw_tracks if track.get("kind") != "text"
            ]
        self._track_by_id = {
            track["id"]: track
            for track in raw_tracks
            if isinstance(track, dict) and isinstance(track.get("id"), str) and track["id"]
        }
        self._voice_track_index = next(
            (
                index
                for index, track in enumerate(raw_tracks)
                if isinstance(track, dict) and track.get("kind") == "voice"
            ),
            -1,
        )
        self.tracks.replace(raw_tracks)
        self.clips.replace(self._display_clip_tracks(raw_tracks))
        self.voice_conform_cues.replace(self._voice_conform_plan)
        self.subtitle_cues.replace(
            source_cues,
            translated_cues,
            preserve_live_translations=preserve_live_translations,
        )
        self.title_cues.replace(title_cues, [])
        clips = [
            clip
            for track in raw_tracks
            if isinstance(track, dict)
            for clip in track.get("clips", [])
            if isinstance(clip, dict)
        ]
        self._clip_by_id = {
            clip["id"]: clip for clip in clips if isinstance(clip.get("id"), str) and clip["id"]
        }
        project_id = snapshot.get("id", "")
        name = snapshot.get("name", "")
        self._project_id = project_id if isinstance(project_id, str) else ""
        self._name = name if isinstance(name, str) else ""
        revision = snapshot.get("revision", 0)
        self._revision = revision if type(revision) is int else 0
        self._duration_us = max(
            (
                clip.get("timeline_range", {}).get("end_us", 0)
                for clip in clips
                if isinstance(clip.get("timeline_range"), dict)
                and type(clip.get("timeline_range", {}).get("end_us")) is int
            ),
            default=0,
        )
        if self._voice_conform_plan is not None:
            self._duration_us = self._voice_conform_plan.output_duration_us
        raw_media = snapshot.get("media", [])
        self._media_path_by_id = {
            item["id"]: item["path"]
            for item in raw_media
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("path"), str)
        }
        from ducai_studio.domain.video_layers import resolved_video_rows

        self._video_clips = resolved_video_rows({"tracks": raw_tracks})
        self._video_starts = [item["timeline_range"]["start_us"] for item in self._video_clips]
        self._voice_clips = sorted(
            (
                {**clip, "track_muted": track.get("muted", False)}
                for track in raw_tracks
                if isinstance(track, dict) and track.get("kind") == "voice"
                for clip in track.get("clips", [])
                if isinstance(clip, dict)
                and isinstance(clip.get("timeline_range"), dict)
                and type(clip["timeline_range"].get("start_us")) is int
                and type(clip["timeline_range"].get("end_us")) is int
            ),
            key=lambda item: item["timeline_range"]["start_us"],
        )
        self._voice_starts = [item["timeline_range"]["start_us"] for item in self._voice_clips]
        voice_path = next(
            (
                self._media_path_by_id.get(clip.get("media_id"), "")
                for clip in self._voice_clips
                if self._media_path_by_id.get(clip.get("media_id"), "")
            ),
            "",
        )
        self._voice_source = QUrl.fromLocalFile(voice_path) if voice_path else QUrl()
        self._linked_audio_by_group = {
            clip["link_group_id"]: {
                **clip,
                "track_muted": track.get("muted", False),
            }
            for track in raw_tracks
            if isinstance(track, dict) and track.get("kind") == "original_audio"
            for clip in track.get("clips", [])
            if isinstance(clip, dict)
            and isinstance(clip.get("link_group_id"), str)
            and clip["link_group_id"]
        }
        video_path = next(
            (
                item.get("path")
                for item in raw_media
                if isinstance(item, dict)
                and item.get("kind") == "video"
                and isinstance(item.get("path"), str)
            ),
            "",
        )
        self._primary_video_path = video_path if isinstance(video_path, str) else ""
        self._video_source = QUrl.fromLocalFile(video_path) if video_path else QUrl()
        self._overlays = self._map_overlay_ranges(snapshot.get("overlays", []))
        self.overlay_model.replace(self._overlays)
        self._set_translation_preference_values(snapshot.get("translation_preferences", {}))
        raw_dialogue_region = snapshot.get("ocr_dialogue_region")
        self._ocr_dialogue_region = (
            [float(value) for value in raw_dialogue_region]
            if isinstance(raw_dialogue_region, (list, tuple)) and len(raw_dialogue_region) == 4
            else []
        )
        raw_exclusions = snapshot.get("ocr_exclusion_regions", [])
        self._ocr_exclusion_regions = (
            [
                [float(value) for value in item]
                for item in raw_exclusions
                if isinstance(item, (list, tuple)) and len(item) == 4
            ]
            if isinstance(raw_exclusions, list)
            else []
        )
        raw_markers = snapshot.get("timeline_markers", [])
        self._timeline_markers = sorted(
            {
                int(value)
                for value in raw_markers
                if type(value) is int and value >= 0
            }
        ) if isinstance(raw_markers, (list, tuple)) else []
        self._has_translated_subtitles = bool(snapshot.get("translated_cues", []))
        self.changed.emit()

    def _set_translation_preference_values(self, raw_preferences: object) -> None:
        if not isinstance(raw_preferences, dict):
            raw_preferences = {}
        preset = raw_preferences.get("preset", "auto")
        custom_rules = raw_preferences.get("custom_rules", "")
        self._translation_style_preset = preset if isinstance(preset, str) and preset else "auto"
        self._translation_custom_rules = custom_rules if isinstance(custom_rules, str) else ""
        raw_saved_styles = raw_preferences.get("saved_styles", [])
        if isinstance(raw_saved_styles, dict):
            raw_saved_styles = [
                {"name": name, **value}
                for name, value in raw_saved_styles.items()
                if isinstance(value, dict)
            ]
        self._translation_style_presets = [
            {
                "name": str(item.get("name", "")).strip(),
                "preset": str(item.get("preset", "auto")).strip() or "auto",
                "custom_rules": str(item.get("custom_rules", "")),
            }
            for item in raw_saved_styles
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ] if isinstance(raw_saved_styles, list) else []
        active_style_name = raw_preferences.get("active_style_name", "")
        self._translation_active_style_name = (
            active_style_name if isinstance(active_style_name, str) else ""
        )

    def apply_translation_preferences(self, response: object) -> None:
        """Merge a disjoint style ACK without resetting live cue/editor models."""

        if not isinstance(response, dict) or response.get("id") != self._project_id:
            raise ValueError("Translation preferences belong to another project")
        revision = response.get("revision")
        if type(revision) is not int or revision <= 0:
            raise ValueError("Translation preferences revision is invalid")
        raw = response.get("translation_preferences")
        if not isinstance(raw, dict):
            raise ValueError("Translation preferences are invalid")
        styles = raw.get("saved_styles", [])
        if isinstance(styles, dict):
            styles = [{**value, "name": name} for name, value in styles.items()
                      if isinstance(value, dict)]
        if not isinstance(styles, list) or any(not isinstance(item, dict) for item in styles):
            raise ValueError("Saved translation styles are invalid")
        for item in [raw, *styles]:
            for key in ("name", "preset", "custom_rules", "active_style_name"):
                if key in item and not isinstance(item[key], str):
                    raise ValueError("Translation preference values must be strings")
        preferences = TranslationPreferences(
            preset=raw.get("preset", "auto"), custom_rules=raw.get("custom_rules", ""),
            saved_styles=tuple(TranslationStylePreset(
                name=item.get("name", ""), preset=item.get("preset", "auto"),
                custom_rules=item.get("custom_rules", ""),
            ) for item in styles),
            active_style_name=raw.get("active_style_name", ""),
        )
        if revision < self._translation_preferences_revision:
            return
        normalized = {"preset": preferences.preset, "custom_rules": preferences.custom_rules}
        if preferences.saved_styles:
            normalized["saved_styles"] = [
                {"name": item.name, "preset": item.preset, "custom_rules": item.custom_rules}
                for item in preferences.saved_styles
            ]
        if preferences.active_style_name:
            normalized["active_style_name"] = preferences.active_style_name
        self._revision = max(self._revision, revision)
        self._canonical_snapshot = {**self._canonical_snapshot, "revision": self._revision,
                                    "translation_preferences": normalized}
        self._translation_preferences_revision = revision
        self._set_translation_preference_values(normalized)
        self.changed.emit()

    def _preserve_translation_preferences(self, snapshot: dict) -> dict:
        # A projection worker may have copied the project before a style ACK.
        # Cue/timeline patches do not own these fields and must keep their head.
        snapshot = dict(snapshot)
        if "translation_preferences" in self._canonical_snapshot:
            snapshot["translation_preferences"] = (
                self._canonical_snapshot["translation_preferences"]
            )
        else:
            snapshot.pop("translation_preferences", None)
        return snapshot

    def apply_effect_patch(self, raw_patch: object) -> None:
        patch = raw_patch if isinstance(raw_patch, dict) else {}
        if patch.get("_effect_patch") is not True or str(patch.get("id", "")) != self._project_id:
            raise ValueError("Effect patch belongs to another project")
        revision = patch.get("revision")
        if type(revision) is not int or revision < self._content_revision:
            raise ValueError("Effect patch revision is invalid")
        candidate = deepcopy(self._canonical_snapshot)
        candidate["overlays"] = self._patched_items(
            candidate.get("overlays", []),
            patch.get("overlay_upserts", []),
            patch.get("removed_overlay_ids", []),
            label="overlay",
        )
        candidate["media"] = self._patched_items(
            candidate.get("media", []),
            patch.get("media_upserts", []),
            patch.get("removed_media_ids", []),
            label="media",
        )
        candidate["revision"] = max(self._revision, revision)
        projected = (
            project_json_for_timeline(candidate)
            if candidate.get("timeline_edit")
            else candidate
        )
        projected_by_id = {
            str(item.get("id", "")): item
            for item in projected.get("overlays", [])
            if isinstance(item, dict) and str(item.get("id", ""))
        }
        removed_ids = patch.get("removed_overlay_ids", [])
        mapped_upserts = [
            projected_by_id[str(item["id"])]
            for item in patch.get("overlay_upserts", [])
            if isinstance(item, dict) and str(item.get("id", "")) in projected_by_id
        ]
        self.overlay_model.apply_patch(mapped_upserts, removed_ids)
        removed = set(removed_ids)
        by_id = {
            str(item.get("id", "")): dict(item)
            for item in self._overlays
            if str(item.get("id", "")) not in removed
        }
        for item in mapped_upserts:
            by_id[str(item["id"])] = item
        self._overlays = list(by_id.values())
        self._media_path_by_id = {
            str(item["id"]): str(item.get("path", ""))
            for item in candidate.get("media", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        self._canonical_snapshot = candidate
        self._revision = candidate["revision"]
        self._content_revision = revision
        self.changed.emit()

    @staticmethod
    def _patched_items(
        current: object,
        upserts: object,
        removed_ids: object,
        *,
        label: str,
    ) -> list[dict]:
        if not isinstance(current, list):
            raise ValueError(f"Canonical {label} list is invalid")
        if not isinstance(upserts, list) or not isinstance(removed_ids, list):
            raise ValueError(f"Compact {label} patch is invalid")
        removed = {
            value for value in removed_ids if isinstance(value, str) and value
        }
        ordered_ids: list[str] = []
        by_id: dict[str, dict] = {}
        for raw in current:
            if not isinstance(raw, dict):
                raise ValueError(f"Canonical {label} is invalid")
            item_id = raw.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ValueError(f"Canonical {label} identity is invalid")
            if item_id in removed:
                continue
            ordered_ids.append(item_id)
            by_id[item_id] = deepcopy(raw)
        for raw in upserts:
            if not isinstance(raw, dict):
                raise ValueError(f"Compact {label} upsert is invalid")
            item_id = raw.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ValueError(f"Compact {label} identity is invalid")
            if item_id not in by_id:
                ordered_ids.append(item_id)
            by_id[item_id] = deepcopy(raw)
        return [by_id[item_id] for item_id in ordered_ids if item_id in by_id]

    @staticmethod
    def _patched_timeline_cues(
        current: object,
        upserts: object,
        removed_ids: object,
        indices: object,
        *,
        label: str,
    ) -> list[dict]:
        if not isinstance(current, list) or not isinstance(upserts, list):
            raise ValueError(f"Compact {label} cue patch is invalid")
        if not isinstance(removed_ids, list) or not isinstance(indices, dict):
            raise ValueError(f"Compact {label} cue identity patch is invalid")
        removed = {
            value for value in removed_ids if isinstance(value, str) and value
        }
        by_id: dict[str, dict] = {}
        order: list[str] = []
        for raw in current:
            if not isinstance(raw, dict):
                raise ValueError(f"Canonical {label} cue is invalid")
            cue_id = raw.get("id")
            if not isinstance(cue_id, str) or not cue_id:
                raise ValueError(f"Canonical {label} cue identity is invalid")
            if cue_id in removed:
                continue
            order.append(cue_id)
            by_id[cue_id] = deepcopy(raw)
        for raw in upserts:
            if not isinstance(raw, dict):
                raise ValueError(f"Compact {label} cue upsert is invalid")
            cue_id = raw.get("id")
            if not isinstance(cue_id, str) or not cue_id:
                raise ValueError(f"Compact {label} cue identity is invalid")
            if cue_id not in by_id:
                order.append(cue_id)
            by_id[cue_id] = {**by_id.get(cue_id, {}), **deepcopy(raw)}
        for cue_id, value in indices.items():
            if cue_id not in by_id or type(value) is not int or value <= 0:
                raise ValueError(f"Compact {label} cue index map is invalid")
            by_id[cue_id]["index"] = value
        values = [by_id[cue_id] for cue_id in order if cue_id in by_id]
        values.sort(key=lambda item: (int(item.get("index", 0)), str(item.get("id", ""))))
        return values

    @classmethod
    def _patched_tracks(
        cls,
        current: object,
        *,
        track_upserts: object,
        removed_track_ids: object,
        clip_upserts: object,
        removed_clip_ids: object,
        track_order: object = None,
        clip_metadata: object = None,
    ) -> list[dict]:
        if not all(
            isinstance(value, list)
            for value in (
                current,
                track_upserts,
                removed_track_ids,
                clip_upserts,
                removed_clip_ids,
            )
        ):
            raise ValueError("Compact timeline patch lists are invalid")
        removed_tracks = {
            value for value in removed_track_ids if isinstance(value, str) and value
        }
        tracks: dict[str, dict] = {}
        order: list[str] = []
        for raw_track in current:
            if not isinstance(raw_track, dict):
                raise ValueError("Canonical track is invalid")
            track_id = raw_track.get("id")
            if not isinstance(track_id, str) or not track_id:
                raise ValueError("Canonical track identity is invalid")
            if track_id in removed_tracks:
                continue
            track = {key: deepcopy(value) for key, value in raw_track.items() if key != "clips"}
            track["clips"] = raw_track.get("clips", [])
            raw_clips = track.get("clips", [])
            if not isinstance(raw_clips, list):
                raise ValueError("Canonical track clips are invalid")
            normalized: list[dict] = []
            for raw_clip in raw_clips:
                if not isinstance(raw_clip, dict):
                    raise ValueError("Canonical clip is invalid")
                clip = deepcopy(raw_clip)
                clip_id = clip.get("id")
                if not isinstance(clip_id, str) or not clip_id:
                    raise ValueError("Canonical clip identity is invalid")
                owner_id = str(clip.get("track_id", track_id))
                if owner_id != track_id:
                    raise ValueError("Canonical clip belongs to another track")
                clip["track_id"] = track_id
                normalized.append(clip)
            track["clips"] = normalized
            tracks[track_id] = track
            order.append(track_id)
        for raw_track in track_upserts:
            if not isinstance(raw_track, dict):
                raise ValueError("Compact track upsert is invalid")
            track_id = raw_track.get("id")
            if not isinstance(track_id, str) or not track_id:
                raise ValueError("Compact track identity is invalid")
            if track_id in removed_tracks:
                raise ValueError("Compact patch removes and upserts the same track")
            previous = tracks.get(track_id, {"id": track_id, "clips": []})
            incoming = deepcopy(raw_track)
            if "clips" in incoming:
                raise ValueError("Track upserts must not replace clips")
            tracks[track_id] = {**previous, **incoming, "clips": previous["clips"]}
            if track_id not in order:
                order.append(track_id)

        removed_clips = {
            value for value in removed_clip_ids if isinstance(value, str) and value
        }
        owners: dict[str, str] = {}
        clips_by_track: dict[str, dict[str, dict]] = {}
        for track_id, track in tracks.items():
            kept: dict[str, dict] = {}
            for clip in track["clips"]:
                clip_id = str(clip.get("id", ""))
                if clip_id in owners:
                    raise ValueError("Canonical clip identity is duplicated")
                owners[clip_id] = track_id
                if clip_id not in removed_clips:
                    kept[clip_id] = clip
            clips_by_track[track_id] = kept

        for raw_clip in clip_upserts:
            if not isinstance(raw_clip, dict):
                raise ValueError("Compact clip upsert is invalid")
            clip_id = raw_clip.get("id")
            track_id = raw_clip.get("track_id")
            if (
                not isinstance(clip_id, str)
                or not clip_id
                or not isinstance(track_id, str)
                or track_id not in tracks
            ):
                raise ValueError("Compact clip identity or owning track is invalid")
            previous_owner = owners.get(clip_id)
            if previous_owner is not None:
                clips_by_track[previous_owner].pop(clip_id, None)
            clip = deepcopy(raw_clip)
            clip["track_id"] = track_id
            clips_by_track[track_id][clip_id] = clip
            owners[clip_id] = track_id

        if clip_metadata is not None:
            if not isinstance(clip_metadata, dict):
                raise ValueError("Compact clip metadata patch is invalid")
            for clip_id, values in clip_metadata.items():
                owner = owners.get(clip_id)
                if (owner is None or clip_id not in clips_by_track[owner]
                        or not isinstance(values, dict)
                        or set(values) != {"cue_index", "payload_revision"}
                        or any(type(value) is not int or value <= 0 for value in values.values())):
                    raise ValueError("Compact clip metadata identity or values are invalid")
                clips_by_track[owner][clip_id].update(values)

        for track_id, track in tracks.items():
            track["clips"] = sorted(
                clips_by_track[track_id].values(),
                key=lambda clip: (
                    int((clip.get("timeline_range") or {}).get("start_us", 0)),
                    str(clip.get("id", "")),
                )
            )
        if track_order is not None:
            if (not isinstance(track_order, list) or len(track_order) != len(tracks)
                    or any(not isinstance(item, str) for item in track_order)
                    or set(track_order) != set(tracks)):
                raise ValueError("Thứ tự track trong patch không hợp lệ")
            order = track_order
        return [tracks[track_id] for track_id in order if track_id in tracks]

    def apply_timeline_patch(self, raw_patch: object) -> None:
        canonical, projected = self.prepare_timeline_patch(
            self.content_patch_snapshot(),
            raw_patch,
        )
        self.apply_prepared_timeline_patch(canonical, projected)

    @classmethod
    def prepare_timeline_patch(
        cls,
        raw_snapshot: object,
        raw_patch: object,
    ) -> tuple[dict, dict]:
        """Validate and project a timeline patch without touching Qt models.

        This method is deliberately side-effect free so large cut projections
        can run on the snapshot worker instead of blocking the GUI thread.
        """

        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        patch = raw_patch if isinstance(raw_patch, dict) else {}
        if (
            patch.get("_timeline_patch") is not True
            or str(patch.get("id", "")) != str(snapshot.get("id", ""))
        ):
            raise ValueError("Timeline patch belongs to another project")
        revision = patch.get("revision")
        current_revision = snapshot.get("revision", 0)
        if type(current_revision) is not int:
            raise ValueError("Canonical timeline revision is invalid")
        if type(revision) is not int or revision <= current_revision:
            raise ValueError("Timeline patch revision is invalid")
        candidate = dict(snapshot)
        if "media_upserts" in patch or "removed_media_ids" in patch:
            candidate["media"] = cls._patched_items(
                candidate.get("media", []),
                patch.get("media_upserts", []),
                patch.get("removed_media_ids", []),
                label="media",
            )
        candidate["tracks"] = cls._patched_tracks(
            candidate.get("tracks", []),
            track_upserts=patch.get("track_upserts", []),
            removed_track_ids=patch.get("removed_track_ids", []),
            clip_upserts=patch.get("clip_upserts", []),
            removed_clip_ids=patch.get("removed_clip_ids", []),
            track_order=patch.get("track_order"),
        )
        if "timeline_edit" in patch:
            timeline_edit = patch.get("timeline_edit")
            if timeline_edit is not None and not isinstance(timeline_edit, dict):
                raise ValueError("Timeline edit patch is invalid")
            candidate["timeline_edit"] = deepcopy(timeline_edit)
        if "timeline_markers" in patch:
            markers = patch.get("timeline_markers")
            if not isinstance(markers, (list, tuple)) or any(
                type(value) is not int or value < 0 for value in markers
            ):
                raise ValueError("Timeline markers are invalid")
            normalized_markers = sorted(set(markers))
            if normalized_markers != list(markers):
                raise ValueError("Timeline markers must be sorted and unique")
            candidate["timeline_markers"] = normalized_markers
        if "voice_conform" in patch:
            voice_conform = patch.get("voice_conform")
            if voice_conform is not None and not isinstance(voice_conform, dict):
                raise ValueError("Voice conform patch is invalid")
            candidate["voice_conform"] = deepcopy(voice_conform)
        if "overlay_upserts" in patch or "removed_overlay_ids" in patch:
            candidate["overlays"] = cls._patched_items(
                candidate.get("overlays", []),
                patch.get("overlay_upserts", []),
                patch.get("removed_overlay_ids", []),
                label="overlay",
            )
        for label, key in (("source", "source_cues"), ("translated", "translated_cues"),
                           ("title", "title_cues")):
            if any(
                name in patch
                for name in (
                    f"{label}_cue_upserts",
                    f"removed_{label}_cue_ids",
                    f"{label}_cue_indices",
                )
            ):
                candidate[key] = cls._patched_timeline_cues(
                    candidate.get(key, []),
                    patch.get(f"{label}_cue_upserts", []),
                    patch.get(f"removed_{label}_cue_ids", []),
                    patch.get(f"{label}_cue_indices", {}),
                    label=label,
                )
        candidate["revision"] = revision
        projected = (
            project_json_for_timeline(candidate)
            if candidate.get("timeline_edit")
            else candidate
        )
        return candidate, projected

    def apply_prepared_timeline_patch(self, canonical: dict, projected: dict) -> None:
        """Commit a worker-prepared snapshot as one Qt model update batch."""

        if str(canonical.get("id", "")) != self._project_id:
            raise ValueError("Timeline patch belongs to another project")
        revision = canonical.get("revision")
        if type(revision) is not int or revision <= self._content_revision:
            raise ValueError("Timeline patch revision is invalid")
        # Model replacement only happens after the complete candidate has
        # validated, so malformed identities cannot partially mutate state.
        self._canonical_snapshot = self._preserve_translation_preferences(canonical)
        self._canonical_snapshot["revision"] = max(self._revision, revision)
        projected = self._preserve_translation_preferences(projected)
        projected["revision"] = self._canonical_snapshot["revision"]
        self._content_revision = revision
        self._replace_projected_snapshot(projected)

    def apply_local_effect_command(
        self,
        command: str,
        overlay_id: str,
        values: dict[str, object],
    ) -> dict[str, object]:
        """Apply an optimistic overlay-only mutation and return its rollback patch.

        The durable project revision is intentionally unchanged.  A later
        ``effect_command`` patch supplies the authoritative revision and full
        serialized overlay, while failures use the returned inverse patch.
        """

        selected = next(
            (dict(item) for item in self._overlays if item.get("id") == overlay_id),
            None,
        )
        if command == "update":
            if selected is None:
                return {}
            updated = dict(selected)
            updated_range = dict(updated.get("range", {}))
            for key, value in values.items():
                if key == "start_us":
                    updated_range["start_us"] = value
                elif key == "end_us":
                    updated_range["end_us"] = value
                else:
                    updated[key] = value
            if updated_range:
                updated["range"] = updated_range
            self._apply_local_effect_patch([updated], [])
            return {"overlay_upserts": [selected], "removed_overlay_ids": []}
        if command == "add_blur":
            if not overlay_id or selected is not None:
                return {}
            blur_count = sum(1 for item in self._overlays if item.get("kind") == "blur")
            offset = min(0.24, blur_count * 0.035)
            overlay = {
                "id": overlay_id,
                "kind": "blur",
                "range": {"start_us": 0, "end_us": self._duration_us},
                "x": min(0.45, 0.08 + offset),
                "y": min(0.76, 0.58 + offset),
                "width": 0.44,
                "height": 0.16,
                "asset_id": None,
                "enabled": True,
                "opacity": 1.0,
                "blur_radius": 22,
                "tracking_mode": "fixed",
                "padding_x": 0.012,
                "padding_y": 0.008,
                "concealment_mode": "blur",
            }
            self._apply_local_effect_patch([overlay], [])
            return {"overlay_upserts": [], "removed_overlay_ids": [overlay_id]}
        if command == "delete_overlay":
            if selected is None:
                return {}
            self._apply_local_effect_patch([], [overlay_id])
            return {"overlay_upserts": [selected], "removed_overlay_ids": []}
        return {}

    def rollback_local_effect(self, raw_patch: object) -> None:
        patch = raw_patch if isinstance(raw_patch, dict) else {}
        self._apply_local_effect_patch(
            patch.get("overlay_upserts", []),
            patch.get("removed_overlay_ids", []),
        )

    def _apply_local_effect_patch(self, upserts: object, removed_ids: object) -> None:
        values = (
            [dict(item) for item in upserts if isinstance(item, dict)]
            if isinstance(upserts, list)
            else []
        )
        removed = (
            {str(item) for item in removed_ids if isinstance(item, str) and item}
            if isinstance(removed_ids, list)
            else set()
        )
        self.overlay_model.apply_patch(values, list(removed))
        by_id = {
            str(item.get("id", "")): dict(item)
            for item in self._overlays
            if str(item.get("id", "")) not in removed
        }
        for item in values:
            overlay_id = str(item.get("id", ""))
            if overlay_id:
                by_id[overlay_id] = item
        self._overlays = list(by_id.values())
        self.changed.emit()

    def apply_subtitle_patch(self, raw_patch: object) -> None:
        canonical, projected = self.prepare_subtitle_patch(self.content_patch_snapshot(), raw_patch)
        self.apply_prepared_subtitle_patch(canonical, projected)

    @classmethod
    def prepare_subtitle_patch(cls, snapshot: dict, raw_patch: object) -> tuple[dict, dict]:
        patch = raw_patch if isinstance(raw_patch, dict) else {}
        if str(patch.get("id", "")) != snapshot.get("id"):
            raise ValueError("Subtitle patch belongs to another project")
        revision = patch.get("revision")
        if type(revision) is not int or revision < snapshot.get("revision", 0):
            raise ValueError("Subtitle patch revision is invalid")
        candidate = dict(snapshot)
        if "source_cues" in patch or "translated_cues" in patch:
            source_cues = patch.get("source_cues", [])
            translated_cues = patch.get("translated_cues", [])
            if not isinstance(source_cues, list) or not isinstance(translated_cues, list):
                raise ValueError("Subtitle cue lists are invalid")
            candidate["source_cues"] = deepcopy(source_cues)
            candidate["translated_cues"] = deepcopy(translated_cues)
        else:
            candidate["source_cues"] = cls._patched_timeline_cues(
                candidate.get("source_cues", []),
                patch.get("source_cue_upserts", []),
                patch.get("removed_source_cue_ids", []),
                patch.get("source_cue_indices", {}),
                label="source cue",
            )
            candidate["translated_cues"] = cls._patched_timeline_cues(
                candidate.get("translated_cues", []),
                patch.get("translated_cue_upserts", []),
                patch.get("removed_translated_cue_ids", []),
                patch.get("translated_cue_indices", {}),
                label="translated cue",
            )
        if any(
            key in patch
            for key in (
                "subtitle_clip_upserts",
                "removed_subtitle_clip_ids",
                "subtitle_track_upserts",
                "removed_subtitle_track_ids",
                "subtitle_clip_metadata",
            )
        ):
            candidate["tracks"] = cls._patched_tracks(
                candidate.get("tracks", []),
                track_upserts=patch.get("subtitle_track_upserts", []),
                removed_track_ids=patch.get("removed_subtitle_track_ids", []),
                clip_upserts=patch.get("subtitle_clip_upserts", []),
                removed_clip_ids=patch.get("removed_subtitle_clip_ids", []),
                clip_metadata=patch.get("subtitle_clip_metadata"),
            )
        if "ocr_dialogue_region" in patch:
            candidate["ocr_dialogue_region"] = deepcopy(patch.get("ocr_dialogue_region"))
        if "ocr_exclusion_regions" in patch:
            candidate["ocr_exclusion_regions"] = deepcopy(patch.get("ocr_exclusion_regions"))
        candidate["revision"] = revision
        projected = (
            project_json_for_timeline(candidate) if candidate.get("timeline_edit") else candidate
        )
        return candidate, projected

    def apply_prepared_subtitle_patch(self, canonical: dict, projected: dict) -> None:
        revision = canonical.get("revision")
        if (canonical.get("id") != self._project_id or type(revision) is not int
                or revision < self._content_revision):
            raise ValueError("Subtitle patch belongs to an outdated project revision")
        self._canonical_snapshot = self._preserve_translation_preferences(canonical)
        self._canonical_snapshot["revision"] = max(self._revision, revision)
        projected = self._preserve_translation_preferences(projected)
        projected["revision"] = self._canonical_snapshot["revision"]
        self._content_revision = revision
        self._replace_projected_snapshot(projected, preserve_live_translations=True)

    def _apply_subtitle_clip_patch(
        self,
        upserts: object,
        removed_ids: object,
        track_upserts: object = None,
        removed_track_ids: object = None,
    ) -> None:
        values = (
            [dict(item) for item in upserts if isinstance(item, dict)]
            if isinstance(upserts, list)
            else []
        )
        removed = (
            {str(value) for value in removed_ids if isinstance(value, str) and value}
            if isinstance(removed_ids, list)
            else set()
        )
        track_values = (
            [dict(item) for item in track_upserts if isinstance(item, dict)]
            if isinstance(track_upserts, list)
            else []
        )
        removed_tracks = (
            {str(value) for value in removed_track_ids if isinstance(value, str) and value}
            if isinstance(removed_track_ids, list)
            else set()
        )
        if not values and not removed and not track_values and not removed_tracks:
            return
        by_track: dict[str, list[dict]] = {}
        for item in values:
            track_id = str(item.get("track_id", ""))
            if track_id:
                by_track.setdefault(track_id, []).append(item)
        upsert_ids = {str(item.get("id", "")) for item in values if str(item.get("id", ""))}
        upsert_target_by_id = {
            str(item.get("id", "")): str(item.get("track_id", ""))
            for item in values
            if str(item.get("id", "")) and str(item.get("track_id", ""))
        }
        tracks = [
            dict(track)
            for track in self._track_by_id.values()
            if str(track.get("id", "")) not in removed_tracks
        ]
        existing_track_ids = {str(track.get("id", "")) for track in tracks}
        for descriptor in track_values:
            track_id = str(descriptor.get("id", ""))
            if not track_id:
                continue
            if track_id in existing_track_ids:
                for track in tracks:
                    if str(track.get("id", "")) != track_id:
                        continue
                    clips = track.get("clips", [])
                    track.update(descriptor)
                    track["clips"] = clips
                    break
            else:
                tracks.append({**descriptor, "clips": []})
                existing_track_ids.add(track_id)
        for track in tracks:
            track_id = str(track.get("id", ""))
            if track_id not in by_track and not removed and not upsert_ids:
                continue
            raw_clips = track.get("clips", [])
            clips = (
                [dict(item) for item in raw_clips if isinstance(item, dict)]
                if isinstance(raw_clips, list)
                else []
            )
            ordered_ids = [str(item.get("id", "")) for item in clips]
            by_id = {
                str(item.get("id", "")): item
                for item in clips
                if str(item.get("id", ""))
                and str(item.get("id", "")) not in removed
                and (
                    str(item.get("id", "")) not in upsert_ids
                    or upsert_target_by_id.get(str(item.get("id", ""))) == track_id
                )
            }
            ordered_ids = [
                clip_id
                for clip_id in ordered_ids
                if clip_id not in removed
                and (clip_id not in upsert_ids or upsert_target_by_id.get(clip_id) == track_id)
            ]
            for item in by_track.get(track_id, []):
                clip_id = str(item.get("id", ""))
                if not clip_id:
                    continue
                if clip_id not in ordered_ids:
                    ordered_ids.append(clip_id)
                by_id[clip_id] = item
            track["clips"] = [by_id[clip_id] for clip_id in ordered_ids if clip_id in by_id]
        self._track_by_id = {
            str(track["id"]): track
            for track in tracks
            if isinstance(track.get("id"), str) and track["id"]
        }
        self.tracks.replace(tracks)
        self.clips.replace(tracks)
        self._clip_by_id = {
            str(clip["id"]): clip
            for track in tracks
            for clip in track.get("clips", [])
            if isinstance(clip, dict) and isinstance(clip.get("id"), str) and clip["id"]
        }

    def replace_voice_timing_rows(self, raw_rows: object) -> None:
        if self._voice_conform_plan is None:
            self.voice_conform_cues.replace_timing_rows(raw_rows)

    def _display_clip_tracks(self, tracks: list) -> list:
        plan = self._voice_conform_plan
        if plan is None:
            return tracks
        # Keep source ranges and the clip lookup canonical for media decoding
        # and commands. Only the paint model uses conformed display time.
        return [
            {**track, "clips": [
                {**clip, "timeline_range": {
                    "start_us": plan.source_to_output_us(clip["timeline_range"]["start_us"]),
                    "end_us": plan.source_to_output_us(clip["timeline_range"]["end_us"]),
                }} for clip in track.get("clips", [])
            ]} if track.get("kind") in {"video", "original_audio", "subtitle", "text"}
            else track for track in tracks
        ]

    def _map_cue_ranges(self, raw_cues: object) -> list[dict]:
        if not isinstance(raw_cues, list):
            return []
        result: list[dict] = []
        for raw in raw_cues:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            raw_range = raw.get("range")
            if self._voice_conform_plan is not None and isinstance(raw_range, dict):
                item["range"] = {
                    **raw_range,
                    "start_us": self._voice_conform_plan.source_to_output_us(
                        int(raw_range.get("start_us", 0))
                    ),
                    "end_us": self._voice_conform_plan.source_to_output_us(
                        int(raw_range.get("end_us", 0))
                    ),
                }
            result.append(item)
        return result

    @staticmethod
    def _restore_legacy_voice_cue_metadata(
        raw_tracks: object,
        source_cues: list[dict],
        translated_cues: list[dict],
    ) -> list[object]:
        """Restore the cue link omitted by early cue-split D1 revisions.

        Those revisions already used the stable ``voice-<index>-<cue-tail>``
        clip id but serialized the newly added cue fields as null/defaults.
        Recover only an unambiguous index-and-tail match so unrelated legacy
        audio is never guessed into a subtitle cue.
        """

        tracks = raw_tracks if isinstance(raw_tracks, list) else []
        cues_by_identity: dict[tuple[int, str], dict | None] = {}
        source_by_id: dict[str, dict] = {}
        for cue in source_cues:
            cue_id = cue.get("id")
            cue_index = cue.get("index")
            if not isinstance(cue_id, str) or not cue_id or type(cue_index) is not int:
                continue
            source_by_id[cue_id] = cue
            identity = (cue_index, cue_id[-12:])
            cues_by_identity[identity] = cue if identity not in cues_by_identity else None
        translated_by_id = {
            cue["id"]: cue
            for cue in translated_cues
            if isinstance(cue.get("id"), str) and cue["id"]
        }

        restored_tracks: list[object] = []
        for raw_track in tracks:
            if not isinstance(raw_track, dict):
                restored_tracks.append(raw_track)
                continue
            track = dict(raw_track)
            if track.get("kind") != "voice":
                restored_tracks.append(track)
                continue
            restored_clips: list[object] = []
            raw_clips = track.get("clips", [])
            for raw_clip in raw_clips if isinstance(raw_clips, list) else []:
                if not isinstance(raw_clip, dict):
                    restored_clips.append(raw_clip)
                    continue
                clip = dict(raw_clip)
                cue_id = clip.get("cue_id")
                source = source_by_id.get(cue_id) if isinstance(cue_id, str) else None
                recovered = source is None
                if source is None:
                    clip_id = clip.get("id")
                    if isinstance(clip_id, str) and clip_id.startswith("voice-"):
                        parts = clip_id.split("-", 2)
                        if len(parts) == 3 and parts[1].isdigit() and parts[2]:
                            source = cues_by_identity.get((int(parts[1]), parts[2]))
                if source is None:
                    restored_clips.append(clip)
                    continue

                cue_id = source["id"]
                clip["cue_id"] = cue_id
                clip["cue_index"] = source["index"]
                translated = translated_by_id.get(cue_id)
                translated_text = translated.get("text") if translated is not None else ""
                if not isinstance(clip.get("cue_text"), str) or not clip["cue_text"].strip():
                    clip["cue_text"] = (
                        translated_text
                        if isinstance(translated_text, str) and translated_text.strip()
                        else str(source.get("text", ""))
                    )
                source_range = source.get("range")
                if not isinstance(clip.get("timing_slot"), dict) and isinstance(source_range, dict):
                    clip["timing_slot"] = dict(source_range)

                slot = clip.get("timing_slot")
                timeline_range = clip.get("timeline_range")
                if recovered and isinstance(slot, dict) and isinstance(timeline_range, dict):
                    restored_range = _range_from_mapping(timeline_range)
                    restored_slot = _range_from_mapping(slot)
                    if restored_range is not None and restored_slot is not None:
                        state = classify_voice_timing(restored_range, restored_slot)
                        unresolved = state == VOICE_TIMING_OVERFLOW
                        clip["timing_unresolved"] = unresolved
                        if unresolved and not str(clip.get("issue_code", "")).strip():
                            clip["issue_code"] = "CUE_SYNC_LIMIT_EXCEEDED"
                        elif not unresolved and clip.get("content_warning") is not True:
                            clip["issue_code"] = ""
                restored_clips.append(clip)
            track["clips"] = restored_clips
            restored_tracks.append(track)
        return restored_tracks

    @staticmethod
    def _restore_legacy_subtitle_cue_metadata(
        raw_tracks: object,
        source_cues: list[dict],
        translated_cues: list[dict],
    ) -> list[object]:
        """Attach stable cue identities to S1/S2 clips from older projects."""

        tracks = raw_tracks if isinstance(raw_tracks, list) else []
        translated_by_id = {
            str(cue.get("id", "")): cue for cue in translated_cues if str(cue.get("id", ""))
        }
        source_ids = {str(cue.get("id", "")) for cue in source_cues}
        translated_by_index = {
            int(cue["index"]): cue for cue in translated_cues if type(cue.get("index")) is int
        }
        cues_by_role = {
            "S1": [
                cue for cue in source_cues if cue.get("cue_role", cue.get("role")) != "scene_text"
            ],
            "S2": [
                cue for cue in source_cues if cue.get("cue_role", cue.get("role")) == "scene_text"
            ],
        }
        restored: list[object] = []
        for raw_track in tracks:
            if not isinstance(raw_track, dict):
                restored.append(raw_track)
                continue
            track = dict(raw_track)
            track_id = str(track.get("id", ""))
            expected = cues_by_role.get(track_id)
            raw_clips = track.get("clips", [])
            if (
                expected is None
                or not isinstance(raw_clips, list)
                or len(raw_clips) != len(expected)
            ):
                restored.append(track)
                continue
            clips: list[object] = []
            for raw_clip, cue in zip(raw_clips, expected, strict=True):
                if not isinstance(raw_clip, dict):
                    clips.append(raw_clip)
                    continue
                clip = dict(raw_clip)
                cue_id = str(cue.get("id", ""))
                cue_index = cue.get("index")
                if cue_id:
                    clip["cue_id"] = cue_id
                if type(cue_index) is int:
                    clip["cue_index"] = cue_index
                translated = translated_by_id.get(cue_id) or (
                    translated_by_index.get(cue_index) if type(cue_index) is int else None
                )
                if (isinstance(translated, dict) and translated.get("id") in source_ids
                        and translated.get("id") != cue_id):
                    translated = None
                text = translated.get("text") if isinstance(translated, dict) else ""
                clip["cue_text"] = (
                    str(text)
                    if isinstance(text, str) and text.strip()
                    else str(cue.get("text", ""))
                )
                clips.append(clip)
            track["clips"] = clips
            restored.append(track)
        return restored

    def _map_overlay_ranges(self, raw_overlays: object) -> list[dict]:
        if not isinstance(raw_overlays, list):
            return []
        result: list[dict] = []
        for raw in raw_overlays:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            raw_range = raw.get("range")
            if self._voice_conform_plan is not None and isinstance(raw_range, dict):
                item["range"] = {
                    **raw_range,
                    "start_us": self._voice_conform_plan.source_to_output_us(
                        int(raw_range.get("start_us", 0))
                    ),
                    "end_us": self._voice_conform_plan.source_to_output_us(
                        int(raw_range.get("end_us", 0))
                    ),
                }
            result.append(item)
        return result
