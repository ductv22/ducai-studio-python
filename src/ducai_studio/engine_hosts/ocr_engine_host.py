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


def _box_to_full_frame(box, band):
    """Map crop-relative normalized OCR geometry into the source frame."""

    if box is None:
        return None
    try:
        x, y, width, height = (float(value) for value in box)
        band_x1 = float(band["x1"])
        band_y1 = float(band["y1"])
        band_width = float(band["x2"]) - band_x1
        band_height = float(band["y2"]) - band_y1
        left = max(0.0, min(1.0, band_x1 + x * band_width))
        top = max(0.0, min(1.0, band_y1 + y * band_height))
        right = max(left, min(1.0, band_x1 + (x + width) * band_width))
        bottom = max(top, min(1.0, band_y1 + (y + height) * band_height))
        if right <= left or bottom <= top:
            return None
        return [left, top, right - left, bottom - top]
    except (KeyError, TypeError, ValueError):
        return None


def _box_overlap_fraction(left, right):
    if left is None or right is None:
        return 0.0
    intersection_left = max(left[0], right[0])
    intersection_top = max(left[1], right[1])
    intersection_right = min(left[0] + left[2], right[0] + right[2])
    intersection_bottom = min(left[1] + left[3], right[1] + right[3])
    intersection = max(0.0, intersection_right - intersection_left) * max(
        0.0, intersection_bottom - intersection_top
    )
    smaller_area = min(left[2] * left[3], right[2] * right[3])
    return intersection / max(1e-9, smaller_area)


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


def _probable_platform_overlay(text, box=None):
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if any(
        _edit_distance(word, "bilibili") <= 2
        for word in re.findall(r"[a-z]{5,12}", compact)
    ):
        return True
    # A repair/episode marker without a platform word is accepted only as a
    # compact edge overlay. Centered dialogue such as “修复3473后再走” must stay
    # available to subtitle consensus.
    return bool(
        re.fullmatch(r"(?:4k)?(?:超清|高清|修复)(?:漫剧|漫画|动漫|短剧)?[|丨]?\d{3,5}", compact)
        and box is not None
        and box[2] <= 0.32
        and (box[0] <= 0.15 or box[0] + box[2] >= 0.85)
    )


def _edit_distance(left, right):
    previous = list(range(len(right) + 1))
    for row, left_character in enumerate(left, start=1):
        current = [row]
        for column, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _channel_name_signature(compact):
    match = re.fullmatch(
        r"([\u3400-\u9fff]{1,4})"
        r"(?:看漫剧|说漫剧|漫剧解说|漫画解说|动漫解说|短剧解说)",
        compact,
    )
    if not match:
        return False
    prefix = match.group(1)
    # A marker like “小李看漫剧” is name-shaped; an arbitrary short sentence
    # like “今天看漫剧” is not.  A keyword/pronoun blacklist is too broad and
    # silently deletes new dialogue variants, so keep the accepted handle
    # shape deliberately narrow.
    return 2 <= len(prefix) <= 4 and prefix[0] in "小老阿"


def _probable_platform_companion(text, box=None):
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if not compact:
        return False
    if box is not None and box[2] > 0.32:
        return False
    return bool(
        re.fullmatch(
            r"(?:4k)?(?:超清|高清|修复)(?:漫剧|漫画|动漫|短剧)?[|丨]?\d{3,5}",
            compact,
        )
        or re.fullmatch(r"[\u3400-\u9fff]{2,6}\d{3,5}", compact)
        or _channel_name_signature(compact)
    )


def _same_visual_row(left, right):
    if left is None or right is None:
        return False
    left_top, left_bottom = left[1], left[1] + left[3]
    right_top, right_bottom = right[1], right[1] + right[3]
    overlap = max(0.0, min(left_bottom, right_bottom) - max(left_top, right_top))
    shorter = max(1e-6, min(left[3], right[3]))
    if overlap / shorter >= 0.35:
        return True
    left_center = (left_top + left_bottom) / 2.0
    right_center = (right_top + right_bottom) / 2.0
    return abs(left_center - right_center) <= max(left[3], right[3]) * 0.55


