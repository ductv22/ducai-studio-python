from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


class ByteCrop:
    shape = (4, 4, 3)
    dtype = "uint8"

    def __init__(self, pixels):
        self.pixels = pixels

    def tobytes(self):
        return self.pixels


def test_host_reports_execution_workers_after_inference_fallback(monkeypatch):
    module = _host_module()
    runtime = SimpleNamespace(provider_id="paddle-fast", model_id="original",
                              execution_device="gpu:0", worker_count=3)

    def recognize(frames, _bands):
        runtime.worker_count = 1
        runtime.execution_fallback_reason = "ocr_predictor_timeout"
        runtime.execution_recovery_attempts = 1
        runtime.execution_recovery_reason = "ocr_predictor_exited"
        return [None] * len(frames)

    runtime.recognize_batched = recognize
    video = SimpleNamespace(duration_us=1_000_000, fps=25., close=lambda: None,
                            read_many=lambda times: [ByteCrop(b"x") for _ in times])
    host = module.OcrEngineHost(runtime_factory=lambda *_args: runtime,
                               video_factory=lambda _path: video)
    monkeypatch.setattr(host, "_crop", lambda frame, *_args: frame)
    assert host.open({"mode": "fast", "media_path": "fixture"})["worker_count"] == 3
    result = host.recognize({"timestamps_us": [0], "bands": [{"name": "upper"}]})
    assert result["execution_device"] == "gpu:0"
    assert result["worker_count"] == 1
    assert result["execution_fallback_reason"] == "ocr_predictor_timeout"
    assert result["execution_recovery_attempts"] == 1
    assert result["execution_recovery_reason"] == "ocr_predictor_exited"
    assert result["evidence"] == []
    host.close()


def test_identical_crop_cache_preserves_every_reading_without_reinference():
    host = _host_module().OcrEngineHost()
    calls = []

    def recognize(crops, bands):
        calls.append((crops, bands))
        return [{"text": str(crop.pixels), "confidence": .98} for crop in crops]

    first = host._recognize_cached([ByteCrop(b"a"), ByteCrop(b"a"), ByteCrop(b"b")],
                                    ["dialogue"] * 3, recognize)
    assert len(first) == 3 and first[0] == first[1]
    assert len(calls[0][0]) == 2
    again = host._recognize_cached([ByteCrop(b"a"), ByteCrop(b"b")],
                                    ["dialogue"] * 2, recognize)
    assert again == [first[0], first[2]] and len(calls) == 1
    host._recognize_cached([ByteCrop(b"a"), ByteCrop(b"c")], ["scene", "dialogue"], recognize)
    assert len(calls[1][0]) == 2  # Reading mode and even a one-byte change are independent.
    host.close()
    host._recognize_cached([ByteCrop(b"a")], ["dialogue"], recognize)
    assert len(calls) == 3


def test_reading_cache_bounded_eviction_and_empty_observations():
    host = _host_module().OcrEngineHost()
    host.READING_CACHE_LIMIT = 2
    calls = []

    def recognize(crops, _bands):
        calls.append(len(crops))
        return [None for _ in crops]

    assert host._recognize_cached([ByteCrop(bytes([i])) for i in range(4)],
                                  ["dialogue"] * 4, recognize) == [None] * 4
    assert len(host._reading_cache) == 2
    assert host._recognize_cached([ByteCrop(b"\x03")], ["dialogue"], recognize) == [None]
    assert calls == [4]
    host._recognize_cached([ByteCrop(b"\x00")], ["dialogue"], recognize)
    assert calls == [4, 1]


class ArrayBox:
    """Array protocol used by ML runtimes, without requiring NumPy in GUI tests."""

    def __init__(self, values):
        self.values = values

    def __bool__(self):
        raise ValueError("The truth value of an array is ambiguous")

    def __iter__(self):
        return iter(self.values)

    def tolist(self):
        return self.values


