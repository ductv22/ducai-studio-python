"""Contract tests for the pinned offline MOSS casting adapter.

These tests intentionally avoid loading the multi-gigabyte model.  They cover
the boundaries that protect the product worker from stale, incomplete, or
cross-window MOSS output.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import types
import wave
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from dialogue_casting import moss  # noqa: E402

from ducai_studio.engine_hosts import dialogue_casting_engine_host as engine_host  # noqa: E402
from ducai_studio.workers.dialogue_casting_worker import (  # noqa: E402
    CastingStopped,
    DialogueCastingWorker,
    analysis_blocks,
)
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage  # noqa: E402


def _segment(start: float, end: float, speaker: str = "S0", text: str = "你好"):
    return SimpleNamespace(start=start, end=end, speaker=speaker, text=text)


def test_normalize_turns_projects_relative_seconds_into_bounded_absolute_us() -> None:
    turns = moss.normalize_turns(
        [_segment(0.125, 1.75, text="你好")],
        10_000_000,
        12_000_000,
    )

    assert turns == [
        {
            "start_us": 10_125_000,
            "end_us": 11_750_000,
            "speaker": "S0",
            "text": "你好",
        }
    ]


@pytest.mark.parametrize(
    "start_us,end_us",
    [
        (0.0, 1_000_000),
        (0, 0),
        (-1, 1_000_000),
        (0, 90_000_001),
        (True, 1_000_000),
    ],
)
def test_normalize_turns_rejects_invalid_window_types_and_lengths(start_us, end_us) -> None:
    with pytest.raises(ValueError, match="Khoảng âm thanh MOSS"):
        moss.normalize_turns([], start_us, end_us)


@pytest.mark.parametrize(
    "segment",
    [
        _segment(-0.1, 0.2),
        _segment(0.4, 0.2),
        _segment(float("nan"), 0.2),
        _segment(0.1, float("inf")),
        _segment(0.1, 2.1),
        _segment(0.1, 0.2, speaker=""),
        SimpleNamespace(start=0.1, end=0.2, speaker=None, text="x"),
    ],
)
def test_normalize_turns_rejects_invalid_segment_bounds_or_speaker(segment) -> None:
    with pytest.raises(ValueError):
        moss.normalize_turns([segment], 10_000_000, 12_000_000)


def test_normalize_turns_allows_real_speaker_overlap_for_structural_guard() -> None:
    turns = moss.normalize_turns(
        [_segment(0.0, 1.1, "S0"), _segment(0.9, 1.8, "S1")],
        0,
        2_000_000,
    )

    assert [(row["speaker"], row["start_us"], row["end_us"]) for row in turns] == [
        ("S0", 0, 1_100_000),
        ("S1", 900_000, 1_800_000),
    ]


def test_normalize_turns_clips_only_small_final_rounding_slop() -> None:
    turns = moss.normalize_turns([_segment(0.0, 1.00001)], 10_000_000, 11_000_000)

    assert turns[0]["start_us"] == 10_000_000
    assert turns[0]["end_us"] == 11_000_000


def _write_locked_file(path, content: bytes) -> dict:
    path.write_bytes(content)
    return {"path": path.name, "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def test_verify_moss_assets_checks_both_model_and_source_trees(tmp_path, monkeypatch) -> None:
    model_root = tmp_path / "model"
    source_root = tmp_path / "source"
    model_root.mkdir()
    source_root.mkdir()
    model_entry = _write_locked_file(model_root / "model.bin", b"model")
    source_entry = _write_locked_file(source_root / "runner.py", b"source")
    lock_path = tmp_path / "moss-lock.json"
    lock_path.write_text(
        json.dumps({"model_files": [model_entry], "source_files": [source_entry]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(moss, "LOCK_PATH", lock_path)

    lock = moss.verify_moss_assets(model_root, source_root)

    assert lock["model_files"][0]["path"] == "model.bin"
    assert lock["source_files"][0]["path"] == "runner.py"


@pytest.mark.parametrize("which", ["model", "source"])
def test_verify_moss_assets_fails_missing_or_corrupt_files(tmp_path, monkeypatch, which) -> None:
    model_root = tmp_path / "model"
    source_root = tmp_path / "source"
    model_root.mkdir()
    source_root.mkdir()
    model_entry = _write_locked_file(model_root / "model.bin", b"model")
    source_entry = _write_locked_file(source_root / "runner.py", b"source")
    lock_path = tmp_path / "moss-lock.json"
    lock_path.write_text(
        json.dumps({"model_files": [model_entry], "source_files": [source_entry]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(moss, "LOCK_PATH", lock_path)
    target = model_root / "model.bin" if which == "model" else source_root / "runner.py"
    target.write_bytes(b"tampered")

    with pytest.raises(
        RuntimeError,
        match="Missing/incomplete casting model|Casting SHA-256 mismatch",
    ):
        moss.verify_moss_assets(model_root, source_root)


def test_verify_moss_assets_rejects_lock_path_escape(tmp_path, monkeypatch) -> None:
    model_root = tmp_path / "model"
    source_root = tmp_path / "source"
    model_root.mkdir()
    source_root.mkdir()
    lock_path = tmp_path / "moss-lock.json"
    lock_path.write_text(
        json.dumps({
            "model_files": [{"path": "../outside", "size": 0, "sha256": ""}],
            "source_files": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(moss, "LOCK_PATH", lock_path)

    with pytest.raises(ValueError, match="escapes output directory"):
        moss.verify_moss_assets(model_root, source_root)


def test_verify_moss_runtime_rejects_unpinned_dependency(monkeypatch, tmp_path) -> None:
    lock_path = tmp_path / "moss-lock.json"
    lock_path.write_text(json.dumps({"runtime": {"torch": "2.8.0"}}), encoding="utf-8")
    monkeypatch.setattr(moss, "LOCK_PATH", lock_path)
    monkeypatch.setattr(moss.importlib.metadata, "version", lambda _name: "2.7.0")

    with pytest.raises(RuntimeError, match="torch==2.8.0"):
        moss.verify_moss_runtime()


def _fake_moss_request(
    tmp_path,
    *,
    text: str,
    generated_tokens: int,
    segments: list,
    tracker: dict[str, list | int] | None = None,
):
    tracker = tracker if tracker is not None else {}
    tracker.setdefault("numpy_seeds", [])
    tracker.setdefault("torch_seeds", [])
    tracker.setdefault("python_seeds", [])
    tracker.setdefault("runner_inits", 0)
    tracker.setdefault("transcriptions", 0)
    runtime = {"torch": "torch-test", "transformers": "transformers-test", "numpy": "numpy-test"}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(moss, "verify_moss_assets", lambda *_: {
        "runtime": runtime,
        "model_revision": "model-test",
        "source_revision": "source-test",
    })
    monkeypatch.setattr(moss, "verify_moss_runtime", lambda: runtime)
    monkeypatch.setattr(moss.importlib.metadata, "version", lambda name: runtime[name])

    fake_numpy = types.ModuleType("numpy")
    fake_numpy.random = types.SimpleNamespace(
        seed=lambda seed: tracker["numpy_seeds"].append(seed)
    )
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(
        is_available=lambda: False,
        is_bf16_supported=lambda: False,
    )
    fake_torch.backends = types.SimpleNamespace(cudnn=types.SimpleNamespace())
    fake_torch.manual_seed = lambda seed: tracker["torch_seeds"].append(seed)
    fake_torch.set_num_threads = lambda _threads: None
    fake_torch.use_deterministic_algorithms = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    source_package = types.ModuleType("moss_transcribe_diarize")
    source_package.__path__ = []
    parser = types.ModuleType("moss_transcribe_diarize.transcript_parser")
    parser.parse_transcript = lambda _text: segments
    monkeypatch.setitem(sys.modules, "moss_transcribe_diarize", source_package)
    monkeypatch.setitem(sys.modules, "moss_transcribe_diarize.transcript_parser", parser)

    class Loader:
        def create_module(self, _spec):
            return types.ModuleType("fake_moss_runner")

        def exec_module(self, module):
            module._attention_candidates = lambda _device: ("sdpa",)

            class Runner:
                def __init__(self, *_args, **_kwargs):
                    tracker["runner_inits"] += 1

                def transcribe(self, *_args, **_kwargs):
                    tracker["transcriptions"] += 1
                    return types.SimpleNamespace(
                        generated_tokens=generated_tokens,
                        text=text,
                    )

            module.ModelRunner = Runner

    monkeypatch.setattr(
        moss.importlib.util,
        "spec_from_file_location",
        lambda *_args, **_kwargs: ModuleSpec("fake_moss_runner", Loader()),
    )

    def fake_ffmpeg(command, **_kwargs):
        with wave.open(command[-1], "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\0\0" * 16_000)

    monkeypatch.setattr(moss.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(
        moss.random,
        "seed",
        lambda seed: tracker["python_seeds"].append(seed),
    )
    for key in (
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "TOKENIZERS_PARALLELISM",
        "CUBLAS_WORKSPACE_CONFIG",
    ):
        monkeypatch.setenv(key, "unset-before-moss-test")
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    request = {
        "moss_models": str(tmp_path / "models"),
        "moss_source": str(tmp_path / "source"),
        "start_us": 0,
        "end_us": 2_000_000,
        "window_id": "media-1:0:2000000",
        "media_path": "source.mp4",
        "ffmpeg": "ffmpeg",
        "scratch_root": str(scratch),
    }
    return monkeypatch, request


def test_moss_session_reuses_runner_and_resets_seed_for_each_window(tmp_path) -> None:
    tracker: dict[str, list | int] = {}
    patch, request = _fake_moss_request(
        tmp_path,
        text="[0.0] [S0] hello [1.0]",
        generated_tokens=1,
        segments=[_segment(0.0, 1.0)],
        tracker=tracker,
    )
    try:
        session = moss.MossTranscriber(request)
        first = session.transcribe(request)
        second = session.transcribe({**request, "window_id": "media-1:0:2000000-repeat"})

        assert first["transcript"] == second["transcript"]
        assert tracker["runner_inits"] == 1
        assert tracker["transcriptions"] == 2
        # One reset protects model construction and one reset precedes each
        # window so a previous window cannot advance random state.
        assert tracker["python_seeds"] == [20261024, 20261024, 20261024]
        assert tracker["numpy_seeds"] == [20261024, 20261024, 20261024]
        assert tracker["torch_seeds"] == [20261024, 20261024, 20261024]
    finally:
        patch.undo()


def test_transcribe_fails_explicitly_when_generation_is_truncated(tmp_path) -> None:
    patch, request = _fake_moss_request(
        tmp_path,
        text="[0.0] [S0] hello [1.0]",
        generated_tokens=moss.MAX_NEW_TOKENS,
        segments=[_segment(0.0, 1.0)],
    )
    try:
        with pytest.raises(RuntimeError, match="chạm giới hạn đầu ra"):
            moss.transcribe(request)
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
        assert os.environ["HF_DATASETS_OFFLINE"] == "1"
        assert os.environ["TOKENIZERS_PARALLELISM"] == "false"
        assert os.environ["CUBLAS_WORKSPACE_CONFIG"] == ":4096:8"
    finally:
        patch.undo()


def test_transcribe_fails_explicitly_on_partial_or_out_of_bounds_parser_output(tmp_path) -> None:
    patch, request = _fake_moss_request(
        tmp_path,
        text="[0.0] [S0] hello",
        generated_tokens=1,
        segments=[_segment(0.0, 1.0)],
    )
    try:
        with pytest.raises(RuntimeError, match="chưa hoàn chỉnh"):
            moss.transcribe(request)
    finally:
        patch.undo()

    patch, request = _fake_moss_request(
        tmp_path,
        text="[0.0] [S0] hello [2.5]",
        generated_tokens=1,
        segments=[_segment(0.0, 2.5)],
    )
    try:
        with pytest.raises(ValueError, match="nằm ngoài khối"):
            moss.transcribe(request)
    finally:
        patch.undo()


def _cue(
    cue_id: str = "cue-1",
    media_id: str = "media-1",
    *,
    start_us: int = 1_000_000,
) -> dict:
    return {
        "id": cue_id,
        "audio_key": f"audio-{cue_id}",
        "parts": [{
            "media_id": media_id,
            "media_path": f"{media_id}.mp4",
            "start_us": start_us,
            "end_us": start_us + 1_000_000,
        }],
    }


def _block_and_stages(cue: dict | None = None, media_id: str = "media-1"):
    cue = cue or _cue(media_id=media_id)
    blocks, fallback = analysis_blocks([cue], {media_id: 20_000_000})
    assert not fallback
    block = blocks[0]
    row = {
        "cue_id": cue["id"],
        "audio_key": cue["audio_key"],
        "label": "unknown",
        "reason": "no_speech",
        "confidence": 0.0,
    }
    classify = {
        "results": [row],
        "diagnostics": {
            "analysis": {"turns": [], "evidence": []},
            "decisions": [{
                "cue_id": cue["id"],
                "machine": row,
                "original_machine": row,
                "evidence": [],
            }],
        },
    }
    moss_result = {
        "id": block["window_id"],
        "start_us": block["start_us"],
        "end_us": block["end_us"],
        "turns": [],
        "transcript": "",
        "provenance": {
            "model_revision": "model",
            "source_revision": "source",
            "attention": "sdpa",
            "seed": 20261024,
            "generated_tokens": 1,
        },
    }
    refine = {
        "id": block["window_id"],
        "start_us": block["start_us"],
        "end_us": block["end_us"],
        "evidence": [],
    }
    return block, {"diarize": {"turns": [], "evidence": []},
                   "classify": classify, "moss": moss_result, "refine": refine}


def test_stage_validation_rejects_moss_window_mismatch_and_accepts_no_speech() -> None:
    block, stages = _block_and_stages()

    DialogueCastingWorker._validate_stage(block, "moss", stages["moss"])
    DialogueCastingWorker._validate_stage(block, "classify", stages["classify"])

    bad = {**stages["moss"], "id": "other-media:0:100"}
    with pytest.raises(ValueError, match="Checkpoint MOSS không khớp"):
        DialogueCastingWorker._validate_stage(block, "moss", bad)


def _refine_row(
    block: dict,
    *,
    start_us: int = 2_000_000,
    end_us: int = 3_000_000,
    origin: str = "moss_speaker",
    cue_id: str | None = None,
    probability: float = 0.99,
    speaker: str = "S0",
) -> dict:
    row = {
        "window_id": block["window_id"],
        "speaker": speaker,
        "start_us": start_us,
        "end_us": end_us,
        "origin": origin,
        "female_probability": probability,
    }
    if cue_id is not None:
        row["cue_id"] = cue_id
    return row


def test_refine_stage_accepts_bounded_speaker_and_cue_samples() -> None:
    block, stages = _block_and_stages()
    stages["refine"]["evidence"] = [
        _refine_row(block),
        _refine_row(
            block,
            start_us=1_000_000,
            end_us=2_000_000,
            origin="moss_cue",
            cue_id="cue-1",
        ),
    ]

    DialogueCastingWorker._validate_stage(block, "refine", stages["refine"])


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda row, _block: row.update(window_id="other"), "không hợp lệ"),
        (lambda row, _block: row.update(origin="other"), "không hợp lệ"),
        (lambda row, _block: row.update(female_probability=float("nan")), "xác suất"),
        (lambda row, _block: row.update(end_us=row["start_us"] + 999_999), "không hợp lệ"),
        (lambda row, block: row.update(end_us=block["end_us"] + 1), "mốc người nói"),
    ],
)
def test_refine_stage_rejects_invalid_profile_window_probability_and_bounds(
    mutate, match: str
) -> None:
    block, stages = _block_and_stages()
    row = _refine_row(block)
    mutate(row, block)
    stages["refine"]["evidence"] = [row]

    with pytest.raises(ValueError, match=match):
        DialogueCastingWorker._validate_stage(block, "refine", stages["refine"])


def test_refine_stage_rejects_unknown_cue_and_speaker_reference_budget() -> None:
    block, stages = _block_and_stages()
    stages["refine"]["evidence"] = [
        _refine_row(
            block,
            start_us=1_000_000,
            end_us=2_000_000,
            origin="moss_cue",
            cue_id="missing",
        )
    ]
    with pytest.raises(ValueError, match="không khớp câu thoại"):
        DialogueCastingWorker._validate_stage(block, "refine", stages["refine"])

    stages["refine"]["evidence"] = [
        _refine_row(block, start_us=index * 1_000_000, end_us=(index + 1) * 1_000_000)
        for index in range(4)
    ]
    with pytest.raises(ValueError, match="vượt giới hạn"):
        DialogueCastingWorker._validate_stage(block, "refine", stages["refine"])


def test_refinement_dependency_rejects_cross_turn_and_competing_speech() -> None:
    window = {
        "turns": [
            {"speaker": "S0", "start_us": 0, "end_us": 2_000_000},
            {"speaker": "S0", "start_us": 2_000_000, "end_us": 4_000_000},
            {"speaker": "S1", "start_us": 5_000_000, "end_us": 6_000_000},
            {"speaker": "S0", "start_us": 4_500_000, "end_us": 6_500_000},
        ]
    }
    crossing = {"evidence": [{
        "speaker": "S0", "start_us": 1_500_000, "end_us": 2_500_000
    }]}
    overlapping = {"evidence": [{
        "speaker": "S0", "start_us": 5_000_000, "end_us": 6_000_000
    }]}

    with pytest.raises(ValueError, match="không khớp người nói"):
        DialogueCastingWorker._validate_refinement_dependency(window, crossing)
    with pytest.raises(ValueError, match="chồng tiếng"):
        DialogueCastingWorker._validate_refinement_dependency(window, overlapping)


def test_refine_host_classifies_bounded_moss_samples_without_loading_real_model(
    tmp_path, monkeypatch
) -> None:
    class FakeSamples:
        def __init__(self, size: int):
            self.size = size

        def __getitem__(self, value: slice):
            return FakeSamples(value.stop - value.start)

    fake_numpy = SimpleNamespace(frombuffer=lambda *_args, **_kwargs: FakeSamples(160_000))

    classifier = SimpleNamespace(
        np=fake_numpy,
        female_probability=lambda samples: 0.99 if samples.size >= 16_000 else 0.01,
    )
    engine = engine_host._ClassificationEngine.__new__(engine_host._ClassificationEngine)
    engine.root = tmp_path.resolve()
    engine.settings = engine_host.Settings(confidence=0.95)
    engine.classifier = classifier
    engine.asset_paths = []
    engine.asset_signature = ()
    monkeypatch.setattr(
        engine_host,
        "decode_audio",
        lambda *_args: b"decoded-audio",
    )
    request = {
        "classifier_models": str(tmp_path),
        "window_id": "w",
        "start_us": 0,
        "end_us": 10_000_000,
        "media_path": "source.mp4",
        "ffmpeg": "ffmpeg",
        "cues": [{"id": "c", "start_us": 6_000_000, "end_us": 7_000_000}],
        "moss_window": {
            "id": "w", "start_us": 0, "end_us": 10_000_000,
            "turns": [{"speaker": "S0", "start_us": 0, "end_us": 10_000_000}],
        },
    }

    result = engine.refine(request)

    assert result["id"] == "w"
    assert {row["origin"] for row in result["evidence"]} == {"moss_speaker", "moss_cue"}
    assert all(row["female_probability"] == 0.99 for row in result["evidence"])
    assert all(1_000_000 <= row["end_us"] - row["start_us"] <= 5_000_000
               for row in result["evidence"])


def test_refine_host_rejects_moss_window_identity_mismatch(tmp_path) -> None:
    engine = engine_host._ClassificationEngine.__new__(engine_host._ClassificationEngine)
    engine.root = tmp_path.resolve()
    engine.settings = engine_host.Settings(confidence=0.95)
    engine.asset_paths = []
    engine.asset_signature = ()
    request = {
        "classifier_models": str(tmp_path),
        "window_id": "expected",
        "start_us": 0,
        "end_us": 10_000_000,
        "moss_window": {
            "id": "wrong", "start_us": 0, "end_us": 10_000_000, "turns": [],
        },
    }

    with pytest.raises(ValueError, match="không khớp khối âm thanh"):
        engine.refine(request)


def test_restore_rejects_checkpoint_settings_drift() -> None:
    worker = DialogueCastingWorker("restore-settings")
    worker.spec = {"input_fingerprint": "input", "settings_fingerprint": "current"}

    with pytest.raises(ValueError, match="không khớp cấu hình"):
        worker._restore({
            "signature": "input",
            "settings_fingerprint": "old",
            "schema_version": 2,
            "stages": {},
        })


def test_bundle_verification_rejects_implementation_drift(tmp_path, monkeypatch) -> None:
    from ducai_studio.workers import dialogue_casting_worker as worker_module

    host = tmp_path / "dialogue_casting_engine_host.py"
    host.write_text("# test host", encoding="utf-8")
    monkeypatch.setattr(worker_module, "engine_bundle_fingerprint", lambda _host: "actual")
    worker = DialogueCastingWorker("bundle-drift")
    worker.spec = {"provider_config": {
        "engine_host_path": str(host),
        "engine_bundle_fingerprint": "stale",
    }}

    with pytest.raises(ValueError, match="đã đổi"):
        worker._verify_bundle()


def test_stage_validation_rejects_wrong_context_binding() -> None:
    block, stages = _block_and_stages()

    with pytest.raises(ValueError, match="bằng chứng từ video"):
        DialogueCastingWorker._validate_stage(
            block,
            "moss",
            {**stages["moss"], "_context": {"window_id": "wrong-window"}},
            {"window_id": block["window_id"]},
        )


def test_classification_validation_rejects_malformed_probability_and_duplicate_decisions() -> None:
    block, stages = _block_and_stages()
    bad_probability = json.loads(json.dumps(stages["classify"]))
    bad_probability["diagnostics"]["analysis"]["evidence"] = [{
        "start_us": 0,
        "end_us": 1_000_000,
        "speaker": "S0",
    }]

    with pytest.raises(ValueError, match="xác suất giọng"):
        DialogueCastingWorker._validate_stage(block, "classify", bad_probability)

    duplicate = json.loads(json.dumps(stages["classify"]))
    duplicate["diagnostics"]["decisions"].append(
        duplicate["diagnostics"]["decisions"][0]
    )
    with pytest.raises(ValueError, match="bằng chứng âm thanh từng câu"):
        DialogueCastingWorker._validate_stage(block, "classify", duplicate)


@pytest.mark.parametrize(
    "turn",
    [
        {"start_us": block_start, "end_us": block_end, "speaker": "S0"}
        for block_start, block_end in ((-1, 1_000_000), (1_000_000, 30_000_000))
    ]
    + [{"start_us": 1.0, "end_us": 2_000_000, "speaker": "S0"}],
)
def test_stage_validation_rejects_malformed_or_out_of_window_moss_turn(turn: dict) -> None:
    block, stages = _block_and_stages()
    bad = {**stages["moss"], "turns": [turn]}

    with pytest.raises(ValueError, match="mốc người nói"):
        DialogueCastingWorker._validate_stage(block, "moss", bad)


def test_crosscheck_request_rejects_mixed_media_and_uses_original_machine() -> None:
    first, stages = _block_and_stages()
    fresh = _refine_row(first)
    stages["refine"]["evidence"] = [fresh]
    stages["classify"]["diagnostics"]["decisions"][0]["machine"] = {
        **stages["classify"]["diagnostics"]["decisions"][0]["machine"],
        "label": "female",
        "reason": "speaker_consensus",
        "confidence": 0.99,
    }
    second, stages_2 = _block_and_stages(_cue("cue-2", "media-2"), "media-2")
    worker = DialogueCastingWorker("crosscheck-contract")
    worker.spec = {"provider_config": {"ffmpeg": "ffmpeg"}}
    worker.stage_results = {first["window_id"]: stages, second["window_id"]: stages_2}

    request = worker._crosscheck_request([first])

    assert {window["id"] for window in request["windows"]} == {
        first["window_id"]
    }
    assert request["baseline"]["cue-1"]["reason"] == "no_speech"
    assert request["baseline"]["cue-1"]["label"] == "unknown"
    assert request["media_id"] == "media-1"
    assert fresh in request["evidence"]

    other, other_stages = _block_and_stages(_cue("cue-3", "media-2"), "media-2")
    worker.stage_results[other["window_id"]] = other_stages
    with pytest.raises(ValueError, match="nhiều video"):
        worker._crosscheck_request([first, other])


def test_checkpoint_persists_stage_evidence_with_schema_version_two(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    worker = DialogueCastingWorker("checkpoint-contract")
    worker.spec = {
        "input_fingerprint": "input-fingerprint",
        "settings_fingerprint": "settings-fingerprint",
    }
    _, stages = _block_and_stages()
    worker.stage_results = {"media-1:0:20000000": stages}
    worker.results = {"cue-1": stages["classify"]["results"][0]}
    events = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]

    worker._checkpoint()

    checkpoint = next(payload for kind, payload in events if kind == "checkpoint")
    artifact = json.loads(
        (tmp_path / "checkpoint-contract" / Path(checkpoint["artifact_path"]).name).read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["schema_version"] == 2
    assert artifact["schema_version"] == 2
    assert artifact["stages"]["media-1:0:20000000"]["moss"]["id"].startswith("media-1:")
    assert artifact["results"][0]["reason"] == "no_speech"


def _start_message(job_id: str, spec: dict, checkpoint: dict | None = None) -> WorkerMessage:
    payload = {"action": "start", "command_id": "start-command", "job": spec}
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    return WorkerMessage(PROTOCOL_VERSION, job_id, "event-start", "command", payload)


def _control_message(job_id: str, action: str) -> WorkerMessage:
    return WorkerMessage(
        PROTOCOL_VERSION,
        job_id,
        f"event-{action}",
        "command",
        {"action": action, "command_id": f"command-{action}"},
    )


def test_pause_releases_host_checkpoints_context_and_resumes_same_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    worker = DialogueCastingWorker("pause-contract")
    worker.stage_results = {"window": {"diarize": {"turns": [], "evidence": []}}}
    expected = json.loads(json.dumps(worker.stage_results))
    lifecycle = []
    events = []
    worker._stop_child = lambda: lifecycle.append("stopped")  # type: ignore[method-assign]
    worker._checkpoint = lambda: lifecycle.append(  # type: ignore[method-assign]
        ("checkpoint", json.loads(json.dumps(worker.stage_results)))
    )
    worker.send = lambda kind, payload: (events.append((kind, payload)) or WorkerMessage(
        PROTOCOL_VERSION, worker.job_id, f"event-{len(events)}", kind, payload
    ))  # type: ignore[method-assign]
    worker.commands.put(_control_message(worker.job_id, "pause"))
    worker.commands.put(_control_message(worker.job_id, "resume"))

    assert worker._control() is True
    assert lifecycle == ["stopped", ("checkpoint", expected)]
    assert [kind for kind, _ in events] == ["paused", "resumed"]
    assert worker.stage_results == expected


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_cancel_or_stop_releases_host_after_checkpoint(action, tmp_path, monkeypatch):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    worker = DialogueCastingWorker(f"{action}-contract")
    lifecycle = []
    events = []
    worker._stop_child = lambda: lifecycle.append("stopped")  # type: ignore[method-assign]
    worker._checkpoint = lambda: lifecycle.append("checkpoint")  # type: ignore[method-assign]
    worker.send = lambda kind, payload: (events.append((kind, payload)) or WorkerMessage(
        PROTOCOL_VERSION, worker.job_id, f"event-{len(events)}", kind, payload
    ))  # type: ignore[method-assign]
    worker.commands.put(_control_message(worker.job_id, action))

    with pytest.raises(CastingStopped):
        worker._control()

    assert lifecycle == ["stopped", "checkpoint"]
    assert events == [("stopped", {"command_id": f"command-{action}", "status": action})]


def _run_fake_worker(
    worker: DialogueCastingWorker,
    spec: dict,
    responses: dict[str, dict],
    checkpoint: dict | None = None,
):
    modes: list[str] = []
    events: list[tuple[str, dict]] = []
    worker._read = lambda: None  # type: ignore[method-assign]
    worker._control = lambda: False  # type: ignore[method-assign]
    worker._stop_child = lambda: None  # type: ignore[method-assign]
    worker._verify_bundle = lambda: None  # type: ignore[method-assign]
    worker.send = lambda kind, payload: (events.append((kind, payload)) or WorkerMessage(
        PROTOCOL_VERSION, worker.job_id, f"event-{len(events)}", kind, payload
    ))  # type: ignore[method-assign]
    worker._checkpoint = lambda: events.append(("checkpoint", {  # type: ignore[method-assign]
        "stages": worker.stage_results, "results": worker.results,
    }))

    def fake_host(mode: str, request: dict) -> dict:
        modes.append(mode)
        response = responses[mode]
        result = response(request) if callable(response) else json.loads(json.dumps(response))
        if mode in {"diarize", "classify", "moss", "refine"}:
            result["_context"] = request["_context"]
        return result

    worker._host = fake_host  # type: ignore[method-assign]
    worker.commands.put(_start_message(worker.job_id, spec, checkpoint))
    assert worker.run() == 0, events
    return modes, events


def test_worker_runs_diarize_classify_moss_refine_then_crosscheck_and_preserves_no_speech(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    cue = _cue()
    block, stages = _block_and_stages(cue)
    responses = {**stages, "crosscheck": {"results": [stages["classify"]["results"][0]],
                                           "diagnostics": {"crosschecks": []}}}
    worker = DialogueCastingWorker("run-order")
    spec = {"input_fingerprint": "sig", "settings_fingerprint": "settings",
            "provider_config": {
                "cues": [cue], "media_durations": {"media-1": 20_000_000},
                "analyzer_id": "analyzer", "engine_bundle_fingerprint": "bundle",
                "moss_model_revision": "model", "moss_source_revision": "source",
            }}

    modes, events = _run_fake_worker(worker, spec, responses)

    assert modes == ["diarize", "classify", "moss", "refine", "crosscheck"]
    assert worker.results["cue-1"]["reason"] == "no_speech"
    assert len([kind for kind, _ in events if kind == "checkpoint"]) == 6
    progress = [payload for kind, payload in events if kind == "progress"]
    assert [row["phase"] for row in progress] == [
        "diarize", "classify", "moss", "refine", "crosscheck", "crosscheck"
    ]
    assert {row["total"] for row in progress} == {5}
    assert block["window_id"] in worker.stage_results


def test_worker_finishes_each_stage_for_all_blocks_before_loading_the_next_engine(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    cues = [_cue("cue-1"), _cue("cue-2", start_us=61_000_000)]

    def diarize(_request: dict) -> dict:
        return {"turns": [], "evidence": []}

    def classify(request: dict) -> dict:
        rows = [{"cue_id": cue["id"], "audio_key": cue["audio_key"],
                 "label": "unknown", "reason": "no_speech", "confidence": 0.0}
                for cue in request["cues"]]
        return {"results": rows, "diagnostics": {
            "analysis": request["analysis"],
            "decisions": [{"cue_id": row["cue_id"], "machine": row,
                           "original_machine": row, "evidence": []}
                          for row in rows],
        }}

    def moss_response(request: dict) -> dict:
        return {"id": request["window_id"], "start_us": request["start_us"],
                "end_us": request["end_us"], "turns": [], "transcript": "",
                "provenance": {"model_revision": "model", "source_revision": "source",
                               "attention": "sdpa", "seed": 20261024,
                               "generated_tokens": 1}}

    def crosscheck(request: dict) -> dict:
        return {"results": [{"cue_id": cue["id"], "audio_key": cue["audio_key"],
                             "label": "unknown", "reason": "no_speech",
                             "confidence": 0.0} for cue in request["cues"]],
                "diagnostics": {"crosschecks": []}}

    def refine(request: dict) -> dict:
        return {"id": request["window_id"], "start_us": request["start_us"],
                "end_us": request["end_us"], "evidence": []}

    worker = DialogueCastingWorker("stage-outer-order")
    spec = {"input_fingerprint": "sig", "settings_fingerprint": "settings",
            "provider_config": {
                "cues": cues, "media_durations": {"media-1": 120_000_000},
                "analyzer_id": "analyzer", "engine_bundle_fingerprint": "bundle",
                "moss_model_revision": "model", "moss_source_revision": "source",
            }}

    modes, _ = _run_fake_worker(worker, spec, {
        "diarize": diarize, "classify": classify, "moss": moss_response,
        "refine": refine, "crosscheck": crosscheck,
    })

    assert modes == [
        "diarize", "diarize", "classify", "classify", "moss", "moss",
        "refine", "refine", "crosscheck",
    ]


def _empty_crosscheck_request(context: str) -> dict:
    return {
        "start_us": 0,
        "end_us": 1,
        "cues": [],
        "windows": [],
        "evidence": [],
        "baseline": {},
        "_context": {"request": context},
    }


def test_worker_reuses_live_engine_host_for_requests_in_the_same_mode(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    host = (Path(__file__).resolve().parents[2] / "src" / "ducai_studio" /
            "engine_hosts" / "dialogue_casting_engine_host.py")
    worker = DialogueCastingWorker("persistent-host")
    worker.spec = {"provider_config": {
        "classifier_python": sys.executable,
        "engine_host_path": str(host),
        "engine_host_sha256": hashlib.sha256(host.read_bytes()).hexdigest(),
    }}
    worker._verify_bundle = lambda: None  # type: ignore[method-assign]

    try:
        first = worker._host("crosscheck", _empty_crosscheck_request("first"))
        assert worker.child is not None
        first_pid = worker.child.pid
        second = worker._host("crosscheck", _empty_crosscheck_request("second"))

        assert first["_context"] == {"request": "first"}
        assert second["_context"] == {"request": "second"}
        assert worker.child is not None
        assert worker.child.pid == first_pid
        assert worker.child.poll() is None
    finally:
        worker._stop_child()


def test_engine_host_one_shot_cli_remains_supported(tmp_path) -> None:
    host = (Path(__file__).resolve().parents[2] / "src" / "ducai_studio" /
            "engine_hosts" / "dialogue_casting_engine_host.py")
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "result.json"
    request_path.write_text(
        json.dumps(_empty_crosscheck_request("one-shot")), encoding="utf-8"
    )

    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(host), "crosscheck",
         str(request_path), str(output_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(output_path.read_text(encoding="utf-8"))["_context"] == {
        "request": "one-shot"
    }


def test_resume_with_completed_stages_skips_only_completed_stages(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    cue = _cue()
    block, stages = _block_and_stages(cue)
    worker = DialogueCastingWorker("resume")
    spec = {"input_fingerprint": "sig", "settings_fingerprint": "settings",
            "provider_config": {
                "cues": [cue], "media_durations": {"media-1": 20_000_000},
                "analyzer_id": "analyzer", "engine_bundle_fingerprint": "bundle",
                "moss_model_revision": "model", "moss_source_revision": "source",
            }}
    worker.spec = spec
    base_request = {**spec["provider_config"], **block, "scratch_root": str(worker.root)}
    stages["diarize"]["_context"] = worker._stage_context(block, "diarize", base_request)
    analysis = {key: value for key, value in stages["diarize"].items() if key != "_context"}
    dependent_request = {**base_request, "analysis": analysis}
    for mode in ("classify", "moss"):
        stages[mode]["_context"] = worker._stage_context(block, mode, dependent_request)
    refine_request = {
        **dependent_request,
        "moss_window": stages["moss"],
        "classification": stages["classify"],
    }
    stages["refine"]["_context"] = worker._stage_context(block, "refine", refine_request)
    checkpoint_path = tmp_path / "resume" / "resume.json"
    checkpoint_path.parent.mkdir(exist_ok=True)
    checkpoint_path.write_text(json.dumps({
        "signature": "sig", "settings_fingerprint": "settings", "schema_version": 2,
        "stages": {block["window_id"]: stages},
        "results": [],
    }), encoding="utf-8")
    responses = {**stages, "crosscheck": {"results": [stages["classify"]["results"][0]],
                                           "diagnostics": {"crosschecks": []}}}
    modes, _ = _run_fake_worker(
        worker,
        spec,
        responses,
        {"artifact": {"uri": str(checkpoint_path)}},
    )

    # The checkpoint already has all four per-window stages; only final
    # source-time crosscheck is allowed to run again.
    assert modes == ["crosscheck"]


def test_resume_missing_refine_runs_only_refine_then_crosscheck(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path))
    cue = _cue()
    block, stages = _block_and_stages(cue)
    worker = DialogueCastingWorker("resume-refine")
    spec = {"input_fingerprint": "sig", "settings_fingerprint": "settings",
            "provider_config": {
                "cues": [cue], "media_durations": {"media-1": 20_000_000},
                "analyzer_id": "analyzer", "engine_bundle_fingerprint": "bundle",
                "moss_model_revision": "model", "moss_source_revision": "source",
            }}
    worker.spec = spec
    base_request = {**spec["provider_config"], **block, "scratch_root": str(worker.root)}
    stages["diarize"]["_context"] = worker._stage_context(block, "diarize", base_request)
    analysis = {key: value for key, value in stages["diarize"].items() if key != "_context"}
    dependent_request = {**base_request, "analysis": analysis}
    for mode in ("classify", "moss"):
        stages[mode]["_context"] = worker._stage_context(block, mode, dependent_request)
    pending_refine = stages.pop("refine")
    checkpoint_path = tmp_path / "resume-refine" / "resume.json"
    checkpoint_path.parent.mkdir(exist_ok=True)
    checkpoint_path.write_text(json.dumps({
        "signature": "sig", "settings_fingerprint": "settings", "schema_version": 2,
        "stages": {block["window_id"]: stages}, "results": [],
    }), encoding="utf-8")
    responses = {
        **stages,
        "refine": pending_refine,
        "crosscheck": {"results": [stages["classify"]["results"][0]],
                       "diagnostics": {"crosschecks": []}},
    }

    modes, _ = _run_fake_worker(
        worker, spec, responses, {"artifact": {"uri": str(checkpoint_path)}}
    )

    assert modes == ["refine", "crosscheck"]