def _horizontal_gap(left, right):
    if left is None or right is None:
        return 1.0
    left_start, left_end = left[0], left[0] + left[2]
    right_start, right_end = right[0], right[0] + right[2]
    if left_end < right_start:
        return right_start - left_end
    if right_end < left_start:
        return left_start - right_end
    return 0.0


def _platform_overlay_indices(rows):
    anchors = {
        index
        for index, item in enumerate(rows)
        if _probable_platform_overlay(item[1], item[4])
    }
    if not anchors:
        return set()
    removed = set(anchors)
    for anchor_index in anchors:
        anchor_box = rows[anchor_index][4]
        if anchor_box is not None and not (
            anchor_box[0] <= 0.15 or anchor_box[0] + anchor_box[2] >= 0.85
        ):
            continue
        for index, item in enumerate(rows):
            if index in removed or not _probable_platform_companion(item[1], item[4]):
                continue
            if (
                _same_visual_row(anchor_box, item[4])
                and _horizontal_gap(anchor_box, item[4]) <= 0.12
            ):
                removed.add(index)
    return removed


def _keep_segment(text, raw_box, width, height, band):
    return not _probable_platform_overlay(
        text, _normalized_box(raw_box, width, height)
    )


def _combine_reading(texts, scores, boxes, width, height, *, band=""):
    rows = []
    for index, raw_text in enumerate(texts):
        text = str(raw_text or "").strip()
        if not text:
            continue
        score = float(scores[index]) if index < len(scores) else 0.0
        raw_box = boxes[index] if index < len(boxes) else None
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
    removed = _platform_overlay_indices(rows)
    watermark_rows = [item for index, item in enumerate(rows) if index in removed]
    watermark_boxes = [item[4] for item in watermark_rows if item[4] is not None]
    watermarks = []
    if watermark_boxes:
        left = min(item[0] for item in watermark_boxes)
        top = min(item[1] for item in watermark_boxes)
        right = max(item[0] + item[2] for item in watermark_boxes)
        bottom = max(item[1] + item[3] for item in watermark_boxes)
        watermark_weight = sum(max(1, len(item[1])) for item in watermark_rows)
        watermarks.append(
            {
                "text": "".join(item[1] for item in watermark_rows),
                "confidence": sum(
                    item[2] * max(1, len(item[1])) for item in watermark_rows
                )
                / max(1, watermark_weight),
                "box": [left, top, right - left, bottom - top],
            }
        )
    rows = [item for index, item in enumerate(rows) if index not in removed]
    if not rows:
        return {
            "text": "",
            "confidence": 0.0,
            "box": None,
            "segment_count": 0,
            "segments": [],
            "watermarks": watermarks,
        }
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
        "segments": [
            {"text": item[1], "confidence": item[2], "box": item[4]}
            for item in rows
            if item[4] is not None
        ],
        "watermarks": watermarks,
    }


