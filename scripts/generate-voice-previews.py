from __future__ import annotations

import argparse
import tempfile
import wave
from pathlib import Path

from ducai_studio.infrastructure.voice_installer import PiperVoiceInstaller
from ducai_studio.providers.tts import PiperTtsProvider, VieNeuTtsProvider, VoiceCatalog


def _duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / stream.getframerate()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate licensed DUCAI voice previews")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    catalog = VoiceCatalog(data_root, preview_root=output)

    vieneu = VieNeuTtsProvider(data_root=data_root, batch_size=1)
    try:
        presets = catalog.vieneu_presets(installed=True)
        for index, entry in enumerate(presets, 1):
            target = Path(entry.preview_path)
            if target.is_file() and 5.0 <= _duration_seconds(target) <= 8.0:
                print(
                    f"VieNeu {index}/{len(presets)}: {entry.name} "
                    f"(giữ {_duration_seconds(target):.3f}s)",
                    flush=True,
                )
                continue
            print(f"VieNeu {index}/{len(presets)}: {entry.name}", flush=True)
            vieneu.preview(
                text=entry.preview_text,
                voice_id=entry.voice_id,
                output_path=target,
            )
            print(f"  {target.name}: {_duration_seconds(target):.3f}s", flush=True)
    finally:
        vieneu.close()

    runtime_python = data_root / "piper_runtime" / ".venv" / "Scripts" / "python.exe"
    piper = PiperTtsProvider(data_root=data_root, runtime_python=runtime_python)
    try:
        entry = catalog.get("vi_VN-vais1000-medium", require_installed=True)
        piper.preview(
            text=entry.preview_text,
            voice_id=entry.voice_id,
            output_path=Path(entry.preview_path),
        )
        print(
            f"Piper {entry.name}: {_duration_seconds(Path(entry.preview_path)):.3f}s",
            flush=True,
        )
    finally:
        piper.close()

    with tempfile.TemporaryDirectory(prefix="ducai-vivos-preview-") as raw_temp:
        temp_root = Path(raw_temp)
        temp_catalog = VoiceCatalog(temp_root, preview_root=output)
        PiperVoiceInstaller(temp_catalog).install("vi_VN-vivos-x_low")
        entry = temp_catalog.get("vi_VN-vivos-x_low", require_installed=True)
        piper = PiperTtsProvider(data_root=temp_root, runtime_python=runtime_python)
        try:
            piper.preview(
                text=entry.preview_text,
                voice_id=entry.voice_id,
                output_path=Path(entry.preview_path),
            )
            print(
                f"Piper {entry.name}: {_duration_seconds(Path(entry.preview_path)):.3f}s",
                flush=True,
            )
        finally:
            piper.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
