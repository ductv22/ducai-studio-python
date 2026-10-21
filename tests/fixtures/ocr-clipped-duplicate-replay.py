"""Replay real OCR evidence through the production aggregation path, CPU-only.

This reads a failed job and its immutable evidence from a read-only SQLite
connection. It never starts an OCR engine or writes to the job database.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import zlib
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ducai_studio.application.ocr_pipeline import (
    OCR_AGGREGATION_VERSION,
    cjk_count,
    compact_text,
)
from ducai_studio.application.subtitle_finalizer import (
    collapse_partial_ocr_cues,
    collapse_proven_ocr_cues,
    finalize_ocr_candidates,
    render_srt,
)
from ducai_studio.domain.jobs import JobKind, JobSpec
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.domain.timeline_edit import project_for_timeline
from ducai_studio.infrastructure.project_repository import (
    decode_project_snapshot,
    encode_project_snapshot,
)
from ducai_studio.job_manager.service import (
    JobManagerService,
    _apply_ocr_cue_regions,
    _merge_preserved_manual_subtitle_cues,
)
from ducai_studio.workers.ocr_worker import OcrWorker, _sampling_intervals

QA_ROOT = Path(r"E:\ducai-a141-qa").resolve()
PLAN_REPORTS = Path(__file__).resolve().parents[2] / "plans" / (
    "261008-2203-cue-role-controls-and-exact-ocr-overhead"
) / "reports"
PRIOR_EQUAL_POLICY_REPORT = PLAN_REPORTS / (
    "ocr-clipped-duplicate-candidate-terminal-winner-audit.json"
)
TARGET_START_US = 540_600_000
TARGET_END_US = 541_500_000
TARGET_READINGS = {"箭毒蛙", "前毒蛙"}
MANUAL_CUE_SOURCES = {"manual_dialogue", "manual_scene_text"}


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _candidate_tuple(candidate: Any) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "range_us": [candidate.range.start_us, candidate.range.end_us],
        "text": candidate.text,
        "cjk_count": cjk_count(candidate.text),
        "confidence": candidate.confidence,
        "evidence_ids": list(candidate.evidence_ids),
        "source": candidate.source,
        "review_reasons": list(candidate.review_reasons),
    }


def _cue_tuple(cue: Any) -> dict[str, Any]:
    return {
        "id": cue.id,
        "index": cue.index,
        "range_us": [cue.range.start_us, cue.range.end_us],
        "text": cue.text,
        "confidence": cue.confidence,
        "evidence_ids": list(cue.evidence_ids),
        "visual_box": _json_value(cue.visual_box),
        "source": cue.source,
        "role": cue.role,
        "subtitle_track_id": cue.subtitle_track_id,
        "review_state": cue.review_state,
    }


def _check_output_paths(output: Path, report_path: Path) -> None:
    output = output.resolve()
    report_path = report_path.resolve()
    if not output.is_relative_to(QA_ROOT):
        raise ValueError(f"isolated output must be under {QA_ROOT}")
    if not (report_path.is_relative_to(QA_ROOT) or report_path.is_relative_to(PLAN_REPORTS)):
        raise ValueError(f"report must be under {QA_ROOT} or {PLAN_REPORTS}")


def _read_job(database: Path, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise RuntimeError("database_connection_not_query_only")
        job_row = connection.execute(
            "SELECT id,state,project_id,input_revision,config_json FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if job_row is None:
            raise ValueError(f"job not found: {job_id}")
        artifact_row = connection.execute(
            "SELECT uri,sha256,size_bytes,state FROM artifacts "
            "WHERE job_id=? AND kind='ocr_evidence'",
            (job_id,),
        ).fetchone()
        if artifact_row is None:
            raise ValueError("immutable OCR evidence artifact not found")
        job = dict(job_row)
        artifact = dict(artifact_row)
    job["config"] = json.loads(job.pop("config_json"))
    return job, artifact


def _target_cues(cues: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [
        _cue_tuple(cue)
        for cue in cues
        if cue.text in TARGET_READINGS
        and cue.range.start_us < TARGET_END_US
        and cue.range.end_us > TARGET_START_US
    ]


def _target_candidates(candidates: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [
        _candidate_tuple(candidate)
        for candidate in candidates
        if candidate.text in TARGET_READINGS
        and candidate.range.start_us < TARGET_END_US
        and candidate.range.end_us > TARGET_START_US
    ]


def _read_raw_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict) and isinstance(raw.get("id"), str):
                rows[raw["id"]] = raw
    return rows


def _raw_scope(row: dict[str, Any]) -> str:
    return str(row.get("source_scope") or (
        f"{row.get('media_id', '')}:{row.get('scan_segment_index', '')}"
        if "media_id" in row or "scan_segment_index" in row else ""
    ))


def _box(row: dict[str, Any]) -> tuple[float, float, float, float] | None:
    value = row.get("box")
    if not isinstance(value, list) or len(value) != 4:
        return None
    return tuple(float(item) for item in value)


def _same_frame_proof_audit(
    proofs: list[dict[str, Any]], raw_rows: dict[str, dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    bands = {
        row["name"]: (
            float(row["x1"]),
            float(row["y1"]),
            float(row["x2"]) - float(row["x1"]),
            float(row["y2"]) - float(row["y1"]),
        )
        for row in config.get("bands", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    support: dict[tuple[str, str, str, str, str], set[int]] = defaultdict(set)
    for row in raw_rows.values():
        if row.get("ignored_overlay") is True or row.get("record_kind") == "scene_probe":
            continue
        try:
            if float(row["confidence"]) < 0.90 or type(row["timestamp_us"]) is not int:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        key = (
            _raw_scope(row),
            str(row.get("provider_id", "")),
            str(row.get("model_id", "")),
            str(row.get("band", "")),
            compact_text(str(row.get("text", ""))),
        )
        support[key].add(row["timestamp_us"])

    failures: Counter[str] = Counter()
    different_text = 0
    same_text = 0
    equal_count_proofs = 0
    equal_count_low_confidence_proofs = 0
    strictly_longer_proofs = 0
    pairs: Counter[str] = Counter()
    suppressed_ids = {proof["evidence_id"] for proof in proofs}
    winner_ids = {proof["winner_evidence_id"] for proof in proofs}
    nonterminal_winner_ids = suppressed_ids & winner_ids
    if nonterminal_winner_ids:
        failures["winner_not_terminal"] += len(nonterminal_winner_ids)
    for proof in proofs:
        partial = raw_rows.get(proof["evidence_id"], {})
        full = raw_rows.get(proof["winner_evidence_id"], {})
        pairs[f"{partial.get('band', '?')}->{full.get('band', '?')}"] += 1
        if partial.get("text") == full.get("text"):
            same_text += 1
        else:
            different_text += 1
        if not partial or not full:
            failures["raw_row_missing"] += 1
            continue
        partial_characters = cjk_count(str(partial.get("text", "")))
        full_characters = cjk_count(str(full.get("text", "")))
        partial_confidence = float(partial.get("confidence", 0))
        if full_characters < max(2, partial_characters):
            failures["winner_cjk_count"] += 1
        elif full_characters == partial_characters:
            equal_count_proofs += 1
            if partial_confidence >= 0.90:
                failures["equal_count_confidence_floor"] += 1
            else:
                equal_count_low_confidence_proofs += 1
        else:
            strictly_longer_proofs += 1
        if (
            partial.get("frame_id") != full.get("frame_id")
            or partial.get("timestamp_us") != full.get("timestamp_us")
            or _raw_scope(partial) != _raw_scope(full)
            or partial.get("provider_id") != full.get("provider_id")
            or partial.get("model_id") != full.get("model_id")
        ):
            failures["same_frame_scope_provider_model"] += 1
        if proof.get("frame_id") != partial.get("frame_id") or proof.get(
            "timestamp_us"
        ) != partial.get("timestamp_us") or proof.get("source_scope") != _raw_scope(partial):
            failures["proof_identity"] += 1
        if full.get("band") == partial.get("band") or float(full.get("confidence", 0)) < 0.90:
            failures["winner_band_or_confidence"] += 1
        partial_box = _box(partial)
        full_box = _box(full)
        crop = bands.get(str(partial.get("band", "")))
        full_crop = bands.get(str(full.get("band", "")))
        if partial_box is None or full_box is None or crop is None or full_crop is None:
            failures["geometry_missing"] += 1
            continue
        px, py, pw, ph = partial_box
        fx, fy, fw, fh = full_box
        cx, cy, cw, ch = crop
        full_cx, full_cy, full_cw, full_ch = full_crop
        intersection = max(0, min(px + pw, fx + fw) - max(px, fx)) * max(
            0, min(py + ph, fy + fh) - max(py, fy)
        )
        if intersection < pw * ph * 0.80:
            failures["overlap_80_percent"] += 1
        if abs((py + ph / 2) - (fy + fh / 2)) > fh * 0.60:
            failures["vertical_centers"] += 1
        cuts_same_edge = any((
            py - cy <= ch * 0.03 and fy < cy - 0.003,
            cy + ch - py - ph <= ch * 0.03 and fy + fh > cy + ch + 0.003,
            px - cx <= cw * 0.03 and fx < cx - 0.003,
            cx + cw - px - pw <= cw * 0.03 and fx + fw > cx + cw + 0.003,
        ))
        if not cuts_same_edge:
            failures["cropped_edge_not_crossed"] += 1
        if not (
            fx > full_cx + 0.001
            and fy > full_cy + 0.001
            and fx + fw < full_cx + full_cw - 0.001
            and fy + fh < full_cy + full_ch - 0.001
        ):
            failures["winner_not_uncut_inside_own_crop"] += 1
        support_key = (
            _raw_scope(full),
            str(full.get("provider_id", "")),
            str(full.get("model_id", "")),
            str(full.get("band", "")),
            compact_text(str(full.get("text", ""))),
        )
        timestamp = full.get("timestamp_us")
        corroborating = sum(
            abs(value - timestamp) <= 800_000
            for value in support.get(support_key, set())
        ) if type(timestamp) is int else 0
        if corroborating < 2:
            failures["uncut_winner_corroboration"] += 1
    return {
        "proof_count": len(proofs),
        "invalid_proof_condition_counts": dict(failures),
        "all_proof_conditions_pass": not failures,
        "proof_role_counts": {
            "equal_count_below_confidence_floor": equal_count_low_confidence_proofs,
            "equal_count_at_or_above_confidence_floor": (
                equal_count_proofs - equal_count_low_confidence_proofs
            ),
            "strictly_longer_winner": strictly_longer_proofs,
            "equal_count_confidence_floor": 0.90,
        },
        "partial_to_full_band_pairs": dict(pairs),
        "same_text_pairs": same_text,
        "different_text_pairs": different_text,
        "raw_rows_available": len(raw_rows),
        "terminal_winner_audit": {
            "suppressed_evidence_ids": len(suppressed_ids),
            "proof_winner_evidence_ids": len(winner_ids),
            "winner_also_suppressed_count": len(nonterminal_winner_ids),
            "all_proof_winners_terminal": not nonterminal_winner_ids,
        },
    }


def _proof_staging_audit(
    baseline: dict[str, Any],
    baseline_report_path: Path,
    prior_candidate_report: dict[str, Any],
    evidence_sha256: str,
    proofs: list[dict[str, Any]],
    raw_rows: dict[str, dict[str, Any]],
    reconciled_evidence: tuple[Any, ...],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify high-confidence equal-count reads stay available to consensus."""
    baseline_proofs = baseline["replay"]["suppression_proofs"]
    prior_proofs = prior_candidate_report["replay"]["suppression_proofs"]
    proof_map = {item["evidence_id"]: item["winner_evidence_id"] for item in proofs}
    baseline_proof_ids = {item["evidence_id"] for item in baseline_proofs}
    prior_proof_ids = {item["evidence_id"] for item in prior_proofs}
    baseline_longer: dict[str, str] = {}
    prior_longer: dict[str, str] = {}
    prior_equal_high: set[str] = set()
    for item in (*baseline_proofs, *prior_proofs):
        partial = raw_rows.get(item["evidence_id"], {})
        winner = raw_rows.get(item["winner_evidence_id"], {})
        if not partial or not winner:
            continue
        partial_count = cjk_count(str(partial.get("text", "")))
        winner_count = cjk_count(str(winner.get("text", "")))
        partial_confidence = float(partial.get("confidence", 0))
        if winner_count > partial_count:
            if item["evidence_id"] in baseline_proof_ids:
                baseline_longer[item["evidence_id"]] = item["winner_evidence_id"]
            if item["evidence_id"] in prior_proof_ids:
                prior_longer[item["evidence_id"]] = item["winner_evidence_id"]
        elif winner_count == partial_count and partial_confidence >= 0.90:
            if item["evidence_id"] in prior_proof_ids:
                prior_equal_high.add(item["evidence_id"])

    reconciled_ids = {item.id for item in reconciled_evidence}
    candidate_ids = {
        evidence_id for row in candidates for evidence_id in row["evidence_ids"]
    }
    proof_ids = set(proof_map)
    immediate_proof_ids = proof_ids - reconciled_ids
    deferred_proof_ids = proof_ids & reconciled_ids
    retained_equal_high = prior_equal_high - proof_map.keys()
    deferred_equal_low = {
        item["evidence_id"]
        for item in proofs
        if item["evidence_id"] in raw_rows
        and item["winner_evidence_id"] in raw_rows
        and cjk_count(str(raw_rows[item["evidence_id"]].get("text", "")))
        == cjk_count(str(raw_rows[item["winner_evidence_id"]].get("text", "")))
        and float(raw_rows[item["evidence_id"]].get("confidence", 0)) < 0.90
    }
    missing_high_equal = sorted(retained_equal_high - reconciled_ids)
    missing_deferred_equal = sorted(deferred_equal_low - reconciled_ids)
    high_equal_still_suppressed = sorted(prior_equal_high & proof_map.keys())

    current_longer = {
        evidence_id: winner_id
        for evidence_id, winner_id in proof_map.items()
        if evidence_id in raw_rows and winner_id in raw_rows
        and cjk_count(str(raw_rows[winner_id].get("text", "")))
        > cjk_count(str(raw_rows[evidence_id].get("text", "")))
    }
    missing_longer = sorted(set(baseline_longer) - set(current_longer))
    changed_longer_winners = sorted(
        evidence_id for evidence_id, winner_id in baseline_longer.items()
        if evidence_id in current_longer and current_longer[evidence_id] != winner_id
    )
    missing_prior_longer = sorted(set(prior_longer) - set(current_longer))
    changed_prior_longer = sorted(
        evidence_id for evidence_id, winner_id in prior_longer.items()
        if evidence_id in current_longer and current_longer[evidence_id] != winner_id
    )
    failures = {
        "high_confidence_equal_count_proofs_still_suppressed": high_equal_still_suppressed,
        "high_confidence_equal_count_evidence_not_retained": missing_high_equal,
        "deferred_low_confidence_equal_count_evidence_not_retained": missing_deferred_equal,
        "immediate_proof_evidence_reentered_candidate_support": sorted(
            immediate_proof_ids & candidate_ids
        ),
        "original_strictly_longer_proofs_missing": missing_longer,
        "original_strictly_longer_winner_changed": changed_longer_winners,
        "prior_strictly_longer_proofs_missing": missing_prior_longer,
        "prior_strictly_longer_winner_changed": changed_prior_longer,
    }
    if baseline["evidence"]["sha256"] != evidence_sha256:
        failures["frozen_baseline_evidence_hash_mismatch"] = [
            baseline["evidence"]["sha256"], evidence_sha256,
        ]
    if prior_candidate_report["evidence"]["sha256"] != evidence_sha256:
        failures["prior_equal_policy_evidence_hash_mismatch"] = [
            prior_candidate_report["evidence"]["sha256"], evidence_sha256,
        ]
    return {
        "status": "PASS" if not any(failures.values()) else "FAIL",
        "equal_count_confidence_floor": 0.90,
        "frozen_baseline_report": str(baseline_report_path.resolve()),
        "frozen_baseline_sha256": baseline["evidence"]["sha256"],
        "prior_equal_policy_report": str(PRIOR_EQUAL_POLICY_REPORT.resolve()),
        "prior_equal_policy_proof_count": len(prior_proofs),
        "prior_high_confidence_equal_count_proof_ids": len(prior_equal_high),
        "high_confidence_equal_count_ids_not_proofed": len(retained_equal_high),
        "high_confidence_equal_count_ids_retained_for_consensus": len(
            retained_equal_high & reconciled_ids
        ),
        "high_confidence_equal_count_ids_in_candidate_support": len(
            retained_equal_high & candidate_ids
        ),
        "deferred_low_confidence_equal_count_proofs": len(deferred_equal_low),
        "deferred_low_confidence_equal_count_ids_retained_for_consensus": len(
            deferred_equal_low & reconciled_ids
        ),
        "proof_removal_stages": {
            "proof_evidence_ids": len(proof_ids),
            "immediate_reconciliation_removals": len(immediate_proof_ids),
            "deferred_evidence_retained_for_consensus": len(deferred_proof_ids),
            "immediate_proof_ids_in_candidate_support": len(
                immediate_proof_ids & candidate_ids
            ),
            "deferred_proof_ids_in_candidate_support": len(
                deferred_proof_ids & candidate_ids
            ),
            "immediate_proof_evidence_ids": sorted(immediate_proof_ids),
            "deferred_proof_evidence_ids": sorted(deferred_proof_ids),
        },
        "baseline_strictly_longer_proof_count": len(baseline_longer),
        "prior_strictly_longer_proof_count": len(prior_longer),
        "strictly_longer_proofs_preserved_with_same_winner": (
            len(baseline_longer) - len(missing_longer) - len(changed_longer_winners)
        ),
        "prior_strictly_longer_proofs_preserved_with_same_winner": sum(
            evidence_id in current_longer and current_longer[evidence_id] == winner_id
            for evidence_id, winner_id in prior_longer.items()
        ),
        "failures": failures,
    }