def _scene_segment_metadata(frame, box, text=""):
    """Estimate a retained panel and a safe surrounding fill colour.

    This does not attempt impossible background reconstruction.  The glyph box
    remains the only concealed region; the expanded panel is used for fitting
    Vietnamese while preserving decorative borders around it.
    """

    if box is None:
        return None
    x, y, width, height = (float(value) for value in box)
    vertical = height > width * 1.35
    expand_x = max(0.015, width * (0.70 if vertical else 0.18))
    expand_y = max(0.012, height * (0.16 if vertical else 0.42))
    left = max(0.0, x - expand_x)
    top = max(0.0, y - expand_y)
    right = min(1.0, x + width + expand_x)
    bottom = min(1.0, y + height + expand_y)
    background_color = ""
    scene_confidence = 0.86 if vertical else 0.45
    cjk_length = len(re.findall(r"[\u3400-\u9fff]", str(text)))
    if 1 <= cjk_length <= 8:
        scene_confidence += 0.05
    center_x = x + width / 2
    if center_x <= 0.35 or center_x >= 0.65:
        scene_confidence += 0.08
    try:
        import numpy as np

        frame_height, frame_width = frame.shape[:2]
        x1 = max(0, min(frame_width - 1, int(round(left * frame_width))))
        y1 = max(0, min(frame_height - 1, int(round(top * frame_height))))
        x2 = max(x1 + 1, min(frame_width, int(round(right * frame_width))))
        y2 = max(y1 + 1, min(frame_height, int(round(bottom * frame_height))))
        gx1 = max(x1, min(x2 - 1, int(round(x * frame_width))))
        gy1 = max(y1, min(y2 - 1, int(round(y * frame_height))))
        gx2 = max(gx1 + 1, min(x2, int(round((x + width) * frame_width))))
        gy2 = max(gy1 + 1, min(y2, int(round((y + height) * frame_height))))
        panel = frame[y1:y2, x1:x2]
        mask = np.ones(panel.shape[:2], dtype=bool)
        mask[gy1 - y1 : gy2 - y1, gx1 - x1 : gx2 - x1] = False
        ring = panel[mask]
        if ring.size:
            bgr = np.median(ring.reshape(-1, ring.shape[-1]), axis=0)
            red = int(round(float(bgr[2])))
            green = int(round(float(bgr[1])))
            blue = int(round(float(bgr[0])))
            background_color = f"#{red:02X}{green:02X}{blue:02X}"
            ring_pixels = ring.reshape(-1, ring.shape[-1]).astype(np.float32)
            glyph_pixels = frame[gy1:gy2, gx1:gx2].reshape(-1, 3).astype(np.float32)
            ring_luma = (
                ring_pixels[:, 0] * 0.114
                + ring_pixels[:, 1] * 0.587
                + ring_pixels[:, 2] * 0.299
            )
            glyph_luma = (
                glyph_pixels[:, 0] * 0.114
                + glyph_pixels[:, 1] * 0.587
                + glyph_pixels[:, 2] * 0.299
            )
            ring_level = float(np.median(ring_luma))
            contrast = max(
                abs(float(np.percentile(glyph_luma, 10.0)) - ring_level),
                abs(float(np.percentile(glyph_luma, 90.0)) - ring_level),
            ) / 255.0
            uniformity = 1.0 - min(1.0, float(np.std(ring_luma)) / 80.0)
            if contrast >= 0.18 and uniformity >= 0.45:
                scene_confidence += 0.30
            elif contrast >= 0.12 and uniformity >= 0.30:
                scene_confidence += 0.15
    except (AttributeError, IndexError, TypeError, ValueError):
        background_color = ""
    return {
        "panel_box": [left, top, right - left, bottom - top],
        "orientation": "vertical" if vertical else "horizontal",
        "style_hint": "calligraphy" if vertical else "display",
        "background_color": background_color,
        "scene_confidence": min(1.0, scene_confidence),
    }


