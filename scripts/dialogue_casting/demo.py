"""Build a portable review page from real local recognition artifacts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from dataclasses import asdict
from pathlib import Path

from .analysis import Settings, classify_cue
from .assets import checked_path, file_sha256, json_digest, read_json, write_json
from .corpus import load_labels, validate_corpus
from .evaluation import prediction_rows
from .inference import analysis_windows, window_key


def speaker_for_word(word: dict, turns: list[dict]) -> str | None:
    lengths = {}
    for turn in turns:
        overlap = min(word["end_us"], turn["end_us"]) - max(word["start_us"], turn["start_us"])
        if overlap > 0:
            lengths[turn["speaker"]] = lengths.get(turn["speaker"], 0) + overlap
    ordered = sorted(lengths.items(), key=lambda item: item[1], reverse=True)
    if not ordered or (len(ordered) > 1 and ordered[1][1] >= ordered[0][1] / 2):
        return None
    return ordered[0][0]


def speaker_aligned_segments(segments: list[dict], excerpt: dict) -> list[dict]:
    """Keep recognized words in order; split using measured word/speaker times."""
    result = []
    offset = excerpt["start_us"]
    turns = [
        {**turn, "start_us": turn["start_us"] - offset, "end_us": turn["end_us"] - offset}
        for turn in excerpt["analysis"]["turns"]
    ]
    for segment in segments:
        words = segment.get("words", [])
        word_text = "".join("".join(w["text"] for w in words).split())
        if (
            not words
            or word_text != "".join(segment["text"].split())
            or all(w["end_us"] == w["start_us"] for w in words)
        ):
            result.append({k: segment[k] for k in ("start_us", "end_us", "text")})
            continue
        group, speaker = [], None

        def flush(items: list[dict]) -> None:
            if items:
                result.append({
                    "start_us": min(w["start_us"] for w in items),
                    "end_us": max(w["end_us"] for w in items),
                    "text": "".join(w["text"] for w in items).strip(),
                })

        for word in words:
            who = speaker_for_word(word, turns)
            if group:
                end = max(w["end_us"] for w in group)
                start = min(w["start_us"] for w in group)
                boundary = (
                    who != speaker or word["start_us"] - end > 800_000
                    or word["end_us"] - start > 8_000_000
                    or group[-1]["text"].rstrip().endswith(("。", "！", "？", "!", "?"))
                )
                if boundary and end > start and word["start_us"] >= end:
                    flush(group)
                    group = []
            if not group:
                speaker = who
            group.append(word)
            if who is not None:
                speaker = who
        if group and max(w["end_us"] for w in group) == min(w["start_us"] for w in group):
            # Zero-length trailing tokens retain text on the preceding cue.
            result[-1]["text"] += "".join(w["text"] for w in group)
        else:
            flush(group)
    return result


def prepare_demo(
    corpus_path: Path, predictions_path: Path, labels_path: Path, output: Path, ffmpeg: str
) -> dict:
    corpus = read_json(corpus_path)
    validate_corpus(corpus)
    predictions = read_json(predictions_path)
    analyses, settings = prediction_rows(predictions, corpus)
    labels = load_labels(labels_path, corpus)
    cues = [cue for cue in corpus["cues"] if cue["split"] == "calibration"]
    if set(analyses) != {cue["id"] for cue in cues}:
        raise ValueError("Demo requires exactly the calibration predictions, not held-out cues")
    if any(cue["id"] not in labels for cue in cues):
        raise ValueError("Calibration listening labels are incomplete")
    page = (corpus_path.parent / "NGHE-VA-GAN-NHAN.html").read_text(encoding="utf-8")
    match = re.search(r'<script id="corpus" type="application/json">(.*?)</script>', page, re.S)
    public = json.loads(match.group(1))
    positions = {cue["id"]: i for i, cue in enumerate(public["cues"], 1)}
    output.mkdir(parents=True, exist_ok=False)
    (output / "clips").mkdir()
    spec = {
        "schema": "ducai-casting-demo-experiment-v1",
        "corpus_id": corpus["corpus_id"],
        "settings": asdict(settings),
        "casting_model_lock_sha256": predictions["model_lock_sha256"],
        "casting_implementation_sha256": predictions["implementation_sha256"],
        "labels_sha256": file_sha256(labels_path),
        "films": [{"id": f["id"], "title": f["title"]} for f in corpus["films"]],
        "audio": [],
        "calibration": [],
        "excerpts": [],
        "held_out": "NOT_RUN; 140 reserved cues are not predicted or scored",
    }
    for cue in cues:
        source = checked_path(corpus_path.parent, cue["clip_path"])
        if file_sha256(source) != cue["clip_sha256"]:
            raise ValueError("Calibration audio changed")
        shutil.copyfile(source, output / cue["clip_path"])
        spec["audio"].append({
            "id": cue["id"], "path": cue["clip_path"], "sha256": cue["clip_sha256"]
        })
        spec["calibration"].append({
            **cue,
            "listening_position": positions[cue["id"]],
            "human": labels[cue["id"]]["label"],
            "machine": classify_cue(cue, analyses[cue["id"]], settings),
        })
    by_prediction = {r["cue_id"]: r for r in predictions["results"]}
    for film in corpus["films"]:
        first = min(
            (cue for cue in cues if cue["film_id"] == film["id"]),
            key=lambda c: c["start_us"],
        )
        windows = analysis_windows(
            [cue for cue in corpus["cues"] if cue["film_id"] == film["id"]],
            film["duration_us"], settings,
        )
        window = next(w for w in windows if first in w["cues"])
        if window_key(film, window, settings) != by_prediction[first["id"]]["window_key"]:
            raise ValueError("Demo excerpt must use the original recognition window")
        source = Path(film["media_path"])
        if source.stat().st_size != film["source_size"]:
            raise ValueError("Original source size changed")
        print(f"Verifying source for excerpt: {film['id']}", flush=True)
        if file_sha256(source) != film["source_sha256"]:
            raise ValueError("Original source checksum changed")
        relative = f"clips/excerpt-{film['id']}.wav"
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-ss", f"{window['start_us'] / 1e6:.6f}", "-i", str(source),
            "-t", f"{(window['end_us'] - window['start_us']) / 1e6:.6f}",
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(output / relative),
        ], check=True, capture_output=True, timeout=180)
        audio = {
            "id": f"excerpt-{film['id']}",
            "path": relative,
            "sha256": file_sha256(output / relative),
        }
        with wave.open(str(output / relative), "rb") as wav:
            decoded_us = round(wav.getnframes() * 1_000_000 / wav.getframerate())
        if abs(decoded_us - (window["end_us"] - window["start_us"])) > 100_000:
            raise ValueError("Continuous excerpt is truncated")
        spec["audio"].append(audio)
        spec["excerpts"].append({
            **audio,
            "film_id": film["id"],
            "start_us": window["start_us"],
            "end_us": window["end_us"],
            "source_sha256": film["source_sha256"],
            "window_key": by_prediction[first["id"]]["window_key"],
            "analysis": analyses[first["id"]],
        })
    spec["experiment_id"] = json_digest(spec)
    write_json(output / "experiment.json", spec)
    return {"experiment_id": spec["experiment_id"], "audio_clips": len(spec["audio"])}


def validate_asr(result: dict, spec: dict, root: Path, model_id: str) -> dict:
    if (
        result.get("schema") != "ducai-whisper-comparison-v1"
        or result.get("experiment_id") != spec["experiment_id"]
        or result.get("model_id") != model_id
    ):
        raise ValueError("ASR output belongs to a different experiment/model")
    known = {item["id"]: item for item in spec["audio"]}
    rows = {}
    for row in result["results"]:
        key = row["audio_id"]
        if key not in known or key in rows or row["audio_sha256"] != known[key]["sha256"]:
            raise ValueError("ASR result has an unknown, duplicate or changed audio identity")
        if file_sha256(checked_path(root, known[key]["path"])) != known[key]["sha256"]:
            raise ValueError("Demo audio changed after ASR")
        with wave.open(str(checked_path(root, known[key]["path"])), "rb") as audio:
            duration_us = round(audio.getnframes() * 1_000_000 / audio.getframerate())
        if type(row["duration_us"]) is not int or row["duration_us"] != duration_us:
            raise ValueError("ASR duration does not match original WAV")
        previous = -1
        for segment in row["segments"]:
            start, end = segment["start_us"], segment["end_us"]
            if (
                type(start) is not int or type(end) is not int
                or start < 0 or start < previous or end <= start or end > row["duration_us"]
                or not isinstance(segment["text"], str) or not segment["text"].strip()
            ):
                raise ValueError("Invalid ASR segment")
            previous = start
            previous_word = -1
            for word in segment.get("words", []):
                if (
                    type(word["start_us"]) is not int or type(word["end_us"]) is not int
                    or word["start_us"] < 0 or word["start_us"] < previous_word
                    or word["end_us"] < word["start_us"] or word["end_us"] > row["duration_us"]
                    or not isinstance(word["text"], str)
                ):
                    raise ValueError("Invalid ASR word timestamp")
                previous_word = word["start_us"]
        rows[key] = row
    if set(rows) != set(known):
        raise ValueError("ASR comparison is incomplete")
    return rows


def write_segment_clip(source: Path, target: Path, start_us: int, end_us: int) -> tuple[int, int]:
    with wave.open(str(source), "rb") as wav:
        rate = wav.getframerate()
        first = max(0, (start_us - 500_000) * rate // 1_000_000)
        last = min(wav.getnframes(), (end_us + 500_000) * rate // 1_000_000)
        wav.setpos(first)
        frames = wav.readframes(last - first)
        params = wav.getparams()
    with wave.open(str(target), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(frames)
    return start_us - round(first * 1_000_000 / rate), end_us - round(first * 1_000_000 / rate)


def build_demo(root: Path) -> dict:
    spec = read_json(root / "experiment.json")
    identity = json_digest({k: v for k, v in spec.items() if k != "experiment_id"})
    if identity != spec["experiment_id"]:
        raise ValueError("Experiment manifest identity mismatch")
    settings = Settings(**spec["settings"])
    models, asr = {}, {}
    for model_id in ("large-v3", "belle-zh"):
        models[model_id] = read_json(root / f"asr-{model_id}.json")
        asr[model_id] = validate_asr(models[model_id], spec, root, model_id)
    for key in ("settings", "device", "compute_type"):
        if models["large-v3"][key] != models["belle-zh"][key]:
            raise ValueError(f"Paired ASR settings differ: {key}")
    calibration = []
    for cue in spec["calibration"]:
        item = {
            **{k: cue[k] for k in (
                "film_id", "start_us", "end_us", "clip_path", "clip_sha256", "human",
                "machine", "listening_position",
            )},
            "id": f"cal-{cue['id']}",
            "title": f"Bộ nghe · câu {cue['listening_position']}",
            "target_start_us": min(500_000, cue["start_us"]),
            "target_end_us": min(500_000, cue["start_us"]) + cue["end_us"] - cue["start_us"],
            "transcripts": {
                model_id: " ".join(s["text"] for s in asr[model_id][cue["id"]]["segments"])
                for model_id in asr
            },
        }
        calibration.append(item)
    datasets = [{"id": "calibration", "title": "30 câu đã đối chiếu", "cues": calibration}]
    for model_id, title in (("large-v3", "Whisper hiện tại"), ("belle-zh", "Whisper tiếng Trung")):
        cues = []
        for excerpt in spec["excerpts"]:
            segments = speaker_aligned_segments(asr[model_id][excerpt["id"]]["segments"], excerpt)
            for index, segment in enumerate(segments, 1):
                cue = {
                    "id": f"{model_id}-{excerpt['film_id']}-{index}",
                    "start_us": excerpt["start_us"] + segment["start_us"],
                    "end_us": excerpt["start_us"] + segment["end_us"],
                }
                relative = f"clips/{cue['id']}.wav"
                begin, end = write_segment_clip(
                    checked_path(root, excerpt["path"]), root / relative,
                    segment["start_us"], segment["end_us"],
                )
                cues.append({
                    **cue,
                    "film_id": excerpt["film_id"],
                    "title": f"Câu {index}",
                    "clip_path": relative,
                    "clip_sha256": file_sha256(root / relative),
                    "human": None,
                    "target_start_us": begin,
                    "target_end_us": end,
                    "machine": classify_cue(cue, excerpt["analysis"], settings),
                    "transcripts": {
                        other: segment["text"] if other == model_id else " ".join(
                            s["text"] for s in asr[other][excerpt["id"]]["segments"]
                            if s["start_us"] < segment["end_us"]
                            and s["end_us"] > segment["start_us"]
                        )
                        for other in asr
                    },
                })
        datasets.append({"id": model_id, "title": f"Đoạn liên tục · {title}", "cues": cues})
    public = {
        "schema": "ducai-casting-demo-v1",
        "experiment_id": spec["experiment_id"],
        "films": spec["films"],
        "datasets": datasets,
        "excerpts": [
            {k: e[k] for k in ("film_id", "path", "start_us", "end_us")}
            for e in spec["excerpts"]
        ],
        "alignment": "ASR words + diarized speaker changes; pause >0.8s; target <=8s",
        "model_hashes": {k: v["model_sha256"] for k, v in models.items()},
        "casting_model_lock_sha256": spec["casting_model_lock_sha256"],
    }
    write_demo_page(root, public)
    summary = {
        "demo_id": public["demo_id"],
        "datasets": [{"id": d["id"], "cues": len(d["cues"])} for d in datasets],
        "asr_accuracy": "NOT_MEASURED",
        "product_gate": "NOT_EVALUATED",
        "held_out": spec["held_out"],
    }
    write_json(root / "demo-summary.json", summary)
    return summary


def write_demo_page(root: Path, public: dict) -> None:
    public.pop("demo_id", None)
    public["demo_id"] = json_digest(public)
    encoded = json.dumps(public, ensure_ascii=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("demo.html").read_text(encoding="utf-8")
    logic = Path(__file__).with_name("demo-review.js").read_text(encoding="utf-8")
    html = template.replace("__DEMO_DATA__", encoded).replace("__REVIEW_LOGIC__", logic)
    (root / "MO-DEMO.html").write_text(html, encoding="utf-8")
    write_json(root / "demo-data.json", public)
