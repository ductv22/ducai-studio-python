import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest
from PySide6.QtCore import QCoreApplication, QSize
from PySide6.QtGui import QImage

from ducai_studio.application.timeline_previews import visible_media_tiles
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import SpeedRatio, TimeRange
from ducai_studio.domain.timeline_edit import cut_state
from ducai_studio.domain.voice_conform import (
    VoiceConformCueInput,
    VoiceConformSettings,
    VoiceTimingMode,
    build_voice_conform_plan,
)
from ducai_studio.providers.preview import PreviewTileRequest, TileKind, fast_media_fingerprint
from ducai_studio.ui.timeline_media_provider import TimelineMediaProvider, timeline_tile_url
from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel

S = 1_000_000


def segment(start=0, end=100 * S, source_start=0, source_end=100 * S):
    return dict(start_us=start, end_us=end, source_start_us=source_start, source_end_us=source_end)


def test_media_tiles_handle_empty_conform_and_bound_wide_monitor_requests():
    assert not visible_media_tiles([], 0, S, 5000 / S, waveform=False)
    tiles = visible_media_tiles([segment()], 0, 100 * S, 8000 / (100 * S), waveform=False)
    assert len(tiles) <= 32
    assert tiles[-1]["x"] + tiles[-1]["width"] == pytest.approx(8000)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Real tiles require FFmpeg")
@pytest.mark.parametrize("duration_us,fps", [(300_000, 10), (60 * S, 1)])
def test_partial_end_bucket_decodes_a_real_frame_instead_of_seeking_past_last_frame(
    tmp_path, duration_us, fps,
):
    source = tmp_path / "source.mp4"
    frames = ("color=c=red:s=160x90:r=10,"
              "drawbox=c=green:t=fill:enable='eq(n,1)',"
              "drawbox=c=blue:t=fill:enable='eq(n,2)'"
              if duration_us == 300_000 else f"color=c=red:s=160x90:r={fps}")
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        frames, "-t", str(duration_us / S), "-c:v", "libx264",
        "-threads", "1", "-y", str(source)], check=True, capture_output=True, timeout=20)
    tiles = visible_media_tiles([segment(end=duration_us, source_end=duration_us)],
        0, duration_us, 900 / duration_us, waveform=False)
    last = tiles[-1]
    assert 0 <= last["start_us"] < duration_us
    provider = TimelineMediaProvider(tmp_path / "tiles")
    image_id = timeline_tile_url(str(source), last).split("/", 3)[-1]
    image = provider.requestImage(image_id, QSize(), QSize())
    assert not image.isNull()
    color = image.pixelColor(40, 40)
    assert (color.blue() if duration_us == 300_000 else color.red()) > 200


@pytest.mark.parametrize("waveform", (True, False))
def test_media_buckets_cover_viewport_and_reuse_unchanged_source_requests(waveform):
    first = visible_media_tiles([segment()], 20 * S, 30 * S, 900 / (10 * S), waveform=waveform)
    second = visible_media_tiles(
        [segment()], 20 * S + 1000, 30 * S + 1000, 900 / (10 * S), waveform=waveform
    )
    assert 1 <= len(first) <= 32
    assert {(t["start_us"], t["duration_us"]) for t in first} <= {
        (t["start_us"], t["duration_us"]) for t in second
    }
    assert len(second) <= len(first) + 1  # Only a newly exposed boundary bucket.
    regions = [r for t in first for r in (t.get("regions") or [t])]
    assert regions[0]["x"] == pytest.approx(0)
    assert regions[-1]["x"] + regions[-1]["width"] == pytest.approx(900)


def test_hundreds_of_conform_spans_do_not_start_one_decode_per_cue():
    spans = [segment(i * S, (i + 1) * S, i * S // 2, (i + 1) * S // 2) for i in range(400)]
    tiles = visible_media_tiles(spans, 0, 400 * S, 900 / (400 * S), waveform=True)
    assert len(tiles) <= 16
    assert sum(len(t["regions"]) for t in tiles) >= 400
    regions = sorted([r for t in tiles for r in t["regions"]], key=lambda r: r["x"])
    assert regions[0]["x"] == 0
    assert regions[-1]["x"] + regions[-1]["width"] == pytest.approx(900)
    assert all(0 <= r["crop_start"] < r["crop_end"] <= 1 for r in regions)


def test_model_tiles_map_trim_speed_and_retained_timeline_without_media_io(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    clip = TimelineClip(
        "v", "V1", TimeRange(0, 20 * S), TimeRange(10 * S, 50 * S), "m", speed=SpeedRatio(2, 1)
    )
    project = ProjectSnapshot(
        "p",
        "Movie",
        1,
        media=(MediaAsset("m", "missing-source.mp4", MediaKind.VIDEO, 60 * S),),
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (clip,)),),
    )
    project = replace(project, timeline_edit=cut_state(project, "keep_from", 5 * S, 20 * S))
    model = ProjectStateModel()
    model.replace(json.loads(json.dumps(asdict(project))))
    # Descriptor construction must not stat/open even missing source media.
    monkeypatch.setattr("pathlib.Path.stat", lambda *_a, **_k: pytest.fail("GUI stat"))
    fragments = [c for c in model._clip_by_id.values() if c.get("media_id") == "m"]
    tiles = model.timelineMediaTiles(fragments[0]["id"], 0, 3 * S, 600)
    assert tiles and all(20 * S <= t["start_us"] < 26 * S for t in tiles)


def test_media_tiles_cover_conformed_output_end_without_double_mapping():
    QCoreApplication.instance() or QCoreApplication([])
    plan = build_voice_conform_plan(
        (VoiceConformCueInput("c", 1, 0, 2 * S, 4 * S),),
        source_duration_us=5 * S,
        settings=VoiceConformSettings(mode=VoiceTimingMode.STRETCH_VIDEO),
    )
    model = ProjectStateModel()
    model.replace(
        {
            "id": "p",
            "revision": 1,
            "name": "Conform",
            "voice_conform": {"plan": plan.as_json()},
            "media": [{"id": "m", "path": "source.mp4"}],
            "tracks": [
                {
                    "id": "V1",
                    "kind": "video",
                    "clips": [
                        {
                            "id": "v",
                            "track_id": "V1",
                            "media_id": "m",
                            "timeline_range": {"start_us": 0, "end_us": 5 * S},
                            "source_range": {"start_us": 0, "end_us": 5 * S},
                        }
                    ],
                }
            ],
        }
    )
    assert model.clips._track_indexes[1][0][0]["timeline_range"]["end_us"] == 7 * S
    tiles = model.timelineMediaTiles("v", 6 * S, 7 * S, 900)
    assert tiles and all(4 * S <= t["start_us"] < 5 * S for t in tiles)
    assert tiles[-1]["x"] + tiles[-1]["width"] == pytest.approx(900)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="Real tiles require FFmpeg")
