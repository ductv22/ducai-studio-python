from __future__ import annotations

import base64
import json
from pathlib import Path

from PySide6.QtCore import QSize, QUrl
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from ducai_studio.providers.preview import (
    FfmpegPreviewTileProvider,
    PreviewTileCache,
    PreviewTileRequest,
    TileKind,
    fast_media_fingerprint,
)


def multi_thumbnail_url(media_path: object, duration_us: object) -> QUrl:
    path = str(media_path or "").strip()
    if not path:
        return QUrl()
    try:
        duration = max(1, int(duration_us))
    except (TypeError, ValueError):
        duration = 1
    payload = json.dumps(
        {"path": path, "duration_us": duration},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return QUrl(f"image://multi-thumbnail/{token}")


class MultiThumbnailProvider(QQuickImageProvider):
    """Render cached card thumbnails off the GUI thread on first visibility."""

    def __init__(self, cache_root: str | Path) -> None:
        super().__init__(
            QQuickImageProvider.ImageType.Image,
            QQuickImageProvider.Flag.ForceAsynchronousImageLoading,
        )
        self._cache = PreviewTileCache(cache_root)

    def requestImage(
        self,
        image_id: str,
        size: QSize,
        requested_size: QSize,
    ) -> QImage:
        try:
            payload = _decode_descriptor(image_id)
            media_path = Path(str(payload["path"])).expanduser().resolve(strict=True)
            duration_us = max(1, int(payload["duration_us"]))
            width = requested_size.width() if requested_size.width() >= 16 else 640
            height = requested_size.height() if requested_size.height() >= 16 else 360
            request = PreviewTileRequest(
                media_path=media_path,
                media_fingerprint=fast_media_fingerprint(media_path),
                kind=TileKind.THUMBNAIL,
                # The first decoded frame is the safest common timestamp for
                # very short videos. Cards are previews, not seek authority.
                start_us=0,
                duration_us=duration_us,
                width=min(width, 1280),
                height=min(height, 720),
            )
            thumbnail_path, _reused = FfmpegPreviewTileProvider(timeout=30).render(
                request, self._cache
            )
            image = QImage(str(thumbnail_path))
            if image.isNull():
                return QImage()
            size.setWidth(image.width())
            size.setHeight(image.height())
            return image
        except (FileNotFoundError, KeyError, TypeError, ValueError, RuntimeError):
            return QImage()


def _decode_descriptor(image_id: str) -> dict[str, object]:
    token = image_id.split("?", 1)[0].strip("/")
    token += "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Multi thumbnail descriptor must be an object")
    return value
