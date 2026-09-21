from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from ducai_studio.application.media_commands import MediaImportService
from ducai_studio.domain.project import OverlayKind, ProjectSnapshot, TrackKind
from ducai_studio.infrastructure.legacy_importer import LegacyProjectImporter
from ducai_studio.providers.media_probe import MediaProbeResult


class LegacyProbe:
    def probe(self, path: str | Path) -> MediaProbeResult:
        candidate = Path(path)
        if candidate.suffix.casefold() == ".mp4":
            return MediaProbeResult(20_000_000, True, True, 1280, 720, "mp4")
        return MediaProbeResult(20_000_000, False, True, format_name="wav")


def _ids():
    value = 0
    while True:
        value += 1
        yield str(value)


def test_legacy_importer_is_read_only_redacts_secrets_and_imports_real_assets(
    tmp_path: Path,
) -> None:
    video = tmp_path / "source.mp4"
    voice = tmp_path / "source_VI_TTS.wav"
    source_srt = tmp_path / "source_ZH_FINAL.srt"
    video.write_bytes(b"video")
    voice.write_bytes(b"wav")
    source_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n我要修仙。\n",
        encoding="utf-8",
    )
    incompatible_cache = tmp_path / "legacy-cache"
    incompatible_cache.mkdir()
    legacy = tmp_path / "legacy-project.json"
    legacy.write_text(
        json.dumps(
            {
                "app": "DUCAI STUDIO",
                "version": "0.1.99",
                "media_items": [{"path": video.name}, {"path": source_srt.name}],
                "audio_tracks": [{"path": voice.name, "role": "voiceover"}],
                "blur_regions": [
                    {
                        "x": 0.1,
                        "y": 0.8,
                        "width": 0.75,
                        "height": 0.12,
                        "blur_radius": 28,
                    }
                ],
                "font_file": "Noto Sans",
                "tts_provider": "VieNeu Voice Clone Local",
                "api_key": "must-not-migrate",
                "cache_paths": [str(incompatible_cache)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = sha256(legacy.read_bytes()).hexdigest()
    ids = _ids()
    importer = LegacyProjectImporter(
        MediaImportService(LegacyProbe(), id_factory=lambda: next(ids))
    )

    dry_run = importer.inspect(legacy)
    snapshot, report = importer.import_project(
        legacy, ProjectSnapshot("project", "Legacy", 1)
    )

    assert sha256(legacy.read_bytes()).hexdigest() == before
    assert dry_run.dry_run is True and dry_run.source_unchanged is True
    assert report.dry_run is False and report.source_unchanged is True
    assert report.imported_settings == {
        "font_file": "Noto Sans",
        "tts_provider": "VieNeu Voice Clone Local",
    }
    assert any("credentials/secrets" in item for item in report.skipped)
    assert report.cache_decisions[0]["decision"].startswith("bỏ qua")
    assert len(snapshot.media) >= 3
    assert any(track.kind is TrackKind.VOICE for track in snapshot.tracks)
    assert [cue.text for cue in snapshot.source_cues] == ["我要修仙。"]
    blur = next(item for item in snapshot.overlays if item.kind is OverlayKind.BLUR)
    assert (blur.x, blur.y, blur.width, blur.height, blur.blur_radius) == (
        0.1,
        0.8,
        0.75,
        0.12,
        28,
    )


def test_legacy_caption_import_requires_valid_timing_and_complete_translation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.json"
    source.write_text(
        json.dumps(
            {
                "captions": [
                    {
                        "start": 0.5,
                        "end": 1.5,
                        "text": "第一句。",
                        "translated_text": "Câu thứ nhất.",
                    },
                    {"start_ms": 2000, "end_ms": 3200, "text": "第二句。"},
                    {"start": 5, "end": 4, "text": "时间错误"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    importer = LegacyProjectImporter(MediaImportService(LegacyProbe()))

    snapshot, _report = importer.import_project(
        source, ProjectSnapshot("project", "Legacy", 1)
    )

    assert [cue.text for cue in snapshot.source_cues] == ["第一句。", "第二句。"]
    assert snapshot.source_cues[0].range.start_us == 500_000
    assert snapshot.translated_cues == ()


def test_legacy_importer_copies_only_identity_proven_safe_cache(tmp_path: Path) -> None:
    cache = tmp_path / "legacy-cache"
    cache.mkdir()
    identity = {
        "schema": "ducai-studio-2-cache-v1",
        "kind": "tts",
        "input_fingerprint": "input-1",
        "provider_id": "vieneu-3.3",
        "provider_version": "3.3.0",
        "model_id": "v3turbo",
        "settings_fingerprint": "settings-1",
    }
    (cache / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (cache / "cue.wav").write_bytes(b"cache-audio")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"cache_paths": [str(cache)]}), encoding="utf-8")
    importer = LegacyProjectImporter(MediaImportService(LegacyProbe()))

    _snapshot, report = importer.import_project(
        legacy,
        ProjectSnapshot("project", "Legacy", 1),
        cache_import_root=tmp_path / "managed-cache",
    )

    assert len(report.imported_caches) == 1
    imported = Path(report.imported_caches[0])
    assert imported.is_dir()
    assert (imported / "cue.wav").read_bytes() == b"cache-audio"
    assert (cache / "cue.wav").read_bytes() == b"cache-audio"
    assert report.cache_decisions[0]["decision"].startswith("đã nhập một chiều")


def test_legacy_importer_rejects_secret_bearing_json_inside_cache(tmp_path: Path) -> None:
    cache = tmp_path / "legacy-cache"
    cache.mkdir()
    identity = {
        "schema": "ducai-studio-2-cache-v1",
        "kind": "tts",
        "input_fingerprint": "input-1",
        "provider_id": "vieneu-3.3",
        "provider_version": "3.3.0",
        "model_id": "v3turbo",
        "settings_fingerprint": "settings-1",
        "api_key": "must-not-migrate",
    }
    (cache / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (cache / "cue.wav").write_bytes(b"cache-audio")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"cache_paths": [str(cache)]}), encoding="utf-8")
    importer = LegacyProjectImporter(MediaImportService(LegacyProbe()))

    _snapshot, report = importer.import_project(
        legacy,
        ProjectSnapshot("project", "Legacy", 1),
        cache_import_root=tmp_path / "managed-cache",
    )

    assert report.imported_caches == ()
    assert "không an toàn" in report.cache_decisions[0]["decision"]
    assert not (tmp_path / "managed-cache").exists()


def test_legacy_importer_rejects_partial_target_but_reuses_identical_target(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "legacy-cache"
    cache.mkdir()
    identity = {
        "schema": "ducai-studio-2-cache-v1",
        "kind": "tts",
        "input_fingerprint": "input-1",
        "provider_id": "vieneu-3.3",
        "provider_version": "3.3.0",
        "model_id": "v3turbo",
        "settings_fingerprint": "settings-1",
    }
    identity_text = json.dumps(identity)
    (cache / "identity.json").write_text(identity_text, encoding="utf-8")
    (cache / "cue.wav").write_bytes(b"cache-audio")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"cache_paths": [str(cache)]}), encoding="utf-8")
    managed = tmp_path / "managed-cache"
    cache_id = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    target = managed / "tts" / cache_id
    target.mkdir(parents=True)
    (target / "identity.json").write_text(identity_text, encoding="utf-8")
    importer = LegacyProjectImporter(MediaImportService(LegacyProbe()))

    _snapshot, partial_report = importer.import_project(
        legacy,
        ProjectSnapshot("project", "Legacy", 1),
        cache_import_root=managed,
    )

    assert partial_report.imported_caches == ()
    assert "không trùng khớp" in partial_report.cache_decisions[0]["decision"]
    assert not (target / "cue.wav").exists()

    (target / "cue.wav").write_bytes(b"cache-audio")
    _snapshot, matching_report = importer.import_project(
        legacy,
        ProjectSnapshot("project", "Legacy", 1),
        cache_import_root=managed,
    )

    assert matching_report.imported_caches == (str(target),)
    assert matching_report.cache_decisions[0]["decision"].startswith(
        "đã có cache trùng khớp"
    )