def _finalization_proof_collapse_audit(
    candidates: tuple[Any, ...],
    finalized_cues: tuple[Any, ...],
    proof_map: dict[str, str],
    deferred_proof_ids: set[str],
) -> dict[str, Any]:
    """Account for candidates removed by the final proof-guided collapse."""
    final_evidence_ids = {
        evidence_id for cue in finalized_cues for evidence_id in cue.evidence_ids
    }
    candidate_evidence_ids = {
        evidence_id for candidate in candidates for evidence_id in candidate.evidence_ids
    }
    owners: dict[str, list[Any]] = defaultdict(list)
    candidate_owners: dict[str, list[Any]] = defaultdict(list)
    for candidate in candidates:
        for evidence_id in candidate.evidence_ids:
            candidate_owners[evidence_id].append(candidate)
    for cue in finalized_cues:
        for evidence_id in cue.evidence_ids:
            owners[evidence_id].append(cue)

    removed = [
        candidate for candidate in candidates
        if not set(candidate.evidence_ids) & final_evidence_ids
    ]
    partially_retained = [
        candidate for candidate in candidates
        if set(candidate.evidence_ids) & final_evidence_ids
        and not set(candidate.evidence_ids) <= final_evidence_ids
    ]
    fully_retained = [
        candidate for candidate in candidates
        if set(candidate.evidence_ids) and set(candidate.evidence_ids) <= final_evidence_ids
    ]
    details = []
    failures = []
    canonical_winner_changes = []
    for candidate in removed:
        evidence_ids = tuple(candidate.evidence_ids)
        all_proven = bool(evidence_ids) and all(key in proof_map for key in evidence_ids)
        winner_ids = {proof_map[key] for key in evidence_ids if key in proof_map}
        winner_owner_lists = {key: owners.get(key, []) for key in winner_ids}
        winner_candidate_lists = {
            key: candidate_owners.get(key, []) for key in winner_ids
        }
        unique_winner_cues = {
            cue.id: cue for values in winner_owner_lists.values() for cue in values
        }
        unowned_winners = sorted(key for key, values in winner_owner_lists.items() if not values)
        multiply_owned_winners = sorted(
            key for key, values in winner_owner_lists.items() if len(values) != 1
        )
        unowned_winner_candidates = sorted(
            key for key, values in winner_candidate_lists.items() if not values
        )
        multiply_owned_winner_candidates = sorted(
            key for key, values in winner_candidate_lists.items() if len(values) != 1
        )
        unique_winner_candidates = {
            item.id: item
            for values in winner_candidate_lists.values()
            for item in values
        }
        canonical_changes = []
        if len(unique_winner_candidates) == 1 and len(unique_winner_cues) == 1:
            winner_candidate = next(iter(unique_winner_candidates.values()))
            winner_cue = next(iter(unique_winner_cues.values()))
            if winner_candidate.text != winner_cue.text or set(
                winner_candidate.evidence_ids
            ) != set(winner_cue.evidence_ids):
                canonical_changes.append({
                    "winner_candidate_id": winner_candidate.id,
                    "winner_cue_id": winner_cue.id,
                    "candidate_text": winner_candidate.text,
                    "cue_text": winner_cue.text,
                    "candidate_evidence_ids": sorted(winner_candidate.evidence_ids),
                    "cue_evidence_ids": sorted(winner_cue.evidence_ids),
                })
        if canonical_changes:
            canonical_winner_changes.extend(canonical_changes)
        verified = (
            all_proven
            and not unowned_winners
            and not multiply_owned_winners
            and not unowned_winner_candidates
            and not multiply_owned_winner_candidates
            and len(unique_winner_cues) == 1
            and len(unique_winner_candidates) == 1
            and not canonical_changes
        )
        if not verified:
            failures.append(candidate.id)
        details.append({
            "candidate_id": candidate.id,
            "range_us": [candidate.range.start_us, candidate.range.end_us],
            "text": candidate.text,
            "evidence_id_count": len(evidence_ids),
            "all_evidence_ids_proofed": all_proven,
            "proof_winner_evidence_id_count": len(winner_ids),
            "retained_winner_cue_count": len(unique_winner_cues),
            "retained_winner_candidate_count": len(unique_winner_candidates),
            "retained_winner_cues": [
                {
                    "id": cue.id,
                    "range_us": [cue.range.start_us, cue.range.end_us],
                    "text": cue.text,
                    "evidence_ids": list(cue.evidence_ids),
                }
                for cue in sorted(
                    unique_winner_cues.values(),
                    key=lambda item: (item.range.start_us, item.range.end_us, item.id),
                )
            ],
            "unowned_winner_evidence_ids": unowned_winners,
            "ambiguous_winner_evidence_ids": multiply_owned_winners,
            "unowned_winner_candidate_evidence_ids": unowned_winner_candidates,
            "ambiguous_winner_candidate_evidence_ids": multiply_owned_winner_candidates,
            "canonical_winner_changes": canonical_changes,
            "verified_single_winner": verified,
        })

    removed_evidence_ids = sorted(candidate_evidence_ids - final_evidence_ids)
    immediate_proof_ids = set(proof_map) - deferred_proof_ids
    immediate_proof_ids_in_candidates = sorted(
        immediate_proof_ids & candidate_evidence_ids
    )
    deferred_removed_evidence_ids = sorted(
        set(removed_evidence_ids) & deferred_proof_ids
    )
    immediate_removed_evidence_ids = sorted(
        set(removed_evidence_ids) & immediate_proof_ids
    )
    removed_evidence_details = []
    unverified_removed_evidence_ids = []
    for evidence_id in removed_evidence_ids:
        removed_owners = candidate_owners.get(evidence_id, [])
        winner_id = proof_map.get(evidence_id)
        winner_candidates = candidate_owners.get(winner_id, []) if winner_id else []
        winner_cues = owners.get(winner_id, []) if winner_id else []
        canonical_unchanged = False
        if len(winner_candidates) == 1 and len(winner_cues) == 1:
            winner_candidate = winner_candidates[0]
            winner_cue = winner_cues[0]
            canonical_unchanged = (
                winner_candidate.text == winner_cue.text
                and set(winner_candidate.evidence_ids) == set(winner_cue.evidence_ids)
            )
            if not canonical_unchanged:
                canonical_winner_changes.append({
                    "removed_evidence_id": evidence_id,
                    "winner_evidence_id": winner_id,
                    "winner_candidate_id": winner_candidate.id,
                    "winner_cue_id": winner_cue.id,
                    "candidate_text": winner_candidate.text,
                    "cue_text": winner_cue.text,
                    "candidate_evidence_ids": sorted(winner_candidate.evidence_ids),
                    "cue_evidence_ids": sorted(winner_cue.evidence_ids),
                })
        verified = bool(
            winner_id
            and len(removed_owners) == 1
            and len(winner_candidates) == 1
            and len(winner_cues) == 1
            and canonical_unchanged
        )
        if not verified:
            unverified_removed_evidence_ids.append(evidence_id)
        removed_evidence_details.append({
            "evidence_id": evidence_id,
            "winner_evidence_id": winner_id,
            "removed_source_candidate_owner_count": len(removed_owners),
            "retained_winner_candidate_count": len(winner_candidates),
            "retained_winner_cue_count": len(winner_cues),
            "retained_winner_texts": sorted({cue.text for cue in winner_cues}),
            "canonical_winner_text_and_evidence_unchanged": canonical_unchanged,
            "verified_single_canonical_winner": verified,
        })

    evidence_text_changes = []
    for candidate in candidates:
        related = {
            cue.id: cue
            for evidence_id in candidate.evidence_ids
            for cue in owners.get(evidence_id, [])
        }
        if not related or all(cue.text == candidate.text for cue in related.values()):
            continue
        evidence_text_changes.append({
            "candidate_id": candidate.id,
            "candidate_text": candidate.text,
            "candidate_range_us": [candidate.range.start_us, candidate.range.end_us],
            "retained_cue_count_sharing_evidence": len(related),
            "retained_texts_sharing_evidence": sorted({cue.text for cue in related.values()}),
        })
    return {
        "status": (
            "PASS"
            if not failures
            and not unverified_removed_evidence_ids
            and not immediate_proof_ids_in_candidates
            else "FAIL"
        ),
        "candidate_count_before_finalization": len(candidates),
        "final_dialogue_cue_count": len(finalized_cues),
        "candidate_support_sets_fully_absent_from_final_cues": len(removed),
        "candidate_support_sets_partially_retained": len(partially_retained),
        "candidate_support_sets_fully_retained": len(fully_retained),
        "removed_candidates_verified_to_one_retained_winner": len(removed) - len(failures),
        "unverified_removed_candidate_ids": failures,
        "removed_candidate_details": details,
        "removed_evidence_id_count": len(removed_evidence_ids),
        "proof_removal_stages": {
            "immediate_reconciliation_proofs": len(immediate_proof_ids),
            "immediate_proof_ids_in_candidate_support": len(
                immediate_proof_ids_in_candidates
            ),
            "deferred_proof_ids_in_candidate_support": len(
                deferred_proof_ids & candidate_evidence_ids
            ),
            "deferred_proof_evidence_removed_during_finalization": len(
                deferred_removed_evidence_ids
            ),
            "immediate_proof_evidence_removed_during_finalization": len(
                immediate_removed_evidence_ids
            ),
            "deferred_proof_evidence_retained_after_finalization": len(
                deferred_proof_ids & final_evidence_ids
            ),
            "immediate_proof_ids_in_candidate_support_list": immediate_proof_ids_in_candidates,
            "deferred_proof_evidence_removed_list": deferred_removed_evidence_ids,
            "immediate_proof_evidence_removed_list": immediate_removed_evidence_ids,
        },
        "removed_evidence_ids_with_proof": sum(
            evidence_id in proof_map for evidence_id in removed_evidence_ids
        ),
        "removed_evidence_ids_without_proof": sum(
            evidence_id not in proof_map for evidence_id in removed_evidence_ids
        ),
        "removed_evidence_ids_verified_to_one_canonical_winner": (
            len(removed_evidence_ids) - len(unverified_removed_evidence_ids)
        ),
        "unverified_removed_evidence_ids": unverified_removed_evidence_ids,
        "removed_evidence_details": removed_evidence_details,
        "evidence_linked_text_changes": evidence_text_changes,
        "evidence_linked_text_change_count": len(evidence_text_changes),
        "canonical_winner_changes": canonical_winner_changes,
        "canonical_winner_change_count": len(canonical_winner_changes),
    }


