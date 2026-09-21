from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from ducai_studio.engine_hosts.whisper_engine_host import WhisperEngineHost, serve


def test_whisper_host_uses_pinned_chinese_decode_contract(tmp_path: Path) -> None:
    audio = tmp_path / "dialogue.wav"
    audio.write_bytes(b"wav")
    model_path = tmp_path / "large-v3"
    model_path.mkdir()
    (model_path / "model.bin").write_bytes(b"model")
    captured: dict[str, object] = {}

    class FakeModel:
        def __init__(self, path: str, **kwargs) -> None:
            captured["model_path"] = path
            captured["model_kwargs"] = kwargs

        def transcribe(self, decoded, **kwargs):
            captured["decoded"] = decoded
            captured["transcribe_kwargs"] = kwargs
            word = SimpleNamespace(start=0.5, end=0.9, word="你好", probability=0.9)
            segment = SimpleNamespace(
                start=0.5,
                end=1.0,
                text="你好。",
                avg_logprob=-0.1,
                no_speech_prob=0.01,
                compression_ratio=1.0,
                words=[word],
            )
            return iter([segment]), SimpleNamespace(language="zh", language_probability=0.99)

    decoded = list(range(64_000))
    host = WhisperEngineHost(
        model_factory=FakeModel,
        audio_decoder=lambda *_args, **_kwargs: decoded,
    )
    events = list(
        host.transcribe(
            {
                "audio_path": str(audio),
                "model_path": str(model_path),
                "start_us": 2_000_000,
                "duration_us": 10_000_000,
                "device": "cpu",
                "compute_type": "int8",
                "language": "zh",
                "beam_size": 5,
                "temperature": 0.0,
                "vad_parameters": {
                    "threshold": 0.18,
                    "min_speech_duration_ms": 100,
                    "min_silence_duration_ms": 600,
                    "speech_pad_ms": 500,
                },
            }
        )
    )

    assert [event for event, _payload in events] == ["metadata", "segment", "complete"]
    assert captured["model_path"] == str(model_path.resolve())
    assert captured["model_kwargs"] == {
        "device": "cpu",
        "compute_type": "int8",
        "local_files_only": True,
    }
    assert captured["decoded"] == decoded[32_000:]
    kwargs = captured["transcribe_kwargs"]
    assert kwargs["language"] == "zh"
    assert kwargs["beam_size"] == 5
    assert kwargs["temperature"] == 0.0
    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["word_timestamps"] is True
    assert kwargs["vad_filter"] is True
    assert kwargs["vad_parameters"]["min_silence_duration_ms"] == 600
    assert events[1][1]["start_us"] == 2_500_000
    assert events[1][1]["end_us"] == 3_000_000


def test_whisper_host_protocol_streams_each_segment(tmp_path: Path) -> None:
    class FakeHost:
        def transcribe(self, _payload):
            yield "metadata", {"device": "cpu"}
            yield "segment", {"text": "你好", "start_us": 0, "end_us": 1_000_000}
            yield "complete", {"segment_count": 1}

    input_stream = StringIO(
        '{"protocol_version":1,"id":"one","action":"transcribe","payload":{}}\n'
    )
    output_stream = StringIO()

    assert serve(input_stream, output_stream, FakeHost()) == 0
    lines = output_stream.getvalue().splitlines()
    assert len(lines) == 3
    assert '"event":"metadata"' in lines[0]
    assert '"event":"segment"' in lines[1]
    assert '"event":"complete"' in lines[2]
