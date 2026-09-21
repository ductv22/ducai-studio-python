from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HOST_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "ducai_studio"
    / "engine_hosts"
    / "ocr_engine_host.py"
)


def _host_module():
    spec = importlib.util.spec_from_file_location("ducai_ocr_engine_host", HOST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_combined_reading_joins_split_chinese_segments_in_reading_order() -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["成仙", "我已"],
        [0.90, 0.98],
        [[60, 10, 100, 30], [10, 10, 50, 30]],
        120,
        40,
    )

    assert reading["text"] == "我已成仙"
    assert reading["segment_count"] == 2
    assert reading["confidence"] > 0.93
    assert reading["box"] == [1 / 12, 0.25, 0.75, 0.5]


def test_combined_reading_removes_platform_overlay_segment_from_lower_band() -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["法术这么贵", "4k超清修复2802 bilibili"],
        [0.99, 0.98],
        [[220, 4, 460, 42], [0, 30, 180, 68]],
        600,
        80,
        band="lower",
    )

    assert reading["text"] == "法术这么贵"
    assert reading["segment_count"] == 1


def test_combined_reading_uses_bottom_subtitle_not_scene_sign_in_upper_band() -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["三十颗下品灵石", "￥300元", "法术这么贵"],
        [0.999, 0.991, 0.995],
        [[220, 0, 400, 42], [450, 52, 560, 89], [180, 62, 420, 98]],
        600,
        100,
        band="upper",
    )

    assert reading["text"] == "法术这么贵"
    assert reading["segment_count"] == 1


def test_combined_reading_keeps_high_subtitle_when_bottommost_in_upper_band() -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["她都记在心里"],
        [0.998],
        [[150, 38, 450, 76]],
        600,
        100,
        band="upper",
    )

    assert reading["text"] == "她都记在心里"
    assert reading["segment_count"] == 1


def test_engine_protocol_is_versioned_and_does_not_emit_tracebacks() -> None:
    module = _host_module()

    class FakeHost:
        def health(self):
            return {"ready": True}

        def close(self):
            pass

    requests = "\n".join(
        (
            json.dumps(
                {
                    "protocol_version": 1,
                    "id": "health-1",
                    "action": "health",
                    "payload": {},
                }
            ),
            json.dumps(
                {
                    "protocol_version": 9,
                    "id": "bad-1",
                    "action": "health",
                    "payload": {},
                }
            ),
        )
    )
    output = io.StringIO()

    assert module.serve(io.StringIO(requests), output, FakeHost()) == 0

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["result"] == {"ready": True}
    assert responses[1]["error"]["code"] == "unsupported_protocol_version"
    assert "traceback" not in output.getvalue().casefold()


def test_video_session_seeks_sparse_windows_but_decodes_nearby_frames(monkeypatch) -> None:
    module = _host_module()
    fake_cv2 = SimpleNamespace(
        CAP_PROP_FPS=5,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_POS_FRAMES=1,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    class FakeCapture:
        def __init__(self, _path: str) -> None:
            self.position = 0
            self.set_positions: list[int] = []
            self.grab_count = 0

        def isOpened(self) -> bool:
            return True

        def get(self, key: int) -> float:
            if key == fake_cv2.CAP_PROP_FPS:
                return 30.0
            return 30_000.0

        def set(self, _key: int, value: int) -> bool:
            self.position = int(value)
            self.set_positions.append(int(value))
            return True

        def grab(self) -> bool:
            self.position += 1
            self.grab_count += 1
            return True

        def read(self):
            self.position += 1
            return True, object()

        def release(self) -> None:
            pass

    media = HOST_PATH
    capture = FakeCapture(str(media))
    session = module.VideoSession(media, capture_factory=lambda _path: capture)

    session.read_many([0, 800_000, 120_000_000])

    assert capture.grab_count == 23
    assert capture.set_positions == [3_600]


def test_engine_host_batches_upper_and_lower_bands_in_one_runtime_call(monkeypatch) -> None:
    module = _host_module()
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())

    class FakeFrame:
        shape = (100, 100, 3)

        def __getitem__(self, _key):
            return self

    class FakeVideo:
        duration_us = 5_000_000
        fps = 30.0

        def read_many(self, timestamps):
            return [FakeFrame() for _ in timestamps]

        def close(self):
            pass

    class FakeRuntime:
        provider_id = "fake-paddle"
        model_id = "fake-model"

        def __init__(self):
            self.calls: list[tuple[int, tuple[str, ...]]] = []

        def recognize_batched(self, frames, bands):
            self.calls.append((len(frames), tuple(bands)))
            return [
                {
                    "text": f"cue-{index}",
                    "confidence": 0.99,
                    "box": None,
                    "segment_count": 1,
                }
                for index in range(len(frames))
            ]

    runtime = FakeRuntime()
    host = module.OcrEngineHost(
        runtime_factory=lambda *_args: runtime,
        video_factory=lambda _path: FakeVideo(),
    )
    host.open(
        {
            "mode": "fast",
            "media_path": str(HOST_PATH),
            "device": "gpu:0",
            "batch_size": 12,
        }
    )

    result = host.recognize(
        {
            "timestamps_us": [0, 400_000],
            "bands": [
                {"name": "upper", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "scale": 1},
                {"name": "lower", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "scale": 1},
            ],
        }
    )

    assert runtime.calls == [
        (4, ("upper", "lower", "upper", "lower")),
    ]
    assert len(result["evidence"]) == 4
    assert [row["band"] for row in result["evidence"]] == [
        "upper",
        "lower",
        "upper",
        "lower",
    ]