def _baseline_canonical_change_audit(
    baseline_cues: list[dict[str, Any]],
    current_cues: list[dict[str, Any]],
    proof_map: dict[str, str],
) -> dict[str, Any]:
    """Explain removed baseline range/text occurrences by retained evidence."""
    delta = _tuple_delta(baseline_cues, current_cues, ("range_us", "text"))
    current_evidence_ids = {
        evidence_id for cue in current_cues for evidence_id in cue["evidence_ids"]
    }
    current_owners: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cue in current_cues:
        for evidence_id in cue["evidence_ids"]:
            current_owners[evidence_id].append(cue)

    details = []
    unexplained = []
    for old in delta["removed"]:
        evidence_ids = set(old["evidence_ids"])
        retained_ids = evidence_ids & current_evidence_ids
        missing_ids = evidence_ids - current_evidence_ids
        unproven_missing_ids = missing_ids - proof_map.keys()
        winner_ids = {proof_map[key] for key in missing_ids if key in proof_map}
        owner_ids = retained_ids | winner_ids
        related_cues = {
            cue["id"]: cue
            for evidence_id in owner_ids
            for cue in current_owners.get(evidence_id, [])
        }
        same_text_survives = any(cue["text"] == old["text"] for cue in current_cues)
        changed_texts = sorted({cue["text"] for cue in related_cues.values()
                                if cue["text"] != old["text"]})
        accounted = not unproven_missing_ids and (
            same_text_survives or bool(related_cues)
        )
        if not accounted:
            unexplained.append(old["id"])
        details.append({
            "baseline_cue_id": old["id"],
            "range_us": old["range_us"],
            "text": old["text"],
            "supporting_evidence_id_count": len(evidence_ids),
            "evidence_ids_retained_directly": len(retained_ids),
            "missing_evidence_ids_with_proof": len(missing_ids) - len(unproven_missing_ids),
            "missing_evidence_ids_without_proof": sorted(unproven_missing_ids),
            "retained_related_cue_count": len(related_cues),
            "retained_related_cue_texts": sorted(
                {cue["text"] for cue in related_cues.values()}
            ),
            "changed_texts_with_related_evidence": changed_texts,
            "same_text_survives_at_any_range": same_text_survives,
            "change_accounted": accounted,
        })
    return {
        "status": "PASS" if not unexplained else "FAIL",
        "removed_range_text_occurrence_count": len(delta["removed"]),
        "added_range_text_occurrence_count": len(delta["added"]),
        "removed_occurrences_accounted_by_retained_or_proven_evidence": (
            len(details) - len(unexplained)
        ),
        "unexplained_removed_baseline_cue_ids": unexplained,
        "removed_occurrences": details,
        "added_occurrences": delta["added"],
    }