def test_combined_reading_accepts_array_geometry_without_truth_test() -> None:
    module = _host_module()
    reading = module._combine_reading(
        ["保留测试字幕"], [0.99],
        [ArrayBox([[10, 10], [110, 10], [110, 30], [10, 30]])], 120, 40,
    )
    assert reading["text"] == "保留测试字幕"
    assert reading["box"] == pytest.approx([1 / 12, .25, 5 / 6, .5])


@pytest.mark.parametrize("use_boxes", [True, False])
def test_paddle_reading_accepts_array_boxes_and_polygon_fallback(use_boxes: bool) -> None:
    module = _host_module()
    runtime = module.PaddleRuntime.__new__(module.PaddleRuntime)
    data = {
        "rec_texts": ["保留"], "rec_scores": [0.99],
        "rec_boxes": ArrayBox([[10, 10, 110, 30]] if use_boxes else []),
        "rec_polys": ArrayBox([[[10, 10], [110, 10], [110, 30], [10, 30]]]),
    }
    runtime.engine = SimpleNamespace(predict=lambda _frames: [SimpleNamespace(json=data)])
    reading = runtime.recognize([SimpleNamespace(shape=(40, 120, 3))])[0]
    assert reading["text"] == "保留"
    assert reading["box"] == pytest.approx([1 / 12, .25, 5 / 6, .5])


@pytest.mark.parametrize("mode", ["fast", "quality"])
@pytest.mark.parametrize("device", ["cpu", "gpu:0"])
def test_paddle_preserves_serial_constructor_models_and_input_order(monkeypatch, mode, device):
    calls, batches = [], []

    def factory(**kwargs):
        calls.append(kwargs)

        def predict(frames):
            batches.append([frame.identity for frame in frames])
            return [SimpleNamespace(json={"rec_texts": [f"文字{frame.identity}"],
                "rec_scores": [.99], "rec_boxes": [[1, 1, 10, 8]]}) for frame in frames]

        return SimpleNamespace(predict=predict)

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=factory))
    monkeypatch.setitem(sys.modules, "paddle", SimpleNamespace(
        is_compiled_with_cuda=lambda: True, version=SimpleNamespace(cuda=lambda: "12.6"),
        device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: 1,
                                  get_device_capability=lambda _index: (8, 6))),
    ))
    module = _host_module()
    runtime = module.PaddleRuntime(mode, device, 12)
    detection_model, recognition_model = module.PADDLE_MODELS[mode]
    assert calls == [{
        "lang": "ch", "device": device,
        "text_detection_model_name": detection_model,
        "text_recognition_model_name": recognition_model,
        "text_recognition_batch_size": 12,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
    }]
    frames = [SimpleNamespace(shape=(20, 80, 3), identity=i) for i in range(5)]
    readings = runtime.recognize_batched(frames, ["dialogue"] * 5)
    assert batches == [[0, 1, 2, 3, 4]]
    assert [reading["text"] for reading in readings] == [f"文字{i}" for i in range(5)]
    assert runtime.provider_id == f"paddle-{mode}" and runtime.model_id == recognition_model


def test_paddle_keeps_result_count_and_band_validation(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddle", SimpleNamespace(
        is_compiled_with_cuda=lambda: True, version=SimpleNamespace(cuda=lambda: "12.6"),
        device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: 1,
                                  get_device_capability=lambda _index: (8, 6))),
    ))
    engine = SimpleNamespace(predict=lambda _frames: [])
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=lambda **_kw: engine))
    runtime = _host_module().PaddleRuntime("fast", "gpu:0", 12)
    frame = SimpleNamespace(shape=(20, 80, 3))
    with pytest.raises(ValueError, match="paddle_band_count_mismatch"):
        runtime.recognize_batched([frame], [])
    with pytest.raises(RuntimeError, match="paddle_result_count_mismatch"):
        runtime.recognize_batched([frame], ["dialogue"])

    def fail(_frames):
        raise RuntimeError("invalid model weights")

    engine.predict = fail
    with pytest.raises(RuntimeError, match="invalid model weights"):
        runtime.recognize_batched([frame], ["dialogue"])


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
    assert [item["text"] for item in reading["segments"]] == ["我已", "成仙"]


