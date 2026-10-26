"""A separate SRT experiment with ECAPA evidence and exact carried review provenance."""

from __future__ import annotations

import copy
import shutil
import time
import wave
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .analysis import Settings, validate_analysis
from .assets import checked_path, file_sha256, json_digest, read_json, write_json
from .demo import validate_asr, write_demo_page
from .ecapa import LOCK, EcapaClassifier
from .srt_demo import classify_srt_cue, prior_review_context, require_identity, validate_review


def implementation_digest() -> str:
    return json_digest(
        {
            name: file_sha256(Path(__file__).with_name(name))
            for name in ("refined_demo.py", "ecapa.py", "srt_demo.py", "analysis.py")
        }
    )


def carry_exact_reviews(
    previous: dict, current_cues: list[dict], reviews: dict
) -> tuple[dict, dict]:
    """Rebind only human state after verifying identical audio, source, time and stable ID."""
    old = {c["id"]: c for d in previous["datasets"] for c in d["cues"]}
    current = {c["id"]: c for c in current_cues}
    if len(current) != len(current_cues):
        raise ValueError("Duplicate current cue identity")
    carried, origins = {}, {}
    for cue_id, review in reviews.items():
        before, after = old[cue_id], current.get(cue_id)
        if after is None or any(
            before[key] != after[key]
            for key in (
                "clip_sha256",
                "film_id",
                "start_us",
                "end_us",
                "target_start_us",
                "target_end_us",
            )
        ):
            raise ValueError(f"Cannot carry a human review to changed audio/timing: {cue_id}")
        carried[cue_id] = copy.deepcopy(review)
        origins[cue_id] = {
            "demo_id": previous["demo_id"],
            "experiment_id": previous["experiment_id"],
            "machine_label": before["machine"]["label"],
            "clip_sha256": before["clip_sha256"],
        }
    return carried, origins