def test_real_source_frames_waveform_and_silent_stream_unavailable(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x90:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-y",
            str(source),
        ],
        capture_output=True,
        check=True,
    )
    provider = TimelineMediaProvider(tmp_path / "tiles")

    def image(waveform, at=0):
        url = timeline_tile_url(
            str(source), {"start_us": at, "duration_us": S, "waveform": waveform}
        )
        return provider.requestImage(url.split("/", 3)[-1], QSize(), QSize())

    frame = image(False)
    assert not frame.isNull() and frame.pixelColor(80, 40).red() > 200
    waveform = image(True)
    assert not waveform.isNull()
    assert any(waveform.pixelColor(x, y).alpha() > 0 for x in range(256) for y in range(40))
    assert image(False) == frame and image(True) == waveform
    assert len(list((tmp_path / "tiles").rglob("*.png"))) == 2
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(source),
            "-an",
            "-c:v",
            "copy",
            str(silent),
        ],
        check=True,
        capture_output=True,
    )
    url = timeline_tile_url(str(silent), {"start_us": 0, "duration_us": S, "waveform": True})
    token = url.split("/", 3)[-1]
    assert provider.requestImage(token, QSize(), QSize()).isNull()


def test_timeline_media_provider_reuses_ffmpeg_configuration(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    provider = TimelineMediaProvider(tmp_path / "tiles")

    class Renderer:
        def render(self, *_args):
            return tmp_path / "missing.png", False

    renderer = Renderer()
    created: list[float] = []

    def factory(*, timeout: float):
        created.append(timeout)
        return renderer

    monkeypatch.setattr(
        "ducai_studio.ui.timeline_media_provider.FfmpegPreviewTileProvider",
        factory,
    )
    # The provider creates the renderer lazily and reuses the immutable
    # configuration for every asynchronous image request.
    assert provider._renderer_for_request() is renderer
    assert provider._renderer_for_request() is renderer
    assert created == [20]


def test_timeline_media_provider_bounds_cold_render_burst_and_coalesces_duplicates(
    tmp_path,
):
    """Opening a dense timeline must not spawn one FFmpeg process per strip."""

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    rendered = tmp_path / "rendered.png"
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(0xFF27BFA3)
    assert image.save(str(rendered))

    provider = TimelineMediaProvider(tmp_path / "tiles", max_concurrent_renders=2)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "calls": 0}

    class Renderer:
        def render(self, request, cache):
            # Match the real renderer's cache fast path: a caller scheduled
            # after the first owner finishes must not count as another decode.
            cached = cache.get(request)
            if cached is not None:
                return cached, True
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["calls"] += 1
            try:
                # Model a cold FFmpeg decode long enough for a synchronized
                # TimelineMediaStrip burst to overlap in the old provider.
                time.sleep(0.02)
                target = cache.commit(request, rendered)
                return target, False
            finally:
                with lock:
                    state["active"] -= 1

    provider._renderer = Renderer()
    urls = [
        timeline_tile_url(
            str(source),
            {"start_us": i * S, "duration_us": S, "waveform": bool(i % 2)},
        )
        for i in range(12)
    ]
    # The first URL is intentionally duplicated to prove that concurrent
    # callers wait for one render instead of decoding the same tile twice.
    urls.append(urls[0])

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        images = list(
            pool.map(
                lambda url: provider.requestImage(
                    url.split("/", 3)[-1], QSize(), QSize()
                ),
                urls,
            )
        )

    assert all(not image.isNull() for image in images)
    assert state["peak"] <= 2
    assert state["calls"] == len(urls) - 1


def test_cached_tile_is_not_queued_behind_unrelated_cold_decodes(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    provider = TimelineMediaProvider(tmp_path / "tiles")
    rendered = tmp_path / "rendered.png"
    expected = QImage(160, 90, QImage.Format.Format_ARGB32)
    expected.fill(0xFF27BFA3)
    assert expected.save(str(rendered))
    request = PreviewTileRequest(source, fast_media_fingerprint(source),
                                 TileKind.THUMBNAIL, 0, S, 160, 90)
    provider._cache.commit(request, rendered)
    url = timeline_tile_url(str(source), {"start_us": 0, "duration_us": S, "waveform": False})
    finished = threading.Event()

    def load():
        try:
            return provider.requestImage(url.split("/", 3)[-1], QSize(), QSize())
        finally:
            finished.set()

    provider._render_slots.acquire()
    provider._render_slots.acquire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(load)
        try:
            assert finished.wait(1), "Cached image waited for unrelated FFmpeg renders"
        finally:
            provider._render_slots.release()
            provider._render_slots.release()
        assert future.result(timeout=3) == expected
