from __future__ import annotations

from enum import StrEnum


class MultiBatchState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"


class MultiStage(StrEnum):
    OCR = "ocr"
    TRANSLATE = "translate"
    TTS = "tts"
    DONE = "done"


class MultiItemState(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    RETRYING = "retrying"
    DEFERRED = "deferred"
    COMPLETED = "completed"
    FAILED = "failed"


STAGES: tuple[MultiStage, ...] = (
    MultiStage.OCR,
    MultiStage.TRANSLATE,
    MultiStage.TTS,
)


def stage_progress(stage: str, progress: float) -> float:
    """Map a child-job fraction to the aggregate item percentage."""

    value = max(0.0, min(1.0, float(progress)))
    try:
        index = STAGES.index(MultiStage(stage))
    except ValueError:
        return 1.0 if stage == MultiStage.DONE.value else value / 3.0
    return (index + value) / len(STAGES)


def stage_local_progress(stage: str, aggregate_progress: float) -> float:
    """Recover the current child-stage fraction from aggregate item progress."""

    value = max(0.0, min(1.0, float(aggregate_progress)))
    if stage == MultiStage.DONE.value:
        return 1.0
    try:
        index = STAGES.index(MultiStage(stage))
    except ValueError:
        return value
    return max(0.0, min(1.0, (value * len(STAGES)) - index))


def worker_progress_fraction(payload: object) -> float:
    """Decode the shared worker progress contract into a clamped fraction."""

    if not isinstance(payload, dict):
        return 0.0
    raw_fraction = payload.get("fraction")
    if raw_fraction is not None and not isinstance(raw_fraction, bool):
        try:
            return max(0.0, min(1.0, float(raw_fraction)))
        except (TypeError, ValueError):
            pass
    current = payload.get("current")
    total = payload.get("total")
    if not isinstance(current, bool) and not isinstance(total, bool):
        try:
            numeric_total = float(total)
            if numeric_total > 0:
                return max(0.0, min(1.0, float(current) / numeric_total))
        except (TypeError, ValueError):
            pass
    raw_progress = payload.get("progress", 0.0)
    if isinstance(raw_progress, bool):
        return 0.0
    try:
        value = float(raw_progress)
    except (TypeError, ValueError):
        return 0.0
    if value > 1.0:
        value /= 100.0
    return max(0.0, min(1.0, value))
