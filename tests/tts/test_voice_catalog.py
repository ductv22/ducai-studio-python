from __future__ import annotations

import hashlib
import json
import sys
import wave
from array import array
from pathlib import Path

import ducai_studio.providers.tts.vieneu as vieneu_module
from ducai_studio.providers.tts.catalog import VoiceCatalog, VoiceRightsState
from ducai_studio.providers.tts.contracts import TtsCueRequest
from ducai_studio.providers.tts.vieneu import VieNeuTtsProvider


def test_catalog_preserves_stable_id_readiness_and_rights(tmp_path: Path) -> None:
    voice_root = tmp_path / "piper_voices"
    voice_root.mkdir()
    model = voice_root / "vi_VN-vais1000-medium.onnx"
    model.write_bytes(b"fixture-model")
    model.with_suffix(".onnx.json").write_text(
        json.dumps(
            {
                "audio": {"sample_rate": 22_050},
                "phoneme_id_map": {"_": [0]},
            }
        ),
        encoding="utf-8",
    )

    catalog = VoiceCatalog(tmp_path)
    installed = catalog.get("vi_VN-vais1000-medium", require_installed=True)
    restricted = catalog.get("vi_VN-vivos-x_low")

    assert installed.installed is True
    assert installed.model_fingerprint
    assert installed.name == "Vais1000 Medium"
    assert restricted.rights_state is VoiceRightsState.RESTRICTED
    assert "phi thương mại" in restricted.rights_label.casefold()
    assert restricted.installable is True
    assert len(restricted.download_files) == 2
    assert len(catalog.entries()) == 23


def test_catalog_rejects_incomplete_onnx_pair(tmp_path: Path) -> None:
    voice_root = tmp_path / "piper_voices"
    voice_root.mkdir()
    (voice_root / "vi_VN-vais1000-medium.onnx").write_bytes(b"fixture-model")

    entry = VoiceCatalog(tmp_path).get("vi_VN-vais1000-medium")

    assert entry.installed is False
    assert not entry.model_path


