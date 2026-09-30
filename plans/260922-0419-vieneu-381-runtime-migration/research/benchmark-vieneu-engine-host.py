from __future__ import annotations

import argparse
import hashlib
import json
import time
import wave
from pathlib import Path

from ducai_studio.providers.tts import TtsCueRequest, VieNeuTtsProvider
from ducai_studio.providers.tts.vieneu import profile_fingerprint

TEXTS = (
    "Xin chào, đây là phép đo tốc độ tổng hợp giọng nói tiếng Việt.",
    "Hệ thống cần giữ đúng nội dung, dấu câu và nhịp đọc tự nhiên.",
    "Mỗi câu được tạo thành một tệp âm thanh riêng để kiểm tra đầy đủ.",
    "Bộ xử lý đồ họa tăng tốc phần suy luận nhưng không được làm mất tiếng.",
    "Kết quả sau nâng cấp phải nhanh, ổn định và có thể tiếp tục từ bộ nhớ đệm.",
    "Những câu dài vẫn phải rõ chữ, không lặp âm và không bị cắt ở cuối.",
    "Cùng một giọng mẫu được dùng cho cả hai lần đo trước và sau nâng cấp.",
    "Đây là câu cuối của bộ kiểm thử ngắn dành cho VieNeu TTS Turbo.",
)


def _wav_receipt(path: Path) -> dict[str, int]:
    with wave.open(str(path), "rb") as stream:
        frames = stream.getnframes()
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        sample_width = stream.getsampwidth()
    return {
        "bytes": path.stat().st_size,
        "frames": frames,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "duration_us": round(frames * 1_000_000 / sample_rate),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve(strict=True)
    output_json = args.output_json.expanduser().resolve()
    artifact_dir = args.artifact_dir.expanduser().resolve()
    warmup_dir = artifact_dir / "warmup"
    measured_dir = artifact_dir / "measured"
    warmup_dir.mkdir(parents=True, exist_ok=True)
    measured_dir.mkdir(parents=True, exist_ok=True)

    provider = VieNeuTtsProvider(data_root=data_root, timeout_seconds=1_800, batch_size=32)
    health = provider.health()
    if health.state.value != "ready":
        raise RuntimeError(health.summary)

    metadata = json.loads(
        (provider.runtime_root / "ducai_runtime.json").read_text(encoding="utf-8-sig")
    )
    profile_hash = profile_fingerprint(provider.profile_path)
    binding = provider.inspect_reference_binding()

    load_started = time.perf_counter()
    provider._ensure_host(load_model=True)  # noqa: SLF001 - benchmark production host
    load_seconds = time.perf_counter() - load_started

    warmup = TtsCueRequest(
        "warmup",
        1,
        0,
        10_000_000,
        TEXTS[0],
        hashlib.sha256(b"vieneu-benchmark-warmup-v1").hexdigest(),
    )
    warmup_started = time.perf_counter()
    warmup_result = provider.synthesize_batch(
        request_id="benchmark-warmup",
        cues=(warmup,),
        voice_id=provider.profile_path.stem,
        output_dir=warmup_dir,
        batch_size=1,
    )
    warmup_seconds = time.perf_counter() - warmup_started
    if warmup_result.failures or len(warmup_result.artifacts) != 1:
        raise RuntimeError(f"Warm-up failed: {warmup_result.failures}")

    cues = tuple(
        TtsCueRequest(
            f"cue-{index:02d}",
            index,
            (index - 1) * 10_000_000,
            index * 10_000_000,
            text,
            hashlib.sha256(f"vieneu-benchmark-v1:{index}:{text}".encode()).hexdigest(),
        )
        for index, text in enumerate(TEXTS, start=1)
    )
    measured_started = time.perf_counter()
    measured_result = provider.synthesize_batch(
        request_id="benchmark-measured",
        cues=cues,
        voice_id=provider.profile_path.stem,
        output_dir=measured_dir,
        batch_size=8,
    )
    measured_seconds = time.perf_counter() - measured_started
    diagnostic_tail = provider._host.diagnostic_tail() if provider._host else ""  # noqa: SLF001
    provider.close()
    if measured_result.failures or len(measured_result.artifacts) != len(cues):
        raise RuntimeError(f"Measured batch failed: {measured_result.failures}")

    wavs = [_wav_receipt(item.wav_path) for item in measured_result.artifacts]
    if any(item["sample_rate"] != 48_000 or item["frames"] <= 0 for item in wavs):
        raise RuntimeError("Benchmark produced an invalid or non-48 kHz WAV")
    audio_seconds = sum(item["duration_us"] for item in wavs) / 1_000_000
    receipt = {
        "schema": "ducai-vieneu-benchmark-v1",
        "runtime_version": str(metadata.get("version", "")),
        "provider_id": provider.provider_id,
        "model_id": provider.model_id,
        "profile_sha256": profile_hash,
        "binding": binding,
        "cue_count": len(cues),
        "load_seconds": round(load_seconds, 6),
        "warmup_seconds": round(warmup_seconds, 6),
        "measured_seconds": round(measured_seconds, 6),
        "audio_seconds": round(audio_seconds, 6),
        "rtf": round(measured_seconds / audio_seconds, 6),
        "batch_metadata": dict(measured_result.metadata),
        "wavs": wavs,
        "host_diagnostic_tail": diagnostic_tail[-2_000:],
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
