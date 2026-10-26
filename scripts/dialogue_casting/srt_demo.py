"""SRT-bounded offline experiment; separate from production and blinded labels."""

from __future__ import annotations

import shutil
import time
import wave
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from .analysis import Settings, classify_cue, clear_ranges, interval, merged_ranges
from .assets import checked_path, file_sha256, json_digest, read_json, write_json
from .corpus import srt_cues, validate_corpus
from .demo import validate_asr, write_demo_page
from .inference import SAMPLE_RATE, Recognizer, implementation_digest

EVALUATION_NOTICE = (
    "Original 140-cue benchmark not scored. Exploratory excerpts overlap reserved audio; "
    "exposed intervals must not be treated as unseen independent evaluation data."
)


def reserved_audio_exposure(corpus: dict, excerpts: list[dict]) -> list[dict]:
    return [
        {"cue_id": cue["id"], "film_id": cue["film_id"], "source_cue_id": cue["source_cue_id"]}
        for cue in corpus["cues"]
        if cue["split"] == "test"
        and any(
            e["film_id"] == cue["film_id"]
            and e["start_us"] < cue["end_us"]
            and e["end_us"] > cue["start_us"]
            for e in excerpts
        )
    ]


def require_identity(document: dict, key: str) -> None:
    if document.get(key) != json_digest({k: v for k, v in document.items() if k != key}):
        raise ValueError(f"Changed experiment document: {key}")


