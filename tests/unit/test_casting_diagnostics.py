from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from dialogue_casting.analysis import Settings  # noqa: E402
from dialogue_casting.speaker_consistency import classify_consistent_cues  # noqa: E402


def _turn(start: int, end: int, speaker: str = "speaker-a") -> dict[str, object]:
    return {"start_us": start, "end_us": end, "speaker": speaker}


def _sample(start: int, end: int, speaker: str = "speaker-a") -> dict[str, object]:
    return {**_turn(start, end, speaker), "female_probability": 0.99}


def test_rescued_decision_keeps_full_machine_diagnostics() -> None:
    cues = [
        {"id": "anchor-a", "start_us": 0, "end_us": 2_000_000},
        {"id": "uncertain-a", "start_us": 3_000_000, "end_us": 5_000_000},
        {"id": "anchor-b", "start_us": 6_000_000, "end_us": 8_000_000},
    ]
    analysis = {
        "turns": [_turn(0, 2_000_000), _turn(3_000_000, 5_000_000), _turn(6_000_000, 8_000_000)],
        "evidence": [_sample(0, 2_000_000), _sample(6_000_000, 8_000_000)],
    }

    rows = classify_consistent_cues(
        cues,
        analysis,
        Settings(confidence=0.95),
        lambda start, _end: 0.6 if start == 3_000_000 else 0.99,
    )

    uncertain = rows[1]
    assert uncertain["machine"]["reason"] == "speaker_consensus"
    assert uncertain["machine"]["label"] == "female"
    assert uncertain["original_machine"]["reason"] == "low_confidence"
    consensus = uncertain["speaker_consensus"]
    assert consensus["status"] == "accepted"
    assert consensus["speaker"] == "speaker-a"
    assert len(consensus["independent_samples"]) == 2
    assert all(
        {"start_us", "end_us", "confidence"} <= set(sample)
        for sample in consensus["independent_samples"]
    )


def test_host_classify_preserves_result_rows_and_returns_full_diagnostics(monkeypatch) -> None:
    from ducai_studio.engine_hosts import dialogue_casting_engine_host as host

    class FakeArray:
        size = 100_000

        def __getitem__(self, _key):
            return self

    class FakeNumpy:
        @staticmethod
        def frombuffer(_raw, *, dtype):
            assert dtype == "<f4"
            return FakeArray()

    class FakeClassifier:
        np = FakeNumpy()

        def __init__(self, _model_root, *, threads):
            assert threads > 0

        @staticmethod
        def female_probability(_samples):
            return 0.99

    fake_ecapa = types.ModuleType("dialogue_casting.ecapa")
    fake_ecapa.EcapaClassifier = FakeClassifier
    monkeypatch.setitem(sys.modules, "dialogue_casting.ecapa", fake_ecapa)
    monkeypatch.setattr(host, "decode_audio", lambda *_args: b"audio")

    request = {
        "start_us": 0,
        "end_us": 5_000_000,
        "media_path": "fixture.wav",
        "ffmpeg": "ffmpeg",
        "classifier_models": "models",
        "analysis": {
            "turns": [_turn(0, 2_000_000)],
            "evidence": [{"speaker": "speaker-a", "start_us": 0, "end_us": 2_000_000}],
        },
        "cues": [
            {"id": "voiced", "audio_key": "a", "start_us": 0, "end_us": 2_000_000},
            {"id": "silent", "audio_key": "b", "start_us": 3_000_000, "end_us": 5_000_000},
        ],
    }

    result = host.classify(request)

    assert [row.keys() for row in result["results"]] == [
        {"cue_id", "audio_key", "label", "reason", "confidence"},
        {"cue_id", "audio_key", "label", "reason", "confidence"},
    ]
    assert result["results"][0]["label"] == "female"
    assert result["results"][1]["reason"] == "no_speech"
    diagnostics = result["diagnostics"]
    assert diagnostics["analysis"]["evidence"][0]["female_probability"] == pytest.approx(0.99)
    assert [row["cue_id"] for row in diagnostics["decisions"]] == ["voiced", "silent"]
    assert diagnostics["decisions"][0]["machine"]["reason"] == "assigned"
    assert diagnostics["decisions"][1]["machine"]["reason"] == "no_speech"


def test_release_build_includes_consensus_helper() -> None:
    project_root = Path(__file__).resolve().parents[2]
    source = project_root / "scripts" / "dialogue_casting" / "speaker_consistency.py"
    build_script = (project_root / "scripts" / "build-release.ps1").read_text(encoding="utf-8")

    compile(source.read_text(encoding="utf-8"), str(source), "exec")
    assert '"speaker_consistency"' in build_script

