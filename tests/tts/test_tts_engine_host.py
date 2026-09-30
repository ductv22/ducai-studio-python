from __future__ import annotations

import sys
import wave
from array import array
from types import ModuleType, SimpleNamespace

import ducai_studio.engine_hosts.tts_engine_host as host_module
from ducai_studio.engine_hosts.tts_engine_host import TtsEngineHost


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
    assert engine.batch_sizes == [32, 16, 16]


def test_vieneu_host_does_not_pin_measured_batches_to_warmup_size(
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

    host._synthesize_vieneu(
        [{"cue_id": "warmup", "text": "Một", "output_path": str(tmp_path / "warm.wav")}],
        {"batch_size": 1},
    )
    host._synthesize_vieneu(
        [{"cue_id": "one", "text": "Hai", "output_path": str(tmp_path / "one.wav")}],
        {"batch_size": 8},
    )

    assert engine.batch_sizes == [1, 8]
    assert host.safe_batch_size is None


def test_vieneu_load_uses_pinned_upstream_cuda_path(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEngine:
        backend = "pytorch"

    def create_engine(**kwargs):
        captured.update(kwargs)
        return FakeEngine()

    fake_vieneu_module = ModuleType("vieneu")
    fake_vieneu_module.Vieneu = create_engine
    fake_torch_module = ModuleType("torch")
    fake_torch_module.version = SimpleNamespace(cuda="12.8")
    fake_torch_module.cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_name=lambda _index: "Test CUDA GPU",
    )
    monkeypatch.setitem(sys.modules, "vieneu", fake_vieneu_module)
    monkeypatch.setitem(sys.modules, "torch", fake_torch_module)
    monkeypatch.setattr(host_module.importlib.metadata, "version", lambda _package: "3.8.1")
    host = TtsEngineHost("vieneu")

    receipt = host.load({"runtime_version": "3.8.1", "max_batch_size": 64})

    assert captured == {
        "mode": "v3turbo",
        "device": "cuda",
        "backend": "pytorch",
        "max_batch_size": 32,
    }
    assert receipt["runtime_version"] == "3.8.1"
    assert receipt["optimization"] == "upstream_v381_fused_cuda_graph"


def test_vieneu_load_rejects_a_legacy_runtime(monkeypatch) -> None:
    monkeypatch.setattr(host_module.importlib.metadata, "version", lambda _package: "3.3.0")
    host = TtsEngineHost("vieneu")

    try:
        host.load({"runtime_version": "3.8.1"})
    except ValueError as exc:
        assert str(exc) == "vieneu_runtime_version_mismatch"
    else:
        raise AssertionError("legacy runtime must be rejected")


def test_vieneu_host_binds_an_upstream_preset() -> None:
    class PresetEngine:
        def get_preset_voice(self, name: str):
            assert name == "Mai Anh"
            return {"speaker_emb": [0.1] * 192, "codes": [[1, 2, 3]]}

    host = TtsEngineHost("vieneu")
    host.engine = PresetEngine()

    receipt = host.bind_preset({"preset_name": "Mai Anh"})

    assert receipt == {
        "binding": "vieneu_v381_builtin_preset",
        "preset_name": "Mai Anh",
    }
    assert host.voice["codes"] == [[1, 2, 3]]


def test_profile_binding_strips_legacy_encoder_pad_only_in_memory(
    tmp_path, monkeypatch
) -> None:
    class FakeArray:
        def __init__(self, *, size: int, shape: tuple[int, ...]) -> None:
            self.size = size
            self.shape = shape
            self.ndim = len(shape)

        def reshape(self, *_shape):
            return FakeArray(size=self.size, shape=(self.size,))

    class LoadedValues:
        files = ["speaker_emb", "codes"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __getitem__(self, key):
            return (
                FakeArray(size=192, shape=(192,))
                if key == "speaker_emb"
                else FakeArray(size=48, shape=(3, 16))
            )

    fake_numpy = ModuleType("numpy")
    fake_numpy.float32 = "float32"
    fake_numpy.int64 = "int64"
    fake_numpy.load = lambda *_args, **_kwargs: LoadedValues()
    fake_numpy.asarray = lambda value, dtype=None: value
    fake_utils = ModuleType("vieneu_utils")
    fake_core = ModuleType("vieneu_utils.core_utils")
    fake_core.strip_encoder_pad_frame = lambda _codes: FakeArray(
        size=32, shape=(2, 16)
    )
    fake_utils.core_utils = fake_core
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    monkeypatch.setitem(sys.modules, "vieneu_utils", fake_utils)
    monkeypatch.setitem(sys.modules, "vieneu_utils.core_utils", fake_core)
    profile = tmp_path / "clone.npz"
    profile.write_bytes(b"unchanged-profile")

    receipt, voice = host_module._profile_receipt(profile)

    assert receipt["stored_reference_codes_shape"] == [3, 16]
    assert receipt["reference_codes_shape"] == [2, 16]
    assert receipt["legacy_pad_frame_removed"] is True
    assert receipt["binding"] == "explicit_v381_speaker_embedding_192d_ref_codes"
    assert voice["codes"].shape == (2, 16)
    assert profile.read_bytes() == b"unchanged-profile"