def validate_review(document: dict, public: dict) -> dict:
    """The same identity/value contract as the offline page's JS importer."""
    if (
        not isinstance(document, dict)
        or document.get("schema") != "ducai-dialogue-casting-demo-review-v1"
        or document.get("label_source") != "demo-assisted-review"
        or document.get("demo_id") != public["demo_id"]
        or document.get("experiment_id") != public["experiment_id"]
        or not isinstance(document.get("reviews"), list)
    ):
        raise ValueError("Review does not belong to the supplied demo/experiment")
    known = {c["id"]: c for d in public["datasets"] for c in d["cues"]}
    result = {}
    for row in document["reviews"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid review row")
        cue_id = row.get("cue_id")
        if not isinstance(cue_id, str) or cue_id not in known or cue_id in result:
            raise ValueError("Unknown/duplicate review cue")
        cue = known[cue_id]
        if (
            row.get("clip_sha256") != cue["clip_sha256"]
            or row.get("machine_label") != cue["machine"]["label"]
            or "override" not in row
            or row["override"] not in (None, "male", "female", "unknown")
            or type(row.get("reviewed")) is not bool
            or not isinstance(row.get("notes"), str)
            or len(row["notes"]) > 20_000
        ):
            raise ValueError("Changed audio/machine identity or invalid review value")
        result[cue_id] = {key: row[key] for key in ("override", "reviewed", "notes")}
    return result


def select_complete_cues(cues: list[dict], excerpt: dict) -> tuple[list[dict], list[dict]]:
    start, end = interval(excerpt)
    selected, excluded = [], []
    dialogue = [cue for cue in cues if cue.get("role", "dialogue") == "dialogue"]
    intersecting = [c for c in dialogue if c["start_us"] < end and c["end_us"] > start]
    for cue in intersecting:
        cue_start, cue_end = interval(cue)
        if cue_start < start or cue_end > end:
            excluded.append({**cue, "reason": "incomplete_at_excerpt_edge"})
            continue
        if cue_end - cue_start > 20_000_000:
            raise ValueError(f"SRT cue exceeds the lab's 20-second limit: {cue['id']}")
        selected.append(
            {
                **cue,
                "srt_overlap_ids": [
                    other["id"]
                    for other in intersecting
                    if other["id"] != cue["id"]
                    and other["start_us"] < cue_end
                    and other["end_us"] > cue_start
                ],
            }
        )
    return selected, excluded


def write_exact_clip(source: Path, target: Path, start_us: int, end_us: int) -> None:
    with wave.open(str(source), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError("SRT demo requires original mono PCM16 16 kHz excerpts")
        first, last = (t * SAMPLE_RATE // 1_000_000 for t in (start_us, end_us))
        if not 0 <= first < last <= wav.getnframes():
            raise ValueError("Complete SRT interval is not available in excerpt audio")
        wav.setpos(first)
        frames = wav.readframes(last - first)
        params = wav.getparams()
    with wave.open(str(target), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(frames)


def prepare_srt_demo(
    previous_root: Path, manifest_path: Path, corpus_path: Path, review_path: Path, output: Path
) -> dict:
    previous = read_json(previous_root / "demo-data.json")
    original = read_json(previous_root / "experiment.json")
    require_identity(previous, "demo_id")
    require_identity(original, "experiment_id")
    if previous["experiment_id"] != original["experiment_id"]:
        raise ValueError("Previous page and experiment differ")
    reviews = validate_review(read_json(review_path), previous)
    corpus = read_json(corpus_path)
    validate_corpus(corpus)
    if original["corpus_id"] != corpus["corpus_id"]:
        raise ValueError("Original SRT corpus differs from the previous experiment")
    settings = Settings(**original["settings"])
    manifest = read_json(manifest_path)
    films = {film["id"]: film for film in corpus["films"]}
    entries = {entry["id"]: entry for entry in manifest["films"]}
    if len(entries) != len(manifest["films"]) or set(entries) != set(films):
        raise ValueError("Source manifest film identities differ from the corpus")
    spec = {
        "schema": "ducai-casting-srt-experiment-v1",
        "previous_demo_id": previous["demo_id"],
        "previous_experiment_id": original["experiment_id"],
        "previous_data_sha256": file_sha256(previous_root / "demo-data.json"),
        "review_sha256": file_sha256(review_path),
        "corpus_id": corpus["corpus_id"],
        "settings": asdict(settings),
        "casting_model_lock_sha256": original["casting_model_lock_sha256"],
        "casting_implementation_sha256": original["casting_implementation_sha256"],
        "films": original["films"],
        "excerpts": original["excerpts"],
        "sources": [],
        "audio": [],
        "srt_cues": [],
        "excluded": [],
        "held_out": EVALUATION_NOTICE,
        "reserved_audio_exposure": reserved_audio_exposure(corpus, original["excerpts"]),
        "alignment": "Original SRT intervals; no Whisper-based boundary changes",
    }
    for excerpt in spec["excerpts"]:
        film_id = excerpt["film_id"]
        entry, film = entries[film_id], films[film_id]
        if entry.get("timebase") != "original_audio":
            raise ValueError("SRT times must explicitly use original audio")
        media = (manifest_path.parent / entry["media_path"]).resolve(strict=True)
        if media != Path(film["media_path"]).resolve() or (
            excerpt["source_sha256"] != film["source_sha256"]
        ):
            raise ValueError("SRT source does not match the original audio excerpt")
        path = (manifest_path.parent / entry["srt_path"]).resolve(strict=True)
        cues = srt_cues(path, include_text=True)
        by_id = {cue["id"]: cue for cue in cues}
        for sampled in corpus["cues"]:
            if sampled["film_id"] != film_id:
                continue
            source = by_id.get(sampled["source_cue_id"])
            if source is None or interval(source) != interval(sampled):
                raise ValueError("SRT timing no longer matches the source listening corpus")
        selected, excluded = select_complete_cues(cues, excerpt)
        spec["sources"].append(
            {
                "film_id": film_id,
                "srt_path": str(path),
                "srt_sha256": file_sha256(path),
                "source_sha256": film["source_sha256"],
                "timebase": "original_audio",
                "sampled_cues_timing_verified": sum(
                    c["film_id"] == film_id for c in corpus["cues"]
                ),
            }
        )
        spec["excluded"].extend({**c, "film_id": film_id} for c in excluded)
        spec["srt_cues"].extend(
            {
                **cue,
                "id": f"srt-{film_id}-{cue['id']}",
                "source_cue_id": cue["id"],
                "film_id": film_id,
                "excerpt_id": excerpt["id"],
            }
            for cue in selected
        )
    if not spec["srt_cues"]:
        raise ValueError("No complete SRT cues available in the existing excerpts")
    # Validate old audio before carrying any progress. No changed-boundary override migration.
    clips = {c["clip_path"]: c["clip_sha256"] for d in previous["datasets"] for c in d["cues"]}
    clips.update({e["path"]: e["sha256"] for e in original["excerpts"]})
    for relative, digest in clips.items():
        if file_sha256(checked_path(previous_root, relative)) != digest:
            raise ValueError("Previous demo audio changed")
    output.mkdir(parents=True, exist_ok=False)
    (output / "clips").mkdir()
    for relative in clips:
        target = checked_path(output, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checked_path(previous_root, relative), target)
    shutil.copyfile(previous_root / "demo-data.json", output / "previous-demo-data.json")
    shutil.copyfile(review_path, output / "review-received.json")
    intake = {
        "schema": "ducai-demo-review-intake-v1",
        "review_sha256": spec["review_sha256"],
        "demo_id": previous["demo_id"],
        "experiment_id": original["experiment_id"],
        "saved_records": len(reviews),
        "reviewed_records": sum(r["reviewed"] for r in reviews.values()),
        "overrides": dict(Counter(r["override"] or "automatic" for r in reviews.values())),
        "machine_labels": dict(
            Counter(r["machine_label"] for r in read_json(review_path)["reviews"])
        ),
        "validation": "PASS: schema, identities, unique cues, clip hashes and machine labels",
        "label_source": "demo-assisted-review; not independent listening truth",
        "unrecorded_cues": "Listening/review status unknown; absence is not acceptance",
    }
    write_json(output / "review-intake.json", intake)
    for cue in spec["srt_cues"]:
        excerpt = next(e for e in spec["excerpts"] if e["id"] == cue["excerpt_id"])
        relative = f"clips/{cue['id']}.wav"
        write_exact_clip(
            checked_path(output, excerpt["path"]),
            output / relative,
            cue["start_us"] - excerpt["start_us"],
            cue["end_us"] - excerpt["start_us"],
        )
        cue.update({"clip_path": relative, "clip_sha256": file_sha256(output / relative)})
        spec["audio"].append({"id": cue["id"], "path": relative, "sha256": cue["clip_sha256"]})
    spec["experiment_id"] = json_digest(spec)
    write_json(output / "experiment.json", spec)
    return {
        "experiment_id": spec["experiment_id"],
        "complete_srt_cues": len(spec["srt_cues"]),
        "excluded_edge_cues": len(spec["excluded"]),
        "review_intake": intake,
    }


def classify_srt_cue(
    cue: dict, analysis: dict, settings: Settings, probability: Callable[[int, int], float]
) -> dict:
    comparison = classify_cue(cue, analysis, settings)
    empty = {"turns": analysis["turns"], "evidence": []}
    structural = classify_cue(cue, empty, settings)
    result = {
        "cue_id": cue["id"],
        "comparison_machine": comparison,
        "machine": structural,
        "evidence_scope": "none",
        "evidence": [],
    }
    if cue.get("srt_overlap_ids"):
        result["machine"] = {**structural, "label": "unknown", "reason": "overlapping_subtitles"}
        return result
    if structural["reason"] != "insufficient_speaker_reference":
        return result
    start, end = interval(cue)
    speaker = structural["speaker"]
    turns = [
        {**turn, "start_us": max(start, turn["start_us"]), "end_us": min(end, turn["end_us"])}
        for turn in analysis["turns"]
        if turn["start_us"] < end and turn["end_us"] > start
    ]
    ranges = merged_ranges(
        [span for turn in turns if turn["speaker"] == speaker for span in clear_ranges(turn, turns)]
    )
    evidence = []
    for first, last in sorted(ranges, key=lambda r: r[1] - r[0], reverse=True)[
        : settings.max_samples_per_speaker
    ]:
        if last - first < settings.min_sample_us:
            continue
        length = min(last - first, settings.max_sample_us)
        first += (last - first - length) // 2
        last = first + length
        evidence.append(
            {
                "speaker": speaker,
                "start_us": first,
                "end_us": last,
                "female_probability": probability(first, last),
            }
        )
    if evidence:
        return {
            **result,
            "machine": classify_cue(cue, {**empty, "evidence": evidence}, settings),
            "evidence_scope": "inside_srt",
            "evidence": evidence,
        }
    return {
        **result,
        "machine": comparison,
        "evidence_scope": "speaker_reference",
        "evidence": [item for item in analysis["evidence"] if item["speaker"] == speaker],
    }


def cast_srt_demo(root: Path, model_root: Path) -> dict:
    spec = read_json(root / "experiment.json")
    require_identity(spec, "experiment_id")
    if spec["schema"] != "ducai-casting-srt-experiment-v1":
        raise ValueError("Not an SRT experiment")
    if spec["casting_implementation_sha256"] != implementation_digest():
        raise ValueError("Cached diarization implementation changed; do not reuse old analysis")
    settings = Settings(**spec["settings"])
    recognizer = Recognizer(model_root, settings)
    if recognizer.model_lock_sha256 != spec["casting_model_lock_sha256"]:
        raise ValueError("Cached diarization models changed")
    started, rows = time.monotonic(), []
    for excerpt in spec["excerpts"]:
        path = checked_path(root, excerpt["path"])
        if file_sha256(path) != excerpt["sha256"]:
            raise ValueError("Cached original excerpt audio changed")
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
                raise ValueError("Invalid original excerpt WAV format")
            samples = recognizer.np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
            samples = samples.astype(recognizer.np.float32) / 32768

        def probability(start: int, end: int, offset=excerpt["start_us"], audio=samples) -> float:
            first = (start - offset) * SAMPLE_RATE // 1_000_000
            last = (end - offset) * SAMPLE_RATE // 1_000_000
            if not 0 <= first < last <= audio.size:
                raise ValueError("Local classifier reference lies outside the original excerpt")
            return recognizer.female_probability(audio[first:last])

        for cue in spec["srt_cues"]:
            if cue["excerpt_id"] == excerpt["id"]:
                rows.append(classify_srt_cue(cue, excerpt["analysis"], settings, probability))
                print(f"SRT casting: {len(rows)}/{len(spec['srt_cues'])}", flush=True)
    result = {
        "schema": "ducai-casting-srt-predictions-v1",
        "experiment_id": spec["experiment_id"],
        "implementation_sha256": file_sha256(Path(__file__)),
        "settings": asdict(settings),
        "packages": recognizer.versions,
        "model_lock_sha256": recognizer.model_lock_sha256,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "results": rows,
    }
    write_json(root / "srt-predictions.json", result)
    return {k: v for k, v in result.items() if k != "results"}


def prior_review_context(cue: dict, machine: dict, known: dict, reviews: dict) -> dict:
    """Flag contradictory feedback; changed boundaries never inherit a gender override."""
    feedback = []
    for key, review in reviews.items():
        old = known[key]
        overlap = min(old["end_us"], cue["end_us"]) - max(old["start_us"], cue["start_us"])
        if old["film_id"] == cue["film_id"] and overlap > 0:
            feedback.append(
                {
                    "cue_id": key,
                    "title": old["title"],
                    "override": review["override"],
                    "notes": review["notes"],
                    "overlap_us": overlap,
                }
            )
    conflicts = [
        f for f in feedback if f["override"] is not None and f["override"] != machine["label"]
    ]
    if machine["label"] != "unknown" and conflicts:
        machine = {**machine, "label": "unknown", "reason": "prior_review_conflict"}
    return {"machine": machine, "prior_feedback": feedback}


def build_srt_demo(root: Path) -> dict:
    spec = read_json(root / "experiment.json")
    require_identity(spec, "experiment_id")
    previous_path = root / "previous-demo-data.json"
    review_path = root / "review-received.json"
    if file_sha256(previous_path) != spec["previous_data_sha256"] or (
        file_sha256(review_path) != spec["review_sha256"]
    ):
        raise ValueError("Preserved previous demo/review changed")
    previous = read_json(previous_path)
    require_identity(previous, "demo_id")
    reviews = validate_review(read_json(review_path), previous)
    models, asr = {}, {}
    for model_id in ("large-v3", "belle-zh"):
        models[model_id] = read_json(root / f"asr-{model_id}.json")
        asr[model_id] = validate_asr(models[model_id], spec, root, model_id)
    for key in ("settings", "device", "compute_type"):
        if models["large-v3"][key] != models["belle-zh"][key]:
            raise ValueError(f"Paired ASR settings differ: {key}")
    result = read_json(root / "srt-predictions.json")
    if (
        result.get("schema") != "ducai-casting-srt-predictions-v1"
        or result.get("experiment_id") != spec["experiment_id"]
        or result.get("implementation_sha256") != file_sha256(Path(__file__))
        or result.get("settings") != spec["settings"]
        or result.get("model_lock_sha256") != spec["casting_model_lock_sha256"]
    ):
        raise ValueError("SRT predictions are stale or belong to a different experiment")
    rows = {r["cue_id"]: r for r in result["results"]}
    if len(rows) != len(result["results"]) or set(rows) != {c["id"] for c in spec["srt_cues"]}:
        raise ValueError("SRT predictions have missing/duplicate/unknown cues")
    old_cues = {c["id"]: c for d in previous["datasets"] for c in d["cues"]}
    cues = []
    for cue in spec["srt_cues"]:
        prediction = rows[cue["id"]]
        # Recompute the decision from saved real probabilities, never accept stale labels alone.
        excerpt = next(e for e in spec["excerpts"] if e["id"] == cue["excerpt_id"])
        probabilities = {
            (e["start_us"], e["end_us"]): e["female_probability"] for e in prediction["evidence"]
        }
        expected = classify_srt_cue(
            cue,
            excerpt["analysis"],
            Settings(**spec["settings"]),
            lambda first, last, values=probabilities: values[(first, last)],
        )
        if expected != prediction:
            raise ValueError("SRT prediction/evidence mismatch")
        cues.append(
            {
                **cue,
                **{k: prediction[k] for k in ("machine", "comparison_machine", "evidence_scope")},
                "title": f"SRT · câu {cue['source_cue_id'].removeprefix('srt-')}",
                "srt_text": cue["text"],
                "human": None,
                "target_start_us": 0,
                "target_end_us": cue["end_us"] - cue["start_us"],
                "transcripts": {
                    model: " ".join(s["text"] for s in asr[model][cue["id"]]["segments"])
                    for model in asr
                },
                "acoustic_machine": prediction["machine"],
                **prior_review_context(cue, prediction["machine"], old_cues, reviews),
            }
        )
    public = {
        **{k: previous[k] for k in ("schema", "films", "excerpts", "casting_model_lock_sha256")},
        "experiment_id": spec["experiment_id"],
        "previous_demo_id": previous["demo_id"],
        "carried_reviews": reviews,
        "datasets": [
            {"id": "srt", "title": "Mới · Bám từng câu SRT", "cues": cues},
            *previous["datasets"],
        ],
        "alignment": spec["alignment"],
        "model_hashes": {key: model["model_sha256"] for key, model in models.items()},
    }
    write_demo_page(root, public)
    summary = {
        "demo_id": public["demo_id"],
        "experiment_id": spec["experiment_id"],
        "srt_cues": len(cues),
        "excluded_edge_cues": len(spec["excluded"]),
        "carried_review_records": len(reviews),
        "labels": dict(Counter(c["machine"]["label"] for c in cues)),
        "acoustic_labels_before_review_flags": dict(
            Counter(c["acoustic_machine"]["label"] for c in cues)
        ),
        "prior_review_conflicts": sum(
            c["machine"]["reason"] == "prior_review_conflict" for c in cues
        ),
        "comparison_labels": dict(Counter(c["comparison_machine"]["label"] for c in cues)),
        "evidence_scopes": dict(Counter(c["evidence_scope"] for c in cues)),
        "asr_accuracy": "NOT_MEASURED",
        "casting_accuracy": "NOT_MEASURED",
        "product_gate": "NOT_EVALUATED",
        "held_out": spec["held_out"],
    }
    write_json(root / "demo-summary.json", summary)
    return summary
