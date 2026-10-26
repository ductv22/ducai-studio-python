from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import wave
from pathlib import Path

from .analysis import interval
from .assets import checked_path, file_sha256, json_digest, read_json, write_json

CORPUS_SCHEMA = "ducai-dialogue-casting-corpus-v1"
LABEL_SCHEMA = "ducai-dialogue-casting-labels-v1"
LABELS = {"male", "female", "mixed", "unclear"}
CONTEXT_US = 500_000


def srt_cues(path: Path, *, include_text: bool = False) -> list[dict]:
    result = []
    stamp = r"(\d+):(\d{2}):(\d{2})[,.](\d{3})"
    timing = re.compile(rf"^{stamp}\s+-->\s+{stamp}(?:\s+.*)?$")
    for block in re.split(
        r"\n\s*\n", path.read_text(encoding="utf-8-sig").replace("\r", "").strip()
    ):
        lines = block.strip().splitlines()
        if not lines:
            continue
        row = 1 if lines[0].isdigit() else 0
        match = timing.match(lines[row]) if len(lines) > row else None
        if not match or len(lines) <= row + 1:
            raise ValueError(f"Malformed SRT block in {path}: {lines[0]}")
        numbers = [int(value) for value in match.groups()]
        if any(numbers[i] > 59 for i in (1, 2, 5, 6)):
            raise ValueError(f"Invalid SRT minutes/seconds in {path}")
        times = [
            (numbers[i] * 3600 + numbers[i + 1] * 60 + numbers[i + 2]) * 1_000_000
            + numbers[i + 3] * 1000
            for i in (0, 4)
        ]
        cue = {"id": f"srt-{len(result) + 1}", "start_us": times[0], "end_us": times[1]}
        interval(cue)
        # Text is deliberately absent from the listening pack: labels follow audio.
        if include_text:
            cue["text"] = "\n".join(lines[row + 1 :])
        result.append(cue)
    if not result:
        raise ValueError(f"SRT has no cues: {path}")
    return result


def probe_duration(media: Path, ffprobe: str) -> int:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(media)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    duration = round(float(json.loads(result.stdout)["format"]["duration"]) * 1_000_000)
    if duration <= 0:
        raise ValueError(f"Invalid original media duration: {media}")
    return duration


def sample_cues(cues: list[dict], *, seed: str, calibration: int, test: int) -> list[dict]:
    if min(calibration, test) < 1:
        raise ValueError("Both calibration and held-out sets must have cues")
    candidates = sorted(cues, key=lambda cue: (cue["start_us"], cue["end_us"], cue["id"]))
    random.Random(seed).shuffle(candidates)
    selected: list[dict] = []
    for cue in candidates:
        start, end = interval(cue)
        # Independent clips, with context, cannot duplicate any labelled audio.
        if any(
            start - CONTEXT_US < item["end_us"] + CONTEXT_US
            and end + CONTEXT_US > item["start_us"] - CONTEXT_US
            for item in selected
        ):
            continue
        selected.append(cue | {"split": "calibration" if len(selected) < calibration else "test"})
        if len(selected) == calibration + test:
            return sorted(selected, key=lambda cue: (cue["split"], cue["start_us"]))
    raise ValueError(f"Only {len(selected)} disjoint cues available; need {calibration + test}")


def validate_corpus(corpus: dict) -> None:
    if corpus.get("schema") != CORPUS_SCHEMA:
        raise ValueError("Unsupported casting corpus schema")
    if corpus.get("corpus_id") != json_digest(
        {k: v for k, v in corpus.items() if k != "corpus_id"}
    ):
        raise ValueError("Casting corpus was modified after preparation")
    films = {film["id"]: film for film in corpus["films"]}
    if len(films) != len(corpus["films"]) or not films:
        raise ValueError("Film identities must be non-empty and unique")
    hashes = {film["source_sha256"] for film in films.values()}
    if len(hashes) != len(films):
        raise ValueError("Different film entries cannot reuse the same original media")
    ids: set[str] = set()
    ranges: dict[str, list[tuple[int, int]]] = {}
    for cue in sorted(corpus["cues"], key=lambda item: (item["film_id"], item["start_us"])):
        start, end = interval(cue)
        if not cue["id"] or cue["id"] in ids or cue["film_id"] not in films:
            raise ValueError("Invalid or duplicate casting cue identity")
        ids.add(cue["id"])
        if cue["split"] not in {"calibration", "test"}:
            raise ValueError("Cue split must be calibration or test")
        if end > films[cue["film_id"]]["duration_us"]:
            raise ValueError("Cue exceeds source duration")
        entries = ranges.setdefault(cue["film_id"], [])
        if entries and start - CONTEXT_US < entries[-1][1] + CONTEXT_US:
            raise ValueError("Listening samples overlap; calibration/test independence is invalid")
        entries.append((start, end))
        if not re.fullmatch(r"[a-f0-9]{64}", cue["clip_sha256"]):
            raise ValueError("Missing listening clip SHA-256")
    if not ids:
        raise ValueError("Corpus has no dialogue cues")


