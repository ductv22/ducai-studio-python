from __future__ import annotations

import sys
import wave
from array import array
from types import ModuleType, SimpleNamespace

from ducai_studio.engine_hosts.tts_engine_host import TtsEngineHost
from ducai_studio.engine_hosts.vieneu_v330_optimization import (
    OPTIMIZATION_SCHEMA,
    install_vieneu_v330_optimizations,
)


class FakeSamples(list):
    @property
    def size(self) -> int:
        return len(self)

    def reshape(self, _shape: int) -> FakeSamples:
        return self


class FakeFinite:
    @staticmethod
    def all() -> bool:
        return True


class MemoryLimitedEngine:
    sample_rate = 48_000

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def infer_batch(self, texts, *, batch_size: int, **_kwargs):
        self.batch_sizes.append(batch_size)
        if batch_size > 16:
            raise RuntimeError("CUDA out of memory")
        return [[0.05] * 480 for _text in texts]


def test_vieneu_host_remembers_oom_reduced_batch_for_following_requests(
    tmp_path, monkeypatch
) -> None:
    fake_numpy = SimpleNamespace(
        float32="float32",
        asarray=lambda values, dtype=None: FakeSamples(values),
        isfinite=lambda _values: FakeFinite(),
    )

    def write_wav(path, values, sample_rate, *, subtype):
        assert subtype == "PCM_16"
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(array("h", (int(value * 32767) for value in values)).tobytes())

    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(write=write_wav))
    host = TtsEngineHost("vieneu")
    engine = MemoryLimitedEngine()
    host.engine = engine
    host.voice = {"speaker_emb": [], "codes": []}

    first = host._synthesize_vieneu(
        [
            {"cue_id": "one", "text": "Một", "output_path": str(tmp_path / "one.wav")},
            {"cue_id": "two", "text": "Hai", "output_path": str(tmp_path / "two.wav")},
        ],
        {"batch_size": 64},
    )
    second = host._synthesize_vieneu(
        [{"cue_id": "three", "text": "Ba", "output_path": str(tmp_path / "three.wav")}],
        {"batch_size": 64},
    )

    assert first["batch_size"] == 16
    assert second["batch_size"] == 16
    assert engine.batch_sizes == [64, 32, 16, 16]


def test_vieneu_cuda_optimization_is_version_gated_and_process_local(monkeypatch) -> None:
    class FakeBatchEngine:
        def generate_batch(self):
            return "original-generate"

        def _generate_codes_batch(self):
            return "original-codes"

        def _prompt_embeds(self):
            return "prompt"

    fake_engine_module = ModuleType("vieneu.v3_turbo_serve.engine")
    fake_engine_module.V3TurboBatchEngine = FakeBatchEngine
    fake_engine_module._min_expected_frames = lambda _value: 1
    fake_engine_module.max_expected_frames = lambda _value: 2
    fake_serve_module = ModuleType("vieneu.v3_turbo_serve")
    fake_serve_module.engine = fake_engine_module
    fake_vieneu_module = ModuleType("vieneu")
    fake_vieneu_module.v3_turbo_serve = fake_serve_module
    monkeypatch.setitem(sys.modules, "vieneu", fake_vieneu_module)
    monkeypatch.setitem(sys.modules, "vieneu.v3_turbo_serve", fake_serve_module)
    monkeypatch.setitem(sys.modules, "vieneu.v3_turbo_serve.engine", fake_engine_module)

    engine = SimpleNamespace(
        backend="pytorch",
        engine=SimpleNamespace(device=SimpleNamespace(type="cuda")),
    )
    receipt = install_vieneu_v330_optimizations(engine, runtime_version="3.3.0")

    assert receipt["enabled"] is True
    assert receipt["schema"] == OPTIMIZATION_SCHEMA
    assert FakeBatchEngine._ducai_optimization_schema == OPTIMIZATION_SCHEMA
    assert FakeBatchEngine.generate_batch.__name__ == "_generate_batch_optimized"
    assert FakeBatchEngine._generate_codes_batch.__name__ == (
        "_generate_codes_batch_optimized"
    )


def test_vieneu_optimization_keeps_cpu_backend_unmodified() -> None:
    engine = SimpleNamespace(
        backend="onnx",
        engine=SimpleNamespace(device=SimpleNamespace(type="cpu")),
    )

    receipt = install_vieneu_v330_optimizations(engine, runtime_version="3.3.0")

    assert receipt["enabled"] is False
    assert receipt["reason"] == "backend_not_pytorch"


def test_vieneu_optimization_rejects_unpinned_runtime() -> None:
    engine = SimpleNamespace(
        backend="pytorch",
        engine=SimpleNamespace(device=SimpleNamespace(type="cuda")),
    )

    receipt = install_vieneu_v330_optimizations(engine, runtime_version="3.4.0")

    assert receipt["enabled"] is False
    assert receipt["reason"] == "runtime_version_not_supported"
