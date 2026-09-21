from __future__ import annotations

import json
from pathlib import Path

from ducai_studio.providers.tts.catalog import VoiceCatalog, VoiceRightsState


def test_catalog_preserves_stable_id_readiness_and_rights(tmp_path: Path) -> None:
    voice_root = tmp_path / "piper_voices"
    voice_root.mkdir()
    model = voice_root / "nghitts-chieu-thanh.onnx"
    model.write_bytes(b"fixture-model")
    model.with_suffix(".onnx.json").write_text(
        json.dumps(
            {
                "audio": {"sample_rate": 22_050},
                "phoneme_id_map": {"_": [0]},
            }
        ),
        encoding="utf-8",
    )

    catalog = VoiceCatalog(tmp_path)
    installed = catalog.get("nghitts-chieu-thanh", require_installed=True)
    restricted = catalog.get("nghitts-tran-thanh")

    assert installed.installed is True
    assert installed.model_fingerprint
    assert installed.name == "Chiếu Thành"
    assert restricted.rights_state is VoiceRightsState.PERMISSION_REQUIRED
    assert "quyền" in restricted.rights_label.casefold()
    assert len(catalog.entries()) == 23


def test_catalog_rejects_incomplete_onnx_pair(tmp_path: Path) -> None:
    voice_root = tmp_path / "piper_voices"
    voice_root.mkdir()
    (voice_root / "nghitts-chieu-thanh.onnx").write_bytes(b"fixture-model")

    entry = VoiceCatalog(tmp_path).get("nghitts-chieu-thanh")

    assert entry.installed is False
    assert not entry.model_path
