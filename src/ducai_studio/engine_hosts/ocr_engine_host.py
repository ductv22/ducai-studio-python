"""Version-neutral Paddle/Rapid OCR process.

This file intentionally avoids importing the DUCAI application package.  It is
executed directly by the Python 3.11 ML environment while the GUI and worker
runtime stay on Python 3.12.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import pickle
import re
import struct
import subprocess
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROTOCOL_VERSION = 1
PADDLE_MODELS = {
    "fast": ("PP-OCRv6_tiny_det", "PP-OCRv6_small_rec"),
    "quality": ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
}
MAX_PREDICTOR_PACKET_BYTES = 256 * 1024 * 1024
PREDICTOR_STARTUP_TIMEOUT_SECONDS = 90
PREDICTOR_REQUEST_TIMEOUT_SECONDS = 120
MIN_PARALLEL_FREE_MEMORY_MB = 4096
RESOURCE_PROBE_INTERVAL_SECONDS = 5
RESOURCE_PROMOTION_RETRY_SECONDS = 60
MAX_DECODED_FRAME_CACHE_BYTES = 256 * 1024 * 1024
_RESOURCE_FALLBACK_REASONS = frozenset({
    "insufficient_free_memory", "memory_probe_unavailable", "gpu_oom", "ocr_cuda_out_of_memory",
})
_TRANSIENT_PREDICTOR_ERRORS = frozenset({
    "ocr_predictor_timeout", "ocr_predictor_exited", "BrokenPipeError",
    "ConnectionResetError", "ConnectionAbortedError",
})

# Only application-owned identifiers may pass through private worker pipes.
# Framework exceptions can contain media paths or credentials; classify them
# without persisting the original message.
_SAFE_ERROR_CODES = frozenset({
    "ocr_predictor_packet_too_large", "ocr_predictor_exited", "ocr_predictor_timeout",
    "ocr_predictor_invalid_handshake", "ocr_predictor_invalid_readings",
    "ocr_predictor_result_count_mismatch", "ocr_result_count_mismatch",
    "paddle_band_count_mismatch", "paddle_result_count_mismatch", "rapid_band_count_mismatch",
    "engine_not_open", "invalid_ocr_worker_count", "invalid_frame_cache_budget",
    "insufficient_free_memory", "memory_probe_unavailable",
    "video_open_failed", "video_fps_unavailable", "video_timestamp_out_of_range",
    "video_seek_failed", "video_decode_failed", "unsupported_ocr_mode", "invalid_batch_size",
    "invalid_device", "timestamps_required", "invalid_timestamp",
    "timestamps_not_strictly_increasing", "bands_required", "invalid_band",
    "invalid_exclusion_regions", "invalid_exclusion_regions_by_band",
    "invalid_band_timestamps", "invalid_evidence_kinds_by_band", "invalid_evidence_kind",
    "invalid_change_detection_flag", "invalid_change_tracking_reset", "invalid_change_timestamps",
    "invalid_change_threshold", "invalid_scene_change_threshold", "invalid_change_probe",
    "request_must_be_object", "unsupported_protocol_version", "request_id_required",
    "payload_must_be_object", "unknown_action", "model_missing", "model_startup_failed", "gpu_oom",
    "ocr_cuda_driver_incompatible", "ocr_cuda_kernel_unsupported", "ocr_cuda_launch_failed",
    "ocr_cuda_out_of_memory", "ocr_cuda_library_missing", "ocr_cuda_cpu_only",
    "ocr_cuda_device_unavailable", "ocr_cuda_blackwell_runtime_incompatible",
})


def _ordered_results(futures, timeout):
    deadline = time.monotonic() + timeout
    for future in futures:
        yield future.result(timeout=max(0.0, deadline - time.monotonic()))


def _send_packet(stream, value):
    data = pickle.dumps(value, protocol=5)
    if len(data) > MAX_PREDICTOR_PACKET_BYTES:
        raise ValueError("ocr_predictor_packet_too_large")
    stream.write(struct.pack("<Q", len(data)))
    stream.write(data)
    stream.flush()


def _read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise EOFError("ocr_predictor_exited")
        result.extend(chunk)
    return bytes(result)


def _receive_packet(stream):
    size = struct.unpack("<Q", _read_exact(stream, 8))[0]
    if size > MAX_PREDICTOR_PACKET_BYTES:
        raise ValueError("ocr_predictor_packet_too_large")
    # These are private inherited pipes to processes we created, not untrusted
    # network/project input. Pickle retains ndarray bytes and numeric types.
    return pickle.loads(_read_exact(stream, size))


def _error_code(error):
    if isinstance(error, MemoryError):
        return "insufficient_free_memory"
    if isinstance(error, TimeoutError):
        return "ocr_predictor_timeout"
    message = str(error)
    if message in _SAFE_ERROR_CODES:
        return message
    lowered = message.casefold()
    if any(value in lowered for value in (
        "cudaerrorinsufficientdriver", "driver version is insufficient",
        "cuda_error_system_driver_mismatch", "cuda error(35)", "cuda error(803)",
    )):
        return "ocr_cuda_driver_incompatible"
    if any(value in lowered for value in (
        "no kernel image", "cudaerrornokernelimagefordevice", "invalid device function",
        "cuda error(209)", "cuda error(98)",
    )):
        return "ocr_cuda_kernel_unsupported"
    if any(value in lowered for value in (
        "cuda error(719)", "cuda error(700)", "cudaerrorlaunchfailure",
        "cudaerrorillegaladdress", "illegal memory access",
    )):
        return "ocr_cuda_launch_failed"
    if ("out of memory" in lowered or "cudaerrormemoryallocation" in lowered) and (
        "gpu" in lowered or "cuda" in lowered
    ):
        return "ocr_cuda_out_of_memory"
    if any(library in lowered for library in (
        "cudnn", "cublas", "cudart", "cusolver", "cusparse", "nvrtc", "nvcuda",
    )) and any(value in lowered for value in (
        "could not load", "cannot load", "error loading", "not found",
        "winerror 126", "winerror 127", "cannot open shared object",
    )):
        return "ocr_cuda_library_missing"
    return type(error).__name__[:80]


def _validate_paddle_gpu(paddle, device):
    """Reject a known-incompatible CUDA build before loading OCR models."""
    if not device.startswith("gpu"):
        return
    if not paddle.is_compiled_with_cuda():
        raise RuntimeError("ocr_cuda_cpu_only")
    index = int(device.split(":", 1)[1]) if ":" in device else 0
    if index < 0 or paddle.device.cuda.device_count() <= index:
        raise RuntimeError("ocr_cuda_device_unavailable")
    capability = paddle.device.cuda.get_device_capability(index)
    cuda = re.match(r"^(\d+)\.(\d+)", str(paddle.version.cuda()))
    if capability[0] >= 12 and (cuda is None or tuple(map(int, cuda.groups())) < (12, 9)):
        raise RuntimeError("ocr_cuda_blackwell_runtime_incompatible")


def _terminate_owned_tree(process, *, force=False):
    if os.name == "nt":
        # Windows venv python.exe launches another interpreter. Killing only
        # the launcher leaks its model and VRAM, so always stop the owned tree.
        command = ["taskkill", "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(command, capture_output=True, timeout=5, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    elif process.poll() is None:
        process.kill() if force else process.terminate()


def _available_ram_mb():
    if os.name != "nt":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total_phys", "available_phys", "total_page", "available_page",
             "total_virtual", "available_virtual", "available_extended")
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.available_phys // (1024 * 1024)


def _available_gpu_mb(device):
    try:
        index = int(device.split(":", 1)[1]) if ":" in device else 0
        if index < 0:
            return None
        result = subprocess.run(
            ["nvidia-smi", f"--id={index}", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _parallel_memory_reason(device):
    ram, gpu = _available_ram_mb(), _available_gpu_mb(device)
    if ram is None or gpu is None:
        return "memory_probe_unavailable"
    return "insufficient_free_memory" if min(ram, gpu) < MIN_PARALLEL_FREE_MEMORY_MB else ""


def _values(value):
    to_list = getattr(value, "tolist", None)
    if callable(to_list):
        value = to_list()
    return list(value) if value is not None else []


def _normalized_box(raw, width, height):
    try:
        values = _values(raw)
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
        values = _values(raw)
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
                _box_sort_key(raw_box if raw_box is not None else (0, 0, width, height)),
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
    if band == "dialogue":
        # Default expanded dialogue crops also contain map/scene columns.
        # Anchor on a horizontal subtitle row, retain adjacent wrapped rows
        # of comparable glyph height, never concatenate a tall vertical label.
        horizontal = [row for row in rows if row[4] is not None
                      and row[4][2] * width >= row[4][3] * height * 1.35
                      and row[2] >= .62 and re.search(r"[\u3400-\u9fff]", row[1])]
        if horizontal:
            anchor = max(horizontal, key=lambda row: row[4][1] + row[4][3])
            box = anchor[4]
            selected = [row for row in rows if row[4] is None or (
                row[4][3] <= box[3] * 1.65
                and (len(re.findall(r"[\u3400-\u9fff]", row[1])) <= 1
                     or row[4][2] * width >= row[4][3] * height * .65)
                and (_same_visual_row(box, row[4]) or (
                    0 <= box[1] - row[4][1] - row[4][3] <= box[3] * .8
                    and _horizontal_gap(box, row[4]) == 0
                ))
            )]
            rows = selected or rows
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
    # Detector boxes in one subtitle row often differ by a pixel in y.
    # Sorting exact y before x rotates right-hand fragments to the front.
    # Cluster horizontal rows first; retain independent vertical scene columns.
    if band != "scene":
        lines = []
        for row in sorted(rows, key=lambda item: item[0]):
            line = next((line for line in lines if row[4] is not None
                         and line[0][4] is not None
                         and _same_visual_row(line[0][4], row[4])), None)
            if line is None:
                lines.append([row])
            else:
                line.append(row)
        rows = [row for line in lines for row in sorted(line, key=lambda item: (
            item[4][0] if item[4] is not None else item[0][1]))]
    else:
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
        # Join adjacent pieces of one horizontal label, not two neighboring
        # cards or separate rows. The vertical path below retains columns.
        if sw > sh * 1.35:
            line = [(index, seed)]
            for other_index, other in enumerate(rows):
                if other_index in used or other_index == index:
                    continue
                ox, oy, ow, oh = other["box"]
                if ow <= oh * 1.35 or not _same_visual_row(seed["box"], other["box"]):
                    continue
                nearest_gap = min(max(0.0, max(ox, member["box"][0]) - min(
                    ox + ow, member["box"][0] + member["box"][2])) for _, member in line)
                if nearest_gap <= max(sh, oh) * .8:
                    line.append((other_index, other))
            if len(line) > 1:
                line.sort(key=lambda entry: entry[1]["box"][0])
                left = min(entry[1]["box"][0] for entry in line)
                top = min(entry[1]["box"][1] for entry in line)
                right = max(entry[1]["box"][0] + entry[1]["box"][2] for entry in line)
                bottom = max(entry[1]["box"][1] + entry[1]["box"][3] for entry in line)
                used.update(entry[0] for entry in line)
                grouped.append({
                    "text": "".join(entry[1]["text"] for entry in line),
                    "confidence": sum(entry[1]["confidence"] for entry in line) / len(line),
                    "box": [left, top, right - left, bottom - top],
                })
                continue
        column = []
        for other_index, other in enumerate(rows):
            if other_index in used:
                continue
            ox, oy, ow, oh = other["box"]
            center_delta = abs((sx + sw / 2) - (ox + ow / 2))
            horizontal_overlap = max(0.0, min(sx + sw, ox + ow) - max(sx, ox))
            if (center_delta <= min(sw, ow) * .55
                    and horizontal_overlap >= min(sw, ow) * .65
                    and ow <= oh * 1.35):
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
            max_glyph_height = max(item[1]["box"][3] for item in column)
            if (max(gaps, default=0.0) <= max(.08, max_glyph_height * .85)
                    and bottom - top > (right - left) * 1.35):
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
        import paddle
        from paddleocr import PaddleOCR

        _validate_paddle_gpu(paddle, device)
        detection_model, recognition_model = PADDLE_MODELS[mode]
        self.provider_id = "paddle-" + mode
        self.model_id = recognition_model
        self.execution_device = device
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
            boxes = _values(data.get("rec_boxes"))
            if not boxes:
                boxes = _values(data.get("rec_polys"))
            readings.append(
                _combine_reading(
                    _values(data.get("rec_texts")),
                    _values(data.get("rec_scores")),
                    boxes,
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


class ChildPredictor:
    def __init__(self, mode, device, batch_size):
        self.closed = False
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()),
             "--predictor-mode", mode, "--device", device,
             "--batch-size", str(batch_size)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        print(json.dumps({"ocr_predictor_pid": self.process.pid, "mode": mode}),
              file=sys.stderr, flush=True)

    def _response(self):
        success, payload = _receive_packet(self.process.stdout)
        if success is not True:
            raise RuntimeError(str(payload)[:80])
        return payload

    def ready(self):
        if self._response() != "ready":
            raise RuntimeError("ocr_predictor_invalid_handshake")

    def recognize(self, frames, bands):
        _send_packet(self.process.stdin, (frames, bands))
        readings = self._response()
        if not isinstance(readings, list):
            raise RuntimeError("ocr_predictor_invalid_readings")
        return readings

    def close(self, *, force=False):
        if self.closed:
            return
        self.closed = True
        try:
            if self.process.poll() is None:
                if force:
                    _terminate_owned_tree(self.process)
                else:
                    try:
                        _send_packet(self.process.stdin, None)
                    except (OSError, ValueError):
                        _terminate_owned_tree(self.process)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _terminate_owned_tree(self.process, force=True)
                    self.process.wait(timeout=5)
        finally:
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()


class ParallelPaddleRuntime(PaddleRuntime):
    """Distribute serial predictors, keeping all cue decisions in one host."""

    def __init__(self, mode, device, batch_size, *, worker_count=3, child_factory=None,
                 defer_reason=""):
        if type(worker_count) is not int or worker_count != 3:
            raise ValueError("invalid_ocr_worker_count")
        self.provider_id = "paddle-" + mode
        self.model_id = PADDLE_MODELS[mode][1]
        self.execution_device = device
        self.worker_count = 1 if defer_reason else worker_count
        self.execution_fallback_reason = defer_reason
        self.execution_recovery_attempts = 0
        self.execution_recovery_reason = ""
        self._settings = (mode, device, batch_size)
        self._child_factory = child_factory or ChildPredictor
        self._serial_runtime = None
        self._resource_waiting = bool(defer_reason)
        self._next_resource_probe = time.monotonic() + RESOURCE_PROBE_INTERVAL_SECONDS
        self._promotion_failures = 0
        self._closed = False
        self.children = []
        self.executor = None
        try:
            self._start_pool(self.worker_count)
        except Exception as error:
            self._use_serial(error)
        except BaseException:
            self.close(force=True)
            raise

    @staticmethod
    def _close_predictors(children, *, force=False):
        if children:
            with ThreadPoolExecutor(max_workers=len(children)) as closer:
                futures = [closer.submit(child.close, force=force) for child in children]
                for future in futures:
                    future.result()

    def _add_predictors(self, count):
        children, ready = [], []
        try:
            for _ in range(count):
                children.append(self._child_factory(*self._settings))
            ready = [self.executor.submit(child.ready) for child in children]
            for _ in _ordered_results(ready, PREDICTOR_STARTUP_TIMEOUT_SECONDS):
                pass
        except BaseException:
            # Expansion must not replace the healthy predictor or leave a new
            # CUDA context/pipe-reader alive after a partial handshake failure.
            self._close_predictors(children, force=True)
            for future in ready:
                with contextlib.suppress(Exception):
                    future.result()
            raise
        self.children.extend(children)

    def _start_pool(self, count=3):
        self.executor = ThreadPoolExecutor(max_workers=3)
        self._add_predictors(count)
        self.worker_count = count

    def _stop_pool(self, *, force=False):
        children, self.children = self.children, []
        try:
            # Close all processes before joining inference-reader threads.
            # Parallel close also prevents a timeout tripling per child.
            self._close_predictors(children, force=force)
        finally:
            if self.executor is not None:
                self.executor.shutdown(wait=True, cancel_futures=True)
                self.executor = None

    def _use_serial(self, error):
        self._stop_pool(force=True)
        self.worker_count = 1
        self.execution_fallback_reason = _error_code(error)
        self._resource_waiting = (self.execution_device.startswith("gpu")
                                  and self.execution_fallback_reason in _RESOURCE_FALLBACK_REASONS)
        if self._resource_waiting:
            # Keep CUDA in an owned child: deleting an in-process Paddle model
            # does not guarantee that its allocator/context releases memory.
            self._start_pool(1)
            delay = RESOURCE_PROBE_INTERVAL_SECONDS
            if self.execution_fallback_reason in {"gpu_oom", "ocr_cuda_out_of_memory"}:
                delay = self._promotion_backoff()
            self._next_resource_probe = time.monotonic() + delay
        else:
            self._serial_runtime = PaddleRuntime(*self._settings)
        print(json.dumps({"ocr_workers": 1, "fallback": self.execution_fallback_reason}),
              file=sys.stderr, flush=True)

    def _promotion_backoff(self):
        self._promotion_failures += 1
        return min(300, RESOURCE_PROMOTION_RETRY_SECONDS
                   * 2 ** min(self._promotion_failures - 1, 3))

    def _maybe_expand_pool(self):
        if not self._resource_waiting or time.monotonic() < self._next_resource_probe:
            return
        self._next_resource_probe = time.monotonic() + RESOURCE_PROBE_INTERVAL_SECONDS
        reason = _parallel_memory_reason(self.execution_device)
        if reason:
            self.execution_fallback_reason = reason
            return
        try:
            # No inference is pending at this boundary. Keep the existing
            # predictor and coordinator caches; never load a fourth model.
            self._add_predictors(3 - len(self.children))
        except Exception as error:
            reason = self.execution_fallback_reason = _error_code(error)
            self._resource_waiting = reason in (
                _RESOURCE_FALLBACK_REASONS | _TRANSIENT_PREDICTOR_ERRORS)
            self._next_resource_probe = time.monotonic() + self._promotion_backoff()
            print(json.dumps({"ocr_workers": 1, "promotion_failed": reason}),
                  file=sys.stderr, flush=True)
            return
        self.worker_count = 3
        self.execution_fallback_reason = ""
        self._resource_waiting = False
        print(json.dumps({"ocr_workers": 3, "resource_promotion": "memory_available"}),
              file=sys.stderr, flush=True)

    def _restart_pool(self, error):
        reason = _error_code(error)
        if self.execution_recovery_attempts or reason not in _TRANSIENT_PREDICTOR_ERRORS:
            self._use_serial(error)
            return False
        self.execution_recovery_attempts = 1
        self.execution_recovery_reason = reason
        # Release every old CUDA context before probing memory or creating a
        # replacement. Never keep a serial model alive beside a new pool.
        self._stop_pool(force=True)
        memory_reason = _parallel_memory_reason(self.execution_device)
        if memory_reason:
            self._use_serial(RuntimeError(memory_reason))
            return False
        try:
            self._start_pool()
        except Exception as restart_error:
            self._use_serial(restart_error)
            return False
        print(json.dumps({"ocr_workers": 3, "recovery": reason, "attempts": 1}),
              file=sys.stderr, flush=True)
        return True

    def _recognize_frames(self, frames, bands):
        if self._closed:
            raise RuntimeError("engine_not_open")
        if len(frames) != len(bands):
            raise ValueError("paddle_band_count_mismatch")
        if not frames:
            return []
        if self._serial_runtime is not None:
            return self._serial_runtime.recognize_batched(frames, bands)
        self._maybe_expand_pool()
        for _ in range(2):
            try:
                return self._recognize_parallel(frames, bands)
            except Exception as error:
                # Cache/evidence publication happens only after a complete
                # replay, never from the successful subset of a failed batch.
                if self._restart_pool(error):
                    continue
                if self._serial_runtime is not None:
                    return self._serial_runtime.recognize_batched(frames, bands)
                return self._recognize_parallel(frames, bands)

    def _recognize_parallel(self, frames, bands):
        count = min(len(self.children), len(frames))
        partitions = [list(range(index, len(frames), count)) for index in range(count)]
        futures = [self.executor.submit(
            self.children[index].recognize,
            [frames[item] for item in indexes], [bands[item] for item in indexes],
        ) for index, indexes in enumerate(partitions)]
        readings = [None] * len(frames)
        for indexes, results in zip(partitions, _ordered_results(
            futures, PREDICTOR_REQUEST_TIMEOUT_SECONDS,
        ), strict=True):
            if len(results) != len(indexes) or any(
                not isinstance(reading, dict) for reading in results
            ):
                raise RuntimeError("ocr_predictor_result_count_mismatch")
            for index, reading in zip(indexes, results, strict=True):
                readings[index] = reading
        return readings

    def close(self, *, force=False):
        if self._closed:
            return
        self._closed = True
        try:
            self._stop_pool(force=force)
        finally:
            self._serial_runtime = None


def _paddle_runtime(mode, device, batch_size, worker_count=3):
    if type(worker_count) is not int or worker_count not in (1, 3):
        raise ValueError("invalid_ocr_worker_count")
    if worker_count == 3 and device.startswith("gpu"):
        return ParallelPaddleRuntime(mode, device, batch_size,
                                     defer_reason=_parallel_memory_reason(device))
    runtime = PaddleRuntime(mode, device, batch_size)
    runtime.worker_count = 1
    runtime.execution_fallback_reason = ""
    return runtime


def _serve_predictor(args):
    output = sys.stdout.buffer
    try:
        with contextlib.redirect_stdout(sys.stderr):
            runtime = PaddleRuntime(args.predictor_mode, args.device, args.batch_size)
        _send_packet(output, (True, "ready"))
        while True:
            request = _receive_packet(sys.stdin.buffer)
            if request is None:
                return 0
            frames, bands = request
            with contextlib.redirect_stdout(sys.stderr):
                readings = runtime.recognize_batched(frames, bands)
            _send_packet(output, (True, readings))
    except Exception as error:
        with contextlib.suppress(OSError, ValueError):
            _send_packet(output, (False, _error_code(error)))
        return 1


class RapidRuntime:
    provider_id = "rapid-compatibility"
    model_id = "PP-OCRv6-tiny-small"

    def __init__(self, _mode, _device, _batch_size):
        from rapidocr import RapidOCR

        self.engine = RapidOCR()
        self.execution_device = "cpu"

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

    def __init__(self, path, capture_factory=None, *, frame_cache_bytes=None):
        if frame_cache_bytes is None:
            available_mb = _available_ram_mb()
            frame_cache_bytes = min(
                MAX_DECODED_FRAME_CACHE_BYTES,
                max(0, (available_mb or 0) - 2048) // 32 * 1024 * 1024,
            )
        if (type(frame_cache_bytes) is not int
                or not 0 <= frame_cache_bytes <= MAX_DECODED_FRAME_CACHE_BYTES):
            raise ValueError("invalid_frame_cache_budget")
        self._frame_cache_budget = frame_cache_bytes
        self._frame_cache = OrderedDict()
        self._frame_cache_size = 0
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
        try:
            self.capture.release()
        finally:
            self._frame_cache.clear()
            self._frame_cache_size = 0

    def _disable_frame_cache(self):
        self._frame_cache.clear()
        self._frame_cache_size = 0
        self._frame_cache_budget = 0

    def _cache_frame(self, target, frame):
        size = getattr(frame, "nbytes", 0)
        if (type(size) is not int or not 0 < size <= self._frame_cache_budget
                or not callable(getattr(frame, "copy", None))):
            return
        while self._frame_cache_size + size > self._frame_cache_budget:
            _, evicted = self._frame_cache.popitem(last=False)
            self._frame_cache_size -= evicted.nbytes
        try:
            # Callers may mask/crop/edit their returned pixels. Neither first
            # reads nor cache hits expose the cache's own frame storage.
            self._frame_cache[target] = frame.copy()
            self._frame_cache_size += size
        except MemoryError:
            self._disable_frame_cache()

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
            cached = self._frame_cache.get(target)
            if cached is not None:
                try:
                    frames.append(cached.copy())
                except MemoryError:
                    self._disable_frame_cache()
                else:
                    self._frame_cache.move_to_end(target)
                    # A hit does not move the physical decoder. The next miss
                    # must still seek/grab from its real cursor, not this target.
                    continue
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
            self._cache_frame(target, frame)
        return frames


class OcrEngineHost:
    READING_CACHE_LIMIT = 256

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
        self._reading_cache = OrderedDict()

    def close(self):
        try:
            closer = getattr(self.runtime, "close", None)
            if callable(closer):
                closer()
        finally:
            if self.video is not None:
                self.video.close()
            self.video = None
            self.runtime = None
            self._reading_cache.clear()
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
        try:
            if runtime_factory is not None:
                self.runtime = runtime_factory(mode, device, batch_size)
            elif mode == "compatibility":
                self.runtime = RapidRuntime(mode, device, batch_size)
            else:
                self.runtime = _paddle_runtime(
                    mode, device, batch_size, payload.get("worker_count", 3),
                )
        except BaseException:
            self.close()
            raise
        self.mode = mode
        return {
            "provider_id": self.runtime.provider_id,
            "model_id": self.runtime.model_id,
            "duration_us": self.video.duration_us,
            "fps": self.video.fps,
            "frame_count": getattr(self.video, "frame_count", 0),
            "worker_count": getattr(self.runtime, "worker_count", 1),
            "execution_fallback_reason": getattr(self.runtime, "execution_fallback_reason", ""),
            "execution_recovery_attempts": getattr(self.runtime, "execution_recovery_attempts", 0),
            "execution_recovery_reason": getattr(self.runtime, "execution_recovery_reason", ""),
            "execution_device": getattr(self.runtime, "execution_device", ""),
            "frame_cache_limit_bytes": getattr(self.video, "_frame_cache_budget", 0),
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
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=(
                cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
            ))
        return crop

    def _recognize_cached(self, crops, crop_bands, recognize_batched):
        """Reuse only byte-identical model inputs, never similar glyphs/frames.

        Global and adaptive passes revisit the same samples. Static subtitle
        crops also repeat across frames. Keep bounded, lightweight readings;
        evidence IDs, timestamps, geometry and scene metadata are still rebuilt
        for every observation below. A mode/media switch clears the cache.
        """
        keys = []
        pending = {}
        for index, (crop, band) in enumerate(zip(crops, crop_bands, strict=True)):
            key = None
            if callable(getattr(crop, "tobytes", None)):
                key = (band, tuple(crop.shape), str(crop.dtype),
                       hashlib.sha256(crop.tobytes()).digest())
            keys.append(key)
            if key is not None and key in self._reading_cache:
                self._reading_cache.move_to_end(key)
            else:
                pending.setdefault(key if key is not None else index, index)
        indexes = list(pending.values())
        fresh = recognize_batched([crops[i] for i in indexes],
                                  [crop_bands[i] for i in indexes]) if indexes else []
        if len(fresh) != len(indexes):
            raise RuntimeError("ocr_result_count_mismatch")
        by_key = dict(zip(pending, fresh, strict=True))
        # Resolve every result before eviction: one batch can exceed the limit.
        readings = [self._reading_cache[key] if key is not None and key in self._reading_cache
                    else by_key[key if key is not None else i] for i, key in enumerate(keys)]
        for i, reading in zip(indexes, fresh, strict=True):
            if keys[i] is not None:
                self._reading_cache[keys[i]] = reading
        while len(self._reading_cache) > self.READING_CACHE_LIMIT:
            self._reading_cache.popitem(last=False)
        return readings

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
        reset_change_tracking = payload.get("reset_change_tracking", False)
        change_threshold = payload.get("change_threshold", 0.115)
        scene_change_threshold = payload.get("scene_change_threshold", 0.10)
        if type(detect_changes) is not bool:
            raise ValueError("invalid_change_detection_flag")
        if type(reset_change_tracking) is not bool:
            raise ValueError("invalid_change_tracking_reset")
        if reset_change_tracking:
            self._change_signature = None
            self._change_event_us = -1_000_000
            self._scene_change_signature = None
            self._scene_change_event_us = -1_000_000
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
                scene_enabled=payload.get("detect_scene_changes", True) is not False,
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
            readings = self._recognize_cached(crops, crop_bands, recognize_batched)
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
        scene_probes = []
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
            is_scene = str(band.get("reading_mode", name)) == "scene"
            if is_scene:
                region = [float(band["x1"]), float(band["y1"]),
                          float(band["x2"]) - float(band["x1"]),
                          float(band["y2"]) - float(band["y1"])]
                scene_probes.append({
                    "id": "scene-probe-" + hashlib.sha256(
                        f"{self.runtime.provider_id}|{frame_id}|{name}|{region}".encode()
                    ).hexdigest()[:24],
                    "record_kind": "scene_probe", "frame_id": frame_id,
                    "timestamp_us": timestamp_us, "region": region,
                    "provider_id": self.runtime.provider_id,
                })
            if not reading.get("text"):
                continue
            if is_scene:
                for segment_index, segment in enumerate(
                    _group_scene_segments(reading.get("segments", []))
                ):
                    text = str(segment.get("text", "")).strip()
                    box = _box_to_full_frame(segment.get("box"), band)
                    if not text or box is None or not re.search(r"[\u3400-\u9fff]", text):
                        continue
                    target = band.get("target_box")
                    if target is not None:
                        intersection = _box_overlap_fraction(box, target) * min(
                            box[2] * box[3], target[2] * target[3])
                        if intersection < target[2] * target[3] * .50:
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
                    if (name not in exclusions_by_band and box[1] >= 0.72
                            and box[2] >= 0.18 and box[3] <= 0.18):
                        continue
                    if any(_box_overlap_fraction(box, region) >= .30 for region in (
                        *global_exclusions, *exclusions_by_band.get(name, ())
                    )):
                        continue
                    metadata = _scene_segment_metadata(frame, box, text)
                    if metadata is None:
                        continue
                    if float(metadata.get("scene_confidence", 0.0)) < 0.45:
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
                            "band": "scene",
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
            "scene_probes": scene_probes,
            "watermark_detections": watermark_detections,
            "event_timestamps_us": change_events,
            "scene_event_timestamps_us": scene_change_events,
            "processed_until_us": timestamps[-1],
            "frame_count": len(timestamps),
            "execution_device": getattr(self.runtime, "execution_device", ""),
            "worker_count": getattr(self.runtime, "worker_count", 1),
            "execution_fallback_reason": getattr(self.runtime, "execution_fallback_reason", ""),
            "execution_recovery_attempts": getattr(self.runtime, "execution_recovery_attempts", 0),
            "execution_recovery_reason": getattr(self.runtime, "execution_recovery_reason", ""),
        }

    def _integrated_change_events(
        self,
        frames,
        timestamps,
        bands,
        threshold,
        scene_threshold,
        *,
        scene_enabled=True,
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
            full_signature = None
            if scene_enabled:
                full_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                full_signature = cv2.resize(
                    full_gray,
                    (96, 54),
                    interpolation=cv2.INTER_AREA,
                ).astype(np.float32) / 255.0
            previous = self._change_signature
            if previous is not None and len(previous) == len(signature):
                score = max(
                    max(float(np.mean(np.abs(current - earlier))),
                        float(np.percentile(np.abs(current - earlier), 99.8)))
                    for current, earlier in zip(signature, previous, strict=True)
                )
                if score >= threshold and timestamp - self._change_event_us >= 180_000:
                    events.append(timestamp)
                    self._change_event_us = timestamp
            previous_scene = self._scene_change_signature
            if scene_enabled and previous_scene is not None:
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
                output_stream.write(_response(request_id, error={"code": _error_code(exc)}))
            output_stream.flush()
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictor-mode", choices=("fast", "quality"))
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--batch-size", type=int, default=12)
    args = parser.parse_args()
    raise SystemExit(_serve_predictor(args) if args.predictor_mode else serve())