def test_same_subtitle_row_one_pixel_jitter_does_not_rotate_fragments():
    module = _host_module()
    reading = module._combine_reading(
        ["你们面前", "现在的我即使出现在"], [.98, .98],
        [[200, 10, 285, 31], [10, 11, 195, 32]], 300, 50, band="lower",
    )
    assert reading["text"] == "现在的我即使出现在你们面前"
    reading = module._combine_reading(
        ["了", "我就放心"], [.98, .98],
        [[200, 10, 220, 31], [10, 11, 195, 32]], 300, 50, band="upper",
    )
    assert reading["text"] == "我就放心了"


def test_dialogue_mode_keeps_subtitle_row_not_tall_map_columns():
    module = _host_module()
    reading = module._combine_reading(
        ["五指湖", "中心区域有一圈环形山脉", "11a"], [.80, .999, .65],
        [[20, 0, 34, 100], [70, 70, 280, 95], [10, 5, 25, 22]],
        300, 110, band="dialogue",
    )
    assert reading["text"] == "中心区域有一圈环形山脉"
    assert reading["segment_count"] == 1


def test_dialogue_mode_preserves_two_subtitle_rows_and_single_glyph():
    module = _host_module()
    reading = module._combine_reading(
        ["第一行对白", "第二行对白"], [.99, .99],
        [[40, 40, 220, 60], [40, 70, 220, 90]], 300, 110, band="dialogue",
    )
    assert reading["text"] == "第一行对白第二行对白"
    for text in ("爹", "娘", "不过"):
        reading = module._combine_reading(
            [text], [.99], [[120, 60, 140, 85]], 300, 110, band="dialogue",
        )
        assert reading["text"] == text


def test_scene_segments_group_vertical_glyphs_without_merging_distant_logo() -> None:
    module = _host_module()

    grouped = module._group_scene_segments(
        [
            {"text": "海", "confidence": 0.96, "box": [0.78, 0.20, 0.05, 0.06]},
            {"text": "王", "confidence": 0.95, "box": [0.78, 0.28, 0.05, 0.06]},
            {"text": "兽", "confidence": 0.97, "box": [0.78, 0.36, 0.05, 0.06]},
            {"text": "独播", "confidence": 0.98, "box": [0.90, 0.03, 0.08, 0.04]},
        ]
    )

    title = next(item for item in grouped if item["text"] == "海王兽")
    assert title["box"] == pytest.approx([0.78, 0.20, 0.05, 0.22])
    assert any(item["text"] == "独播" for item in grouped)


def test_crop_relative_box_is_mapped_to_full_frame_coordinates() -> None:
    module = _host_module()

    box = module._box_to_full_frame(
        [0.25, 0.5, 0.5, 0.25],
        {"x1": 0.1, "y1": 0.6, "x2": 0.9, "y2": 1.0},
    )

    assert box == pytest.approx([0.3, 0.8, 0.4, 0.1])


def test_scene_segments_join_horizontal_fragments_not_separate_cards():
    module = _host_module()
    grouped = module._group_scene_segments([
        {"text": "太南", "confidence": .95, "box": [.1, .3, .12, .04]},
        {"text": "小会", "confidence": .96, "box": [.23, .301, .12, .04]},
        {"text": "黄枫谷", "confidence": .96, "box": [.70, .3, .15, .04]},
    ])
    assert [row["text"] for row in grouped] == ["太南小会", "黄枫谷"]