def _tuple_delta(
    before: list[dict[str, Any]], after: list[dict[str, Any]], fields: tuple[str, ...]
) -> dict[str, Any]:
    def key(row: dict[str, Any]) -> str:
        return json.dumps({field: row.get(field) for field in fields}, ensure_ascii=True,
                          sort_keys=True)

    before_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    after_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in before:
        before_groups[key(row)].append(row)
    for row in after:
        after_groups[key(row)].append(row)
    removed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    for signature in before_groups.keys() | after_groups.keys():
        old_rows = before_groups.get(signature, [])
        new_rows = after_groups.get(signature, [])
        if len(old_rows) > len(new_rows):
            removed.extend(old_rows[len(new_rows):])
        elif len(new_rows) > len(old_rows):
            added.extend(new_rows[len(old_rows):])
    return {"removed": removed, "added": added}


def _candidate_comparison(
    baseline_path: Path,
    evidence_sha256: str,
    current_candidates: list[dict[str, Any]],
    current_cues: list[dict[str, Any]],
    proofs: list[dict[str, Any]],
    raw_rows: dict[str, dict[str, Any]],
    retained_evidence_ids: set[str],
    reconciled_evidence: tuple[Any, ...],
) -> dict[str, Any]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    prior_candidate_report = json.loads(
        PRIOR_EQUAL_POLICY_REPORT.read_text(encoding="utf-8")
    )
    old_candidates = baseline["all_candidates"]
    old_cues = baseline["all_final_cues"]
    candidate_delta = _tuple_delta(
        old_candidates,
        current_candidates,
        ("range_us", "text", "confidence", "evidence_ids", "source", "review_reasons"),
    )
    cue_delta = _tuple_delta(
        old_cues,
        current_cues,
        ("range_us", "text", "confidence", "evidence_ids", "visual_box", "source",
         "role", "subtitle_track_id", "review_state"),
    )
    old_evidence_ids = {item for row in old_candidates for item in row["evidence_ids"]}
    new_evidence_ids = {item for row in current_candidates for item in row["evidence_ids"]}
    proof_by_id = {item["evidence_id"]: item for item in proofs}
    old_proof_ids = {
        item["evidence_id"] for item in baseline["replay"]["suppression_proofs"]
    }
    removed_evidence: list[dict[str, Any]] = []
    for evidence_id in sorted(old_evidence_ids - new_evidence_ids):
        raw = raw_rows.get(evidence_id, {})
        proof = proof_by_id.get(evidence_id)
        removed_evidence.append({
            "evidence_id": evidence_id,
            "raw_row_found": bool(raw),
            "text": raw.get("text", ""),
            "band": raw.get("band", ""),
            "confidence": raw.get("confidence"),
            "timestamp_us": raw.get("timestamp_us"),
            "frame_id": raw.get("frame_id", ""),
            "source_scope": _raw_scope(raw) if raw else "",
            "removed_by_reconciliation_proof": proof is not None,
            "retained_after_reconciliation": evidence_id in retained_evidence_ids,
            "winner_evidence_id": proof.get("winner_evidence_id") if proof else None,
            "proof_frame_id": proof.get("frame_id") if proof else None,
            "proof_timestamp_us": proof.get("timestamp_us") if proof else None,
        })
    newly_suppressed = [item for item in proofs if item["evidence_id"] not in old_proof_ids]
    new_texts = {row["text"] for row in current_cues}
    absent_cues = [row for row in old_cues if row["text"] not in new_texts]
    current_cue_by_evidence = {
        evidence_id: row
        for row in current_cues
        for evidence_id in row["evidence_ids"]
    }
    absent_cue_details: list[dict[str, Any]] = []
    for row in absent_cues:
        evidence_ids = set(row["evidence_ids"])
        proven_ids = evidence_ids.intersection(proof_by_id)
        unproven_ids = evidence_ids - proven_ids
        winner_ids = {proof_by_id[item]["winner_evidence_id"] for item in proven_ids}
        winner_cues = {
            id(current_cue_by_evidence[winner]): current_cue_by_evidence[winner]
            for winner in winner_ids if winner in current_cue_by_evidence
        }
        overlapping_winners = [
            winner for winner in winner_cues.values()
            if winner["range_us"][0] < row["range_us"][1]
            and winner["range_us"][1] > row["range_us"][0]
        ]
        absent_cue_details.append({
            "baseline_cue_id": row["id"],
            "baseline_cue_index": row["index"],
            "range_us": row["range_us"],
            "text": row["text"],
            "supporting_evidence_id_count": len(evidence_ids),
            "supporting_ids_with_same_frame_proof": len(proven_ids),
            "supporting_ids_without_same_frame_proof": sorted(unproven_ids),
            "unproven_support_ids_retained_after_reconciliation": len(
                unproven_ids.intersection(retained_evidence_ids)
            ),
            "unproven_support_ids_absent_after_reconciliation": sorted(
                unproven_ids - retained_evidence_ids
            ),
            "proof_winner_texts": sorted({winner["text"] for winner in winner_cues.values()}),
            "retained_overlapping_winner_cue_count": len(overlapping_winners),
        })
    range_text_delta = _tuple_delta(old_cues, current_cues, ("range_us", "text"))
    proof_staging_audit = _proof_staging_audit(
        baseline,
        baseline_path,
        prior_candidate_report,
        evidence_sha256,
        proofs,
        raw_rows,
        reconciled_evidence,
        current_candidates,
    )
    canonical_change_audit = _baseline_canonical_change_audit(
        old_cues,
        current_cues,
        {item["evidence_id"]: item["winner_evidence_id"] for item in proofs},
    )
    unproved_candidate_ids = [
        item for item in removed_evidence if not item["removed_by_reconciliation_proof"]
    ]
    return {
        "baseline_report": str(baseline_path.resolve()),
        "prior_equal_policy_report": str(PRIOR_EQUAL_POLICY_REPORT.resolve()),
        "candidate_tuple_delta": candidate_delta,
        "cue_tuple_delta": cue_delta,
        "candidate_counts": {
            "baseline": len(old_candidates),
            "candidate": len(current_candidates),
        },
        "proof_staging_audit": proof_staging_audit,
        "canonical_change_audit": canonical_change_audit,
        "final_cue_counts": {"baseline": len(old_cues), "candidate": len(current_cues)},
        "final_cue_tuple_accounting": {
            "exact_unchanged_full_tuples": len(old_cues) - len(cue_delta["removed"]),
            "removed_full_tuples": len(cue_delta["removed"]),
            "added_full_tuples": len(cue_delta["added"]),
            "same_range_and_text_occurrences": len(old_cues) - len(range_text_delta["removed"]),
            "removed_range_and_text_occurrences": len(range_text_delta["removed"]),
            "added_range_and_text_occurrences": len(range_text_delta["added"]),
            "net_cue_count_change": len(current_cues) - len(old_cues),
        },
        "evidence_id_sets": {
            "baseline_candidate_ids": len(old_evidence_ids),
            "candidate_candidate_ids": len(new_evidence_ids),
            "removed_from_candidate_output": len(old_evidence_ids - new_evidence_ids),
            "added_to_candidate_output": len(new_evidence_ids - old_evidence_ids),
            "removed_ids_with_explicit_proof": sum(
                item["removed_by_reconciliation_proof"] for item in removed_evidence
            ),
            "removed_ids_without_proof": sum(
                not item["removed_by_reconciliation_proof"] for item in removed_evidence
            ),
            "removed_ids_without_proof_retained_after_reconciliation": sum(
                item["retained_after_reconciliation"] for item in unproved_candidate_ids
            ),
            "removed_ids_without_proof_absent_after_reconciliation": sum(
                not item["retained_after_reconciliation"] for item in unproved_candidate_ids
            ),
            "removed_ids_without_raw_rows": sum(
                not item["raw_row_found"] for item in removed_evidence
            ),
        },
        "removed_candidate_evidence": removed_evidence,
        "new_suppression_proofs": newly_suppressed,
        "baseline_cues_with_text_absent_from_candidate": {
            "occurrence_count": len(absent_cues),
            "unique_text_count": len({row["text"] for row in absent_cues}),
            "occurrences_with_at_least_one_same_frame_proof": sum(
                item["supporting_ids_with_same_frame_proof"] > 0
                for item in absent_cue_details
            ),
            "occurrences_with_retained_overlapping_proof_winner": sum(
                item["retained_overlapping_winner_cue_count"] > 0
                for item in absent_cue_details
            ),
            "occurrences_with_unproven_support_ids": sum(
                bool(item["supporting_ids_without_same_frame_proof"])
                for item in absent_cue_details
            ),
            "unproven_support_id_count": sum(
                len(item["supporting_ids_without_same_frame_proof"])
                for item in absent_cue_details
            ),
            "unproven_support_ids_retained_after_reconciliation": sum(
                item["unproven_support_ids_retained_after_reconciliation"]
                for item in absent_cue_details
            ),
            "unproven_support_ids_absent_after_reconciliation": sum(
                len(item["unproven_support_ids_absent_after_reconciliation"])
                for item in absent_cue_details
            ),
            "details": absent_cue_details,
        },
        "limitations": [
            "The captured baseline report's manual_or_scene_cues field selected non-ocr "
            "provider source labels and is mislabeled; do not interpret it as manual or "
            "scene coverage.",
            "The source job has scene_text_enabled=false and zero project cues; no real "
            "scene/manual quality conclusion is drawn from the OCR-only replay.",
            "Short dialogue counts are zero in real replay; short/manual/scene preservation is "
            "covered only by the isolated synthetic boundary assertions.",
        ],
    }