def test_catalog_loads_vieneu_presets_with_distinct_preview_contract(tmp_path: Path) -> None:
    preset_file = (
        tmp_path
        / "vieneu_3_8_runtime"
        / ".venv"
        / "Lib"
        / "site-packages"
        / "vieneu"
        / "assets"
        / "voices_v3_turbo.json"
    )
    preset_file.parent.mkdir(parents=True)
    preset_file.write_text(
        json.dumps(
            {
                "presets": {
                    "Mai Anh": {"description": "Nữ miền Bắc"},
                    "Minh Đức": {"description": "Nam miền Bắc"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    preview_root = tmp_path / "previews"
    preview_root.mkdir()
    (preview_root / "vieneu-mai-anh.wav").write_bytes(b"RIFF")

    entries = VoiceCatalog(tmp_path, preview_root=preview_root).vieneu_presets(installed=True)

    assert [entry.voice_id for entry in entries] == ["preset:Mai Anh", "preset:Minh Đức"]
    assert entries[0].installed is True
    assert entries[0].preview_available is True
    assert entries[1].preview_available is False
    assert entries[0].preview_text != entries[1].preview_text
    assert all(entry.rights_state is VoiceRightsState.RESTRICTED for entry in entries)


def test_catalog_detects_installed_vieneu_assets_without_starting_cuda(tmp_path: Path) -> None:
    runtime_root = tmp_path / "vieneu_3_8_runtime"
    runtime_python = runtime_root / ".venv" / "Scripts" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"runtime")
    (runtime_root / "ducai_runtime.json").write_text(
        json.dumps({"version": "3.8.1"}),
        encoding="utf-8",
    )
    preset_file = (
        runtime_root
        / ".venv"
        / "Lib"
        / "site-packages"
        / "vieneu"
        / "assets"
        / "voices_v3_turbo.json"
    )
    preset_file.parent.mkdir(parents=True)
    preset_file.write_text(
        json.dumps({"presets": {"Mai Anh": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert VoiceCatalog(tmp_path).vieneu_runtime_installed() is True

    (runtime_root / "ducai_runtime.json").write_text(
        json.dumps({"version": "3.3.0"}),
        encoding="utf-8",
    )
    assert VoiceCatalog(tmp_path).vieneu_runtime_installed() is False


def test_vieneu_provider_uses_preset_without_clone_profile(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    class Host:
        def request(self, action: str, payload: dict) -> dict:
            calls.append((action, payload))
            if action == "bind_preset":
                return {"binding": "vieneu_v381_builtin_preset", "preset_name": "Mai Anh"}
            if action == "synthesize_batch":
                job = payload["jobs"][0]
                output = Path(job["output_path"])
                with wave.open(str(output), "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(48_000)
                    stream.writeframes(array("h", [0] * 480).tobytes())
                return {
                    "items": [
                        {
                            "cue_id": job["cue_id"],
                            "ok": True,
                            "output_path": str(output),
                            "sample_rate": 48_000,
                            "duration_us": 10_000,
                        }
                    ],
                    "batch_size": 1,
                }
            raise AssertionError(action)

    provider = VieNeuTtsProvider(data_root=tmp_path)
    provider._ensure_host = lambda *, load_model: Host()  # type: ignore[method-assign]

    result = provider.synthesize_batch(
        request_id="preset-test",
        cues=(TtsCueRequest("cue", 1, 0, 1_000_000, "Xin chào", "fingerprint"),),
        voice_id="preset:Mai Anh",
        output_dir=tmp_path / "audio",
    )

    assert not result.failures
    assert result.metadata["voice_binding"] == "vieneu_v381_builtin_preset"
    assert result.metadata["preset_name"] == "Mai Anh"
    assert calls[0] == ("bind_preset", {"preset_name": "Mai Anh"})


def test_bundled_voice_previews_are_distinct_and_between_five_and_eight_seconds() -> None:
    root = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "assets"
        / "voice-previews"
    )
    previews = sorted(root.glob("*.wav"))
    digests: set[str] = set()
    durations: list[float] = []
    for path in previews:
        with wave.open(str(path), "rb") as stream:
            durations.append(stream.getnframes() / stream.getframerate())
        digests.add(hashlib.sha256(path.read_bytes()).hexdigest())

    assert len(previews) == 27
    assert len(digests) == len(previews)
    assert min(durations) >= 5.0
    assert max(durations) <= 8.0


def test_vieneu_health_rejects_self_declared_but_corrupt_runtime_and_profile(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    runtime_root = data_root / "vieneu_3_8_runtime"
    runtime_python = runtime_root / ".venv" / "Scripts" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"not-a-python-runtime")
    (runtime_root / "ducai_runtime.json").write_text(
        json.dumps({"version": "3.8.1"}),
        encoding="utf-8",
    )
    profile = data_root / "vieneu_profiles" / "clone.npz"
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"not-an-npz")
    profile.with_suffix(".json").write_text("{}", encoding="utf-8")
    host_script = tmp_path / "tts_engine_host.py"
    host_script.write_text("# fixture", encoding="utf-8")

    health = VieNeuTtsProvider(
        runtime_python=runtime_python,
        data_root=data_root,
        profile_path=profile,
        host_script=host_script,
    ).health()

    assert health.state.value == "unavailable"
    assert health.details["profile_ready"] is True
    assert health.details["profile_valid"] is False


def test_vieneu_runtime_without_profile_remains_selectable_for_first_enrollment(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "data"
    runtime_root = data_root / "vieneu_3_8_runtime"
    runtime_root.mkdir(parents=True)
    (runtime_root / "ducai_runtime.json").write_text(
        json.dumps({"version": "3.8.1"}),
        encoding="utf-8",
    )
    host_script = tmp_path / "tts_engine_host.py"
    host_script.write_text("# fixture", encoding="utf-8")
    monkeypatch.setattr(
        vieneu_module,
        "_probe_runtime_and_profile",
        lambda *_args: {
            "version": "3.8.1",
            "cuda_available": True,
            "cuda_version": "12.8",
            "device_name": "Fixture GPU",
        },
    )

    health = VieNeuTtsProvider(
        runtime_python=Path(sys.executable),
        data_root=data_root,
        profile_path=data_root / "vieneu_profiles" / "clone.npz",
        host_script=host_script,
    ).health()

    assert health.state.value == "ready"
    assert health.details["runtime_ready"] is True
    assert health.details["profile_ready"] is False
    assert "chọn file mẫu" in health.summary


def test_vieneu_health_rejects_runtime_when_cuda_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "data"
    runtime_root = data_root / "vieneu_3_8_runtime"
    runtime_root.mkdir(parents=True)
    (runtime_root / "ducai_runtime.json").write_text(
        json.dumps({"version": "3.8.1"}),
        encoding="utf-8",
    )
    host_script = tmp_path / "tts_engine_host.py"
    host_script.write_text("# fixture", encoding="utf-8")
    monkeypatch.setattr(
        vieneu_module,
        "_probe_runtime_and_profile",
        lambda *_args: {
            "version": "3.8.1",
            "cuda_available": False,
            "cuda_version": "12.8",
            "device_name": "",
        },
    )

    health = VieNeuTtsProvider(
        runtime_python=Path(sys.executable),
        data_root=data_root,
        profile_path=data_root / "vieneu_profiles" / "clone.npz",
        host_script=host_script,
    ).health()

    assert health.state.value == "unavailable"
    assert health.details["cuda_available"] is False
