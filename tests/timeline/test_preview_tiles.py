from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImageReader

from ducai_studio.providers.preview import (
    FfmpegPreviewTileProvider,
    PreviewTileCache,
    PreviewTileRequest,
    TileKind,
    fast_media_fingerprint,
)
from ducai_studio.ui.multi_thumbnail_provider import (
    MultiThumbnailProvider,
    multi_thumbnail_url,
)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
@pytest.mark.parametrize("kind", [TileKind.THUMBNAIL, TileKind.WAVEFORM])
def test_ffmpeg_preview_tiles_are_real_pngs_and_reused_from_cache(
    tmp_path: Path, kind: TileKind
) -> None:
    media = tmp_path / "tile source.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-y",
            str(media),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    request = PreviewTileRequest(
        media,
        fast_media_fingerprint(media),
        kind,
        0,
        500_000,
        160,
        48,
    )
    cache = PreviewTileCache(tmp_path / "cache")
    provider = FfmpegPreviewTileProvider()

    first, first_was_cached = provider.render(request, cache)
    first_mtime = first.stat().st_mtime_ns
    second, second_was_cached = provider.render(request, cache)

    assert first_was_cached is False
    assert second_was_cached is True
    assert second == first
    assert second.stat().st_mtime_ns == first_mtime
    image = QImageReader(str(first))
    assert image.canRead()
    assert image.size().width() > 0
    assert image.size().height() > 0


def test_preview_tile_request_rejects_missing_media_and_invalid_dimensions(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        PreviewTileRequest(
            tmp_path / "missing.mp4",
            "fingerprint",
            TileKind.THUMBNAIL,
            0,
            1_000_000,
            160,
            90,
        )

    media = tmp_path / "media.mp4"
    media.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="dimensions"):
        PreviewTileRequest(
            media,
            "fingerprint",
            TileKind.THUMBNAIL,
            0,
            1_000_000,
            8,
            8,
        )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
def test_multi_thumbnail_provider_decodes_descriptor_and_reuses_cached_png(
    tmp_path: Path,
) -> None:
    media = tmp_path / "thẻ video.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=10",
            "-t",
            "0.5",
            "-c:v",
            "libx264",
            "-y",
            str(media),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    url = multi_thumbnail_url(media, 500_000).toString()
    image_id = url.removeprefix("image://multi-thumbnail/")
    provider = MultiThumbnailProvider(tmp_path / "thumbnail-cache")

    first = provider.requestImage(image_id, QSize(), QSize(320, 180))
    second = provider.requestImage(image_id, QSize(), QSize(320, 180))

    assert not first.isNull()
    assert first.size() == QSize(320, 180)
    assert second == first
    assert len(list((tmp_path / "thumbnail-cache").rglob("*.png"))) == 1