def _read_review_inputs(
    database: Path, job_id: str
) -> tuple[dict[str, Any], Any, dict[str, dict[str, Any]]]:
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        job = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        revision = connection.execute(
            "SELECT * FROM project_revisions WHERE project_id=? AND revision=?",
            (job["project_id"], job["input_revision"]),
        ).fetchone()
        raw = json.loads(
            zlib.decompress(revision["snapshot_blob"])
            if revision["snapshot_encoding"] == "zlib-json-v1"
            else revision["snapshot_json"]
        )
        artifacts = {
            row["kind"]: dict(row)
            for row in connection.execute("SELECT * FROM artifacts WHERE job_id=?", (job_id,))
        }
    return job, decode_project_snapshot(raw), artifacts


def _cue_for_boundary(
    cue_id: str,
    index: int,
    start_us: int,
    end_us: int,
    text: str,
    evidence_id: str,
    *,
    source: str = "ocr_review",
    role: str = "dialogue",
    box: tuple[float, float, float, float] = (0.40, 0.85, 0.20, 0.08),
) -> SubtitleCue:
    return SubtitleCue(
        cue_id,
        index,
        TimeRange(start_us, end_us),
        text,
        "zh",
        source=source,
        confidence=0.99,
        evidence_ids=(evidence_id,) if evidence_id else (),
        visual_box=box,
        visual_box_source="ocr" if box else "",
        role=role,
        subtitle_track_id="S2" if role == "scene_text" else "S1",
    )