def prepare_refined_demo(previous_root: Path, review_path: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("Use a new refined-demo output directory")
    public = read_json(previous_root / "demo-data.json")
    base = read_json(previous_root / "experiment.json")
    require_identity(public, "demo_id")
    require_identity(base, "experiment_id")
    if public["experiment_id"] != base["experiment_id"]:
        raise ValueError("Previous SRT demo and experiment differ")
    if base["schema"] != "ducai-casting-srt-experiment-v1":
        raise ValueError("Refinement requires the original SRT experiment")
    reviews = validate_review(read_json(review_path), public)
    # Validate ASR provenance before preserving the existing transcripts unchanged.
    for model_id in ("large-v3", "belle-zh"):
        validate_asr(
            read_json(previous_root / f"asr-{model_id}.json"), base, previous_root, model_id
        )
    settings = {**base["settings"], "confidence": 0.95}
    Settings(**settings)
    paths = {c["clip_path"]: c["clip_sha256"] for d in public["datasets"] for c in d["cues"]}
    paths.update({e["path"]: e["sha256"] for e in base["excerpts"]})
    for relative, digest in paths.items():
        if file_sha256(checked_path(previous_root, relative)) != digest:
            raise ValueError(f"Previous audio changed: {relative}")
    spec = {
        "schema": "ducai-refined-srt-experiment-v1",
        "base_demo_id": public["demo_id"],
        "base_experiment_id": base["experiment_id"],
        "base_files": {
            name: file_sha256(previous_root / name)
            for name in (
                "experiment.json",
                "demo-data.json",
                "srt-predictions.json",
                "asr-large-v3.json",
                "asr-belle-zh.json",
            )
        },
        "review_sha256": file_sha256(review_path),
        "model_lock_sha256": file_sha256(LOCK),
        "implementation_sha256": implementation_digest(),
        "settings": settings,
        "threshold_notice": (
            "ECAPA 0.95 experimental operating point chosen after exposed calibration/diagnostics; "
            "not an independently frozen product threshold. Original model/gates unchanged."
        ),
        "audio_files": paths,
        "reserved_audio_exposure": base["reserved_audio_exposure"],
        "product_gate": "NOT_EVALUATED",
    }
    spec["experiment_id"] = json_digest(spec)
    output.mkdir(parents=True)
    for relative in paths:
        target = checked_path(output, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checked_path(previous_root, relative), target)
    for name in spec["base_files"]:
        shutil.copyfile(previous_root / name, output / f"base-{name}")
    shutil.copyfile(review_path, output / "review-received.json")
    shutil.copyfile(LOCK, output / "ecapa-lock.json")
    write_json(output / "experiment.json", spec)
    write_json(
        output / "review-intake.json",
        {
            "review_sha256": spec["review_sha256"],
            "schema": "ducai-refined-review-intake-v1",
            "base_demo_id": public["demo_id"],
            "base_experiment_id": base["experiment_id"],
            "records": len(reviews),
            "label_source": "demo-assisted-review",
            "notice": (
                "Only exact human overrides/reviewed/notes are carried; previous machine bindings "
                "remain in base-demo-data.json and review-received.json."
            ),
        },
    )
    return {
        "experiment_id": spec["experiment_id"],
        "audio_files": len(paths),
        "review_records": len(reviews),
    }


def load_context(root: Path) -> tuple[dict, dict, dict, dict]:
    spec = read_json(root / "experiment.json")
    require_identity(spec, "experiment_id")
    if spec["schema"] != "ducai-refined-srt-experiment-v1":
        raise ValueError("Not a refined SRT experiment")
    if file_sha256(root / "ecapa-lock.json") != spec["model_lock_sha256"]:
        raise ValueError("Copied ECAPA model lock changed")
    if spec["implementation_sha256"] != implementation_digest() or spec[
        "model_lock_sha256"
    ] != file_sha256(LOCK):
        raise ValueError("Refined implementation/model lock changed; prepare a fresh experiment")
    for name, digest in spec["base_files"].items():
        if file_sha256(root / f"base-{name}") != digest:
            raise ValueError(f"Preserved base result changed: {name}")
    if file_sha256(root / "review-received.json") != spec["review_sha256"]:
        raise ValueError("Preserved feedback changed")
    for relative, digest in spec["audio_files"].items():
        if file_sha256(checked_path(root, relative)) != digest:
            raise ValueError(f"Original audio changed: {relative}")
    public, base = read_json(root / "base-demo-data.json"), read_json(root / "base-experiment.json")
    require_identity(public, "demo_id")
    require_identity(base, "experiment_id")
    reviews = validate_review(read_json(root / "review-received.json"), public)
    return spec, base, public, reviews


def cast_refined_demo(root: Path, model_root: Path) -> dict:
    spec, base, _, _ = load_context(root)
    settings = Settings(**spec["settings"])
    started = time.monotonic()
    classifier = EcapaClassifier(model_root, threads=settings.threads)
    rows, analyses = [], []
    for excerpt in base["excerpts"]:
        with wave.open(str(checked_path(root, excerpt["path"])), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                raise ValueError("Invalid excerpt PCM format")
            samples = classifier.np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
            samples = samples.astype(classifier.np.float32) / 32768
        cached = {}

        def probability(
            start: int, end: int, offset=excerpt["start_us"], audio=samples, cache=cached
        ) -> float:
            key = (start, end)
            if key not in cache:
                first, last = ((t - offset) * 16000 // 1_000_000 for t in key)
                if not 0 <= first < last <= audio.size:
                    raise ValueError("ECAPA reference outside original excerpt")
                cache[key] = classifier.female_probability(audio[first:last])
            return cache[key]

        analysis = {
            "turns": copy.deepcopy(excerpt["analysis"]["turns"]),
            "evidence": [
                {**item, "female_probability": probability(item["start_us"], item["end_us"])}
                for item in excerpt["analysis"]["evidence"]
            ],
        }
        validate_analysis(analysis)
        for cue in base["srt_cues"]:
            if cue["excerpt_id"] == excerpt["id"]:
                rows.append(classify_srt_cue(cue, analysis, settings, probability))
                print(f"ECAPA SRT {len(rows)}/{len(base['srt_cues'])}", flush=True)
        analyses.append({"excerpt_id": excerpt["id"], "analysis": analysis})
    result = {
        "schema": "ducai-refined-srt-predictions-v1",
        "experiment_id": spec["experiment_id"],
        "implementation_sha256": implementation_digest(),
        "model_lock_sha256": file_sha256(LOCK),
        "settings": asdict(settings),
        "packages": classifier.versions,
        "device": "cpu",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "analyses": analyses,
        "results": rows,
    }
    write_json(root / "refined-predictions.json", result)
    return {k: v for k, v in result.items() if k not in ("analyses", "results")}


def verify_predictions(spec: dict, base: dict, result: dict) -> dict:
    if (
        any(
            result.get(key) != spec[key]
            for key in ("experiment_id", "implementation_sha256", "model_lock_sha256", "settings")
        )
        or result.get("schema") != "ducai-refined-srt-predictions-v1"
    ):
        raise ValueError("Stale refined predictions")
    rows = {row["cue_id"]: row for row in result["results"]}
    analyses = {row["excerpt_id"]: row["analysis"] for row in result["analyses"]}
    if len(rows) != len(result["results"]) or set(rows) != {c["id"] for c in base["srt_cues"]}:
        raise ValueError("Missing/duplicate refined cue results")
    if len(analyses) != len(result["analyses"]) or set(analyses) != {
        e["id"] for e in base["excerpts"]
    }:
        raise ValueError("Missing/duplicate refined analyses")
    for excerpt in base["excerpts"]:
        if analyses[excerpt["id"]]["turns"] != excerpt["analysis"]["turns"]:
            raise ValueError("Cached speaker timestamps changed")
    for cue in base["srt_cues"]:
        row = rows[cue["id"]]
        values = {(e["start_us"], e["end_us"]): e["female_probability"] for e in row["evidence"]}
        expected = classify_srt_cue(
            cue,
            analyses[cue["excerpt_id"]],
            Settings(**spec["settings"]),
            lambda first, last, values=values: values[(first, last)],
        )
        if expected != row:
            raise ValueError("Refined decisions do not match saved evidence")
    return rows


def build_refined_demo(root: Path) -> dict:
    spec, base, previous, reviews = load_context(root)
    result = read_json(root / "refined-predictions.json")
    rows = verify_predictions(spec, base, result)
    old = {c["id"]: c for d in previous["datasets"] for c in d["cues"]}
    datasets = copy.deepcopy(previous["datasets"])
    srt = next(d for d in datasets if d["id"] == "srt")
    srt["title"] = "Mới · ECAPA bám từng câu SRT"
    for cue in srt["cues"]:
        row = rows[cue["id"]]
        cue.update(
            {
                "acoustic_machine": row["machine"],
                "comparison_machine": old[cue["id"]]["acoustic_machine"],
                "comparison_title": "Bản SRT trước · nhận giọng thô, chưa có sửa tay: ",
                "previous_machine": old[cue["id"]]["machine"],
                "evidence_scope": row["evidence_scope"],
                **prior_review_context(cue, row["machine"], old, reviews),
            }
        )
    all_cues = [c for d in datasets for c in d["cues"]]
    carried, origins = carry_exact_reviews(previous, all_cues, reviews)
    public = {
        **{
            k: previous[k]
            for k in ("schema", "films", "excerpts", "casting_model_lock_sha256", "model_hashes")
        },
        "experiment_id": spec["experiment_id"],
        "previous_demo_id": previous["demo_id"],
        "datasets": datasets,
        "carried_reviews": carried,
        "review_origins": origins,
        "alignment": previous["alignment"],
        "casting_backend": "ECAPA-TDNN · CPU · thử nghiệm",
        "casting_backend_lock_sha256": spec["model_lock_sha256"],
        "review_notice": (
            f"Đã giữ nguyên {len(carried)} dấu kiểm tra/sửa tay của bạn trên đúng âm thanh. "
            "Tự động dùng kết quả máy mới; không dùng sửa tay để huấn luyện "
            "hoặc suy lan sang câu khác."
        ),
        "srt_notice": (
            "Bản mới dùng ECAPA trên âm thanh gốc theo SRT. Phần nhãn đã sửa vẫn được ưu tiên; "
            "kết quả máy và bản trước hiển thị riêng để đối chiếu."
        ),
    }
    write_demo_page(root, public)
    labelled = [
        c for c in srt["cues"] if reviews.get(c["id"], {}).get("override") in ("male", "female")
    ]
    observed_errors = [
        c["id"]
        for c in labelled
        if c["acoustic_machine"]["label"] in ("male", "female")
        and c["acoustic_machine"]["label"] != reviews[c["id"]]["override"]
    ]
    summary = {
        "demo_id": public["demo_id"],
        "experiment_id": spec["experiment_id"],
        "srt_cues": len(srt["cues"]),
        "labels": dict(Counter(c["machine"]["label"] for c in srt["cues"])),
        "raw_labels": dict(Counter(c["acoustic_machine"]["label"] for c in srt["cues"])),
        "raw_reasons": dict(Counter(c["acoustic_machine"]["reason"] for c in srt["cues"])),
        "evidence_scopes": dict(Counter(c["evidence_scope"] for c in srt["cues"])),
        "carried_review_records": len(carried),
        "raw_disagreements_on_selected_reviewed_cues": observed_errors,
        "selected_reviewed_gender_cues": len(labelled),
        "selected_reviewed_automatically_assigned": sum(
            c["acoustic_machine"]["label"] in ("male", "female") for c in labelled
        ),
        "casting_seconds": result["elapsed_seconds"],
        "automatic_accuracy": None,
        "notice": (
            "Selected assisted review is not independent evaluation. Missing records do not "
            "prove correctness. Calibrated/experiment data are exposed; original product gate "
            "remains unproven."
        ),
        "product_gate": "NOT_EVALUATED",
    }
    write_json(root / "demo-summary.json", summary)
    return summary
