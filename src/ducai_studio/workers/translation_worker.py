from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from ducai_studio.application.subtitle_finalizer import render_srt
from ducai_studio.application.translation_pipeline import (
    STYLE_QA_CODES,
    ChunkState,
    FilmBible,
    FilmTerm,
    SemanticGroup,
    TranslationChunk,
    TranslationLedger,
    TranslationQaIssue,
    TranslationQaSeverity,
    TranslationValue,
    build_ledger,
    contains_cjk,
    downgrade_style_qa_issues,
    film_bible_request,
    film_bible_translation_qa,
    ledger_from_dict,
    ledger_to_dict,
    local_translation_qa,
    mark_chunk_complete,
    normalize_semantic_qa_issues,
    parse_film_bible_response,
    parse_json_object,
    parse_qa_response,
    parse_translation_response,
    plan_chunks,
    qa_request,
    repair_request,
    replan_ledger_for_resume,
    semantic_groups,
    summarize_qa,
    translation_qa_snapshot,
    translation_request,
    translation_style_qa,
    wrap_vietnamese_subtitle,
)
from ducai_studio.application.voice_pipeline import file_sha256
from ducai_studio.domain.jobs import thaw_json
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.infrastructure.credential_store import WindowsCredentialStore
from ducai_studio.providers.translation.codex_cli import CodexCliProvider
from ducai_studio.providers.translation.contracts import (
    ResponseFormatFailure,
    TransientProviderFailure,
    TranslationProvider,
    TranslationProviderFailure,
    TranslationRequest,
)
from ducai_studio.providers.translation.gemini_api import GeminiApiProvider
from ducai_studio.providers.translation.gemini_cli import GeminiCliProvider
from ducai_studio.providers.translation.gemini_web import GeminiWebProvider, WebNoResponseStarted
from ducai_studio.providers.translation.openai_compatible import (
    OPENAI_COMPATIBLE_PROVIDER_IDS,
    OpenAICompatibleProvider,
)

from .protocol import PROTOCOL_VERSION, WorkerMessage


class WorkerStopped(BaseException):
    pass


class TranslationCircuitOpen(TransientProviderFailure):
    code = "translation_circuit_open"


def _explicit_qa_replacement(issue: TranslationQaIssue) -> str:
    """Return only a suggestion that is safe to treat as replacement dialogue.

    Gemini usually returns the exact Vietnamese sentence in ``suggestion``, but
    it can also return editing advice.  Applying that advice verbatim would put
    phrases such as "Nên dùng tên..." into the subtitle.  Explicitly labelled
    replacements are accepted after their label; otherwise keep the parser
    deliberately conservative and leave ambiguous blockers for REVIEW.
    """

    suggestion = issue.suggestion.strip()
    if not suggestion or "\n" in suggestion or "\r" in suggestion:
        return ""
    labelled = re.match(
        r"^(?:sửa(?:\s+lại)?(?:\s+câu)?\s+thành|thay(?:\s+bằng|\s+thành)|"
        r"câu\s+đúng(?:\s+là)?|bản\s+dịch\s+đúng(?:\s+là)?)\s*[:：]\s*(.+)$",
        suggestion,
        re.IGNORECASE,
    )
    candidate = labelled.group(1).strip() if labelled else suggestion
    candidate = re.sub(r"^['\"“”‘’]|['\"“”‘’]$", "", candidate).strip()
    if not candidate or len(candidate) > 500:
        return ""
    if any(token in candidate for token in ("{", "}", "=>", "```")):
        return ""
    if contains_cjk(candidate):
        return ""
    if not labelled and candidate.casefold().startswith(
        (
            "sửa",
            "cần",
            "nên",
            "hãy",
            "lỗi",
            "dịch",
            "bản dịch",
            "câu dịch",
            "tên nhân vật",
            "thay thế",
            "đề nghị",
            "gợi ý",
            "giữ ",
            "dùng ",
        )
    ):
        return ""
    return candidate


def _repair_values_from_qa_suggestions(
    values: Sequence[TranslationValue],
    issues: Sequence[TranslationQaIssue],
) -> tuple[TranslationValue, ...]:
    """Apply an explicit QA replacement when Gemini leaves a blocker unchanged.

    Web QA occasionally returns a precise replacement but the follow-up repair
    repeats the old sentence. Only blocker suggestions that are clearly a full
    replacement are eligible; free-form explanations are left for another
    remote repair cycle and still produce REVIEW rather than a false PASS.
    """
    by_index = {value.index: value for value in values}
    changed = False
    for issue in issues:
        if issue.severity is not TranslationQaSeverity.BLOCKER:
            continue
        candidate = _explicit_qa_replacement(issue)
        if not candidate:
            continue
        current = by_index.get(issue.index)
        if current is None or current.text.strip() == candidate:
            continue
        by_index[issue.index] = TranslationValue(current.cue_id, current.index, candidate)
        changed = True
    return tuple(by_index[index] for index in sorted(by_index)) if changed else tuple(values)


def _drop_resolved_qa_issues(
    values: Sequence[TranslationValue],
    issues: Sequence[TranslationQaIssue],
    *,
    previous_values: Sequence[TranslationValue],
    accept_changed_without_suggestion: bool = False,
) -> tuple[TranslationQaIssue, ...]:
    """Drop only blockers whose targeted replacement demonstrably changed text."""
    before_by_index = {value.index: value.text.strip() for value in previous_values}
    after_by_index = {value.index: value.text.strip() for value in values}
    remaining: list[TranslationQaIssue] = []
    for issue in issues:
        if issue.severity is not TranslationQaSeverity.BLOCKER:
            remaining.append(issue)
            continue
        candidate = _explicit_qa_replacement(issue)
        before = before_by_index.get(issue.index)
        after = after_by_index.get(issue.index)
        applied_explicit_fix = bool(
            candidate and before is not None and before != candidate and after == candidate
        )
        applied_targeted_change = bool(
            accept_changed_without_suggestion
            and not candidate
            and before is not None
            and after is not None
            and before != after
        )
        if applied_explicit_fix or applied_targeted_change:
            continue
        remaining.append(issue)
    return tuple(remaining)


def _cue_from_config(raw: object) -> SubtitleCue:
    if not isinstance(raw, dict):
        raise ValueError("translation_source_cue_must_be_object")
    def optional_box(key: str) -> tuple[float, float, float, float] | None:
        value = raw.get(key)
        if value is None:
            return None
        if (
            not isinstance(value, list)
            or len(value) != 4
            or not all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in value
            )
        ):
            raise ValueError(f"translation_source_cue_{key}_invalid")
        return tuple(float(item) for item in value)

    raw_evidence = raw.get("evidence_ids", [])
    if not isinstance(raw_evidence, list) or not all(
        isinstance(item, str) and item for item in raw_evidence
    ):
        raise ValueError("translation_source_cue_evidence_invalid")
    confidence = raw.get("confidence")
    return SubtitleCue(
        str(raw["id"]),
        int(raw["index"]),
        TimeRange(int(raw["start_us"]), int(raw["end_us"])),
        str(raw["text"]),
        str(raw.get("language", "zh")),
        revision=int(raw.get("revision", 1)),
        source=str(raw.get("source", "translation_input")),
        confidence=None if confidence is None else float(confidence),
        evidence_ids=tuple(raw_evidence),
        visual_box=optional_box("visual_box"),
        visual_box_source=str(raw.get("visual_box_source", "")),
        role=str(raw.get("role", "dialogue")),
        orientation=str(raw.get("orientation", "horizontal")),
        style_hint=str(raw.get("style_hint", "standard")),
        panel_box=optional_box("panel_box"),
        text_anchor=raw.get("text_anchor"),
        background_color=str(raw.get("background_color", "")),
        text_color=str(raw.get("text_color", "")),
        review_state=str(raw.get("review_state", "accepted")),
        scene_font_scale=float(raw.get("scene_font_scale", 1.0)),
        text_font_size=int(raw.get("text_font_size", 12)),
        subtitle_track_id=str(raw.get("subtitle_track_id", "")),
        background_padding_x=int(raw.get("background_padding_x", 20)),
        background_padding_y=int(raw.get("background_padding_y", 10)),
        background_style=str(raw.get("background_style", "solid")),
        letter_spacing_percent=int(raw.get("letter_spacing_percent", 0)),
        font_bold=bool(raw.get("font_bold", False)),
        font_italic=bool(raw.get("font_italic", False)),
        text_opacity_percent=int(raw.get("text_opacity_percent", 100)),
        background_opacity_percent=int(
            raw.get("background_opacity_percent", 90)
        ),
        fade_enabled=bool(raw.get("fade_enabled", False)),
        fade_duration_ms=int(raw.get("fade_duration_ms", 300)),
    )


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8-sig", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _split_chunk(chunk: TranslationChunk) -> tuple[TranslationChunk, TranslationChunk]:
    if len(chunk.cues) < 2:
        raise ValueError("Cannot split an isolated translation cue")
    if len(chunk.groups) > 1:
        middle = len(chunk.cues) / 2
        boundaries: list[tuple[float, int]] = []
        count = 0
        for index, group in enumerate(chunk.groups[:-1], start=1):
            count += len(group.cues)
            boundaries.append((abs(count - middle), index))
        split_at = min(boundaries)[1]
        left_groups = chunk.groups[:split_at]
        right_groups = chunk.groups[split_at:]
    else:
        cues = chunk.cues
        split_at = len(cues) // 2
        left_groups = (SemanticGroup(f"{chunk.groups[0].id}-a", cues[:split_at]),)
        right_groups = (SemanticGroup(f"{chunk.groups[0].id}-b", cues[split_at:]),)
    return (
        TranslationChunk(chunk.index, left_groups),
        TranslationChunk(chunk.index, right_groups),
    )