def test_scene_vertical_columns_do_not_absorb_neighbor_or_top_horizontal_row():
    module = _host_module()
    grouped = module._group_scene_segments([
        {"text": "弟", "confidence": .95, "box": [.50, .2, .10, .12]},
        {"text": "子", "confidence": .95, "box": [.50, .4, .10, .12]},
        {"text": "一", "confidence": .95, "box": [.50, .6, .10, .12]},
        {"text": "名", "confidence": .95, "box": [.50, .8, .10, .12]},
        {"text": "十塊靈石", "confidence": .95, "box": [.35, .2, .11, .72]},
        {"text": "上方文字", "confidence": .95, "box": [.35, .0, .32, .05]},
    ])
    assert {row["text"] for row in grouped} == {"弟子一名", "十塊靈石", "上方文字"}


@pytest.mark.parametrize("target_box, expected_count", [
    (None, 1), ([.17, .765, .35, .03], 1), ([.17, .765, .35, .13], 0),
])
def test_scene_roi_retains_textured_horizontal_and_empty_frame_probes(
    monkeypatch, target_box, expected_count,
):
    module = _host_module()
    host = module.OcrEngineHost()
    frame = SimpleNamespace(shape=(1080, 1920, 3))
    host.video = SimpleNamespace(read_many=lambda times: [frame] * len(times))
    monkeypatch.setattr(host, "_crop", lambda *args: frame)
    monkeypatch.setattr(module, "_scene_segment_metadata", lambda *args: {
        "scene_confidence": .58, "orientation": "horizontal", "style_hint": "display",
    })
    host.runtime = SimpleNamespace(
        provider_id="paddle-quality", model_id="medium",
        recognize_batched=lambda *args: [
            {"text": "天南", "segments": [{"text": "天南", "confidence": .93,
                                          "box": [.1, .1, .5, .2]}]},
            {"text": "", "segments": []},
        ],
    )
    result = host.recognize({
        "timestamps_us": [0, 400_000],
        "bands": [{"name": "scene-roi-0", "reading_mode": "scene", "x1": .1,
                   "y1": .75, "x2": .8, "y2": .9, "scale": 2.0,
                   "target_box": target_box}],
        "exclusion_regions_by_band": {"scene-roi-0": [[.1, .92, .8, .06]]},
    })
    assert len(result["evidence"]) == expected_count
    if expected_count:
        assert result["evidence"][0]["band"] == "scene"
        assert result["evidence"][0]["box"] == pytest.approx([.17, .765, .35, .03])
    assert len(result["scene_probes"]) == 2


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


@pytest.mark.parametrize(
    "companion",
    ["4K超清漫剧|3473", "呼啦呼啦2313", "小李看漫剧"],
)
def test_combined_reading_removes_same_row_channel_fragment_next_to_bilibili(
    companion: str,
) -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["真正的台词", companion, "bilibilr"],
        [0.99, 0.98, 0.97],
        [
            [20, 12, 360, 58],
            [610, 14, 790, 58],
            [800, 12, 980, 60],
        ],
        1000,
        80,
        band="lower",
    )

    assert reading["text"] == "真正的台词"
    assert reading["segment_count"] == 1


@pytest.mark.parametrize("anchor", ["bilibili", "bilibilr", "bilbili", "billbili"])
def test_combined_reading_accepts_common_bilibili_ocr_variants(anchor: str) -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["真正的台词", "小李看漫剧", anchor],
        [0.99, 0.98, 0.97],
        [[20, 12, 360, 58], [610, 14, 790, 58], [800, 12, 980, 60]],
        1000,
        80,
        band="lower",
    )

    assert reading["text"] == "真正的台词"