def prepare_pack(
    manifest_path: Path,
    output: Path,
    *,
    calibration: int = 15,
    test: int = 70,
    seed: str = "ducai-casting-1",
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> dict:
    manifest = read_json(manifest_path)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite a listening pack: {output}")
    if not manifest.get("films"):
        raise ValueError("The source manifest needs films")
    prepared = []
    # Validate and sample before creating output. Original subtitle times are required.
    for entry in manifest["films"]:
        if entry.get("timebase") != "original_audio":
            raise ValueError(
                "Use original-audio timestamps; export/migrate edited times explicitly"
            )
        media = (manifest_path.parent / entry["media_path"]).resolve(strict=True)
        cues = (
            srt_cues((manifest_path.parent / entry["srt_path"]).resolve(strict=True))
            if "srt_path" in entry
            else entry["cues"]
        )
        # JSON input carries explicit cue role; standard dialogue SRTs have no role metadata.
        cues = [cue for cue in cues if cue.get("role", "dialogue") == "dialogue"]
        selected = sample_cues(
            cues, seed=f"{seed}:{entry['id']}", calibration=calibration, test=test
        )
        print(f"Fingerprinting original source: {entry['title']}", flush=True)
        film = {
            "id": entry["id"],
            "title": entry["title"],
            "media_path": str(media),
            "source_sha256": file_sha256(media),
            "source_size": media.stat().st_size,
            "duration_us": probe_duration(media, ffprobe),
            "language": entry.get("language", "unverified"),
            "genre": entry.get("genre", "unverified"),
        }
        for cue in selected:
            if cue["end_us"] > film["duration_us"]:
                raise ValueError("Source subtitle lies outside original media")
        prepared.append((film, selected))
    if len({item[0]["id"] for item in prepared}) != len(prepared):
        raise ValueError("Duplicate film identity in source manifest")
    if len({item[0]["source_sha256"] for item in prepared}) != len(prepared):
        raise ValueError("Duplicate original media in source manifest")
    output.mkdir(parents=True)
    clips = output / "clips"
    clips.mkdir()
    corpus = {
        "schema": CORPUS_SCHEMA,
        "seed": seed,
        "context_us": CONTEXT_US,
        "sampling": "seeded, independent non-overlapping dialogue cues; no prediction filtering",
        "films": [item[0] for item in prepared],
        "cues": [],
    }
    for film, cues in prepared:
        for cue in cues:
            identity = json_digest(
                {
                    "source": film["source_sha256"],
                    "cue_id": cue["id"],
                    "start_us": cue["start_us"],
                    "end_us": cue["end_us"],
                }
            )[:24]
            target = clips / f"{identity}.wav"
            start = max(0, cue["start_us"] - CONTEXT_US)
            end = min(film["duration_us"], cue["end_us"] + CONTEXT_US)
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-n",
                    "-ss",
                    f"{start / 1_000_000:.6f}",
                    "-i",
                    film["media_path"],
                    "-t",
                    f"{(end - start) / 1_000_000:.6f}",
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(target),
                ],
                check=True,
                capture_output=True,
                timeout=180,
            )
            with wave.open(str(target), "rb") as audio:
                expected = (end - start) * 16000 // 1_000_000
                if abs(audio.getnframes() - expected) > 1600:
                    raise RuntimeError(f"Truncated original listening clip: {cue['id']}")
            corpus["cues"].append(
                {
                    "id": identity,
                    "source_cue_id": cue["id"],
                    "film_id": film["id"],
                    "start_us": cue["start_us"],
                    "end_us": cue["end_us"],
                    "split": cue["split"],
                    "clip_path": f"clips/{identity}.wav",
                    "clip_sha256": file_sha256(target),
                }
            )
            print(
                f"Listening clips: {len(corpus['cues'])}/{len(prepared) * (calibration + test)}",
                flush=True,
            )
    corpus["corpus_id"] = json_digest(corpus)
    validate_corpus(corpus)
    write_json(output / "corpus.json", corpus)
    render_annotation_page(corpus, output)
    shutil.copyfile(Path(__file__).with_name("annotation-guide.md"), output / "HUONG-DAN.md")
    return corpus


def render_annotation_page(corpus: dict, output: Path) -> None:
    # Embed only listening identity/audio and source provenance, never model output or split.
    public = {
        "corpus_id": corpus["corpus_id"],
        "films": corpus["films"],
        "cues": [
            {
                key: cue[key]
                for key in ("id", "film_id", "start_us", "end_us", "clip_path", "clip_sha256")
            }
            for cue in corpus["cues"]
        ],
    }
    random.Random(corpus["seed"] + ":blind-order").shuffle(public["cues"])
    data = json.dumps(public, ensure_ascii=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("annotation.html").read_text(encoding="utf-8")
    (output / "NGHE-VA-GAN-NHAN.html").write_text(
        template.replace("__CORPUS_DATA__", data), encoding="utf-8"
    )


def load_labels(path: Path, corpus: dict) -> dict[str, dict]:
    document = read_json(path)
    if (
        document.get("schema") != LABEL_SCHEMA
        or document.get("corpus_id") != corpus["corpus_id"]
        or document.get("label_source") != "human-listening"
        or not str(document.get("annotator", "")).strip()
    ):
        raise ValueError("Independent human listening labels for this exact corpus are required")
    known = {cue["id"]: cue for cue in corpus["cues"]}
    labels = {}
    for row in document["labels"]:
        cue_id = row["cue_id"]
        if cue_id not in known or cue_id in labels or row["label"] not in LABELS:
            raise ValueError("Unknown/duplicate cue or invalid listening label")
        if row.get("clip_sha256") != known[cue_id]["clip_sha256"]:
            raise ValueError("Label does not match the original listening clip")
        labels[cue_id] = row
    return labels


def verify_clips(corpus: dict, corpus_path: Path) -> None:
    for cue in corpus["cues"]:
        path = checked_path(corpus_path.parent, cue["clip_path"])
        if file_sha256(path) != cue["clip_sha256"]:
            raise ValueError(f"Listening clip was modified: {cue['id']}")
