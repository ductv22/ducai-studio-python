from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .analysis import Settings, classify_cue, validate_analysis
from .assets import (
    LOCK_PATH,
    file_sha256,
    install_models,
    json_digest,
    model_files,
    read_json,
    runtime_versions,
    write_json,
)
from .corpus import load_labels, prepare_pack, validate_corpus, verify_clips
from .ecapa import download_model as download_ecapa_model
from .ecapa import verify_model as verify_ecapa_model
from .ecapa import verify_runtime as verify_ecapa_runtime
from .evaluation import PREDICTION_SCHEMA, calibrate, evaluate_files
from .inference import (
    Recognizer,
    analysis_windows,
    implementation_digest,
    window_key,
)


def predict(args) -> dict:
    corpus = read_json(args.corpus)
    validate_corpus(corpus)
    verify_clips(corpus, args.corpus)
    settings = Settings(threads=args.threads)
    selected = [cue for cue in corpus["cues"] if not args.film_id or cue["film_id"] == args.film_id]
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("Prediction limit must be positive")
        selected = selected[: args.limit]
    if not selected:
        raise ValueError("No cues selected for prediction")
    recognizer = Recognizer(args.model_root, settings)
    cache = args.cache or args.output.parent / "casting-window-cache"
    cache.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.monotonic()
    decoded_us = 0
    cache_hits = 0
    for film in corpus["films"]:
        cues = [cue for cue in selected if cue["film_id"] == film["id"]]
        if not cues:
            continue
        media = Path(film["media_path"])
        if (
            media.stat().st_size != film["source_size"]
            or file_sha256(media) != film["source_sha256"]
        ):
            raise RuntimeError(f"Original source changed; prepare a new corpus: {media}")
        for window in analysis_windows(cues, film["duration_us"], settings):
            key = window_key(film, window, settings)
            cached = cache / f"{key}.json"
            if cached.is_file():
                payload = read_json(cached)
                if payload.get("key") != key or payload.get("sha256") != json_digest(
                    payload["analysis"]
                ):
                    raise RuntimeError(f"Corrupt recognition cache: {cached}")
                raw = payload["analysis"]
                validate_analysis(raw)
                cache_hits += 1
            else:
                print(
                    f"Recognizing {film['id']}: {window['start_us'] / 1e6:.2f}–"
                    f"{window['end_us'] / 1e6:.2f} s",
                    flush=True,
                )
                raw = recognizer.analyze(media, window["start_us"], window["end_us"], args.ffmpeg)
                validate_analysis(raw)
                decoded_us += window["end_us"] - window["start_us"]
                write_json(cached, {"key": key, "sha256": json_digest(raw), "analysis": raw})
            for cue in window["cues"]:
                result = classify_cue(cue, raw, settings)
                results.append(result | {"analysis": raw, "window_key": key})
            print(f"Recognized cues: {len(results)}/{len(selected)}", flush=True)
    return {
        "schema": PREDICTION_SCHEMA,
        "corpus_id": corpus["corpus_id"],
        "model_lock_sha256": file_sha256(LOCK_PATH),
        "implementation_sha256": implementation_digest(),
        "packages": recognizer.versions,
        "settings": asdict(settings),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "newly_decoded_us": decoded_us,
        "cached_windows": cache_hits,
        "complete_corpus": len(results) == len(corpus["cues"]),
        "product_gate": "NOT_EVALUATED",
        "results": results,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="DUCAI offline dialogue-casting experiment (no GUI integration)"
    )
    commands = root.add_subparsers(dest="command", required=True)
    install = commands.add_parser(
        "install-models", help="Explicitly download and verify pinned models"
    )
    install.add_argument("--model-root", type=Path, required=True)
    install.add_argument("--download-cache", type=Path, required=True)
    install.add_argument("--offline", action="store_true")
    ecapa_install = commands.add_parser(
        "install-ecapa-model", help="Explicitly download and verify the pinned ECAPA model"
    )
    ecapa_install.add_argument("--model-root", type=Path, required=True)
    ecapa_install.add_argument("--download-cache", type=Path, required=True)
    ecapa_install.add_argument("--offline", action="store_true")
    verify = commands.add_parser(
        "verify", help="Verify model hashes and isolated runtime without network"
    )
    verify.add_argument("--model-root", type=Path, required=True)
    ecapa_verify = commands.add_parser(
        "verify-ecapa", help="Verify the ECAPA model and pinned runtime without network"
    )
    ecapa_verify.add_argument("--model-root", type=Path, required=True)
    prepare = commands.add_parser(
        "prepare", help="Create an independent, blinded listening/label pack"
    )
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--test-per-film", type=int, default=70)
    prepare.add_argument("--calibration-per-film", type=int, default=15)
    prepare.add_argument("--seed", default="ducai-casting-1")
    prepare.add_argument("--ffmpeg", default="ffmpeg")
    prepare.add_argument("--ffprobe", default="ffprobe")
    recognition = commands.add_parser(
        "predict", help="Run bounded source-audio recognition offline"
    )
    recognition.add_argument("--corpus", type=Path, required=True)
    recognition.add_argument("--model-root", type=Path, required=True)
    recognition.add_argument("--output", type=Path, required=True)
    recognition.add_argument("--cache", type=Path)
    recognition.add_argument(
        "--limit", type=int, help="Smoke only; partial output cannot pass evaluation"
    )
    recognition.add_argument("--film-id")
    recognition.add_argument("--threads", type=int, default=2)
    recognition.add_argument("--ffmpeg", default="ffmpeg")
    calibration = commands.add_parser(
        "calibrate", help="Freeze confidence from independent calibration labels"
    )
    calibration.add_argument("--corpus", type=Path, required=True)
    calibration.add_argument("--predictions", type=Path, required=True)
    calibration.add_argument("--labels", type=Path, required=True)
    calibration.add_argument("--output", type=Path, required=True)
    evaluation = commands.add_parser(
        "evaluate", help="Enforce the held-out accuracy AND coverage gate"
    )
    evaluation.add_argument("--corpus", type=Path, required=True)
    evaluation.add_argument("--predictions", type=Path, required=True)
    evaluation.add_argument("--labels", type=Path, required=True)
    evaluation.add_argument("--frozen", type=Path, required=True)
    evaluation.add_argument("--output", type=Path, required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parser().parse_args(argv)
    try:
        if args.command == "install-models":
            value = install_models(args.model_root, args.download_cache, offline=args.offline)
        elif args.command == "install-ecapa-model":
            value = download_ecapa_model(
                args.model_root, args.download_cache, offline=args.offline
            )
        elif args.command == "verify":
            value = {
                "models": {key: str(path) for key, path in model_files(args.model_root).items()},
                "packages": runtime_versions(),
                "product_gate": "NOT_EVALUATED",
            }
        elif args.command == "verify-ecapa":
            value = {
                "model": verify_ecapa_model(args.model_root),
                "packages": verify_ecapa_runtime(),
                "product_gate": "NOT_EVALUATED",
            }
        elif args.command == "prepare":
            corpus = prepare_pack(
                args.manifest,
                args.output,
                calibration=args.calibration_per_film,
                test=args.test_per_film,
                seed=args.seed,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
            )
            value = {
                "corpus_id": corpus["corpus_id"],
                "films": len(corpus["films"]),
                "cues": len(corpus["cues"]),
                "product_gate": "NOT_EVALUATED",
            }
        elif args.command == "predict":
            value = predict(args)
            write_json(args.output, value)
            value = {key: item for key, item in value.items() if key != "results"}
        elif args.command == "calibrate":
            corpus = read_json(args.corpus)
            labels = load_labels(args.labels, corpus)
            value = calibrate(corpus, read_json(args.predictions), labels)
            write_json(args.output, value)
        else:
            value = evaluate_files(args.corpus, args.predictions, args.labels, args.frozen)
            write_json(args.output, value)
            print(json.dumps({key: item for key, item in value.items() if key != "metrics"}))
            return 0 if value["gate"] == "PASS" else 2
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"DIALOGUE_CASTING_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