@pytest.mark.parametrize(
    "dialogue",
    [
        "我也想看漫剧",
        "今天看漫剧",
        "第2313章开始了",
        "我修炼了1000年",
        "你也想看漫剧吗",
    ],
)
def test_combined_reading_keeps_adjacent_dialogue_near_bilibili(dialogue: str) -> None:
    module = _host_module()

    reading = module._combine_reading(
        [dialogue, "bilibili"],
        [0.99, 0.98],
        [[430, 12, 790, 60], [800, 12, 980, 60]],
        1000,
        80,
        band="lower",
    )

    assert reading["text"] == dialogue
    assert reading["segment_count"] == 1


def test_combined_reading_keeps_distant_channel_like_dialogue() -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["小李看漫剧", "bilibili"],
        [0.99, 0.98],
        [[20, 12, 300, 60], [800, 12, 980, 60]],
        1000,
        80,
        band="lower",
    )

    assert reading["text"] == "小李看漫剧"
    assert reading["segment_count"] == 1


def test_combined_reading_keeps_adjacent_ordinary_comic_dialogue() -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["我也爱看漫画", "bilibili"],
        [0.99, 0.98],
        [[430, 12, 790, 60], [800, 12, 980, 60]],
        1000,
        80,
        band="lower",
    )

    assert reading["text"] == "我也爱看漫画"
    assert reading["segment_count"] == 1


def test_combined_reading_keeps_centered_repair_number_dialogue() -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["修复3473后再走"],
        [0.99],
        [[300, 12, 700, 60]],
        1000,
        80,
        band="lower",
    )

    assert reading["text"] == "修复3473后再走"
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