def _group_scene_segments(segments):
    rows = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text", "")).strip()
        box = segment.get("box")
        if (
            not text
            or not isinstance(box, list)
            or len(box) != 4
            or not re.search(r"[\u3400-\u9fff]", text)
        ):
            continue
        rows.append(
            {
                "text": text,
                "confidence": float(segment.get("confidence", 0.0)),
                "box": [float(value) for value in box],
            }
        )
    used = set()
    grouped = []
    for index, seed in enumerate(rows):
        if index in used:
            continue
        sx, sy, sw, sh = seed["box"]
        column = []
        for other_index, other in enumerate(rows):
            if other_index in used:
                continue
            ox, oy, ow, oh = other["box"]
            center_delta = abs((sx + sw / 2) - (ox + ow / 2))
            if center_delta <= max(sw, ow) * 0.72:
                column.append((other_index, other))
        column.sort(key=lambda item: item[1]["box"][1])
        if len(column) >= 2:
            top = min(item[1]["box"][1] for item in column)
            bottom = max(
                item[1]["box"][1] + item[1]["box"][3] for item in column
            )
            left = min(item[1]["box"][0] for item in column)
            right = max(
                item[1]["box"][0] + item[1]["box"][2] for item in column
            )
            gaps = [
                column[position + 1][1]["box"][1]
                - (
                    column[position][1]["box"][1]
                    + column[position][1]["box"][3]
                )
                for position in range(len(column) - 1)
            ]
            if max(gaps, default=0.0) <= 0.08 and bottom - top > (right - left) * 1.35:
                used.update(item[0] for item in column)
                grouped.append(
                    {
                        "text": "".join(item[1]["text"] for item in column),
                        "confidence": sum(item[1]["confidence"] for item in column)
                        / len(column),
                        "box": [left, top, right - left, bottom - top],
                    }
                )
                continue
        used.add(index)
        grouped.append(seed)
    return grouped


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
        self._change_signature = None
        self._change_event_us = -1_000_000
        self._scene_change_signature = None
        self._scene_change_event_us = -1_000_000

    def close(self):
        if self.video is not None:
            self.video.close()
        self.video = None
        self.runtime = None
        self._change_signature = None
        self._change_event_us = -1_000_000
        self._scene_change_signature = None
        self._scene_change_event_us = -1_000_000

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
    def _crop(frame, band, exclusion_regions=()):
        import cv2

        height, width = frame.shape[:2]
        x1 = max(0, min(width - 1, int(round(float(band["x1"]) * width))))
        x2 = max(x1 + 1, min(width, int(round(float(band["x2"]) * width))))
        y1 = max(0, min(height - 1, int(round(float(band["y1"]) * height))))
        y2 = max(y1 + 1, min(height, int(round(float(band["y2"]) * height))))
        # ``frame[y1:y2, x1:x2]`` is a view. Copy before masking so dialogue
        # exclusions for the scene pass cannot mutate the decoded frame later
        # reused by another band or by change detection.
        crop = frame[y1:y2, x1:x2]
        if exclusion_regions:
            crop = crop.copy()
        for region in exclusion_regions:
            left = max(x1, min(x2, int(round(float(region[0]) * width))))
            top = max(y1, min(y2, int(round(float(region[1]) * height))))
            right = max(
                x1,
                min(x2, int(round((float(region[0]) + float(region[2])) * width))),
            )
            bottom = max(
                y1,
                min(y2, int(round((float(region[1]) + float(region[3])) * height))),
            )
            if right <= left or bottom <= top:
                continue
            cv2.rectangle(
                crop,
                (left - x1, top - y1),
                (right - x1, bottom - y1),
                (0, 0, 0),
                thickness=-1,
            )
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
        normalized_bands = []
        for band in bands:
            name = band.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("invalid_band")
            normalized_bands.append((name, band))
        known_bands = {name for name, _band in normalized_bands}

        def normalized_regions(raw, error_code):
            if raw is None:
                return []
            if not isinstance(raw, list):
                raise ValueError(error_code)
            values = []
            for item in raw:
                if (
                    not isinstance(item, (list, tuple))
                    or len(item) != 4
                    or any(
                        isinstance(value, bool) or not isinstance(value, (int, float))
                        for value in item
                    )
                ):
                    raise ValueError(error_code)
                x, y, region_width, region_height = (float(value) for value in item)
                if (
                    x < 0
                    or y < 0
                    or region_width <= 0
                    or region_height <= 0
                    or x + region_width > 1.000001
                    or y + region_height > 1.000001
                ):
                    raise ValueError(error_code)
                values.append([x, y, region_width, region_height])
            return values

        global_exclusions = normalized_regions(
            payload.get("exclusion_regions", []),
            "invalid_exclusion_regions",
        )
        raw_by_band = payload.get("exclusion_regions_by_band", {})
        if not isinstance(raw_by_band, dict) or not set(raw_by_band).issubset(known_bands):
            raise ValueError("invalid_exclusion_regions_by_band")
        exclusions_by_band = {
            name: normalized_regions(values, "invalid_exclusion_regions_by_band")
            for name, values in raw_by_band.items()
        }
        band_timestamps = payload.get("band_timestamps_us")
        evidence_kinds_by_band = payload.get("evidence_kinds_by_band", {})
        if band_timestamps is not None:
            if not isinstance(band_timestamps, dict):
                raise ValueError("invalid_band_timestamps")
            if set(band_timestamps) != known_bands:
                raise ValueError("invalid_band_timestamps")
            for values in band_timestamps.values():
                if (
                    not isinstance(values, list)
                    or not all(type(value) is int for value in values)
                    or values != sorted(values)
                    or len(values) != len(set(values))
                    or not set(values).issubset(timestamps)
                ):
                    raise ValueError("invalid_band_timestamps")
            if (
                not isinstance(evidence_kinds_by_band, dict)
                or set(evidence_kinds_by_band) != known_bands
                or any(
                    value not in {"global", "burst", "scene"}
                    for value in evidence_kinds_by_band.values()
                )
            ):
                raise ValueError("invalid_evidence_kinds_by_band")
        detect_changes = payload.get("detect_changes", False)
        change_threshold = payload.get("change_threshold", 0.115)
        scene_change_threshold = payload.get("scene_change_threshold", 0.10)
        if type(detect_changes) is not bool:
            raise ValueError("invalid_change_detection_flag")
        change_timestamps = payload.get("change_timestamps_us", timestamps)
        if (
            not isinstance(change_timestamps, list)
            or not change_timestamps
            or not all(type(value) is int and value >= 0 for value in change_timestamps)
            or change_timestamps != sorted(change_timestamps)
            or len(change_timestamps) != len(set(change_timestamps))
        ):
            raise ValueError("invalid_change_timestamps")
        if (
            isinstance(change_threshold, bool)
            or not isinstance(change_threshold, (int, float))
            or not 0.02 <= float(change_threshold) <= 0.8
        ):
            raise ValueError("invalid_change_threshold")
        if (
            isinstance(scene_change_threshold, bool)
            or not isinstance(scene_change_threshold, (int, float))
            or not 0.02 <= float(scene_change_threshold) <= 0.8
        ):
            raise ValueError("invalid_scene_change_threshold")
        if detect_changes:
            decode_timestamps = sorted({*timestamps, *change_timestamps})
            decoded_frames = self.video.read_many(decode_timestamps)
            frame_by_timestamp = dict(
                zip(decode_timestamps, decoded_frames, strict=True)
            )
            frames = [frame_by_timestamp[value] for value in timestamps]
            change_frames = [frame_by_timestamp[value] for value in change_timestamps]
        else:
            frames = self.video.read_many(timestamps)
            change_frames = frames
        change_events, scene_change_events = (
            self._integrated_change_events(
                change_frames,
                change_timestamps,
                tuple(band for _name, band in normalized_bands),
                float(change_threshold),
                float(scene_change_threshold),
            )
            if detect_changes
            else ([], [])
        )

        # Preserve the old fallback for third-party/test runtimes that only
        # implement ``recognize``.  The bundled Paddle and Rapid runtimes use
        # the batched method, avoiding one detector invocation per subtitle band.
        readings_by_band = []
        jobs = [
            (timestamp_us, name, band)
            for timestamp_us in timestamps
            for name, band in normalized_bands
            if band_timestamps is None
            or timestamp_us in band_timestamps[name]
        ]
        recognize_batched = getattr(self.runtime, "recognize_batched", None)
        if callable(recognize_batched):
            crops = []
            crop_bands = []
            frame_by_timestamp = dict(zip(timestamps, frames, strict=True))
            for timestamp_us, name, band in jobs:
                crops.append(
                    self._crop(
                        frame_by_timestamp[timestamp_us],
                        band,
                        (*global_exclusions, *exclusions_by_band.get(name, ())),
                    )
                )
                crop_bands.append(str(band.get("reading_mode", name)))
            readings = recognize_batched(crops, crop_bands)
            expected = len(jobs)
            if len(readings) != expected:
                raise RuntimeError("ocr_result_count_mismatch")
            readings_by_band.extend(
                (
                    timestamp_us,
                    name,
                    band,
                    frame_by_timestamp[timestamp_us],
                    reading,
                    str(evidence_kinds_by_band.get(name, payload.get("evidence_kind", "global"))),
                )
                for (timestamp_us, name, band), reading in zip(
                    jobs, readings, strict=True
                )
            )
        else:
            for name, band in normalized_bands:
                band_jobs = [job for job in jobs if job[1] == name]
                if not band_jobs:
                    continue
                frame_by_timestamp = dict(zip(timestamps, frames, strict=True))
                crops = [
                    self._crop(
                        frame_by_timestamp[timestamp_us],
                        band,
                        (*global_exclusions, *exclusions_by_band.get(name, ())),
                    )
                    for timestamp_us, name, band in band_jobs
                ]
                reading_mode = str(band_jobs[0][2].get("reading_mode", name))
                readings = self.runtime.recognize(crops, reading_mode)
                if len(readings) != len(band_jobs):
                    raise RuntimeError("ocr_result_count_mismatch")
                readings_by_band.extend(
                    (
                        timestamp_us,
                        name,
                        band,
                        frame_by_timestamp[timestamp_us],
                        reading,
                        str(
                            evidence_kinds_by_band.get(
                                name, payload.get("evidence_kind", "global")
                            )
                        ),
                    )
                    for (timestamp_us, _name, band), reading in zip(
                        band_jobs, readings, strict=True
                    )
                )

        evidence = []
        watermark_detections = []
        default_evidence_kind = str(payload.get("evidence_kind", "global"))
        if default_evidence_kind not in {"global", "burst", "scene"}:
            raise ValueError("invalid_evidence_kind")
        for timestamp_us, name, band, frame, reading, evidence_kind in readings_by_band:
            if reading is None:
                continue
            frame_id = f"frame-{timestamp_us:012d}"
            frame_watermark_boxes = []
            for watermark in reading.get("watermarks", []):
                text = str(watermark.get("text", "")).strip()
                box = _box_to_full_frame(watermark.get("box"), band)
                if not text or box is None:
                    continue
                frame_watermark_boxes.append(box)
                identity_raw = "|".join(
                    (
                        self.runtime.provider_id,
                        self.runtime.model_id,
                        frame_id,
                        name,
                        "watermark",
                        text,
                    )
                )
                watermark_detections.append(
                    {
                        "id": "ocr-watermark-"
                        + hashlib.sha256(identity_raw.encode("utf-8")).hexdigest()[:24],
                        "frame_id": frame_id,
                        "timestamp_us": timestamp_us,
                        "band": name,
                        "text": text,
                        "confidence": round(float(watermark.get("confidence", 0.0)), 6),
                        "provider_id": self.runtime.provider_id,
                        "model_id": self.runtime.model_id,
                        "box": box,
                        "ignored_overlay": True,
                        "evidence_kind": evidence_kind,
                    }
                )
            if not reading.get("text"):
                continue
            if name == "scene":
                for segment_index, segment in enumerate(
                    _group_scene_segments(reading.get("segments", []))
                ):
                    text = str(segment.get("text", "")).strip()
                    box = _box_to_full_frame(segment.get("box"), band)
                    if not text or box is None or not re.search(r"[\u3400-\u9fff]", text):
                        continue
                    if _probable_platform_overlay(text, box):
                        continue
                    if any(
                        _box_overlap_fraction(box, watermark_box) >= 0.30
                        for watermark_box in frame_watermark_boxes
                    ):
                        continue
                    # Ordinary hard subtitles remain owned by the tested
                    # two-band path.  Narrow/vertical labels are retained even
                    # near the bottom because technique and artifact cards can
                    # legitimately be placed there.
                    if box[1] >= 0.72 and box[2] >= 0.18 and box[3] <= 0.18:
                        continue
                    metadata = _scene_segment_metadata(frame, box, text)
                    if metadata is None:
                        continue
                    if float(metadata.get("scene_confidence", 0.0)) < 0.65:
                        continue
                    identity_raw = "|".join(
                        (
                            self.runtime.provider_id,
                            self.runtime.model_id,
                            frame_id,
                            name,
                            str(segment_index),
                            text,
                        )
                    )
                    evidence.append(
                        {
                            "id": "ocr-scene-"
                            + hashlib.sha256(identity_raw.encode("utf-8")).hexdigest()[:24],
                            "frame_id": frame_id,
                            "timestamp_us": timestamp_us,
                            "band": name,
                            "text": text,
                            "confidence": round(float(segment.get("confidence", 0.0)), 6),
                            "provider_id": self.runtime.provider_id,
                            "model_id": self.runtime.model_id,
                            "box": box,
                            "evidence_kind": "scene",
                            **metadata,
                        }
                    )
                continue
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
                    "box": _box_to_full_frame(reading.get("box"), band),
                    "segment_count": int(reading.get("segment_count", 1)),
                    "evidence_kind": evidence_kind,
                }
            )
        return {
            "evidence": evidence,
            "watermark_detections": watermark_detections,
            "event_timestamps_us": change_events,
            "scene_event_timestamps_us": scene_change_events,
            "processed_until_us": timestamps[-1],
            "frame_count": len(timestamps),
        }

    def _integrated_change_events(
        self,
        frames,
        timestamps,
        bands,
        threshold,
        scene_threshold,
    ):
        """Measure subtitle-band and full-frame changes on already decoded frames.

        Band changes drive the short-dialogue rescue pass.  A separate tiny
        full-frame signature drives sparse scene-text OCR so a name card or
        technique label appearing away from the subtitle bands is not missed.
        Both signatures reuse Fast-pass frames; this never decodes the video a
        second time.
        """

        import cv2
        import numpy as np

        events = []
        scene_events = []
        for timestamp, frame in zip(timestamps, frames, strict=True):
            signature = []
            for band in bands:
                crop = self._crop(frame, {**band, "scale": 1.0})
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                small = cv2.resize(gray, (96, 24), interpolation=cv2.INTER_AREA)
                signature.append(small.astype(np.float32) / 255.0)
            full_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            full_signature = cv2.resize(
                full_gray,
                (96, 54),
                interpolation=cv2.INTER_AREA,
            ).astype(np.float32) / 255.0
            previous = self._change_signature
            if previous is not None and len(previous) == len(signature):
                score = max(
                    float(np.mean(np.abs(current - earlier)))
                    for current, earlier in zip(signature, previous, strict=True)
                )
                if score >= threshold and timestamp - self._change_event_us >= 180_000:
                    events.append(timestamp)
                    self._change_event_us = timestamp
            previous_scene = self._scene_change_signature
            if previous_scene is not None:
                scene_diff = np.abs(full_signature - previous_scene)
                # A title card can occupy only a small part of the frame, so
                # global mean difference alone is too insensitive.  The 98th
                # percentile catches newly drawn glyph strokes while the
                # downstream temporal/geometry consensus rejects one-frame
                # motion and compression noise.
                scene_score = max(
                    float(np.mean(scene_diff)),
                    float(np.percentile(scene_diff, 98.0)),
                )
                if (
                    scene_score >= scene_threshold
                    and timestamp - self._scene_change_event_us >= 180_000
                ):
                    scene_events.append(timestamp)
                    self._scene_change_event_us = timestamp
            self._change_signature = signature
            self._scene_change_signature = full_signature
        return events, scene_events

    def detect_changes(self, payload):
        if self.video is None:
            raise RuntimeError("engine_not_open")
        start_us = payload.get("start_us", 0)
        end_us = payload.get("end_us", self.video.duration_us)
        interval_us = payload.get("interval_us", 200_000)
        threshold = payload.get("threshold", 0.115)
        bands = payload.get("bands")
        if (
            type(start_us) is not int
            or type(end_us) is not int
            or type(interval_us) is not int
            or start_us < 0
            or end_us <= start_us
            or end_us > self.video.duration_us
            or not 120_000 <= interval_us <= 500_000
            or isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0.02 <= float(threshold) <= 0.8
            or not isinstance(bands, list)
            or not bands
        ):
            raise ValueError("invalid_change_probe")
        timestamps = list(range(start_us, end_us, interval_us))
        frames = self.video.read_many(timestamps)
        signatures = []
        import cv2
        import numpy as np

        for frame in frames:
            band_signatures = []
            for band in bands:
                crop = self._crop(frame, {**band, "scale": 1.0})
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                small = cv2.resize(gray, (96, 24), interpolation=cv2.INTER_AREA)
                band_signatures.append(small.astype(np.float32) / 255.0)
            signatures.append(band_signatures)
        events = []
        previous_event = -1_000_000
        for index in range(1, len(signatures)):
            score = max(
                float(np.mean(np.abs(current - previous)))
                for current, previous in zip(
                    signatures[index], signatures[index - 1], strict=True
                )
            )
            timestamp = timestamps[index]
            if score >= float(threshold) and timestamp - previous_event >= 180_000:
                events.append(timestamp)
                previous_event = timestamp
        return {
            "event_timestamps_us": events,
            "probed_until_us": end_us,
            "probe_count": len(timestamps),
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
                elif action == "detect_changes":
                    result = host.detect_changes(payload)
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