def _isolated_boundaries() -> dict[str, Any]:
    cues = (
        _cue_for_boundary("winner", 1, 10, 20, "箭毒蛙", "full-id"),
        _cue_for_boundary("proven-partial", 2, 10, 20, "前毒蛙", "partial-id"),
        _cue_for_boundary("unproven-full", 3, 30, 40, "箭毒蛙", "independent-full"),
        _cue_for_boundary("unproven-partial", 4, 30, 40, "前毒蛙", "independent-partial"),
        _cue_for_boundary("short-father", 5, 50, 60, "爹", "short-father-id"),
        _cue_for_boundary("short-mother", 6, 50, 60, "娘", "short-mother-id"),
        _cue_for_boundary("manual", 7, 10, 20, "手动字幕", "", source="manual"),
        _cue_for_boundary("scene", 8, 10, 20, "画面文字", "scene-id", role="scene_text"),
        _cue_for_boundary("repeat-a", 9, 70, 80, "重复文本", "repeat-a-id"),
        _cue_for_boundary("repeat-b", 10, 90, 100, "重复文本", "repeat-b-id"),
        _cue_for_boundary("separate-left", 11, 110, 120, "左边对白", "left-id",
                          box=(0.05, 0.85, 0.20, 0.08)),
        _cue_for_boundary("separate-right", 12, 110, 120, "右边对白", "right-id",
                          box=(0.75, 0.85, 0.20, 0.08)),
    )
    collapsed = collapse_partial_ocr_cues(
        cues,
        suppressed_evidence={"partial-id": "full-id"},
    )
    ids = {cue.id for cue in collapsed}
    expected_retained = {
        "winner", "unproven-full", "unproven-partial", "short-father", "short-mother",
        "manual", "scene", "repeat-a", "repeat-b", "separate-left", "separate-right",
    }
    if ids != expected_retained:
        raise AssertionError(f"isolated boundary cues changed unexpectedly: {sorted(ids)}")
    return {
        "status": "PASS",
        "scope": "synthetic boundary cues only; not real OCR or project coverage",
        "proven_equal_length_partial_removed": "proven-partial" not in ids,
        "unproven_equal_length_pair_preserved": {
            "unproven-full": "unproven-full" in ids,
            "unproven-partial": "unproven-partial" in ids,
        },
        "short_dialogue_preserved": {"爹": "short-father" in ids, "娘": "short-mother" in ids},
        "manual_cue_preserved": "manual" in ids,
        "scene_cue_preserved": "scene" in ids,
        "repeated_text_at_distinct_times_preserved": {
            "repeat-a": "repeat-a" in ids,
            "repeat-b": "repeat-b" in ids,
        },
        "separate_simultaneous_lines_preserved": {
            "separate-left": "separate-left" in ids,
            "separate-right": "separate-right" in ids,
        },
        "retained_cue_count": len(collapsed),
    }


