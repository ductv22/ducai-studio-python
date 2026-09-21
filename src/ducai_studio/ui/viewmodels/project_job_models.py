from __future__ import annotations

from bisect import bisect_left, bisect_right
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
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.domain.timeline import (
    VOICE_TIMING_OVERFLOW,
    classify_voice_timing,
)
from ducai_studio.domain.voice_conform import VoiceConformPlan, decode_voice_conform_plan
from ducai_studio.ui.multi_thumbnail_provider import multi_thumbnail_url

_INVALID_INDEX = QModelIndex()


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
            item.get("project_status_override") is True
            and item.get("is_batch_current") is not True
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
            and str(item.get("batch_state", "idle"))
            not in {"running", "pause_requested"}
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
        if child_phase in {"cache_restore", "conform_cache_restore"} and state in {
            "running",
            "retrying",
        }:
            return f"Đang khôi phục cache · {labels.get(stage, stage)} {progress}%"
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
            review_text = (
                f"{blocker_count} mục cần xem lại"
                if blocker_count
                else "cần xem lại QA"
            )
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
            error_message = " ".join(
                value.strip() for value in (reason, action) if value.strip()
            )
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
                if (
                    MultiVideoListModel._uses_project_status(item)
                )
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
            self.ThumbnailUrlRole: multi_thumbnail_url(
                item.get("video_path", ""), duration
            ),
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
            item["display_label"] = f"{prefix}{counters[prefix]}"
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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._track_indexes: list[tuple[list[dict], list[int], list[int]]] = []
        self._viewport_start_us = 0
        self._viewport_end_us = 60_000_000
        self._prefetch_us = 10_000_000
        self._max_delegates = 480

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
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
            Qt.ItemDataRole.DisplayRole: item["id"],
        }
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
                            "track_kind": track.get("kind", ""),
                            "track_name": track.get("name", ""),
                            "track_index": track_index,
                        }
                    )
                indexed_clips.sort(key=lambda item: item["timeline_range"]["start_us"])
                starts = [item["timeline_range"]["start_us"] for item in indexed_clips]
                prefix_max: list[int] = []
                maximum = -1
                for item in indexed_clips:
                    maximum = max(maximum, item["timeline_range"]["end_us"])
                    prefix_max.append(maximum)
                self._track_indexes.append((indexed_clips, starts, prefix_max))
        self._refresh()

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
        self._refresh()

    def _refresh(self) -> None:
        query_start = max(0, self._viewport_start_us - self._prefetch_us)
        query_end = self._viewport_end_us + self._prefetch_us
        tracks_in_view: list[list[dict]] = []
        for clips, starts, prefix_max in self._track_indexes:
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
            for track_items in tracks_in_view:
                if not track_items:
                    continue
                target = max(1, int(self._max_delegates * len(track_items) / total))
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
        if items == self._items:
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()


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
        self._refresh()
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
        self._refresh()

    def _refresh(self) -> None:
        query_start = max(0, self._viewport_start_us - self._prefetch_us)
        query_end = self._viewport_end_us + self._prefetch_us
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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._start_us: list[int] = []

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
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
            Qt.ItemDataRole.DisplayRole: item["source_text"],
        }
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
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> dict:
        return dict(self._items[row]) if 0 <= row < len(self._items) else {}

    def find(self, cue_id: str) -> dict:
        return next((dict(item) for item in self._items if item["cue_id"] == cue_id), {})

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
        changes = raw_changes if isinstance(raw_changes, list) else []
        changed_by_role: dict[int, list[int]] = {
            self.SourceTextRole: [],
            self.TranslatedTextRole: [],
        }
        for change in changes:
            if not isinstance(change, dict):
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
        row = bisect_right(self._start_us, max(0, timeline_us)) - 1
        if row < 0 or row >= len(self._items):
            return {}
        item = self._items[row]
        if int(item["start_us"]) <= timeline_us < int(item["end_us"]):
            return dict(item)
        return {}

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
        source = [dict(item) for item in raw_source] if isinstance(raw_source, list) else []
        translated = (
            [dict(item) for item in raw_translated] if isinstance(raw_translated, list) else []
        )
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
        rows: list[dict] = []
        base = source if source else translated
        for fallback_index, cue in enumerate(base, start=1):
            index = cue.get("index", fallback_index)
            timing = cue.get("range", {})
            translation = translated_by_index.get(index, {}) if source else cue
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
        self._patch_rows(rows)

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
        if old_middle:
            self.beginRemoveRows(_INVALID_INDEX, prefix, prefix + old_middle - 1)
            del self._items[prefix : prefix + old_middle]
            self.endRemoveRows()
        if new_middle:
            self.beginInsertRows(_INVALID_INDEX, prefix, prefix + new_middle - 1)
            self._items[prefix:prefix] = rows[prefix : prefix + new_middle]
            self.endInsertRows()
        changed_rows: list[int] = []
        for index, row in enumerate(rows):
            if index >= len(self._items):
                break
            if self._items[index] != row:
                self._items[index] = row
                changed_rows.append(index)
        self._emit_changed_ranges(changed_rows, list(self.roleNames()))
        self._row_by_cue_index = {
            int(item["index"]): row
            for row, item in enumerate(self._items)
            if type(item.get("index")) is int
        }
        self._start_us = [int(item["start_us"]) for item in self._items]
        if len(old_ids) != len(new_ids):
            self.countChanged.emit()