def test_engine_host_measures_changes_on_the_same_frames_used_for_ocr(monkeypatch) -> None:
    module = _host_module()
    class FakeSmall:
        def __init__(self, value: float) -> None:
            self.value = value

        def astype(self, _dtype):
            return self

        def __truediv__(self, denominator: float):
            return FakeSmall(self.value / denominator)

        def __sub__(self, other):
            return FakeSmall(self.value - other.value)

    class FakeFrame:
        shape = (80, 160, 3)

        def __init__(self, value: float) -> None:
            self.value = value

        def __getitem__(self, _key):
            return self

    fake_numpy = SimpleNamespace(
        float32=float,
        abs=lambda value: FakeSmall(abs(value.value)),
        mean=lambda value: value.value,
        percentile=lambda value, _percentile: value.value,
    )
    fake_cv2 = SimpleNamespace(
        COLOR_BGR2GRAY=1,
        INTER_AREA=2,
        cvtColor=lambda frame, _mode: FakeSmall(frame.value),
        resize=lambda frame, _size, interpolation: frame,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)

    class FakeVideo:
        duration_us = 2_000_000
        fps = 30.0

        def read_many(self, timestamps):
            return [
                FakeFrame(0 if timestamp < 800_000 else 255)
                for timestamp in timestamps
            ]

        def close(self):
            pass

    class FakeRuntime:
        provider_id = "fake-paddle"
        model_id = "fake-model"

        def recognize_batched(self, frames, _bands):
            return [
                {
                    "text": "测试",
                    "confidence": 0.99,
                    "box": None,
                    "segment_count": 1,
                }
                for _frame in frames
            ]

    host = module.OcrEngineHost(
        runtime_factory=lambda *_args: FakeRuntime(),
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
    band = {
        "name": "lower",
        "x1": 0.0,
        "y1": 0.5,
        "x2": 1.0,
        "y2": 1.0,
        "scale": 1.0,
    }

    first = host.recognize(
        {
            "timestamps_us": [0, 400_000],
            "bands": [band],
            "detect_changes": True,
        }
    )
    second = host.recognize(
        {
            "timestamps_us": [800_000, 1_200_000],
            "change_timestamps_us": [600_000, 800_000, 1_000_000, 1_200_000],
            "bands": [band],
            "detect_changes": True,
        }
    )

    assert first["event_timestamps_us"] == []
    assert second["event_timestamps_us"] == [800_000]
    assert second["scene_event_timestamps_us"] == [800_000]


def test_change_probe_reads_intermediate_frames_without_sending_them_to_ocr(
    monkeypatch,
) -> None:
    module = _host_module()

    class FakeSmall:
        def __init__(self, value: float) -> None:
            self.value = value

        def astype(self, _dtype):
            return self

        def __truediv__(self, denominator: float):
            return FakeSmall(self.value / denominator)

        def __sub__(self, other):
            return FakeSmall(self.value - other.value)

    class FakeFrame:
        shape = (80, 160, 3)

        def __init__(self, value: float) -> None:
            self.value = value

        def __getitem__(self, _key):
            return self

    monkeypatch.setitem(
        sys.modules,
        "numpy",
        SimpleNamespace(
            float32=float,
            abs=lambda value: FakeSmall(abs(value.value)),
            mean=lambda value: value.value,
            percentile=lambda value, _percentile: value.value,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(
            COLOR_BGR2GRAY=1,
            INTER_AREA=2,
            cvtColor=lambda frame, _mode: FakeSmall(frame.value),
            resize=lambda frame, _size, interpolation: frame,
        ),
    )

    class FakeVideo:
        duration_us = 1_000_000
        fps = 30.0

        def __init__(self) -> None:
            self.calls = []

        def read_many(self, timestamps):
            self.calls.append(tuple(timestamps))
            return [FakeFrame(255 if timestamp == 200_000 else 0) for timestamp in timestamps]

        def close(self):
            pass

    class FakeRuntime:
        provider_id = "fake-paddle"
        model_id = "fake-model"

        def __init__(self) -> None:
            self.ocr_frame_count = 0

        def recognize_batched(self, frames, _bands):
            self.ocr_frame_count += len(frames)
            return [
                {"text": "测试", "confidence": 0.99, "box": None, "segment_count": 1}
                for _frame in frames
            ]

    video = FakeVideo()
    runtime = FakeRuntime()
    host = module.OcrEngineHost(
        runtime_factory=lambda *_args: runtime,
        video_factory=lambda _path: video,
    )
    host.open(
        {
            "mode": "fast",
            "media_path": str(HOST_PATH),
            "device": "gpu:0",
            "batch_size": 12,
        }
    )
    band = {
        "name": "lower",
        "x1": 0.0,
        "y1": 0.5,
        "x2": 1.0,
        "y2": 1.0,
        "scale": 1.0,
    }

    result = host.recognize(
        {
            "timestamps_us": [0, 400_000],
            "change_timestamps_us": [0, 200_000, 400_000],
            "bands": [band],
            "detect_changes": True,
        }
    )

    assert video.calls == [(0, 200_000, 400_000)]
    assert runtime.ocr_frame_count == 2
    assert 200_000 in result["event_timestamps_us"]


def test_engine_host_emits_watermark_geometry_without_subtitle_evidence(
    monkeypatch,
) -> None:
    module = _host_module()
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())

    class FakeFrame:
        shape = (100, 200, 3)

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

        def recognize_batched(self, frames, bands):
            return [
                {
                    "text": "",
                    "confidence": 0.0,
                    "box": None,
                    "segment_count": 0,
                    "watermarks": [
                        {
                            "text": "小李看漫剧bilibili",
                            "confidence": 0.98,
                            "box": [0.5, 0.2, 0.4, 0.3],
                        }
                    ],
                }
                for _frame in frames
            ]

    host = module.OcrEngineHost(
        runtime_factory=lambda *_args: FakeRuntime(),
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
            "timestamps_us": [400_000],
            "bands": [
                {
                    "name": "lower",
                    "x1": 0.1,
                    "y1": 0.6,
                    "x2": 0.9,
                    "y2": 1.0,
                    "scale": 1,
                }
            ],
        }
    )

    assert result["evidence"] == []
    assert len(result["watermark_detections"]) == 1
    detection = result["watermark_detections"][0]
    assert detection["ignored_overlay"] is True
    assert detection["text"] == "小李看漫剧bilibili"
    assert detection["box"] == pytest.approx([0.5, 0.68, 0.32, 0.12])


