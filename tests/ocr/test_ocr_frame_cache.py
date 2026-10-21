from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HOST_PATH = Path(__file__).parents[2] / "src/ducai_studio/engine_hosts/ocr_engine_host.py"


class Frame:
    nbytes = 6

    def __init__(self, index):
        self.pixels = [index] * 6

    def copy(self):
        result = Frame(0)
        result.pixels = self.pixels.copy()
        return result


@pytest.fixture
def setup_session(monkeypatch):
    spec = importlib.util.spec_from_file_location("ocr_cached_host_test", HOST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cv2 = SimpleNamespace(CAP_PROP_FPS=1, CAP_PROP_FRAME_COUNT=2, CAP_PROP_POS_FRAMES=3)
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setattr(module, "_available_ram_mb", lambda: 32_768)

    class Capture:
        def __init__(self, _path):
            self.position = 0
            self.reads = 0
            self.grabs = 0
            self.seeks = []
            self.released = False
            self.fail_at = None

        def isOpened(self):
            return True

        def get(self, key):
            return 10 if key == cv2.CAP_PROP_FPS else 100

        def set(self, _key, target):
            self.position = target
            self.seeks.append(target)
            return True

        def read(self):
            self.reads += 1
            if self.released or self.position == self.fail_at:
                return False, None
            result = Frame(self.position)
            self.position += 1
            return True, result

        def grab(self):
            self.grabs += 1
            self.position += 1
            return True

        def release(self):
            self.released = True

    def create(budget=18):
        capture = Capture(HOST_PATH)
        session = module.VideoSession(HOST_PATH, capture_factory=lambda _path: capture,
                                      frame_cache_bytes=budget)
        return session, capture

    return module, create


def test_repeated_frames_reuse_exact_pixels_without_redecoding(setup_session):
    _module, create = setup_session
    session, capture = create()
    first = session.read_many([0, 200_000, 400_000])
    assert [frame.pixels[0] for frame in first] == [0, 2, 4]
    repeated = session.read_many([200_000, 200_001, 400_000, 600_000])
    assert [frame.pixels[0] for frame in repeated] == [2, 2, 4, 6]
    assert capture.reads == 4
    assert capture.seeks == []
    assert session._frame_cache_size <= session._frame_cache_budget
    session.close()


def test_cache_never_exposes_pixels_owned_by_another_call(setup_session):
    _module, create = setup_session
    session, _capture = create()
    first = session.read_many([200_000])[0]
    first.pixels[0] = 99
    second, third = session.read_many([200_000, 200_001])
    second.pixels[0] = 77
    assert third.pixels[0] == 2
    assert session.read_many([200_000])[0].pixels[0] == 2
    session.close()


def test_cache_eviction_is_byte_bounded_and_lru(setup_session):
    _module, create = setup_session
    session, capture = create(12)
    session.read_many([0, 100_000])
    session.read_many([0, 200_000])
    assert list(session._frame_cache) == [0, 2]
    session.read_many([100_000])
    assert capture.reads == 4
    assert session._frame_cache_size == 12
    session.close()
    assert session._frame_cache_size == 0 and not session._frame_cache
    with pytest.raises(RuntimeError, match="video_decode_failed"):
        session.read_many([100_000])


@pytest.mark.parametrize("budget", [0, 5])
def test_disabled_or_undersized_cache_preserves_reads(setup_session, budget):
    _module, create = setup_session
    session, capture = create(budget)
    session.read_many([200_000, 200_000])
    assert capture.reads == 2 and capture.seeks == [2]
    assert not session._frame_cache and session._frame_cache_size == 0
    session.close()


def test_eof_tolerance_and_real_decode_errors_are_not_hidden(setup_session):
    _module, create = setup_session
    session, capture = create()
    final = session.read_many([session.duration_us])[0]
    again = session.read_many([9_900_000])[0]
    assert final.pixels == again.pixels == [99] * 6 and capture.reads == 1
    with pytest.raises(RuntimeError, match="video_timestamp_out_of_range"):
        session.read_many([10_100_000])
    capture.fail_at = 98
    with pytest.raises(RuntimeError, match="video_decode_failed"):
        session.read_many([9_800_000])
    assert 98 not in session._frame_cache
    session.close()


def test_cache_is_session_owned_not_shared_between_media(setup_session):
    _module, create = setup_session
    first, capture_a = create()
    second, capture_b = create()
    first.read_many([300_000])
    second.read_many([300_000])
    assert capture_a.reads == capture_b.reads == 1
    first.close()
    assert second.read_many([300_000])[0].pixels[0] == 3
    assert capture_b.reads == 1
    second.close()


@pytest.mark.parametrize("budget", [-1, True, "12", 1.5, 512 * 1024 ** 2])
def test_invalid_or_excessive_cache_budget_rejected(setup_session, budget):
    _module, create = setup_session
    with pytest.raises(ValueError, match="invalid_frame_cache_budget"):
        create(budget)


@pytest.mark.parametrize("free_mb,expected", [(None, 0), (1024, 0), (2048, 0),
                                            (4096, 64), (65536, 256)])
def test_automatic_budget_reserves_memory_and_never_exceeds_cap(
    setup_session, monkeypatch, free_mb, expected,
):
    module, create = setup_session
    monkeypatch.setattr(module, "_available_ram_mb", lambda: free_mb)
    session, _capture = create(None)
    assert session._frame_cache_budget == expected * 1024 ** 2
    session.close()


def test_failed_cache_allocation_disables_optimization_not_ocr(
    setup_session, monkeypatch,
):
    _module, create = setup_session
    session, capture = create()
    monkeypatch.setattr(Frame, "copy", lambda _self: (_ for _ in ()).throw(MemoryError()))
    assert session.read_many([200_000])[0].pixels[0] == 2
    assert session._frame_cache_budget == 0 and not session._frame_cache
    assert session.read_many([200_000])[0].pixels[0] == 2 and capture.reads == 2
    session.close()