class ProjectStateModel(QObject):
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tracks = TimelineTrackListModel(self)
        self.clips = TimelineClipListModel(self)
        self.voice_conform_cues = VoiceConformCueListModel(self)
        self.subtitle_cues = SubtitleCueListModel(self)
        self._project_id = ""
        self._name = ""
        self._revision = 0
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
        if self._voice_conform_plan is not None:
            position_us = self._voice_conform_plan.output_to_source_us(position_us)
        return self.subtitle_cues.at_time(position_us)

    @Slot(float, result=float)
    def sourceToOutputUs(self, source_us: float) -> float:
        if self._voice_conform_plan is None:
            return float(max(0, int(source_us)))
        return float(self._voice_conform_plan.source_to_output_us(int(source_us)))

    @Slot(float, result=float)
    def outputToSourceUs(self, output_us: float) -> float:
        if self._voice_conform_plan is None:
            return float(max(0, int(output_us)))
        return float(self._voice_conform_plan.output_to_source_us(int(output_us)))

    def clip_info(self, clip_id: str) -> dict | None:
        return self._clip_by_id.get(clip_id)

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
            if linked_audio.get("track_muted") is True:
                audio_volume = 0.0
        elif clip.get("track_muted") is True:
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
                        if voice_clip.get("track_muted") is True
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
        segment = next(
            item
            for item in plan.segments
            if item.output_start_us <= position_us < item.output_end_us
        )
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
            if linked_audio.get("track_muted") is True:
                audio_volume = 0.0
        elif clip.get("track_muted") is True:
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
    ) -> None:
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        raw_conform = snapshot.get("voice_conform")
        self._voice_conform_plan = None
        if isinstance(raw_conform, dict):
            try:
                self._voice_conform_plan = decode_voice_conform_plan(raw_conform.get("plan"))
            except (KeyError, TypeError, ValueError):
                self._voice_conform_plan = None
        source_cues = self._map_cue_ranges(snapshot.get("source_cues", []))
        translated_cues = self._map_cue_ranges(snapshot.get("translated_cues", []))
        raw_tracks = self._restore_legacy_voice_cue_metadata(
            snapshot.get("tracks", []),
            source_cues,
            translated_cues,
        )
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
        self.clips.replace(raw_tracks)
        self.voice_conform_cues.replace(self._voice_conform_plan)
        self.subtitle_cues.replace(
            source_cues,
            translated_cues,
            preserve_live_translations=preserve_live_translations,
        )
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
        self._video_clips = sorted(
            (
                {**clip, "track_muted": track.get("muted", False)}
                for track in raw_tracks
                if isinstance(track, dict) and track.get("kind") == "video"
                for clip in track.get("clips", [])
                if isinstance(clip, dict)
                and isinstance(clip.get("timeline_range"), dict)
                and type(clip["timeline_range"].get("start_us")) is int
                and type(clip["timeline_range"].get("end_us")) is int
            ),
            key=lambda item: item["timeline_range"]["start_us"],
        )
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
        self._has_translated_subtitles = bool(snapshot.get("translated_cues", []))
        self.changed.emit()

    def apply_subtitle_patch(self, raw_patch: object) -> None:
        patch = raw_patch if isinstance(raw_patch, dict) else {}
        if str(patch.get("id", "")) != self._project_id:
            raise ValueError("Subtitle patch belongs to another project")
        revision = patch.get("revision")
        if type(revision) is not int or revision < self._revision:
            raise ValueError("Subtitle patch revision is invalid")
        source_cues = self._map_cue_ranges(patch.get("source_cues", []))
        translated_cues = self._map_cue_ranges(patch.get("translated_cues", []))
        self.subtitle_cues.replace(source_cues, translated_cues)
        self._revision = revision
        self._has_translated_subtitles = bool(translated_cues)
        self.changed.emit()

    def replace_voice_timing_rows(self, raw_rows: object) -> None:
        if self._voice_conform_plan is None:
            self.voice_conform_cues.replace_timing_rows(raw_rows)

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
