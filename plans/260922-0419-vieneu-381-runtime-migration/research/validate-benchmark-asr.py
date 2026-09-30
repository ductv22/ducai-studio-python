from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import re
import site
import unicodedata
from pathlib import Path

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


def _configure_cuda_paths() -> None:
    handles = []
    for root in site.getsitepackages():
        for package in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            path = Path(root) / "nvidia" / package / "bin"
            if not path.is_dir():
                continue
            os.environ["PATH"] = str(path.resolve()) + os.pathsep + os.environ.get("PATH", "")
            if os.name == "nt" and hasattr(os, "add_dll_directory"):
                with contextlib.suppress(OSError):
                    handles.append(os.add_dll_directory(str(path.resolve())))
    globals()["_DLL_HANDLES"] = handles


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFC", value).casefold()
    return " ".join(re.findall(r"[\wÀ-ỹ]+", value, flags=re.UNICODE))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _configure_cuda_paths()

    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(args.model.resolve(strict=True)),
        device="cuda",
        compute_type="float16",
        local_files_only=True,
    )
    wavs = sorted(args.audio_dir.resolve(strict=True).glob("*.wav"))
    if len(wavs) != len(TEXTS):
        raise RuntimeError(f"Expected {len(TEXTS)} WAVs, found {len(wavs)}")
    items = []
    for expected, wav in zip(TEXTS, wavs, strict=True):
        segments, info = model.transcribe(
            str(wav),
            language="vi",
            beam_size=5,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        ratio = difflib.SequenceMatcher(
            None, _normalized(expected), _normalized(transcript)
        ).ratio()
        items.append(
            {
                "file": wav.name,
                "expected": expected,
                "transcript": transcript,
                "similarity": round(ratio, 6),
                "language": str(getattr(info, "language", "")),
            }
        )
    minimum = min(item["similarity"] for item in items)
    average = sum(item["similarity"] for item in items) / len(items)
    result = {
        "schema": "ducai-vieneu-benchmark-asr-v1",
        "engine": "faster-whisper-large-v3",
        "device": "cuda",
        "cue_count": len(items),
        "minimum_similarity": round(minimum, 6),
        "average_similarity": round(average, 6),
        "items": items,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if minimum >= 0.6 and average >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
