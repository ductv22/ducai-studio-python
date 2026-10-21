"""Asynchronous, cached real frame/audio images for visible Timeline clips."""

import base64
import binascii
import json
import subprocess
from collections import OrderedDict
from pathlib import Path
from threading import Event, Lock, Semaphore

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from ducai_studio.providers.preview import (
    FfmpegPreviewTileProvider,
    PreviewTileCache,
    PreviewTileRequest,
    TileKind,
    fast_media_fingerprint,
)


def timeline_tile_url(path: str, tile: dict) -> str:
    payload = json.dumps(
        {
            "path": path,
            "start_us": tile["start_us"],
            "duration_us": tile["duration_us"],
            "waveform": tile["waveform"],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"image://timeline-media/{token}"


class TimelineMediaProvider(QQuickImageProvider):
    """Qt's async image loader owns decoding; no subprocess runs on the GUI.

    Negative results are bounded and keyed by media stat identity. Changing a
    file or restarting retries it, but a silent/missing stream is not decoded
    again for every viewport gesture. No fabricated waveform is substituted.
    """

    def __init__(
        self,
        cache_root: str | Path,
        *,
        max_concurrent_renders: int = 2,
    ):
        super().__init__(
            QQuickImageProvider.ImageType.Image,
            QQuickImageProvider.Flag.ForceAsynchronousImageLoading,
        )
        if isinstance(max_concurrent_renders, bool) or max_concurrent_renders < 1:
            raise ValueError("max_concurrent_renders must be a positive integer")
        self._cache = PreviewTileCache(cache_root)
        # Image requests are already dispatched asynchronously by Qt.  Reuse
        # the immutable FFmpeg resolver/configuration instead of resolving the
        # executable for every thumbnail or waveform request.  Keep creation
        # lazy so a machine without FFmpeg retains the existing per-request
        # failure behavior and the application can still open.
        self._renderer: FfmpegPreviewTileProvider | None = None
        self._failed: OrderedDict[str, None] = OrderedDict()
        self._lock = Lock()
        # Qt dispatches image-provider work asynchronously, but a cold
        # viewport can still enqueue hundreds of strips at the same time.
        # Bound only the expensive FFmpeg section; cache hits and QImage
        # decoding remain concurrent and the GUI thread is never involved.
        self._render_slots = Semaphore(int(max_concurrent_renders))
        # Multiple QML delegates can ask for one URL before the first render
        # commits it.  Followers wait for that one render instead of starting
        # duplicate FFmpeg processes.
        self._inflight: dict[str, Event] = {}

    def _renderer_for_request(self) -> FfmpegPreviewTileProvider:
        renderer = self._renderer
        if renderer is not None:
            return renderer
        with self._lock:
            renderer = self._renderer
            if renderer is None:
                renderer = FfmpegPreviewTileProvider(timeout=20)
                self._renderer = renderer
            return renderer

    def requestImage(self, image_id: str, size: QSize, requested_size: QSize) -> QImage:
        failure_key = image_id
        inflight_event: Event | None = None
        owns_render = False
        try:
            token = image_id.split("?", 1)[0].strip("/")
            token += "=" * (-len(token) % 4)
            payload = json.loads(base64.urlsafe_b64decode(token).decode("utf-8"))
            path = Path(payload["path"]).expanduser().resolve(strict=True)
            fingerprint = fast_media_fingerprint(path)
            kind = TileKind.WAVEFORM if payload["waveform"] else TileKind.THUMBNAIL
            request = PreviewTileRequest(
                path,
                fingerprint,
                kind,
                int(payload["start_us"]),
                int(payload["duration_us"]),
                256 if kind is TileKind.WAVEFORM else 160,
                40 if kind is TileKind.WAVEFORM else 90,
            )
            failure_key = request.cache_key
            with self._lock:
                if failure_key in self._failed:
                    return QImage()

                inflight_event = self._inflight.get(failure_key)
                if inflight_event is None:
                    inflight_event = Event()
                    self._inflight[failure_key] = inflight_event
                    owns_render = True

            if not owns_render:
                # The owner always signals, including timeout/decoder failure.
                # The cache is the hand-off so followers do not need an
                # unbounded in-memory image map.
                inflight_event.wait()
                with self._lock:
                    if failure_key in self._failed:
                        return QImage()
                target = self._cache.get(request)
                if target is None:
                    return QImage()
            else:
                target = self._cache.get(request)
                if target is None:
                    with self._render_slots:
                        target, _reused = self._renderer_for_request().render(
                            request, self._cache
                        )
            image = QImage(str(target))
            if image.isNull():
                return QImage()
            size.setWidth(image.width())
            size.setHeight(image.height())
            return image
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
            binascii.Error,
            subprocess.TimeoutExpired,
        ):
            with self._lock:
                self._failed[failure_key] = None
                self._failed.move_to_end(failure_key)
                if len(self._failed) > 256:
                    self._failed.popitem(last=False)
            return QImage()
        finally:
            if owns_render and inflight_event is not None:
                with self._lock:
                    self._inflight.pop(failure_key, None)
                    inflight_event.set()