def test_engine_host_decodes_mixed_adaptive_bands_in_one_chronological_read(
    monkeypatch,
) -> None:
    module = _host_module()
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())
    monkeypatch.setattr(
        module,
        "_scene_segment_metadata",
        lambda _frame, box, text="": {
            "panel_box": box,
            "orientation": "vertical",
            "style_hint": "calligraphy",
            "background_color": "#111111",
            "scene_confidence": 1.0,
        },
    )

    class FakeFrame:
        shape = (100, 200, 3)

        def __getitem__(self, _key):
            return self

    class FakeVideo:
        duration_us = 5_000_000
        fps = 30.0

        def __init__(self):
            self.calls = []

        def read_many(self, timestamps):
            self.calls.append(tuple(timestamps))
            return [FakeFrame() for _ in timestamps]

        def close(self):
            pass

    class FakeRuntime:
        provider_id = "fake-paddle"
        model_id = "fake-model"

        def recognize_batched(self, frames, bands):
            result = []
            for band in bands:
                if band == "scene":
                    result.append(
                        {
                            "text": "海王兽",
                            "confidence": 0.99,
                            "box": [0.7, 0.2, 0.1, 0.3],
                            "segment_count": 1,
                            "segments": [
                                {
                                    "text": "海王兽",
                                    "confidence": 0.99,
                                    "box": [0.7, 0.2, 0.1, 0.3],
                                }
                            ],
                            "watermarks": [],
                        }
                    )
                else:
                    result.append(
                        {
                            "text": "好吧",
                            "confidence": 0.99,
                            "box": [0.3, 0.7, 0.2, 0.1],
                            "segment_count": 1,
                            "segments": [],
                            "watermarks": [],
                        }
                    )
            return result

    video = FakeVideo()
    host = module.OcrEngineHost(
        runtime_factory=lambda *_args: FakeRuntime(),
        video_factory=lambda _path: video,
    )
    host.open(
        {
            "mode": "fast",
            "media_path": str(HOST_PATH),
            "device": "gpu:0",
            "batch_size": 12,
        }
    )
    subtitle_band = {
        "name": "lower",
        "x1": 0.0,
        "y1": 0.5,
        "x2": 1.0,
        "y2": 1.0,
        "scale": 1.0,
    }
    scene_band = {
        "name": "scene",
        "x1": 0.0,
        "y1": 0.0,
        "x2": 1.0,
        "y2": 1.0,
        "scale": 1.0,
    }

    result = host.recognize(
        {
            "timestamps_us": [1_000_000, 1_200_000, 1_400_000],
            "bands": [subtitle_band, scene_band],
            "band_timestamps_us": {
                "lower": [1_000_000, 1_400_000],
                "scene": [1_200_000],
            },
            "evidence_kinds_by_band": {
                "lower": "burst",
                "scene": "scene",
            },
        }
    )

    assert video.calls == [(1_000_000, 1_200_000, 1_400_000)]
    assert [(item["timestamp_us"], item["evidence_kind"]) for item in result["evidence"]] == [
        (1_000_000, "burst"),
        (1_200_000, "scene"),
        (1_400_000, "burst"),
    ]


