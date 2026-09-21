"""Version-neutral Paddle/Rapid OCR process.

This file intentionally avoids importing the DUCAI application package.  It is
executed directly by the Python 3.11 ML environment while the GUI and worker
runtime stay on Python 3.12.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

PROTOCOL_VERSION = 1
PADDLE_MODELS = {
    "fast": ("PP-OCRv6_tiny_det", "PP-OCRv6_small_rec"),
    "quality": ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
}


def _values(value):
    return list(value) if value is not None else []


def _normalized_box(raw, width, height):
    try:
        values = list(raw)
        if len(values) == 4 and all(isinstance(value, (int, float)) for value in values):
            x1, y1, x2, y2 = (float(value) for value in values)
        else:
            points = [tuple(float(coordinate) for coordinate in point) for point in values]
            x1 = min(point[0] for point in points)
            y1 = min(point[1] for point in points)
            x2 = max(point[0] for point in points)
            y2 = max(point[1] for point in points)
        left = max(0.0, x1 / width)
        top = max(0.0, y1 / height)
        right = min(1.0, x2 / width)
        bottom = min(1.0, y2 / height)
        if right <= left or bottom <= top:
            return None
        return [left, top, right - left, bottom - top]
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return None


def _box_sort_key(raw):
    try:
        values = list(raw)
        if len(values) == 4 and all(isinstance(value, (int, float)) for value in values):
            x1, y1, _x2, y2 = (float(value) for value in values)
        else:
            points = [tuple(float(coordinate) for coordinate in point) for point in values]
            x1 = min(point[0] for point in points)
            y1 = min(point[1] for point in points)
            y2 = max(point[1] for point in points)
        return ((y1 + y2) / 2.0, x1)
    except (IndexError, TypeError, ValueError):
        return (0.0, 0.0)


def _probable_platform_overlay(text):
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if re.search(r"修复\d{3,5}", compact):
        return True
    return any(
        SequenceMatcher(None, word, "bilibili").ratio() >= 0.70
        for word in re.findall(r"[a-z]{5,12}", compact)
    )


def _keep_segment(text, raw_box, width, height, band):
    return not _probable_platform_overlay(text)


def _combine_reading(texts, scores, boxes, width, height, *, band=""):
    rows = []
    for index, raw_text in enumerate(texts):
        text = str(raw_text or "").strip()
        if not text:
            continue
        score = float(scores[index]) if index < len(scores) else 0.0
        raw_box = boxes[index] if index < len(boxes) else None
        if not _keep_segment(text, raw_box, width, height, band):
            continue
        normalized = _normalized_box(raw_box, width, height)
        rows.append(
            (
                _box_sort_key(raw_box or (0, 0, width, height)),
                text,
                score,
                raw_box,
                normalized,
            )
        )
    if not rows:
        return None
    if band == "upper":
        # The upper crop intentionally reaches high enough to catch subtitles
        # whose vertical position changes between episodes.  Select the
        # bottom-most text row relative to the crop contents instead of using
        # an absolute 0.92 cutoff: that cutoff discarded valid subtitles which
        # ended around 0.70-0.85 inside this crop.
        bottoms = [
            item[4][1] + item[4][3]
            for item in rows
            if item[4] is not None
        ]
        if bottoms:
            bottom_floor = max(bottoms) - 0.08
            rows = [
                item
                for item in rows
                if item[4] is None or item[4][1] + item[4][3] >= bottom_floor
            ]
    rows.sort(key=lambda item: item[0])
    text = "".join(item[1] for item in rows)
    weight = sum(max(1, len(item[1])) for item in rows)
    confidence = sum(item[2] * max(1, len(item[1])) for item in rows) / max(1, weight)
    normalized = [
        _normalized_box(item[3], width, height)
        for item in rows
        if item[3] is not None
    ]
    normalized = [item for item in normalized if item is not None]
    union = None
    if normalized:
        left = min(item[0] for item in normalized)
        top = min(item[1] for item in normalized)
        right = max(item[0] + item[2] for item in normalized)
        bottom = max(item[1] + item[3] for item in normalized)
        union = [left, top, right - left, bottom - top]
    return {
        "text": text,
        "confidence": max(0.0, min(1.0, confidence)),
        "box": union,
        "segment_count": len(rows),
    }


class PaddleRuntime:
    def __init__(self, mode, device, batch_size):
        from paddleocr import PaddleOCR

        detection_model, recognition_model = PADDLE_MODELS[mode]
        self.provider_id = "paddle-" + mode
        self.model_id = recognition_model
        self.engine = PaddleOCR(
            lang="ch",
            device=device,
            text_detection_model_name=detection_model,
            text_recognition_model_name=recognition_model,
            text_recognition_batch_size=batch_size,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

    def _recognize_frames(self, frames, bands):
        if len(frames) != len(bands):
            raise ValueError("paddle_band_count_mismatch")
        results = list(self.engine.predict(list(frames)))
        if len(results) != len(frames):
            raise RuntimeError("paddle_result_count_mismatch")
        readings = []
        for frame, result, band in zip(frames, results, bands, strict=True):
            payload = result.json
            if callable(payload):
                payload = payload()
            data = payload.get("res", payload)
            height, width = frame.shape[:2]
            readings.append(
                _combine_reading(
                    _values(data.get("rec_texts")),
                    _values(data.get("rec_scores")),
                    _values(data.get("rec_boxes") or data.get("rec_polys")),
                    width,
                    height,
                    band=band,
                )
            )
        return readings

    def recognize(self, frames, band=""):
        return self._recognize_frames(frames, [band] * len(frames))

    def recognize_batched(self, frames, bands):
        """Recognize crops from multiple subtitle bands in one model call.

        Paddle's detector has a non-trivial per-call cost.  The old path made
        one ``predict`` call for the upper band and another for the lower band
        for every OCR window.  Keeping the band label next to each crop lets
        us submit both bands together while preserving the exact filtering and
        evidence identity of the old path.
        """

        return self._recognize_frames(frames, list(bands))


class RapidRuntime:
    provider_id = "rapid-compatibility"
    model_id = "PP-OCRv6-tiny-small"

    def __init__(self, _mode, _device, _batch_size):
        from rapidocr import RapidOCR

        self.engine = RapidOCR()

    def recognize(self, frames, band=""):
        readings = []
        for frame in frames:
            output = self.engine(frame)
            height, width = frame.shape[:2]
            readings.append(
                _combine_reading(
                    _values(getattr(output, "txts", None)),
                    _values(getattr(output, "scores", None)),
                    _values(getattr(output, "boxes", None)),
                    width,
                    height,
                    band=band,
                )
            )
        return readings

    def recognize_batched(self, frames, bands):
        # RapidOCR has no batch API.  Keep the method so the host can use one
        # code path and preserve the existing per-crop behavior as a fallback.
        if len(frames) != len(bands):
            raise ValueError("rapid_band_count_mismatch")
        return [
            reading
            for frame, band in zip(frames, bands, strict=True)
            for reading in self.recognize([frame], band)
        ]


class VideoSession:
    # Sparse jumps use keyframe seeks while normal OCR windows are decoded
    # forward. The cursor is already at the final sample of the previous
    # window, so adjacent windows do not incur a seek.
    MAX_SEQUENTIAL_SKIP_SECONDS = 3.0

    def __init__(self, path, capture_factory=None):
        if capture_factory is None:
            import cv2

            capture_factory = cv2.VideoCapture
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.capture = capture_factory(str(self.path))
        if not self.capture.isOpened():
            raise RuntimeError("video_open_failed")
        import cv2

        self._cv2 = cv2
        self.fps = float(self.capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if self.fps <= 0:
            raise RuntimeError("video_fps_unavailable")
        self.frame_count = int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.duration_us = int(round(self.frame_count * 1_000_000 / self.fps))
        self._next_frame_index = 0

    def close(self):
        self.capture.release()

    def read_many(self, timestamps_us):
        frames = []
        for timestamp_us in timestamps_us:
            target = max(0, int(round(int(timestamp_us) * self.fps / 1_000_000)))
            if self.frame_count > 0 and target >= self.frame_count:
                if target == self.frame_count:
                    # Container durations commonly point to the boundary immediately
                    # after the final frame. Decode the final valid frame in that case.
                    target = self.frame_count - 1
                else:
                    raise RuntimeError("video_timestamp_out_of_range")
            sequential_limit = max(
                1, int(round(self.fps * self.MAX_SEQUENTIAL_SKIP_SECONDS))
            )
            if (
                target < self._next_frame_index
                or target - self._next_frame_index > sequential_limit
            ):
                seeked = self.capture.set(self._cv2.CAP_PROP_POS_FRAMES, target)
                if seeked:
                    self._next_frame_index = target
                elif target < self._next_frame_index:
                    raise RuntimeError("video_seek_failed")
            while self._next_frame_index < target:
                if not self.capture.grab():
                    raise RuntimeError("video_decode_failed")
                self._next_frame_index += 1
            ok, frame = self.capture.read()
            if not ok or frame is None:
                raise RuntimeError("video_decode_failed")
            self._next_frame_index += 1
            frames.append(frame)
        return frames


class OcrEngineHost:
    def __init__(self, runtime_factory=None, video_factory=None):
        self.runtime_factory = runtime_factory
        self.video_factory = video_factory
        self.runtime = None
        self.video = None
        self.mode = ""

    def close(self):
        if self.video is not None:
            self.video.close()
        self.video = None
        self.runtime = None

    def health(self):
        import cv2

        return {
            "protocol_version": PROTOCOL_VERSION,
            "python": sys.version.split()[0],
            "opencv": cv2.__version__,
            "modes": ["fast", "quality", "compatibility"],
        }

    def open(self, payload):
        self.close()
        mode = payload.get("mode", "fast")
        if mode not in {"fast", "quality", "compatibility"}:
            raise ValueError("unsupported_ocr_mode")
        batch_size = payload.get("batch_size", 12)
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("invalid_batch_size")
        device = payload.get("device", "gpu:0")
        if not isinstance(device, str) or not device:
            raise ValueError("invalid_device")
        video_factory = self.video_factory or VideoSession
        self.video = video_factory(payload["media_path"])
        runtime_factory = self.runtime_factory
        if runtime_factory is None:
            runtime_factory = RapidRuntime if mode == "compatibility" else PaddleRuntime
        self.runtime = runtime_factory(mode, device, batch_size)
        self.mode = mode
        return {
            "provider_id": self.runtime.provider_id,
            "model_id": self.runtime.model_id,
            "duration_us": self.video.duration_us,
            "fps": self.video.fps,
        }

    @staticmethod
    def _crop(frame, band):
        import cv2

        height, width = frame.shape[:2]
        x1 = max(0, min(width - 1, int(round(float(band["x1"]) * width))))
        x2 = max(x1 + 1, min(width, int(round(float(band["x2"]) * width))))
        y1 = max(0, min(height - 1, int(round(float(band["y1"]) * height))))
        y2 = max(y1 + 1, min(height, int(round(float(band["y2"]) * height))))
        crop = frame[y1:y2, x1:x2]
        scale = float(band.get("scale", 1.0))
        if abs(scale - 1.0) > 0.001:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        return crop

    def recognize(self, payload):
        if self.video is None or self.runtime is None:
            raise RuntimeError("engine_not_open")
        timestamps = payload.get("timestamps_us")
        bands = payload.get("bands")
        if not isinstance(timestamps, list) or not timestamps:
            raise ValueError("timestamps_required")
        if not all(type(value) is int and value >= 0 for value in timestamps):
            raise ValueError("invalid_timestamp")
        if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
            raise ValueError("timestamps_not_strictly_increasing")
        if not isinstance(bands, list) or not bands:
            raise ValueError("bands_required")
        frames = self.video.read_many(timestamps)
        normalized_bands = []
        for band in bands:
            name = band.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("invalid_band")
            normalized_bands.append((name, band))

        # Preserve the old fallback for third-party/test runtimes that only
        # implement ``recognize``.  The bundled Paddle and Rapid runtimes use
        # the batched method, avoiding one detector invocation per subtitle band.
        readings_by_band = []
        recognize_batched = getattr(self.runtime, "recognize_batched", None)
        if callable(recognize_batched):
            crops = []
            crop_bands = []
            for frame in frames:
                for name, band in normalized_bands:
                    crops.append(self._crop(frame, band))
                    crop_bands.append(name)
            readings = recognize_batched(crops, crop_bands)
            expected = len(frames) * len(normalized_bands)
            if len(readings) != expected:
                raise RuntimeError("ocr_result_count_mismatch")
            for frame_index, timestamp_us in enumerate(timestamps):
                for band_index, (name, _band) in enumerate(normalized_bands):
                    readings_by_band.append(
                        (
                            timestamp_us,
                            name,
                            readings[
                                frame_index * len(normalized_bands) + band_index
                            ],
                        )
                    )
        else:
            for name, band in normalized_bands:
                crops = [self._crop(frame, band) for frame in frames]
                readings = self.runtime.recognize(crops, name)
                if len(readings) != len(timestamps):
                    raise RuntimeError("ocr_result_count_mismatch")
                readings_by_band.extend(
                    (timestamp_us, name, reading)
                    for timestamp_us, reading in zip(timestamps, readings, strict=True)
                )

        evidence = []
        for timestamp_us, name, reading in readings_by_band:
            if reading is None:
                continue
            frame_id = f"frame-{timestamp_us:012d}"
            identity_raw = "|".join(
                (
                    self.runtime.provider_id,
                    self.runtime.model_id,
                    frame_id,
                    name,
                    reading["text"],
                )
            )
            evidence.append(
                {
                    "id": "ocr-"
                    + hashlib.sha256(identity_raw.encode("utf-8")).hexdigest()[:24],
                    "frame_id": frame_id,
                    "timestamp_us": timestamp_us,
                    "band": name,
                    "text": reading["text"],
                    "confidence": round(float(reading["confidence"]), 6),
                    "provider_id": self.runtime.provider_id,
                    "model_id": self.runtime.model_id,
                    "box": reading.get("box"),
                    "segment_count": int(reading.get("segment_count", 1)),
                }
            )
        return {
            "evidence": evidence,
            "processed_until_us": timestamps[-1],
            "frame_count": len(timestamps),
        }


def _response(request_id, *, result=None, error=None):
    body = {"protocol_version": PROTOCOL_VERSION, "id": request_id, "ok": error is None}
    if error is None:
        body["result"] = result if result is not None else {}
    else:
        body["error"] = error
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"


def serve(input_stream=None, output_stream=None, host=None):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    host = host or OcrEngineHost()
    try:
        for line in input_stream:
            request_id = "unknown"
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("request_must_be_object")
                if message.get("protocol_version") != PROTOCOL_VERSION:
                    raise ValueError("unsupported_protocol_version")
                request_id = message.get("id")
                action = message.get("action")
                payload = message.get("payload", {})
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError("request_id_required")
                if not isinstance(payload, dict):
                    raise ValueError("payload_must_be_object")
                if action == "health":
                    result = host.health()
                elif action == "open":
                    result = host.open(payload)
                elif action == "recognize":
                    result = host.recognize(payload)
                elif action == "close":
                    host.close()
                    result = {"closed": True}
                elif action == "shutdown":
                    host.close()
                    output_stream.write(_response(request_id, result={"stopped": True}))
                    output_stream.flush()
                    return 0
                else:
                    raise ValueError("unknown_action")
                output_stream.write(_response(request_id, result=result))
            except Exception as exc:
                code = str(exc) if str(exc).replace("_", "").isalnum() else type(exc).__name__
                output_stream.write(_response(request_id, error={"code": code[:80]}))
            output_stream.flush()
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