def test_video_session_decodes_forward_when_sparse_seek_is_unsupported(monkeypatch) -> None:
    module = _host_module()
    fake_cv2 = SimpleNamespace(
        CAP_PROP_FPS=5,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_POS_FRAMES=1,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    class FakeCapture:
        def __init__(self, _path: str) -> None:
            self.position = 0
            self.set_positions: list[int] = []
            self.grab_count = 0

        def isOpened(self) -> bool:
            return True

        def get(self, key: int) -> float:
            if key == fake_cv2.CAP_PROP_FPS:
                return 30.0
            return 30_000.0

        def set(self, _key: int, value: int) -> bool:
            self.set_positions.append(int(value))
            return False

        def grab(self) -> bool:
            self.position += 1
            self.grab_count += 1
            return True

        def read(self):
            frame = self.position
            self.position += 1
            return True, frame

        def release(self) -> None:
            pass

    media = HOST_PATH
    capture = FakeCapture(str(media))
    session = module.VideoSession(media, capture_factory=lambda _path: capture)

    frames = session.read_many([3_333_333])

    assert frames == [100]
    assert capture.set_positions == [100]
    assert capture.grab_count == 100
    assert session._next_frame_index == 101


def test_video_session_decodes_final_frame_for_timestamp_at_duration(monkeypatch) -> None:
    module = _host_module()
    fake_cv2 = SimpleNamespace(
        CAP_PROP_FPS=5,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_POS_FRAMES=1,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    class FakeCapture:
        def __init__(self, _path: str) -> None:
            self.position = 0
            self.set_positions: list[int] = []

        def isOpened(self) -> bool:
            return True

        def get(self, key: int) -> float:
            if key == fake_cv2.CAP_PROP_FPS:
                return 30.0
            return 30.0

        def set(self, _key: int, value: int) -> bool:
            self.position = int(value)
            self.set_positions.append(int(value))
            return True

        def grab(self) -> bool:
            self.position += 1
            return self.position <= 30

        def read(self):
            if self.position >= 30:
                return False, None
            frame = self.position
            self.position += 1
            return True, frame

        def release(self) -> None:
            pass

    media = HOST_PATH
    capture = FakeCapture(str(media))
    session = module.VideoSession(media, capture_factory=lambda _path: capture)

    frames = session.read_many([session.duration_us])

    assert frames == [29]
    assert session._next_frame_index == 30
    assert capture.set_positions == []


def test_video_session_rejects_timestamp_beyond_eof_tolerance(monkeypatch) -> None:
    module = _host_module()
    fake_cv2 = SimpleNamespace(
        CAP_PROP_FPS=5,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_POS_FRAMES=1,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    class FakeCapture:
        def __init__(self, _path: str) -> None:
            self.read_count = 0

        def isOpened(self) -> bool:
            return True

        def get(self, key: int) -> float:
            if key == fake_cv2.CAP_PROP_FPS:
                return 30.0
            return 30.0

        def set(self, _key: int, _value: int) -> bool:
            return True

        def grab(self) -> bool:
            return True

        def read(self):
            self.read_count += 1
            return True, object()

        def release(self) -> None:
            pass

    media = HOST_PATH
    capture = FakeCapture(str(media))
    session = module.VideoSession(media, capture_factory=lambda _path: capture)

    try:
        session.read_many([session.duration_us + 33_334])
    except RuntimeError as exc:
        assert str(exc) == "video_timestamp_out_of_range"
    else:
        raise AssertionError("Timestamp beyond the one-frame EOF tolerance must fail")

    assert capture.read_count == 0


def test_video_session_rejects_backward_read_when_seek_is_unsupported(monkeypatch) -> None:
    module = _host_module()
    fake_cv2 = SimpleNamespace(
        CAP_PROP_FPS=5,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_POS_FRAMES=1,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    class FakeCapture:
        def __init__(self, _path: str) -> None:
            self.position = 0

        def isOpened(self) -> bool:
            return True

        def get(self, key: int) -> float:
            if key == fake_cv2.CAP_PROP_FPS:
                return 30.0
            return 30_000.0

        def set(self, _key: int, _value: int) -> bool:
            return False

        def grab(self) -> bool:
            self.position += 1
            return True

        def read(self):
            frame = self.position
            self.position += 1
            return True, frame

        def release(self) -> None:
            pass

    media = HOST_PATH
    session = module.VideoSession(media, capture_factory=FakeCapture)
    session.read_many([333_333])

    try:
        session.read_many([166_667])
    except RuntimeError as exc:
        assert str(exc) == "video_seek_failed"
    else:
        raise AssertionError("Backward read must fail when the backend cannot seek")
