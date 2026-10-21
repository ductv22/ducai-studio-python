from types import SimpleNamespace

import pytest

from ducai_studio.providers import font_loading


def test_successful_font_path_does_not_read_a_second_copy(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(font_loading, "QFontDatabase", SimpleNamespace(
        addApplicationFont=lambda _path: 7,
        addApplicationFontFromData=lambda _data: calls.append("unexpected"),
    ))
    assert font_loading.load_application_font(tmp_path / "unused.ttf") == 7
    assert calls == []


@pytest.mark.parametrize("data_result", [8, -1])
def test_font_data_fallback_preserves_bytes_and_failure(tmp_path, monkeypatch, data_result):
    path = tmp_path / "font.ttf"
    path.write_bytes(b"bundled font bytes")
    calls = []

    def load_data(data):
        calls.append(bytes(data))
        return data_result

    monkeypatch.setattr(font_loading, "QFontDatabase", SimpleNamespace(
        addApplicationFont=lambda _path: -1, addApplicationFontFromData=load_data,
    ))
    assert font_loading.load_application_font(path) == data_result
    assert calls == [b"bundled font bytes"]


def test_unreadable_font_is_still_reported_as_failure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(font_loading, "QFontDatabase", SimpleNamespace(
        addApplicationFont=lambda _path: -1,
        addApplicationFontFromData=lambda _data: calls.append("unexpected"),
    ))
    assert font_loading.load_application_font(tmp_path / "missing.ttf") == -1
    assert calls == []