def _review_import_replay(
    database: Path,
    job_id: str,
    output: Path,
    proofs: list[dict[str, Any]],
    proof_map: dict[str, str],
    deferred_reading_ids: set[str],
    retained_deferred_reading_ids: set[str],
    finalized_cues: tuple[Any, ...],
    finalization_blockers: list[str],
    finalization_warnings: list[str],
) -> dict[str, Any]:
    job, original, artifacts = _read_review_inputs(database, job_id)
    review_artifact = artifacts["ocr_review_srt"]
    qa_artifact = artifacts["ocr_qa_json"]
    review_path = Path(review_artifact["uri"]).resolve()
    qa_path = Path(qa_artifact["uri"]).resolve()
    review_hash = hashlib.sha256(review_path.read_bytes()).hexdigest()
    original_qa_bytes = qa_path.read_bytes()
    if review_hash != review_artifact["sha256"]:
        raise RuntimeError("review SRT does not match immutable artifact record")
    if hashlib.sha256(original_qa_bytes).hexdigest() != qa_artifact["sha256"]:
        raise RuntimeError("review QA does not match immutable artifact record")
    qa = json.loads(original_qa_bytes)
    isolated = output / "review-import"
    isolated.mkdir(parents=True, exist_ok=True)
    candidate_review_path = isolated / "candidate-review.srt"
    candidate_review_path.write_text(
        "\ufeff" + render_srt(finalized_cues), encoding="utf-8"
    )
    candidate_review_bytes = candidate_review_path.read_bytes()
    candidate_review_hash = hashlib.sha256(candidate_review_bytes).hexdigest()
    candidate_review_artifact = {
        **review_artifact,
        "uri": str(candidate_review_path),
        "sha256": candidate_review_hash,
        "size_bytes": len(candidate_review_bytes),
    }
    candidate_qa = {
        "schema_version": 3,
        "ocr_aggregation_version": OCR_AGGREGATION_VERSION,
        "kind": "ocr_hard_sub_coverage",
        "status": "REVIEW",
        "job_id": job_id,
        "input_fingerprint": job["input_fingerprint"],
        "settings_fingerprint": job["settings_fingerprint"],
        "cue_count": len(finalized_cues),
        "suppressed_readings": proofs,
        "proof_only_collapse": True,
        "deferred_reading_ids": sorted(deferred_reading_ids),
        "retained_deferred_reading_ids": sorted(
            deferred_reading_ids & retained_deferred_reading_ids
        ),
        "blockers": list(finalization_blockers),
        "warnings": list(finalization_warnings),
        "cue_regions": [
            {
                "id": cue.id,
                "index": cue.index,
                "start_us": cue.range.start_us,
                "end_us": cue.range.end_us,
                "box": list(cue.visual_box) if cue.visual_box is not None else None,
                "source": cue.visual_box_source or "estimated",
                "role": cue.role,
                "orientation": cue.orientation,
                "style_hint": cue.style_hint,
                "text_font_size": cue.text_font_size,
                "panel_box": list(cue.panel_box) if cue.panel_box is not None else None,
                "background_color": cue.background_color,
                "review_state": cue.review_state,
                "confidence": cue.confidence,
                "evidence_ids": list(cue.evidence_ids),
            }
            for cue in finalized_cues
        ],
        "output_path": str(candidate_review_path),
        "output_sha256": candidate_review_hash,
        "output_size": len(candidate_review_bytes),
        "output_mtime_ns": candidate_review_path.stat().st_mtime_ns,
        "replay_source_review_sha256": review_hash,
        "replay_source_qa_sha256": hashlib.sha256(original_qa_bytes).hexdigest(),
    }
    candidate_qa_path = isolated / "candidate-review-qa.json"
    candidate_qa_path.write_text(
        json.dumps(candidate_qa, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    candidate_qa_bytes = candidate_qa_path.read_bytes()
    candidate_qa_artifact = {
        **qa_artifact,
        "uri": str(candidate_qa_path),
        "sha256": hashlib.sha256(candidate_qa_bytes).hexdigest(),
        "size_bytes": len(candidate_qa_bytes),
    }
    service = JobManagerService(
        database_path=isolated / "state.db",
        artifact_root=isolated / "artifacts",
    )
    service.projects.save(project_path=isolated / "verification.ducai", snapshot=original)
    service.jobs.create(
        JobSpec(
            JobKind.OCR,
            original.id,
            original.revision,
            job["provider_id"],
            job["provider_version"],
            job["model_id"],
            job["input_fingerprint"],
            job["settings_fingerprint"],
            json.loads(job["config_json"]),
        ),
        job_id=job_id,
    )
    imported = service.import_review_ocr_srt(
        job_id,
        {"payload": {"stage": "review"}, "artifact": candidate_review_artifact},
        {
            "payload": {"stage": "review"},
            "artifact": candidate_qa_artifact,
        },
    )
    reopened = service.projects.load(original.id)
    if encode_project_snapshot(reopened)[0] != json.dumps(
        imported, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ):
        raise AssertionError("isolated review import does not survive save/reopen")

    parsed = service.media_imports.import_srt(
        original, str(candidate_review_path), language="zh"
    )
    with_regions = _apply_ocr_cue_regions(parsed, candidate_qa)
    canonicalized = service._canonicalize_ocr_result(
        with_regions,
        json.loads(job["config_json"]),
        watermarks_in_timeline=isinstance(
            candidate_qa.get("watermark_regions"), list
        ),
    )
    review_cues = tuple(
        replace(cue, source="ocr_review") for cue in canonicalized.source_cues
    )
    expected_clean = collapse_proven_ocr_cues(
        review_cues,
        suppressed_evidence=proof_map,
    )
    expected_snapshot = replace(canonicalized, source_cues=expected_clean)
    expected_snapshot = _merge_preserved_manual_subtitle_cues(original, expected_snapshot)
    expected_clean = expected_snapshot.source_cues
    expected_target = [
        cue for cue in expected_clean
        if cue.text in TARGET_READINGS and cue.range.start_us < TARGET_END_US
        and cue.range.end_us > TARGET_START_US and cue.is_dialogue
    ]
    projected = project_for_timeline(reopened)
    actual_target = [
        cue for cue in projected.source_cues
        if cue.text in TARGET_READINGS and cue.range.start_us < TARGET_END_US
        and cue.range.end_us > TARGET_START_US and cue.is_dialogue
    ]
    expected_rows = [_cue_tuple(cue) for cue in expected_clean]
    actual_rows = [_cue_tuple(cue) for cue in projected.source_cues]
    full_import_delta = _tuple_delta(
        expected_rows,
        actual_rows,
        ("index", "range_us", "text", "confidence", "evidence_ids",
         "visual_box", "source", "role", "subtitle_track_id", "review_state"),
    )
    if full_import_delta["removed"] or full_import_delta["added"]:
        raise AssertionError(
            f"isolated review import differs from the proof-guided expected source cues: "
            f"removed={full_import_delta['removed'][:5]}; added={full_import_delta['added'][:5]}"
        )
    if len(expected_target) != 1 or len(actual_target) != 1 or actual_target[0].text != "箭毒蛙":
        raise AssertionError(
            f"isolated review import did not retain only the complete target: "
            f"expected={expected_target}; actual={actual_target}"
        )
    before_ids = {item for cue in with_regions.source_cues for item in cue.evidence_ids}
    after_ids = {item for cue in expected_clean for item in cue.evidence_ids}
    removed_ids = before_ids - after_ids
    unproven_removed = sorted(removed_ids - proof_map.keys())
    removed_winner_ownership = []
    missing_winners = []
    ambiguous_winners = []
    for evidence_id in sorted(removed_ids):
        winner_id = proof_map.get(evidence_id)
        winner_owners = [
            cue for cue in expected_clean if winner_id in cue.evidence_ids
        ] if winner_id else []
        if not winner_id or not winner_owners:
            missing_winners.append({
                "evidence_id": evidence_id,
                "winner_evidence_id": winner_id,
            })
        if len(winner_owners) != 1:
            ambiguous_winners.append({
                "evidence_id": evidence_id,
                "winner_evidence_id": winner_id,
                "retained_winner_cue_count": len(winner_owners),
            })
        removed_winner_ownership.append({
            "evidence_id": evidence_id,
            "winner_evidence_id": winner_id,
            "retained_winner_cue_count": len(winner_owners),
            "retained_winner_texts": sorted({cue.text for cue in winner_owners}),
        })
    if unproven_removed or missing_winners or ambiguous_winners:
        raise AssertionError(
            f"review import removed evidence without a retained proof winner: "
            f"unproven={unproven_removed[:10]}; missing_winners={missing_winners[:10]}; "
            f"ambiguous_winners={ambiguous_winners[:10]}"
        )
    original_manual = [
        cue for cue in original.source_cues if cue.source in MANUAL_CUE_SOURCES
    ]
    imported_manual = [
        cue for cue in projected.source_cues if cue.source in MANUAL_CUE_SOURCES
    ]
    if len(original_manual) != len(imported_manual):
        raise AssertionError("isolated review import changed the manual cue count")
    return {
        "status": "PASS",
        "isolated_database": str(isolated / "state.db"),
        "live_database_read_only": True,
        "candidate_qa_copy": str(candidate_qa_path),
        "candidate_review_srt": str(candidate_review_path),
        "candidate_review_srt_sha256": candidate_review_hash,
        "candidate_review_qa_sha256": candidate_qa_artifact["sha256"],
        "fresh_v5_review_artifacts_used_for_import": True,
        "source_review_srt_used_for_import": False,
        "source_review_srt_sha256_verified": review_hash,
        "source_qa_sha256_verified": hashlib.sha256(original_qa_bytes).hexdigest(),
        "original_job_qa_proofs": len(qa.get("suppressed_readings", [])),
        "candidate_proofs_used": len(proofs),
        "candidate_final_cue_count": len(finalized_cues),
        "proof_only_collapse": candidate_qa["proof_only_collapse"],
        "deferred_reading_ids": len(candidate_qa["deferred_reading_ids"]),
        "retained_deferred_reading_ids": len(
            candidate_qa["retained_deferred_reading_ids"]
        ),
        "qa_regions_applied_before_collapse": True,
        "before_import_cue_count": len(with_regions.source_cues),
        "after_import_cue_count": len(projected.source_cues),
        "target_before": [
            {"text": cue.text, "range_us": [cue.range.start_us, cue.range.end_us]}
            for cue in with_regions.source_cues
            if cue.text in TARGET_READINGS and cue.range.start_us < TARGET_END_US
            and cue.range.end_us > TARGET_START_US
        ],
        "target_after": [
            {"text": cue.text, "range_us": [cue.range.start_us, cue.range.end_us]}
            for cue in actual_target
        ],
        "removed_evidence_id_count": len(removed_ids),
        "removed_evidence_ids_with_same_frame_proof": len(removed_ids),
        "unproven_removed_evidence_ids": unproven_removed,
        "removed_ids_without_retained_winner": missing_winners,
        "removed_ids_with_ambiguous_retained_winner": ambiguous_winners,
        "removed_evidence_winner_ownership": removed_winner_ownership,
        "original_manual_cue_count": len(original_manual),
        "imported_manual_cue_count": len(imported_manual),
        "full_source_cue_tuple_parity": True,
        "cue_ids_excluded_from_parity": "SRT importer generates fresh IDs on each isolated parse",
        "expected_source_cue_count": len(expected_rows),
        "reopened_source_cue_count": len(actual_rows),
        "scene_text_evaluation": {
            "enabled_in_source_job": bool(
                json.loads(job["config_json"]).get("scene_text_enabled", True)
            ),
            "reopened_scene_cue_count": sum(
                cue.is_scene_text for cue in projected.source_cues
            ),
            "quality_coverage": (
                "not evaluated because scene_text_enabled is false"
                if not json.loads(job["config_json"]).get("scene_text_enabled", True)
                else "cue count observed; no separate scene-quality metric in this replay"
            ),
        },
        "save_reopen_parity": True,
    }


def main() -> None:
    if len(sys.argv) not in {6, 7}:
        raise SystemExit(
            "usage: ocr-clipped-duplicate-replay.py DATABASE JOB_ID "
            "ISOLATED_OUTPUT REPORT_PATH baseline|candidate [BASELINE_REPORT]"
        )
    database, job_id, output_arg, report_arg, expected = sys.argv[1:6]
    baseline_report = Path(sys.argv[6]).resolve() if len(sys.argv) == 7 else None
    if expected not in {"baseline", "candidate"}:
        raise SystemExit("expected must be baseline or candidate")
    if expected == "candidate" and baseline_report is None:
        raise SystemExit("candidate replay requires the captured baseline report")
    database_path = Path(database).resolve()
    output = Path(output_arg).resolve()
    report_path = Path(report_arg).resolve()
    _check_output_paths(output, report_path)
    if output.exists() and not output.is_dir():
        raise ValueError("isolated output path is not a directory")
    output.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    job, artifact = _read_job(database_path, job_id)
    if job["state"] == "running":
        raise RuntimeError("refusing to replay a running job")
    evidence_path = Path(artifact["uri"]).resolve()
    raw_bytes = evidence_path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest != artifact["sha256"] or len(raw_bytes) != artifact["size_bytes"]:
        raise RuntimeError("immutable evidence artifact does not match the DB record")
    raw_rows = _read_raw_rows(evidence_path)

    os.environ["DUCAI_ARTIFACT_STAGING_ROOT"] = str(output / "staging")
    worker = OcrWorker(job_id)
    worker.config = job["config"]
    worker._configure_scan_plan()
    worker.evidence_path = evidence_path

    configured_interval = worker._integer_config("sample_interval_us", 800_000, 400_000)
    fast_interval, quality_interval = _sampling_intervals(configured_interval)
    fast_candidates, rescue_windows = worker._fast_candidates(
        worker._scan_plan_duration_us or worker._integer_config("duration_us", 0, 1),
        fast_interval,
    )
    configured_windows = worker._configured_scan_windows(
        worker._scan_plan_duration_us or worker._integer_config("duration_us", 0, 1)
    )
    if configured_windows:
        rescue_windows = configured_windows
    duration_us = worker._scan_plan_duration_us or worker._integer_config("duration_us", 0, 1)
    candidates = worker._final_candidates(
        fast_candidates,
        rescue_windows,
        duration_us,
        quality_interval,
    )
    evidence = worker._load_evidence()
    proofs = [asdict(item) for item in getattr(worker, "_suppressed_readings", ())]
    proof_map = {item["evidence_id"]: item["winner_evidence_id"] for item in proofs}
    deferred_proof_ids = {
        item.evidence_id for item in getattr(worker, "_deferred_readings", ())
    }
    finalized = finalize_ocr_candidates(
        candidates,
        video_duration_us=duration_us,
        evidence=evidence,
        suppressed_evidence=proof_map if expected == "candidate" else None,
    )
    finalization_proof_audit = (
        _finalization_proof_collapse_audit(
            candidates, finalized.cues, proof_map, deferred_proof_ids
        )
        if expected == "candidate"
        else {"status": "not_run", "reason": "baseline has no candidate proof map"}
    )
    scene_cues = worker._scene_cues(
        evidence,
        duration_us=duration_us,
        first_index=len(finalized.cues) + 1,
        dialogue_cues=finalized.cues,
    )
    cues = (*finalized.cues, *scene_cues)

    proof_audit = _same_frame_proof_audit(proofs, raw_rows, worker.config)
    if expected == "candidate" and not proof_audit["all_proof_conditions_pass"]:
        raise AssertionError(f"same-frame proof audit failed: {proof_audit}")
    target = _target_cues(cues)
    target_candidate_rows = _target_candidates(candidates)
    target_candidate_ids = {item["id"] for item in target_candidate_rows}
    target_overlap_blockers = [
        blocker
        for blocker in finalized.blockers
        if blocker.startswith("cue_overlap:")
        and any(candidate_id in blocker for candidate_id in target_candidate_ids)
    ]
    target_dialogue_readings = [item for item in target if item["role"] == "dialogue"]
    if expected == "baseline" and len(target_dialogue_readings) < 2:
        raise AssertionError(f"baseline did not reproduce both target readings: {target}")
    if expected == "candidate" and (
        len(target_dialogue_readings) != 1 or target_overlap_blockers
    ):
        raise AssertionError(
            f"candidate must leave exactly one target dialogue cue and no overlap: "
            f"target={target}; blockers={target_overlap_blockers}"
        )

    candidate_rows = [_candidate_tuple(item) for item in candidates]
    cue_rows = [_cue_tuple(item) for item in cues]
    comparison = None
    review_import = None
    boundaries = None
    if expected == "candidate":
        assert baseline_report is not None
        comparison = _candidate_comparison(
            baseline_report,
            digest,
            candidate_rows,
            cue_rows,
            proofs,
            raw_rows,
            {item.id for item in evidence},
            evidence,
        )
        if not finalized.blockers:
            raise AssertionError("review artifact requires at least one finalization blocker")
        review_import = _review_import_replay(
            database_path,
            job_id,
            output,
            proofs,
            proof_map,
            deferred_proof_ids,
            {
                evidence_id
                for cue in finalized.cues
                for evidence_id in cue.evidence_ids
            },
            finalized.cues,
            list(finalized.blockers),
            list(finalized.warnings),
        )
        boundaries = _isolated_boundaries()

    proof_audit_results = {
        "same_frame_proofs": (
            "PASS" if proof_audit["all_proof_conditions_pass"] else "FAIL"
        ),
        "proof_staging": (
            comparison["proof_staging_audit"]["status"] if comparison else "not_run"
        ),
        "baseline_canonical_changes": (
            comparison["canonical_change_audit"]["status"] if comparison else "not_run"
        ),
        "finalization_winner_ownership": finalization_proof_audit["status"],
        "fresh_review_import": review_import["status"] if review_import else "not_run",
        "isolated_boundaries": boundaries["status"] if boundaries else "not_run",
    }
    failed_proof_audits = sorted(
        name for name, status in proof_audit_results.items()
        if status not in {"PASS", "not_run"}
    )
    all_proof_audits_pass = not failed_proof_audits
    report_status = (
        "FAIL" if not all_proof_audits_pass
        else "REVIEW" if finalized.blockers
        else "PASS"
    )

    report = {
        "status": report_status,
        "proof_audits_status": "PASS" if all_proof_audits_pass else "FAIL",
        "proof_audit_results": proof_audit_results,
        "failed_proof_audits": failed_proof_audits,
        "release_outcome": "REVIEW" if finalized.blockers else "PASS",
        "mode": expected,
        "job": {
            "id": job["id"],
            "state": job["state"],
            "project_id": job["project_id"],
            "input_revision": job["input_revision"],
        },
        "database_read_only": True,
        "database_connection": "sqlite mode=ro; PRAGMA query_only=1",
        "evidence": {
            "path": str(evidence_path),
            "sha256": digest,
            "db_sha256": artifact["sha256"],
            "bytes": len(raw_bytes),
            "db_bytes": artifact["size_bytes"],
            "raw_ndjson_rows": raw_bytes.count(b"\n"),
            "record_state": artifact["state"],
        },
        "replay": {
            "inference": False,
            "gpu_or_api": False,
            "grouping_rescue_finalization": True,
            "scene_text_enabled": bool(worker.config.get("scene_text_enabled", True)),
            "real_scene_cue_coverage": bool(scene_cues),
            "raw_evidence_rows_loaded_after_reconciliation": len(evidence),
            "raw_evidence_artifact_rows": len(raw_rows),
            "suppression_proofs": proofs,
            "suppressed_evidence_to_winner": proof_map,
            "same_frame_proof_audit": proof_audit,
            "fast_candidate_count": len(fast_candidates),
            "rescue_window_count": len(rescue_windows),
            "candidate_count_before_finalization": len(candidates),
            "final_dialogue_candidate_count": len(finalized.cues),
            "final_scene_cue_count": len(scene_cues),
            "finalization_proof_collapse_audit": finalization_proof_audit,
            "finalization_status": str(finalized.status),
            "finalization_blockers": list(finalized.blockers),
            "finalization_warnings": list(finalized.warnings),
        },
        "target_interval_us": [TARGET_START_US, TARGET_END_US],
        "target_candidates_before_finalization": target_candidate_rows,
        "target_cues": target,
        "target_dialogue_count": len(target_dialogue_readings),
        "target_overlap_blockers": target_overlap_blockers,
        "all_candidates": candidate_rows,
        "all_final_cues": cue_rows,
        "baseline_comparison": comparison,
        "review_import": review_import,
        "isolated_boundary_assertions": boundaries,
        "rescue_windows": [
            {"range_us": [item.range.start_us, item.range.end_us], "reasons": list(item.reasons)}
            for item in rescue_windows
        ],
        "preservation_probes": {
            "short_dialogue_counts": {
                text: sum(cue.text == text for cue in cues) for text in ("爹", "娘")
            },
            "manual_cue_coverage": (
                "not represented in the OCR-only evidence replay; see isolated review-import audit"
            ),
            "scene_text_evaluation": {
                "enabled_in_source_job": bool(
                    job["config"].get("scene_text_enabled", True)
                ),
                "generated_cue_count": len(scene_cues),
                "quality_coverage": (
                    "not evaluated because scene_text_enabled is false"
                    if not job["config"].get("scene_text_enabled", True)
                    else "cue count observed; no separate scene-quality metric in this replay"
                ),
            },
            "synthetic_manual_and_scene_boundaries": (
                "reported separately in isolated_boundary_assertions; not real evidence coverage"
            ),
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "mode": expected,
        "artifact_sha256": digest,
        "raw_ndjson_rows": report["evidence"]["raw_ndjson_rows"],
        "final_cues": len(cues),
        "target_dialogue_count": len(target_dialogue_readings),
        "scene_text_enabled": report["replay"]["scene_text_enabled"],
        "proof_audit_pass": proof_audit["all_proof_conditions_pass"],
        "candidate_diff": {
            "removed_tuples": len(comparison["cue_tuple_delta"]["removed"])
            if comparison else None,
            "added_tuples": len(comparison["cue_tuple_delta"]["added"])
            if comparison else None,
            "removed_evidence_without_proof": comparison["evidence_id_sets"][
                "removed_ids_without_proof"
            ] if comparison else None,
        },
        "review_import_status": review_import["status"] if review_import else "not_run",
        "report": str(report_path),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