def _filter_chunk(chunk: TranslationChunk, retained_ids: set[int]) -> TranslationChunk:
    groups = tuple(
        SemanticGroup(
            group.id,
            tuple(cue for cue in group.cues if cue.index in retained_ids),
        )
        for group in chunk.groups
        if any(cue.index in retained_ids for cue in group.cues)
    )
    return TranslationChunk(chunk.index, groups)


def _strict_partial_translation(
    raw: str,
    chunk: TranslationChunk,
) -> (
    tuple[
        tuple[TranslationValue, ...],
        tuple[FilmTerm, ...],
        tuple[TranslationQaIssue, ...],
    ]
    | None
):
    """Salvage only a complete, non-empty, ordered subset of the requested IDs."""
    try:
        payload = parse_json_object(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    rows = payload.get("translations")
    if not isinstance(rows, list) or not rows:
        return None
    received: list[int] = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            return None
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        received.append(int(row["id"]))
    expected = list(chunk.ids)
    if len(received) >= len(expected) or len(received) != len(set(received)):
        return None
    expected_subset = [value for value in expected if value in set(received)]
    if received != expected_subset:
        return None
    cues_by_index = {cue.index: cue for cue in chunk.cues}
    if any(value not in cues_by_index for value in received):
        return None
    try:
        partial_payload = dict(payload)
        raw_issues = payload.get("issues", [])
        partial_payload["issues"] = (
            [
                issue
                for issue in raw_issues
                if isinstance(issue, dict) and issue.get("id") in received
            ]
            if isinstance(raw_issues, list)
            else []
        )
        result = parse_translation_response(
            json.dumps(partial_payload, ensure_ascii=False),
            expected_cues=tuple(cues_by_index[value] for value in received),
        )
    except (ValueError, ResponseFormatFailure, json.JSONDecodeError):
        return None
    return result.values, result.glossary_updates, result.qa_issues


class TranslationWorker:
    def __init__(
        self,
        job_id: str,
        *,
        provider_factory: Callable[[Mapping[str, Any]], TranslationProvider] | None = None,
    ) -> None:
        self.job_id = job_id
        self.commands: queue.Queue[WorkerMessage] = queue.Queue()
        self.acknowledgements: dict[str, WorkerMessage] = {}
        self.spec: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.provider_factory = provider_factory or self._provider_from_config
        self.provider: TranslationProvider | None = None
        self.fallback_provider: TranslationProvider | None = None
        self._fallback_active = False
        self.cues: tuple[SubtitleCue, ...] = ()
        self.chunks: tuple[TranslationChunk, ...] = ()
        self.ledger: TranslationLedger | None = None
        self.next_chunk = 0
        self._job_started = time.monotonic()
        self._progress_baseline_completed = 0
        self._activity_phase = "preparing"
        self._last_provider_progress = 0.0
        self._failed_provider_attempts = 0
        self._phase_failure_streaks: dict[str, int] = {}
        self.qa_mode = "independent"
        self.translation_purpose = "standard"
        self.timing_repair_targets: dict[int, dict[str, Any]] = {}
        self.resume_review_issues: dict[int, tuple[TranslationQaIssue, ...]] = {}
        self.qa_non_blocking = False
        self.qa_policy = "legacy"
        self.cjk_is_blocker = False
        self.auto_compact_overlong_once = False
        self.max_overlong_compaction_passes = 0
        self.translation_style_preset = "legacy"
        self.translation_custom_rules = ""
        staging_root = (
            Path(os.environ.get("DUCAI_ARTIFACT_STAGING_ROOT", Path.cwd() / ".ducai-staging"))
            .expanduser()
            .resolve()
        )
        self.job_staging = staging_root / job_id
        self.job_staging.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.job_staging / "translation-ledger-working.json"
        self._reader = threading.Thread(target=self._read_commands, daemon=True)

    def send(self, kind: str, payload: dict[str, Any]) -> WorkerMessage:
        message = WorkerMessage(
            PROTOCOL_VERSION,
            self.job_id,
            str(uuid4()),
            kind,
            payload,
        )
        sys.stdout.buffer.write(message.to_json_line())
        sys.stdout.buffer.flush()
        return message

    def acknowledge(self, command_id: str, kind: str, payload: dict[str, Any]) -> None:
        message = self.send(kind, {"command_id": command_id, **payload})
        self.acknowledgements[command_id] = message

    def _read_commands(self) -> None:
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return
            try:
                message = WorkerMessage.from_json_line(line)
            except Exception:
                continue
            if message.job_id == self.job_id:
                self.commands.put(message)

    def run(self) -> int:
        self._reader.start()
        try:
            start = self.commands.get(timeout=30)
        except queue.Empty:
            self.send("failed", {"code": "translation_start_timeout"})
            return 2
        if start.kind != "control" or start.payload.get("action") != "start":
            self.send("failed", {"code": "translation_start_command_invalid"})
            return 2
        command_id = start.payload.get("command_id")
        if not isinstance(command_id, str):
            self.send("failed", {"code": "translation_start_command_invalid"})
            return 2
        try:
            self._configure(dict(start.payload))
            self.acknowledge(command_id, "started", {"status": "running"})
            self._execute()
            return 0
        except WorkerStopped:
            return 0
        except TranslationProviderFailure as exc:
            self.send("failed", {"code": exc.code, "message": str(exc)[:500]})
            return 2
        except Exception as exc:
            code = str(exc) if str(exc).replace("_", "").isalnum() else type(exc).__name__
            self.send(
                "failed",
                {"code": code[:80] or "translation_worker_failed", "message": str(exc)[:500]},
            )
            return 2
        finally:
            if self.provider is not None:
                self.provider.close()
            if self.fallback_provider is not None:
                self.fallback_provider.close()

    def _configure(self, start_payload: dict[str, Any]) -> None:
        spec = start_payload.get("job")
        if not isinstance(spec, dict) or spec.get("kind") != "translate":
            raise ValueError("translation_job_spec_missing")
        config = spec.get("provider_config")
        if not isinstance(config, dict):
            raise ValueError("translation_provider_config_missing")
        raw_cues = config.get("source_cues")
        if not isinstance(raw_cues, list) or not raw_cues:
            raise ValueError("translation_source_cues_missing")
        self.spec = spec
        self.config = config
        # ``qa_non_blocking`` is retained for old jobs.  New Multi jobs use an
        # explicit policy so CJK remains the only content blocker while other
        # semantic findings are preserved as warnings.
        self.qa_non_blocking = config.get("qa_non_blocking") is True
        self.qa_policy = str(config.get("qa_policy", "legacy")).strip().casefold()
        if self.qa_policy not in {"legacy", "multi_auto"}:
            raise ValueError("translation_qa_policy_invalid")
        raw_cjk_is_blocker = config.get("cjk_is_blocker", self.qa_policy == "multi_auto")
        raw_auto_compact = config.get(
            "auto_compact_overlong_once", self.qa_policy == "multi_auto"
        )
        if type(raw_cjk_is_blocker) is not bool or type(raw_auto_compact) is not bool:
            raise ValueError("translation_qa_policy_options_invalid")
        self.cjk_is_blocker = raw_cjk_is_blocker
        self.auto_compact_overlong_once = raw_auto_compact
        raw_compaction_passes = config.get(
            "max_overlong_compaction_passes",
            1 if self.auto_compact_overlong_once else 0,
        )
        if (
            type(raw_compaction_passes) is not int
            or not 0 <= raw_compaction_passes <= 1
        ):
            raise ValueError("translation_overlong_compaction_passes_invalid")
        self.max_overlong_compaction_passes = raw_compaction_passes
        if self.qa_policy == "multi_auto" and not self.cjk_is_blocker:
            raise ValueError("translation_multi_policy_cjk_blocker_disabled")
        self.translation_style_preset = str(
            config.get("translation_style_preset", "legacy")
        ).strip().casefold()
        self.translation_custom_rules = str(
            config.get("translation_custom_rules", "")
        )
        if self.translation_style_preset not in {
            "legacy",
            "auto",
            "xianxia",
            "wuxia",
            "palace",
            "modern",
            "neutral",
        }:
            raise ValueError("translation_style_preset_invalid")
        if len(self.translation_custom_rules) > 4_000:
            raise ValueError("translation_custom_rules_too_long")
        self.translation_purpose = str(
            config.get("translation_purpose", "standard")
        ).strip().casefold()
        if self.translation_purpose not in {"standard", "voice_timing_repair"}:
            raise ValueError("translation_purpose_invalid")
        raw_timing_targets = config.get("timing_repair_targets", [])
        if not isinstance(raw_timing_targets, list):
            raise ValueError("translation_timing_repair_targets_invalid")
        timing_targets: dict[int, dict[str, Any]] = {}
        for raw_target in raw_timing_targets:
            if not isinstance(raw_target, dict) or type(raw_target.get("id")) is not int:
                raise ValueError("translation_timing_repair_target_invalid")
            cue_index = int(raw_target["id"])
            target_chars = raw_target.get("target_chars")
            if (
                cue_index <= 0
                or type(target_chars) is not int
                or not 1 <= target_chars <= 2_000
            ):
                raise ValueError("translation_timing_repair_target_invalid")
            timing_targets[cue_index] = dict(raw_target)
        self.timing_repair_targets = timing_targets
        if self.translation_purpose == "voice_timing_repair" and not timing_targets:
            raise ValueError("translation_timing_repair_targets_missing")
        self.qa_mode = str(config.get("qa_mode", "independent")).strip().casefold()
        if self.qa_mode not in {"integrated", "independent"}:
            raise ValueError("translation_qa_mode_invalid")
        self.cues = tuple(_cue_from_config(raw) for raw in raw_cues)
        if [cue.index for cue in self.cues] != list(range(1, len(self.cues) + 1)):
            raise ValueError("translation_source_indices_not_contiguous")
        raw_preserved = config.get("preserved_manual_translations", [])
        if not isinstance(raw_preserved, list):
            raise ValueError("translation_preserved_manual_cues_invalid")
        preserved_manual: dict[int, str] = {}
        for item in raw_preserved:
            if (
                not isinstance(item, dict)
                or type(item.get("index")) is not int
                or not isinstance(item.get("text"), str)
                or not str(item["text"]).strip()
            ):
                raise ValueError("translation_preserved_manual_cues_invalid")
            preserved_manual[int(item["index"])] = str(item["text"])
        cue_by_index = {cue.index: cue for cue in self.cues}
        if any(
            index not in cue_by_index
            or cue_by_index[index].source
            not in {"manual_dialogue", "manual_scene_text"}
            for index in preserved_manual
        ):
            raise ValueError("translation_preserved_manual_cues_mismatch")
        max_chunk_cues = self._integer_config("max_chunk_cues", 24, 1, 150)
        max_chunk_source_chars = self._integer_config("max_chunk_source_chars", 1_800, 200, 8_000)
        groups = semantic_groups(self.cues)
        base_chunks = plan_chunks(
            groups,
            max_cues=max_chunk_cues,
            max_source_chars=max_chunk_source_chars,
        )
        planned_chunks_list: list[TranslationChunk] = []
        for base_chunk in base_chunks:
            run: list[SubtitleCue] = []
            run_is_manual: bool | None = None

            def flush_run() -> None:
                nonlocal run
                if not run:
                    return
                chunk_index = len(planned_chunks_list)
                planned_chunks_list.append(
                    TranslationChunk(
                        chunk_index,
                        (
                            SemanticGroup(
                                f"translation-run-{chunk_index + 1}",
                                tuple(run),
                            ),
                        ),
                    )
                )
                run = []

            for cue in base_chunk.cues:
                is_manual = cue.index in preserved_manual
                if run_is_manual is not None and is_manual != run_is_manual:
                    flush_run()
                run_is_manual = is_manual
                run.append(cue)
            flush_run()
        planned_chunks = tuple(planned_chunks_list)
        self.provider = self.provider_factory(config)
        self.fallback_provider = self._fallback_from_config(config)
        expected_ledger = build_ledger(
            self.cues,
            planned_chunks,
            provider_id=self.provider.provider_id,
            model_id=self.provider.model_id,
            film_bible=FilmBible(
                title=str(config.get("title", "")),
                style_preset=self.translation_style_preset,
                custom_rules=self.translation_custom_rules,
            ),
        )
        if preserved_manual:
            expected_ledger = replace(
                expected_ledger,
                chunks=tuple(
                    replace(entry, state=ChunkState.QA_PASS)
                    if all(index in preserved_manual for index in entry.cue_ids)
                    else entry
                    for entry in expected_ledger.chunks
                ),
                translations=preserved_manual,
            )
        restored = self._restore_checkpoint(start_payload.get("checkpoint"))
        if restored is None and self.translation_purpose == "voice_timing_repair":
            raw_seed = config.get("seed_ledger")
            if not isinstance(raw_seed, dict):
                raise ValueError("translation_timing_repair_seed_missing")
            restored = ledger_from_dict(raw_seed)
        if restored is not None:
            if restored.signature != expected_ledger.signature:
                raise ValueError("translation_checkpoint_signature_mismatch")
            checkpoint = start_payload.get("checkpoint")
            raw_qa = checkpoint.get("translation_qa") if isinstance(checkpoint, dict) else None
            if isinstance(raw_qa, dict) and isinstance(raw_qa.get("issues"), list):
                recovered: dict[int, list[TranslationQaIssue]] = {}
                for row in raw_qa["issues"]:
                    if not isinstance(row, dict) or type(row.get("id")) is not int:
                        continue
                    details = row.get("details")
                    if not isinstance(details, list):
                        details = [row]
                    for detail in details:
                        if not isinstance(detail, dict):
                            continue
                        try:
                            severity = TranslationQaSeverity(str(detail.get("severity", "")))
                            issue = TranslationQaIssue(
                                int(row["id"]),
                                str(detail.get("code", row.get("code", ""))),
                                severity,
                                str(detail.get("message", row.get("message", ""))),
                                str(detail.get("suggestion", row.get("suggestion", ""))),
                            )
                        except (TypeError, ValueError):
                            continue
                        recovered.setdefault(issue.index, []).append(issue)
                if recovered:
                    chunks = [
                        replace(
                            chunk,
                            qa_issues=tuple(
                                issue
                                for cue_id in chunk.cue_ids
                                for issue in recovered.get(cue_id, ())
                            ),
                        )
                        if chunk.state is ChunkState.PENDING and not chunk.qa_issues
                        else chunk
                        for chunk in restored.chunks
                    ]
                    restored = replace(restored, chunks=tuple(chunks))
            if self.qa_non_blocking:
                normalized_chunks = []
                for entry in restored.chunks:
                    normalized_issues = normalize_semantic_qa_issues(
                        entry.qa_issues,
                        keep_cjk_blocker=self.qa_policy == "multi_auto",
                        only_cjk_blocker=self.qa_policy == "multi_auto",
                    )
                    has_complete_translation = all(
                        cue_id in restored.translations for cue_id in entry.cue_ids
                    )
                    has_completed_qa = entry.state in {
                        ChunkState.QA_PASS,
                        ChunkState.QA_REVIEW,
                    }
                    normalized_chunks.append(
                        replace(
                            entry,
                            state=(
                                entry.state
                                if not has_complete_translation or not has_completed_qa
                                else ChunkState.QA_REVIEW
                                if any(
                                    issue.severity is TranslationQaSeverity.BLOCKER
                                    for issue in normalized_issues
                                )
                                else ChunkState.QA_PASS
                            ),
                            qa_issues=normalized_issues,
                        )
                    )
                restored = replace(restored, chunks=tuple(normalized_chunks))
            review_issues: dict[int, list[TranslationQaIssue]] = {}
            if self.qa_mode == "integrated":
                for entry in restored.chunks:
                    for issue in entry.qa_issues:
                        if issue.severity is TranslationQaSeverity.BLOCKER:
                            review_issues.setdefault(issue.index, []).append(issue)
            self.resume_review_issues = {
                index: tuple(issues) for index, issues in review_issues.items()
            }
            self.ledger, self.chunks = replan_ledger_for_resume(
                restored,
                self.cues,
                max_cues=max_chunk_cues,
                max_source_chars=max_chunk_source_chars,
            )
        else:
            self.ledger = expected_ledger
            self.chunks = planned_chunks
        self.next_chunk = next(
            (
                item.chunk_index
                for item in self.ledger.chunks
                if item.state is not ChunkState.QA_PASS
            ),
            len(self.ledger.chunks),
        )

    def _provider_from_config(self, config: Mapping[str, Any]) -> TranslationProvider:
        provider_id = str(self.spec.get("provider_id", ""))
        model_id = str(self.spec.get("model_id", ""))
        if provider_id == "gemini-api":
            references = config.get("credential_references")
            if not isinstance(references, list) or not all(
                isinstance(value, str) for value in references
            ):
                raise ValueError("gemini_api_credential_references_missing")
            store = WindowsCredentialStore()
            return GeminiApiProvider(
                model_id=model_id,
                credential_references=references,
                credential_resolver=store.read,
                timeout_seconds=float(config.get("provider_timeout_seconds", 180)),
            )
        if provider_id in OPENAI_COMPATIBLE_PROVIDER_IDS:
            references = config.get("credential_references")
            if not isinstance(references, list) or not all(
                isinstance(value, str) for value in references
            ):
                raise ValueError("openai_compatible_credential_references_missing")
            base_url = config.get("base_url")
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError("openai_compatible_base_url_missing")
            json_mode = config.get("json_mode", True)
            if type(json_mode) is not bool:
                raise ValueError("openai_compatible_json_mode_invalid")
            extra_body = config.get("extra_body", {})
            if not isinstance(extra_body, Mapping):
                raise ValueError("openai_compatible_extra_body_invalid")
            store = WindowsCredentialStore()
            return OpenAICompatibleProvider(
                provider_id=provider_id,
                model_id=model_id,
                base_url=base_url,
                credential_references=references,
                credential_resolver=store.read,
                timeout_seconds=float(config.get("provider_timeout_seconds", 300)),
                json_mode=json_mode,
                extra_body=extra_body,
            )
        if provider_id == "gemini-cli":
            return GeminiCliProvider(
                model_id=model_id,
                executable=str(config.get("cli_executable", "gemini")),
                timeout_seconds=float(config.get("provider_timeout_seconds", 300)),
            )
        if provider_id == "gemini-web":
            return GeminiWebProvider(
                model_id=model_id,
                profile_path=str(config["profile_path"]),
                headed=config.get("headed") is True,
                guest=config.get("guest") is True,
                temporary_chat=config.get("temporary_chat") is not False,
                browser_executable=str(config.get("browser_executable", "")),
                timeout_seconds=float(config.get("provider_timeout_seconds", 900)),
                first_response_timeout_seconds=float(
                    config.get("web_first_response_timeout_seconds", 30)
                ),
                response_stall_timeout_seconds=float(
                    config.get("web_response_stall_timeout_seconds", 45)
                ),
                session_retries=self._integer_config("web_session_retries", 1, 0, 4),
                max_requests_per_session=self._integer_config(
                    "web_max_requests_per_session", 5, 1, 20
                ),
                interrupt_check=self._provider_control_point,
            )
        raise ValueError("translation_provider_not_supported")

    def _fallback_from_config(self, config: Mapping[str, Any]) -> TranslationProvider | None:
        if str(self.spec.get("provider_id", "")) != "gemini-web":
            return None
        if config.get("codex_fallback_enabled") is not True:
            return None
        try:
            return CodexCliProvider(
                model_id=str(config.get("codex_fallback_model", "gpt-5.6-luna")),
                executable=str(config.get("codex_fallback_executable", "codex")),
                timeout_seconds=float(config.get("codex_fallback_timeout_seconds", 300)),
            )
        except (OSError, ValueError):
            return None

    def _generate(self, request: TranslationRequest):
        assert self.provider is not None
        if self._fallback_active:
            assert self.fallback_provider is not None
            try:
                response = self.fallback_provider.generate(request)
            except TransientProviderFailure as exc:
                self._record_provider_failure(request, exc)
                raise
            self._phase_failure_streaks[request.purpose.value] = 0
            self._emit_provider_request(request, response)
            return response
        try:
            response = self._generate_with_control(self.provider, request)
        except TransientProviderFailure as exc:
            self.send(
                "warning",
                {
                    "code": str(getattr(exc, "code", "transient_provider_failure"))[:80],
                    "message": str(exc)[:300],
                    "request_id": request.request_id,
                    "purpose": request.purpose.value,
                    "expected_cues": len(request.expected_ids),
                },
            )
            self._record_provider_failure(request, exc)
            if self.fallback_provider is None:
                raise
            self.provider.close()
            self._fallback_active = True
            self.send(
                "warning",
                {
                    "code": "codex_fallback_activated",
                    "message": (
                        "Gemini Web vẫn lỗi sau các phiên sạch; chuyển các chunk còn lại "
                        "sang GPT-5.6 Luna qua Codex CLI, giữ nguyên cache và QA."
                    ),
                },
            )
            response = self.fallback_provider.generate(request)
        self._phase_failure_streaks[request.purpose.value] = 0
        self._emit_provider_request(request, response)
        return response

    def _generate_with_control(
        self, provider: TranslationProvider, request: TranslationRequest,
    ):
        # HTTP providers have no browser/thread affinity. Keep exactly one
        # request in flight while the worker thread checkpoints and handles
        # controls. A paused response waits here until resume; a cancelled
        # response is discarded when this isolated worker exits. Do not move
        # Playwright/CLI sessions onto a different thread.
        if not isinstance(provider, (GeminiApiProvider, OpenAICompatibleProvider)):
            return provider.generate(request)
        self._provider_control_point()
        results: queue.Queue[object] = queue.Queue(maxsize=1)

        def generate() -> None:
            try:
                results.put(provider.generate(request))
            except BaseException as exc:
                results.put(exc)

        thread = threading.Thread(target=generate, name="translation-http", daemon=True)
        thread.start()
        while True:
            self._provider_control_point()
            try:
                result = results.get(timeout=0.1)
            except queue.Empty:
                continue
            # A command queued as the HTTP call completed still owns priority
            # over publishing a response or starting a later request.
            self._provider_control_point()
            if isinstance(result, BaseException):
                raise result
            return result

    def _record_provider_failure(
        self,
        request: TranslationRequest,
        failure: TransientProviderFailure,
    ) -> None:
        phase = request.purpose.value
        self._failed_provider_attempts += 1
        self._phase_failure_streaks[phase] = self._phase_failure_streaks.get(phase, 0) + 1
        total_limit = self._integer_config("max_job_provider_failures", 24, 3, 100)
        phase_limit = self._integer_config("max_phase_provider_failures", 6, 2, 20)
        if (
            self._failed_provider_attempts >= total_limit
            or self._phase_failure_streaks[phase] >= phase_limit
        ):
            raise TranslationCircuitOpen(
                "Dừng retry Gemini có kiểm soát: số lỗi transport của job/pha đã "
                "chạm ngưỡng an toàn; cache hiện tại vẫn được giữ nguyên."
            ) from failure

    def _emit_provider_request(self, request: TranslationRequest, response: object) -> None:
        provider_id = str(getattr(response, "provider_id", ""))
        model_id = str(getattr(response, "model_id", ""))
        usage = getattr(response, "usage", {})
        safe_usage = thaw_json(usage) if isinstance(usage, Mapping) else {}
        self.send(
            "provider_request",
            {
                "request_id": request.request_id,
                "purpose": request.purpose.value,
                "expected_cues": len(request.expected_ids),
                "provider_id": provider_id,
                "model_id": model_id,
                "transport": safe_usage,
            },
        )

    def _restore_checkpoint(self, checkpoint: object) -> TranslationLedger | None:
        if not isinstance(checkpoint, dict):
            return None
        artifact = checkpoint.get("artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("uri"), str):
            return None
        source = Path(str(artifact["uri"])).expanduser().resolve(strict=True)
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("translation_checkpoint_ledger_invalid")
        return ledger_from_dict(raw)

    def _execute(self) -> None:
        assert self.provider is not None and self.ledger is not None
        self._job_started = time.monotonic()
        self._progress_baseline_completed = self._completed_progress_units()
        if self.ledger.translations:
            restored = tuple(
                TranslationValue(cue.id, cue.index, str(self.ledger.translations[cue.index]))
                for cue in self.cues
                if cue.index in self.ledger.translations
            )
            self._emit_translation_delta(restored, snapshot=True)
        self._emit_qa_status(snapshot=True)
        if not self.ledger.film_bible.synopsis and not self.ledger.film_bible.terms:
            self._send_progress("film_bible")
            self._build_film_bible()
            self._checkpoint()
        for chunk in self.chunks:
            if self.ledger.chunks[chunk.index].state in {
                ChunkState.QA_PASS,
                ChunkState.QA_REVIEW,
            }:
                continue
            self.next_chunk = chunk.index
            self._control_point()
            if self.translation_purpose == "voice_timing_repair":
                self._send_progress("qa_repair")
                values = tuple(
                    TranslationValue(
                        cue.id,
                        cue.index,
                        str(self.ledger.translations[cue.index]),
                    )
                    for cue in chunk.cues
                )
                film_bible = self.ledger.film_bible
                attempts = 0
                issues = self._quality_issues(chunk, values, film_bible)
            else:
                self._send_progress("translation")
                values, glossary, integrated_issues, attempts = self._translate_chunk(chunk)
                film_bible = self.ledger.film_bible.merge_terms(glossary)
                self._send_progress(
                    "integrated_qa" if self.qa_mode == "integrated" else "independent_qa"
                )
                issues = self._quality_issues(
                    chunk,
                    values,
                    film_bible,
                    integrated_issues=integrated_issues,
                )
            carried_review_issues = tuple(
                issue for index in chunk.ids for issue in self.resume_review_issues.get(index, ())
            )
            if carried_review_issues:
                issues = tuple(
                    {
                        (item.index, item.code): item for item in (*issues, *carried_review_issues)
                    }.values()
                )
            if self.qa_policy == "multi_auto":
                issues = normalize_semantic_qa_issues(
                    issues,
                    keep_cjk_blocker=self.cjk_is_blocker,
                    only_cjk_blocker=self.cjk_is_blocker,
                )
            compaction_attempted = False
            style_repair_attempted = False
            for _repair_cycle in range(2):
                if style_repair_attempted:
                    issues = downgrade_style_qa_issues(issues)
                blockers = [
                    issue for issue in issues if issue.severity is TranslationQaSeverity.BLOCKER
                ]
                if self.qa_policy == "multi_auto":
                    overlong_codes = {"overlong_translation", "cue_too_long"}
                    cjk_blockers = [
                        issue
                        for issue in blockers
                        if "cjk" in issue.code.casefold()
                    ]
                    technical_blockers = [
                        issue
                        for issue in blockers
                        if "cjk" not in issue.code.casefold()
                        and issue.code.casefold() not in overlong_codes
                    ]
                    overlong_blockers = [
                        issue
                        for issue in blockers
                        if issue.code.casefold() in overlong_codes
                    ]
                    # Resolve CJK first.  A cue that is both Chinese and too
                    # long must not spend its single compaction pass until the
                    # final Vietnamese text is known.
                    policy_blockers = [*cjk_blockers, *technical_blockers]
                    if (
                        not cjk_blockers
                        and not compaction_attempted
                        and self.auto_compact_overlong_once
                    ):
                        policy_blockers.extend(
                            overlong_blockers[: self.max_overlong_compaction_passes]
                        )
                    blockers = policy_blockers
                if not blockers:
                    break
                if any(issue.code == "malformed_integrated_qa" for issue in blockers):
                    # The translated rows are complete, but the model's QA
                    # envelope is untrustworthy. Do not spend another provider
                    # request rewriting otherwise valid text: checkpoint the
                    # whole chunk for review and continue with later chunks.
                    self.send(
                        "warning",
                        {
                            "code": "integrated_qa_deferred",
                            "message": (
                                "QA tích hợp sai định dạng; đã giữ bản dịch và "
                                "đánh dấu rà soát."
                            ),
                            "cue_ids": list(chunk.ids),
                        },
                    )
                    break
                values_before_suggestions = values
                suggested_values = _repair_values_from_qa_suggestions(values, blockers)
                if self.qa_policy == "multi_auto" and any(
                    issue.code.casefold() in {"overlong_translation", "cue_too_long"}
                    for issue in blockers
                ) and any(
                    before.text != after.text
                    for before, after in zip(values, suggested_values, strict=True)
                ):
                    compaction_attempted = True
                if any(
                    before.text != after.text
                    for before, after in zip(values, suggested_values, strict=True)
                ):
                    values = suggested_values
                    self._cache_partial(values, ())
                    unresolved_previous = _drop_resolved_qa_issues(
                        values,
                        blockers,
                        previous_values=values_before_suggestions,
                    )
                    issues = tuple(
                        {
                            (item.index, item.code): item
                            for item in (
                                *self._quality_issues(chunk, values, film_bible),
                                *unresolved_previous,
                            )
                        }.values()
                    )
                    unresolved_ids = {issue.index for issue in unresolved_previous}
                    for index in set(chunk.ids).difference(unresolved_ids):
                        self.resume_review_issues.pop(index, None)
                    blockers = [
                        issue for issue in issues if issue.severity is TranslationQaSeverity.BLOCKER
                    ]
                    if not blockers:
                        break
                self._send_progress("qa_repair")
                repair_ids = {issue.index for issue in blockers}
                if (
                    self.qa_policy == "multi_auto"
                    and self.max_overlong_compaction_passes > 0
                    and any(
                        issue.code.casefold() in {"overlong_translation", "cue_too_long"}
                        for issue in blockers
                    )
                ):
                    compaction_attempted = True
                repair_chunk = _filter_chunk(chunk, repair_ids)
                repair_values = tuple(value for value in values if value.index in repair_ids)
                repair_issues = tuple(blockers)
                repair_bible = film_bible
                values_before_repair = values
                if any(issue.code in STYLE_QA_CODES for issue in repair_issues):
                    style_repair_attempted = True

                def make_repair_request(
                    current: TranslationChunk,
                    repair_values: Sequence[TranslationValue] = repair_values,
                    repair_issues: Sequence[TranslationQaIssue] = repair_issues,
                    repair_bible: FilmBible = repair_bible,
                ) -> TranslationRequest:
                    return repair_request(
                        current,
                        tuple(value for value in repair_values if value.index in current.ids),
                        tuple(issue for issue in repair_issues if issue.index in current.ids),
                        film_bible=repair_bible,
                    )

                try:
                    (
                        repaired_subset,
                        updates,
                        repair_integrated_issues,
                        repair_attempts,
                    ) = self._request_chunk_resilient(
                        repair_chunk,
                        make_repair_request,
                    )
                except ResponseFormatFailure as exc:
                    # A malformed targeted repair must not terminate a long
                    # translation after hours of valid cached work. Keep the
                    # original cue and its blocker, checkpoint it as QA_REVIEW,
                    # and continue translating later chunks. The final review
                    # navigator exposes this cue for another cached retry or a
                    # manual edit; no CJK repair response is ever accepted.
                    self.send(
                        "warning",
                        {
                            "code": "targeted_repair_deferred",
                            "message": str(exc)[:300],
                            "cue_ids": sorted(repair_ids),
                        },
                    )
                    break
                merged = {value.index: value for value in values}
                merged.update({value.index: value for value in repaired_subset})
                values = tuple(merged[index] for index in chunk.ids)
                attempts += repair_attempts
                film_bible = film_bible.merge_terms(updates)
                unresolved_previous = _drop_resolved_qa_issues(
                    values,
                    repair_issues,
                    previous_values=values_before_repair,
                    accept_changed_without_suggestion=True,
                )
                issues = tuple(
                    {
                        (item.index, item.code): item
                        for item in (
                            *self._quality_issues(
                                chunk,
                                values,
                                film_bible,
                                integrated_issues=repair_integrated_issues,
                            ),
                            *unresolved_previous,
                        )
                    }.values()
                )
                unresolved_ids = {issue.index for issue in unresolved_previous}
                for index in repair_ids.difference(unresolved_ids):
                    self.resume_review_issues.pop(index, None)
            if style_repair_attempted:
                issues = downgrade_style_qa_issues(issues)
            if self.qa_non_blocking:
                issues = normalize_semantic_qa_issues(
                    issues,
                    keep_cjk_blocker=self.qa_policy == "multi_auto" and self.cjk_is_blocker,
                    only_cjk_blocker=self.qa_policy == "multi_auto" and self.cjk_is_blocker,
                )
            if self.qa_policy == "multi_auto" and not self.auto_compact_overlong_once:
                # A caller may deliberately disable the one-shot condensation
                # pass.  Overlong text remains evidence, but it must not turn
                # into a content blocker under the Multi policy.
                issues = tuple(
                    replace(
                        issue,
                        severity=TranslationQaSeverity.WARNING,
                        original_severity=issue.original_severity
                        or TranslationQaSeverity.BLOCKER,
                    )
                    if issue.code.casefold() in {"overlong_translation", "cue_too_long"}
                    and issue.severity is TranslationQaSeverity.BLOCKER
                    else issue
                    for issue in issues
                )
            if (
                self.qa_policy == "multi_auto"
                and compaction_attempted
                and self.max_overlong_compaction_passes > 0
            ):
                issues = tuple(
                    replace(
                        issue,
                        severity=TranslationQaSeverity.WARNING,
                        original_severity=TranslationQaSeverity.BLOCKER,
                        message=(
                            issue.message
                            + " Đã thử rút gọn một lần; vẫn giữ nguyên để không mất nghĩa."
                        ),
                    )
                    if issue.code.casefold() in {"overlong_translation", "cue_too_long"}
                    and issue.severity is TranslationQaSeverity.BLOCKER
                    else issue
                    for issue in issues
                )
            self.ledger = mark_chunk_complete(
                self.ledger,
                chunk_index=chunk.index,
                values=values,
                qa_issues=issues,
                film_bible=film_bible,
                attempts=attempts,
            )
            self.next_chunk = chunk.index + 1
            self._checkpoint()
            self._send_progress("translation_qa")
        self._publish_result()

    def _build_film_bible(self) -> None:
        assert self.provider is not None and self.ledger is not None
        request = film_bible_request(
            self.cues,
            title=str(self.config.get("title", "")),
            style_preset=self.translation_style_preset,
            custom_rules=self.translation_custom_rules,
        )
        for attempt in range(1, 4):
            self._control_point()
            try:
                response = self._generate(request)
                bible = parse_film_bible_response(
                    response.text,
                    title=str(self.config.get("title", "")),
                    style_preset=self.translation_style_preset,
                    custom_rules=self.translation_custom_rules,
                )
                self.ledger = replace(self.ledger, film_bible=bible)
                return
            except (ValueError, ResponseFormatFailure) as exc:
                self.provider.close()
                if attempt == 3:
                    self.send(
                        "warning",
                        {
                            "code": "film_bible_format_fallback",
                            "message": str(exc)[:300],
                        },
                    )
                    return
                self._interruptible_wait(attempt * 2)
            except WebNoResponseStarted:
                self.provider.close()
                raise
            except TransientProviderFailure:
                self.provider.close()
                if attempt == 3:
                    raise
                self._interruptible_wait(attempt * 5)

    def _translate_chunk(
        self, chunk: TranslationChunk
    ) -> tuple[
        tuple[TranslationValue, ...],
        tuple[FilmTerm, ...],
        tuple[TranslationQaIssue, ...],
        int,
    ]:
        assert self.ledger is not None
        cached = tuple(
            TranslationValue(cue.id, cue.index, str(self.ledger.translations[cue.index]))
            for cue in chunk.cues
            if cue.index in self.ledger.translations
        )
        missing_ids = {cue.index for cue in chunk.cues} - {value.index for value in cached}
        if not missing_ids:
            return cached, (), (), 0
        missing_groups = tuple(
            SemanticGroup(
                group.id,
                tuple(cue for cue in group.cues if cue.index in missing_ids),
            )
            for group in chunk.groups
            if any(cue.index in missing_ids for cue in group.cues)
        )
        pending_chunk = TranslationChunk(chunk.index, missing_groups)
        previous = tuple(
            TranslationValue(
                cue.id,
                cue.index,
                str(self.ledger.translations[cue.index]),
            )
            for cue in self.cues[: chunk.cues[0].index - 1]
            if cue.index in self.ledger.translations
        )
        following = self.cues[chunk.cues[-1].index :]
        values, terms, issues, attempts = self._request_chunk_resilient(
            pending_chunk,
            lambda current: translation_request(
                current,
                film_bible=self.ledger.film_bible,
                previous=previous,
                following=following,
            ),
        )
        combined = tuple(sorted((*cached, *values), key=lambda value: value.index))
        if tuple(value.index for value in combined) != chunk.ids:
            raise ValueError("translation_cached_chunk_coverage_mismatch")
        return combined, terms, issues, attempts

    def _request_chunk_resilient(
        self,
        chunk: TranslationChunk,
        request_factory: Callable[[TranslationChunk], TranslationRequest],
        *,
        depth: int = 0,
    ) -> tuple[
        tuple[TranslationValue, ...],
        tuple[FilmTerm, ...],
        tuple[TranslationQaIssue, ...],
        int,
    ]:
        assert self.provider is not None
        attempts = 0
        format_failures = 0
        transient_failures = 0
        last_error: Exception | None = None
        while True:
            self._control_point()
            attempts += 1
            response_text = ""
            reject_partial_response = False
            try:
                request = request_factory(chunk)
                if format_failures:
                    request = replace(
                        request,
                        prompt=(
                            request.prompt + "\nLƯỢT CỨU LỖI: chỉ trả JSON gọn, đủ đúng mọi ID; "
                            "không giải thích."
                        ),
                    )
                response = self._generate(request)
                response_text = response.text
                result = parse_translation_response(response.text, expected_cues=chunk.cues)
                if request.request_id.startswith("repair-") and any(
                    contains_cjk(value.text) for value in result.values
                ):
                    # Do not checkpoint or emit a repair that still contains
                    # Chinese. Retry the same targeted request inside this
                    # resilient call so it does not consume an outer QA cycle.
                    reject_partial_response = True
                    raise ResponseFormatFailure(
                        "Targeted translation repair still contains CJK"
                    )
                self._cache_partial(result.values, result.glossary_updates)
                return (
                    result.values,
                    result.glossary_updates,
                    result.qa_issues,
                    attempts,
                )
            except (ValueError, ResponseFormatFailure) as exc:
                partial = (
                    _strict_partial_translation(response_text, chunk)
                    if response_text and not reject_partial_response
                    else None
                )
                if partial is not None:
                    partial_values, partial_terms, partial_issues = partial
                    self._cache_partial(partial_values, partial_terms)
                    received = {value.index for value in partial_values}
                    remaining = _filter_chunk(chunk, set(chunk.ids).difference(received))
                    (
                        remaining_values,
                        remaining_terms,
                        remaining_issues,
                        remaining_attempts,
                    ) = self._request_chunk_resilient(
                        remaining,
                        request_factory,
                        depth=depth,
                    )
                    combined_by_id = {
                        value.index: value for value in (*partial_values, *remaining_values)
                    }
                    return (
                        tuple(combined_by_id[index] for index in chunk.ids),
                        partial_terms + remaining_terms,
                        partial_issues + remaining_issues,
                        attempts + remaining_attempts,
                    )
                last_error = ResponseFormatFailure(str(exc))
                format_failures += 1
                self.provider.close()
                if format_failures < (4 if len(chunk.cues) == 1 else 2):
                    self._interruptible_wait(min(8, format_failures * 2))
                    continue
                break
            except TransientProviderFailure as exc:
                last_error = exc
                transient_failures += 1
                self.provider.close()
                transient_attempt_limit = 2 if len(chunk.cues) > 1 else 5
                if transient_failures < transient_attempt_limit:
                    self._interruptible_wait(min(120, 5 * 2 ** (transient_failures - 1)))
                    continue
                raise last_error from exc
        if len(chunk.cues) <= 1 or depth >= 8:
            assert last_error is not None
            raise last_error
        left, right = _split_chunk(chunk)
        left_values, left_terms, left_issues, left_attempts = self._request_chunk_resilient(
            left, request_factory, depth=depth + 1
        )
        right_values, right_terms, right_issues, right_attempts = self._request_chunk_resilient(
            right, request_factory, depth=depth + 1
        )
        return (
            left_values + right_values,
            left_terms + right_terms,
            left_issues + right_issues,
            attempts + left_attempts + right_attempts,
        )

    def _cache_partial(self, values: Sequence[TranslationValue], terms: Sequence[FilmTerm]) -> None:
        assert self.ledger is not None
        translated = {int(key): str(value) for key, value in self.ledger.translations.items()}
        translated.update({value.index: value.text for value in values})
        film_bible = self.ledger.film_bible.merge_terms(terms)
        self.ledger = replace(
            self.ledger,
            translations=translated,
            film_bible=film_bible,
        )
        self._checkpoint()
        self._emit_translation_delta(values)

    def _emit_translation_delta(
        self,
        values: Sequence[TranslationValue],
        *,
        snapshot: bool = False,
    ) -> None:
        if not values:
            return
        assert self.ledger is not None
        self.send(
            "translation_delta",
            {
                "values": [
                    {
                        "id": value.cue_id,
                        "index": value.index,
                        "text": value.text,
                    }
                    for value in values
                ],
                "completed_cues": len(self.ledger.translations),
                "snapshot": snapshot,
            },
        )

    def _emit_qa_status(self, *, snapshot: bool = False) -> None:
        assert self.ledger is not None
        self.send(
            "translation_qa_status",
            {
                **translation_qa_snapshot(self.ledger, self.cues),
                "snapshot": snapshot,
            },
        )

    def _run_qa(
        self,
        chunk: TranslationChunk,
        values: Sequence[TranslationValue],
        film_bible: FilmBible,
    ) -> tuple[TranslationQaIssue, ...]:
        assert self.provider is not None
        local = self._run_local_qa(chunk, values, film_bible)
        request = qa_request(chunk, values, film_bible=film_bible)
        for attempt in range(1, 4):
            self._control_point()
            try:
                response = self._generate(request)
                remote = parse_qa_response(response.text, allowed_ids=chunk.ids)
                return tuple({(item.index, item.code): item for item in (*local, *remote)}.values())
            except (ValueError, ResponseFormatFailure):
                self.provider.close()
                if attempt < 3:
                    self._interruptible_wait(attempt * 2)
                    continue
                return local + (
                    TranslationQaIssue(
                        chunk.ids[0],
                        "remote_qa_format_unavailable",
                        TranslationQaSeverity.BLOCKER,
                        "Gemini QA không trả JSON hợp lệ sau ba lần.",
                    ),
                )
            except TransientProviderFailure:
                self.provider.close()
                if attempt == 3:
                    raise
                self._interruptible_wait(attempt * 5)
        raise AssertionError("Unreachable QA retry state")

    def _run_local_qa(
        self,
        chunk: TranslationChunk,
        values: Sequence[TranslationValue],
        film_bible: FilmBible,
    ) -> tuple[TranslationQaIssue, ...]:
        banned = self.config.get("banned_latin_terms", [])
        if not isinstance(banned, list) or not all(isinstance(value, str) for value in banned):
            banned = []
        issues = (
            *local_translation_qa(
                values,
                banned_latin_terms=banned,
                scene_text_ids=(cue.index for cue in chunk.cues if cue.is_scene_text),
            ),
            *film_bible_translation_qa(chunk, values, film_bible=film_bible),
            *translation_style_qa(chunk, values, film_bible=film_bible),
        )
        return tuple({(item.index, item.code): item for item in issues}.values())

    def _quality_issues(
        self,
        chunk: TranslationChunk,
        values: Sequence[TranslationValue],
        film_bible: FilmBible,
        *,
        integrated_issues: Sequence[TranslationQaIssue] = (),
    ) -> tuple[TranslationQaIssue, ...]:
        if self.qa_mode == "independent":
            base = self._run_qa(chunk, values, film_bible)
        else:
            base = tuple(
                {
                    (item.index, item.code): item
                    for item in (
                        *self._run_local_qa(chunk, values, film_bible),
                        *integrated_issues,
                    )
                }.values()
            )
        timing_issues: list[TranslationQaIssue] = []
        for value in values:
            target = self.timing_repair_targets.get(value.index)
            if target is None:
                continue
            target_chars = int(target["target_chars"])
            normalized_length = len(" ".join(value.text.split()))
            if normalized_length <= target_chars:
                continue
            timing_issues.append(
                TranslationQaIssue(
                    value.index,
                    "CUE_TOO_LONG",
                    TranslationQaSeverity.BLOCKER,
                    str(target.get("message", "")).strip()
                    or (
                        f"Cue dài: cần rút còn khoảng {target_chars} ký tự để voice "
                        "kịp timeline, vẫn phải giữ đủ nghĩa và tên riêng."
                    ),
                )
            )
        return tuple(
            {
                (item.index, item.code): item
                for item in (*base, *timing_issues)
            }.values()
        )

    def _publish_result(self) -> None:
        assert self.ledger is not None
        expected = tuple(cue.index for cue in self.cues)
        received = tuple(sorted(int(value) for value in self.ledger.translations))
        if received != expected:
            raise ValueError("translation_final_cue_coverage_mismatch")
        translated = tuple(
            SubtitleCue(
                cue.id,
                cue.index,
                cue.range,
                (
                    wrap_vietnamese_subtitle(str(self.ledger.translations[cue.index]))
                    if cue.is_dialogue
                    else " ".join(str(self.ledger.translations[cue.index]).split())
                ),
                "vi",
                source="gemini_translation",
                confidence=cue.confidence,
                evidence_ids=cue.evidence_ids,
                visual_box=cue.visual_box,
                visual_box_source=cue.visual_box_source,
                role=cue.role,
                orientation=cue.orientation,
                style_hint=cue.style_hint,
                panel_box=cue.panel_box,
                text_anchor=cue.text_anchor,
                background_color=cue.background_color,
                text_color=cue.text_color,
                review_state=cue.review_state,
                scene_font_scale=cue.scene_font_scale,
                text_font_size=cue.text_font_size,
                subtitle_track_id=cue.subtitle_track_id,
                background_padding_x=cue.background_padding_x,
                background_padding_y=cue.background_padding_y,
                background_style=cue.background_style,
                letter_spacing_percent=cue.letter_spacing_percent,
                font_bold=cue.font_bold,
                font_italic=cue.font_italic,
                text_opacity_percent=cue.text_opacity_percent,
                background_opacity_percent=cue.background_opacity_percent,
                fade_enabled=cue.fade_enabled,
                fade_duration_ms=cue.fade_duration_ms,
            )
            for cue in self.cues
        )
        qa = summarize_qa(self.ledger)
        output_path = Path(
            str(self.config.get("staging_output_path", self.config["output_path"]))
        ).expanduser().resolve()
        qa_path = output_path.with_name(output_path.stem + "_QA.json")
        if qa["status"] != "PASS":
            _atomic_text(qa_path, json.dumps(qa, ensure_ascii=False, indent=2))
            review_path = output_path.with_name(output_path.stem + "_REVIEW.srt")
            _atomic_text(review_path, render_srt(translated))
            cjk_cue_ids = sorted(
                {
                    int(item["id"])
                    for item in qa.get("issues", [])
                    if isinstance(item, dict)
                    and type(item.get("id")) is int
                    and "cjk" in str(item.get("code", "")).casefold()
                    and str(item.get("severity", "")).casefold() == "blocker"
                }
            )
            self._checkpoint(
                checkpoint_key="review",
                artifact_path=review_path,
                artifact_kind="translation_review_srt",
                extra_payload={
                    "stage": "review",
                    "qa_path": str(qa_path),
                    "qa": qa,
                },
            )
            payload = {
                "code": "translation_review_required",
                "status": "REVIEW",
                "output_path": str(review_path),
                "qa_path": str(qa_path),
            }
            if self.qa_policy == "multi_auto":
                payload.update(
                    {
                        "cjk_cue_ids": cjk_cue_ids,
                        "cjk_blocker_count": len(cjk_cue_ids),
                        "warnings_only": not cjk_cue_ids,
                    }
                )
            self.send("failed", payload)
            return
        _atomic_text(output_path, render_srt(translated))
        output_stat = output_path.stat()
        qa.update(
            {
                "job_id": self.job_id,
                "input_fingerprint": self.spec.get("input_fingerprint", ""),
                "settings_fingerprint": self.spec.get("settings_fingerprint", ""),
                "output_path": str(output_path),
                "output_sha256": file_sha256(output_path),
                "output_size": output_stat.st_size,
                "output_mtime_ns": output_stat.st_mtime_ns,
            }
        )
        _atomic_text(qa_path, json.dumps(qa, ensure_ascii=False, indent=2))
        self.send(
            "succeeded",
            {
                "status": "PASS",
                "output_path": str(output_path),
                "qa_path": str(qa_path),
                "cue_count": len(translated),
                "warning_count": int(qa.get("warning_count", 0)),
                "blocking_count": int(qa.get("blocking_count", 0)),
            },
        )

    def _control_point(self) -> None:
        while True:
            try:
                message = self.commands.get_nowait()
            except queue.Empty:
                return
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if isinstance(command_id, str) and command_id in self.acknowledgements:
                cached = self.acknowledgements[command_id]
                sys.stdout.buffer.write(cached.to_json_line())
                sys.stdout.buffer.flush()
                continue
            if action == "pause" and isinstance(command_id, str):
                self._checkpoint()
                self.acknowledge(command_id, "paused", {"status": "paused"})
                self._wait_while_paused()
            elif action in {"cancel", "stop"}:
                self._checkpoint()
                payload = {"status": str(action)}
                if isinstance(command_id, str):
                    payload["command_id"] = command_id
                self.send("stopped", payload)
                raise WorkerStopped

    def _provider_control_point(self) -> None:
        self._control_point()
        now = time.monotonic()
        if now - self._last_provider_progress >= 1.0:
            self._last_provider_progress = now
            self._send_progress(self._activity_phase)

    def _wait_while_paused(self) -> None:
        while True:
            message = self.commands.get()
            action = message.payload.get("action")
            command_id = message.payload.get("command_id")
            if action == "stop" and not isinstance(command_id, str):
                self.send("stopped", {"status": "stop"})
                raise WorkerStopped
            if not isinstance(command_id, str):
                continue
            if action == "resume":
                self.acknowledge(command_id, "resumed", {"status": "running"})
                return
            if action in {"cancel", "stop"}:
                self.send("stopped", {"status": str(action), "command_id": command_id})
                raise WorkerStopped

    def _interruptible_wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        previous_tick = 0.0
        while time.monotonic() < deadline:
            self._control_point()
            now = time.monotonic()
            if now - previous_tick >= 1.0:
                previous_tick = now
                self._send_progress("recovery_backoff", eta_override_ms=None)
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def _send_progress(self, phase: str, *, eta_override_ms: int | None | object = ...) -> None:
        assert self.ledger is not None
        self._activity_phase = phase
        elapsed_ms = max(1, int((time.monotonic() - self._job_started) * 1_000))
        translated_units = len(self.ledger.translations)
        qa_units = sum(
            len(item.cue_ids)
            for item in self.ledger.chunks
            if item.state in {ChunkState.QA_PASS, ChunkState.QA_REVIEW}
        )
        completed = qa_units if self.qa_mode == "integrated" else translated_units + qa_units
        total = len(self.cues) if self.qa_mode == "integrated" else len(self.cues) * 2
        session_completed = max(0, completed - self._progress_baseline_completed)
        rate = session_completed / (elapsed_ms / 1_000)
        remaining = max(0, total - completed)
        calculated_eta = int(remaining / rate * 1_000) if rate > 0 else None
        eta_ms = calculated_eta if eta_override_ms is ... else eta_override_ms
        self.send(
            "progress",
            {
                "phase": phase,
                "current": completed,
                "total": total,
                "elapsed_ms": elapsed_ms,
                "eta_ms": eta_ms,
                "chunk_current": self.next_chunk,
                "chunk_total": len(self.chunks),
                "translated_cues": translated_units,
                "qa_cues": qa_units,
                "cue_total": len(self.cues),
            },
        )

    def _completed_progress_units(self) -> int:
        assert self.ledger is not None
        translated_units = len(self.ledger.translations)
        qa_units = sum(
            len(item.cue_ids)
            for item in self.ledger.chunks
            if item.state in {ChunkState.QA_PASS, ChunkState.QA_REVIEW}
        )
        return qa_units if self.qa_mode == "integrated" else translated_units + qa_units

    def _checkpoint(
        self,
        *,
        checkpoint_key: str = "latest",
        artifact_path: Path | None = None,
        artifact_kind: str = "translation_ledger",
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        assert self.ledger is not None
        source = artifact_path
        if source is not None and not source.resolve().is_relative_to(self.job_staging.parent):
            staged = self.job_staging / f"artifact-{uuid4().hex}{source.suffix}.partial"
            shutil.copyfile(source, staged)
            source = staged
        if source is None:
            working = json.dumps(ledger_to_dict(self.ledger), ensure_ascii=False, indent=2)
            self.ledger_path.write_text(working, encoding="utf-8", newline="\n")
            source = self.job_staging / f"ledger-{uuid4().hex}.json.partial"
            shutil.copyfile(self.ledger_path, source)
        digest = file_sha256(source)
        completed_ids = sorted(int(value) for value in self.ledger.translations)
        cursor_us = self.cues[completed_ids[-1] - 1].range.end_us if completed_ids else 0
        payload = {
            "stage_key": "translation",
            "checkpoint_key": checkpoint_key,
            "schema_version": 1,
            "cursor_us": cursor_us,
            "payload": {
                "stage": "translation",
                "next_chunk": self.next_chunk,
                "completed_cues": len(self.ledger.translations),
                **(extra_payload or {}),
            },
            "artifact_path": str(source),
            "artifact_kind": artifact_kind,
            "artifact_state": "technical",
            "expected_sha256": digest,
        }
        self.send("checkpoint", payload)
        self._emit_qa_status()

    def _integer_config(self, key: str, default: int, minimum: int, maximum: int) -> int:
        raw = self.config.get(key, default)
        if type(raw) is not int or not minimum <= raw <= maximum:
            raise ValueError(f"translation_{key}_invalid")
        return raw


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        return 2
    from ducai_studio.infrastructure.licensing_state import worker_activation_exit_code

    denied = worker_activation_exit_code()
    if denied:
        return denied
    return TranslationWorker(sys.argv[1]).run()


if __name__ == "__main__":
    raise SystemExit(main())
