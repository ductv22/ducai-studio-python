"""Source-time casting contracts. Text and visual formatting are not evidence."""

from __future__ import annotations

from ducai_studio.domain.dialogue_casting import DialogueCastingState
from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.domain.project import ProjectSnapshot
from ducai_studio.domain.timeline_edit import build_scan_plan


def casting_inputs(snapshot: ProjectSnapshot, media_identities: dict[str, str]) -> list[dict]:
    spans = build_scan_plan(snapshot)["segments"]
    rows = []
    dialogue = [cue for cue in snapshot.source_cues if cue.is_dialogue]
    for cue in dialogue:
        parts = []
        for span in spans:
            a = max(cue.range.start_us, span["base_start_us"])
            b = min(cue.range.end_us, span["base_end_us"])
            if b <= a:
                continue
            def source_time(value: int, span: dict = span) -> int:
                return span["source_start_us"] + (
                    (value - span["base_start_us"])
                    * (span["source_end_us"] - span["source_start_us"])
                    // (span["base_end_us"] - span["base_start_us"])
                )
            item = {
                "media_id": span["media_id"], "media_path": span["media_path"],
                "start_us": source_time(a), "end_us": source_time(b),
                "source_identity": media_identities[span["media_id"]],
            }
            if parts and parts[-1]["media_id"] == item["media_id"] and (
                parts[-1]["end_us"] == item["start_us"]
            ):
                parts[-1]["end_us"] = item["end_us"]
            else:
                parts.append(item)
        # A completely cut-away cue is also absent from the TTS projection.
        if not parts:
            continue
        overlaps = sorted(other.id for other in dialogue if other.id != cue.id
                          and other.range.start_us < cue.range.end_us
                          and other.range.end_us > cue.range.start_us)
        key = stable_fingerprint({
            "schema": "ducai-casting-audio-v1", "parts": parts,
            "range": [cue.range.start_us, cue.range.end_us], "overlaps": overlaps,
        })
        rows.append({
            "id": cue.id, "index": cue.index, "text": cue.text,
            "start_us": cue.range.start_us, "end_us": cue.range.end_us,
            "parts": parts, "audio_key": key, "srt_overlap_ids": overlaps,
        })
    return rows


def effective_labels(rows: list[dict], state: DialogueCastingState | None,
                     analyzer_id: str) -> dict[str, dict]:
    results = {row.cue_id: row for row in state.results} if state else {}
    overrides = {row.cue_id: row for row in state.overrides} if state else {}
    resolved = {}
    for row in rows:
        result, override = results.get(row["id"]), overrides.get(row["id"])
        valid = bool(state and state.analyzer_id == analyzer_id and result
                     and result.audio_key == row["audio_key"])
        manual = bool(override and override.audio_key == row["audio_key"])
        label = override.label if manual else result.label if valid else "unknown"
        resolved[row["id"]] = {
            "label": label,
            "manual": manual,
            "ready": valid or (manual and override.label in {"male", "female"}),
            "reason": "manual" if manual else result.reason if valid else "needs_analysis",
            "confidence": result.confidence if valid else 0.0,
            "machine_label": result.label if valid else "unknown",
            "machine_reason": result.reason if valid else "needs_analysis",
            "tts_suppressed": bool(valid and result.reason == "no_speech" and label == "unknown"),
        }
    return resolved
