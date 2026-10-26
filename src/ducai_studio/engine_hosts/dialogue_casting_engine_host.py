"""Bounded external inference with one reusable ML engine per host process."""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# Source checkout and portable package expose the same pinned lab implementation.
if not (Path(__file__).parent / "dialogue_casting").is_dir():
    sys.path.insert(0, str(Path(__file__).parents[3] / "scripts"))

from dialogue_casting.analysis import Settings, clear_ranges, merged_ranges
from dialogue_casting.assets import LOCK_PATH, read_json, runtime_versions, verify_file
from dialogue_casting.inference import decode_audio


def _stat_signature(paths: list[Path]) -> tuple[tuple[str, int, int], ...]:
    try:
        return tuple(
            (str(path), path.stat().st_size, path.stat().st_mtime_ns)
            for path in paths
        )
    except OSError as exc:
        raise RuntimeError("Tệp model phân vai đã đổi hoặc bị xóa; hãy tạo tác vụ mới") from exc


def _assert_assets_unchanged(
    paths: list[Path], expected: tuple[tuple[str, int, int], ...]
) -> None:
    if _stat_signature(paths) != expected:
        raise RuntimeError("Tệp model phân vai đã đổi trong lúc chạy; hãy tạo tác vụ mới")


class _DiarizationEngine:
    def __init__(self, request: dict) -> None:
        runtime_versions()
        import numpy as np
        import sherpa_onnx as sherpa

        self.root = Path(request["diarization_models"]).resolve()
        files = {}
        asset_paths = []
        for entry in read_json(LOCK_PATH)["artifacts"]:
            if entry["id"] not in {"segmentation", "embedding"}:
                continue
            for member in entry.get("members", [entry]):
                path = self.root / member["path"]
                verify_file(path, member)
                asset_paths.append(path)
                if path.suffix == ".onnx":
                    files[entry["id"]] = path
        settings = Settings(confidence=0.95)
        config = sherpa.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(files["segmentation"])
                ),
                num_threads=settings.threads,
                provider="cpu",
                debug=False,
            ),
            embedding=sherpa.SpeakerEmbeddingExtractorConfig(
                model=str(files["embedding"]),
                num_threads=settings.threads,
                provider="cpu",
                debug=False,
            ),
            clustering=sherpa.FastClusteringConfig(
                num_clusters=-1, threshold=settings.clustering_threshold
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError("Model phân đoạn người nói không hợp lệ; hãy cài lại bộ nhận diện")
        self.engine = sherpa.OfflineSpeakerDiarization(config)
        self.np = np
        self.settings = settings
        self.asset_paths = asset_paths
        self.asset_signature = _stat_signature(asset_paths)

    def run(self, request: dict) -> dict:
        if Path(request["diarization_models"]).resolve() != self.root:
            raise ValueError("Không được đổi bộ model trong cùng một tiến trình phân vai")
        _assert_assets_unchanged(self.asset_paths, self.asset_signature)
        start, end = request["start_us"], request["end_us"]
        samples = self.np.frombuffer(
            decode_audio(Path(request["media_path"]), start, end, request["ffmpeg"]),
            dtype="<f4",
        ).copy()
        if not self.np.isfinite(samples).all():
            raise RuntimeError("Âm thanh gốc chứa dữ liệu không hợp lệ")
        turns = [
            {
                "start_us": max(start, start + round(turn.start * 1_000_000)),
                "end_us": min(end, start + round(turn.end * 1_000_000)),
                "speaker": f"speaker-{turn.speaker}",
            }
            for turn in self.engine.process(samples).sort_by_start_time()
        ]
        turns = [turn for turn in turns if turn["end_us"] > turn["start_us"]]
        ranges = defaultdict(list)
        for turn in turns:
            ranges[turn["speaker"]].extend(clear_ranges(turn, turns))
        evidence = []
        for speaker, spans in sorted(ranges.items()):
            longest = sorted(
                merged_ranges(spans), key=lambda item: item[1] - item[0], reverse=True
            )
            for first, last in longest[: self.settings.max_samples_per_speaker]:
                if last - first < self.settings.min_sample_us:
                    continue
                length = min(last - first, self.settings.max_sample_us)
                first += (last - first - length) // 2
                evidence.append(
                    {"speaker": speaker, "start_us": first, "end_us": first + length}
                )
        return {"turns": turns, "evidence": evidence}


def _classification_result_payload(cue: dict, result: dict) -> dict:
    confidence = result.get("confidence", 0.0)
    if result.get("label") == "unknown" and confidence is None:
        confidence = 0.0
    return {
        "cue_id": cue["id"],
        "audio_key": cue["audio_key"],
        "label": result["label"],
        "reason": result["reason"],
        "confidence": confidence,
    }


class _ClassificationEngine:
    def __init__(self, request: dict) -> None:
        ecapa = importlib.import_module("dialogue_casting.ecapa")

        self.root = Path(request["classifier_models"]).resolve()
        self.settings = Settings(confidence=0.95)
        self.classifier = ecapa.EcapaClassifier(self.root, threads=self.settings.threads)
        lock_path = getattr(ecapa, "LOCK", None)
        lock = read_json(lock_path) if lock_path is not None else {"files": []}
        self.asset_paths = [
            (self.root / entry["path"]).resolve() for entry in lock["files"]
        ]
        self.asset_signature = _stat_signature(self.asset_paths)

    def refine(self, request: dict) -> dict:
        from dialogue_casting.asr_crosscheck import moss_sample_ranges

        if Path(request["classifier_models"]).resolve() != self.root:
            raise ValueError("Không được đổi bộ model trong cùng một tiến trình phân vai")
        _assert_assets_unchanged(self.asset_paths, self.asset_signature)
        window = request["moss_window"]
        start, end = request["start_us"], request["end_us"]
        if (window["id"], window["start_us"], window["end_us"]) != (
            request["window_id"], start, end
        ):
            raise ValueError("Mẫu nghe MOSS không khớp khối âm thanh gốc")
        references = moss_sample_ranges(window, request["cues"], self.settings)
        samples = self.classifier.np.frombuffer(
            decode_audio(Path(request["media_path"]), start, end, request["ffmpeg"]),
            dtype="<f4",
        ) if references else None
        cache = {}
        for row in references:
            bounds = row["start_us"], row["end_us"]
            if bounds not in cache:
                first, last = ((value - start) * 16000 // 1_000_000 for value in bounds)
                if samples is None or not 0 <= first < last <= samples.size:
                    raise ValueError("Mẫu nghe phân vai nằm ngoài âm thanh gốc")
                cache[bounds] = self.classifier.female_probability(samples[first:last])
            row["female_probability"] = cache[bounds]
        return {"id": window["id"], "start_us": start, "end_us": end,
                "evidence": references}

    def run(self, request: dict) -> dict:
        from dialogue_casting.speaker_consistency import classify_consistent_cues

        if Path(request["classifier_models"]).resolve() != self.root:
            raise ValueError("Không được đổi bộ model trong cùng một tiến trình phân vai")
        _assert_assets_unchanged(self.asset_paths, self.asset_signature)
        start, end = request["start_us"], request["end_us"]
        samples = self.classifier.np.frombuffer(
            decode_audio(Path(request["media_path"]), start, end, request["ffmpeg"]),
            dtype="<f4",
        )
        cache = {}

        def probability(first_us: int, last_us: int) -> float:
            if (first_us, last_us) not in cache:
                first, last = (
                    (timestamp - start) * 16000 // 1_000_000
                    for timestamp in (first_us, last_us)
                )
                if not 0 <= first < last <= samples.size:
                    raise ValueError("Tham chiếu giọng nằm ngoài khối âm thanh gốc")
                cache[first_us, last_us] = self.classifier.female_probability(
                    samples[first:last]
                )
            return cache[first_us, last_us]

        analysis = request["analysis"]
        analysis["evidence"] = [
            {
                **item,
                "female_probability": probability(item["start_us"], item["end_us"]),
            }
            for item in analysis["evidence"]
        ]
        decisions = classify_consistent_cues(
            request["cues"], analysis, self.settings, probability
        )
        rows = [
            _classification_result_payload(cue, decision["machine"])
            for cue, decision in zip(request["cues"], decisions, strict=True)
        ]
        return {
            "results": rows,
            "diagnostics": {"analysis": analysis, "decisions": decisions},
        }


def crosscheck(request: dict) -> dict:
    from dialogue_casting.asr_crosscheck import crosscheck_cues

    decisions = crosscheck_cues(
        request["cues"],
        request["windows"],
        request["evidence"],
        request["baseline"],
        Settings(confidence=0.95),
    )
    return {
        "results": [
            _classification_result_payload(cue, decision["machine"])
            for cue, decision in zip(request["cues"], decisions, strict=True)
        ],
        "diagnostics": {"crosschecks": decisions},
    }


class EngineSession:
    """Load at most one engine and reuse it for sequential bounded requests."""

    def __init__(self, mode: str) -> None:
        if mode not in {"diarize", "classify", "moss", "refine", "crosscheck"}:
            raise ValueError(f"Chế độ phân vai không được hỗ trợ: {mode}")
        self.mode = mode
        self.engine = None

    def run(self, request: dict) -> dict:
        if self.mode == "crosscheck":
            return crosscheck(request)
        if self.engine is None:
            if self.mode == "diarize":
                self.engine = _DiarizationEngine(request)
            elif self.mode in {"classify", "refine"}:
                self.engine = _ClassificationEngine(request)
            else:
                from dialogue_casting.moss import MossTranscriber

                self.engine = MossTranscriber(request)
        if self.mode == "moss":
            return self.engine.transcribe(request)
        if self.mode == "refine":
            return self.engine.refine(request)
        return self.engine.run(request)


def diarize(request: dict) -> dict:
    return EngineSession("diarize").run(request)


def classify(request: dict) -> dict:
    return EngineSession("classify").run(request)


def moss(request: dict) -> dict:
    return EngineSession("moss").run(request)


def _write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(partial, path)


def _execute(session: EngineSession, request_path: Path, output_path: Path) -> int:
    try:
        request = read_json(request_path)
        result = session.run(request)
        if "_context" in request:
            result["_context"] = request["_context"]
        _write_result(output_path, result)
        return 0
    except Exception as exc:
        _write_result(output_path, {"error": f"{type(exc).__name__}: {exc}"})
        return 2


def _serve(mode: str) -> int:
    session = EngineSession(mode)
    for raw_line in sys.stdin.buffer:
        try:
            envelope = json.loads(raw_line)
            if set(envelope) != {"request_path", "output_path"}:
                raise ValueError("Yêu cầu host phân vai không hợp lệ")
            request_path = Path(envelope["request_path"])
            output_path = Path(envelope["output_path"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"Invalid casting host request: {exc}", file=sys.stderr, flush=True)
            return 2
        _execute(session, request_path, output_path)
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[2] == "--serve":
        try:
            return _serve(sys.argv[1])
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 2
    if len(sys.argv) != 4:
        return 2
    try:
        session = EngineSession(sys.argv[1])
    except Exception as exc:
        Path(sys.argv[3]).write_text(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return 2
    return _execute(session, Path(sys.argv[2]), Path(sys.argv[3]))


if __name__ == "__main__":
    raise SystemExit(main())