def test_scene_text_rejects_glyph_inside_same_frame_watermark(monkeypatch) -> None:
    module = _host_module()
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())
    monkeypatch.setattr(
        module,
        "_scene_segment_metadata",
        lambda _frame, box, text="": {
            "panel_box": box,
            "orientation": "vertical" if box[3] > box[2] else "horizontal",
            "style_hint": "calligraphy",
            "background_color": "#111111",
            "scene_confidence": 1.0,
        },
    )

    class FakeFrame:
        shape = (100, 200, 3)

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

        def recognize_batched(self, frames, _bands):
            return [
                {
                    "text": "独播海王兽",
                    "confidence": 0.99,
                    "box": [0.2, 0.2, 0.7, 0.5],
                    "segment_count": 2,
                    "segments": [
                        {
                            "text": "独播",
                            "confidence": 0.897422,
                            "box": [0.940104, 0.047222, 0.021354, 0.065741],
                        },
                        {
                            "text": "海王兽",
                            "confidence": 0.995,
                            "box": [0.72, 0.24, 0.07, 0.28],
                        },
                    ],
                    "watermarks": [
                        {
                            "text": "bilibili",
                            "confidence": 0.87493,
                            "box": [0.877604, 0.044444, 0.071354, 0.07037],
                        }
                    ],
                }
                for _frame in frames
            ]

    host = module.OcrEngineHost(
        runtime_factory=lambda *_args: FakeRuntime(),
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
            "timestamps_us": [400_000],
            "bands": [
                {
                    "name": "scene",
                    "x1": 0.0,
                    "y1": 0.0,
                    "x2": 1.0,
                    "y2": 1.0,
                    "scale": 1.0,
                }
            ],
            "evidence_kind": "scene",
        }
    )

    assert [item["text"] for item in result["evidence"]] == ["海王兽"]
    assert [item["text"] for item in result["watermark_detections"]] == [
        "bilibili"
    ]


def test_combined_reading_keeps_union_box_for_filtered_watermark() -> None:
    module = _host_module()

    reading = module._combine_reading(
        ["真正的台词", "4K超清漫剧|3473", "bilibili"],
        [0.99, 0.98, 0.97],
        [[20, 12, 360, 58], [610, 14, 790, 58], [800, 12, 980, 60]],
        1000,
        80,
        band="lower",
    )

    assert reading["text"] == "真正的台词"
    assert len(reading["watermarks"]) == 1
    assert reading["watermarks"][0]["box"] == pytest.approx(
        [0.61, 0.15, 0.37, 0.6]
    )


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


def test_crop_masks_full_frame_exclusion_without_mutating_decoded_frame(
    monkeypatch,
) -> None:
    module = _host_module()
    rectangle_calls = []

    class FakeCrop:
        def __init__(self, *, copied: bool = False) -> None:
            self.copied = copied

        def copy(self):
            return FakeCrop(copied=True)

    class FakeFrame:
        shape = (100, 200, 3)

        def __init__(self) -> None:
            self.view = FakeCrop()

        def __getitem__(self, _key):
            return self.view

    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(
            INTER_AREA=2,
            rectangle=lambda crop, start, end, color, thickness: rectangle_calls.append(
                (crop, start, end, color, thickness)
            ),
        ),
    )
    frame = FakeFrame()

    crop = module.OcrEngineHost._crop(
        frame,
        {"name": "scene", "x1": 0.10, "y1": 0.20, "x2": 0.90, "y2": 0.80},
        [[0.25, 0.30, 0.20, 0.20]],
    )

    assert crop.copied is True
    assert frame.view.copied is False
    assert rectangle_calls == [(crop, (30, 10), (70, 30), (0, 0, 0), -1)]
def test_dialogue_row_geometry_uses_pixels_on_wide_shallow_crop():
    reading = _host_module()._combine_reading(
        ["五指洲", "中心区域有一圈环形山脉"], [.485, .9998],
        [[200, 47, 218, 87], [262, 66, 484, 90]], 750, 104, band="dialogue",
    )
    assert reading["text"] == "中心区域有一圈环形山脉"
    assert reading["segment_count"] == 1


def test_dialogue_row_keeps_narrow_single_glyph_beside_long_fragment():
    reading = _host_module()._combine_reading(
        ["川", "你好世界"], [.99, .99], [[10, 70, 20, 95], [23, 70, 153, 95]],
        750, 104, band="dialogue",
    )
    assert reading["text"] == "川你好世界"
    assert reading["segment_count"] == 2
