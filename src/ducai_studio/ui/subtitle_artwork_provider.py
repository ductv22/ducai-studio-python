from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import unquote

from PySide6.QtCore import QSize
from PySide6.QtQuick import QQuickImageProvider

from ducai_studio.providers.subtitle_artwork import layout_json, paint_artwork


class SubtitleArtworkProvider(QQuickImageProvider):
    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)

    @staticmethod
    @lru_cache(maxsize=8)
    def _image(key: str):
        payload_json = key if key.startswith("{") else unquote(key)
        payload = json.loads(payload_json)
        return paint_artwork(layout_json(payload_json), payload["width"], payload["height"])

    def requestImage(self, identifier: str, size: QSize, requested_size: QSize):
        image = self._image(identifier)
        size.setWidth(image.width())
        size.setHeight(image.height())
        return image
