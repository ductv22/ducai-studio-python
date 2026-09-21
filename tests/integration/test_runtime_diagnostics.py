from __future__ import annotations

import json
from pathlib import Path

from ducai_studio.infrastructure.runtime_diagnostics import run_runtime_diagnostics


def test_runtime_diagnostics_reports_every_managed_boundary_without_secrets(
    tmp_path: Path,
) -> None:
    report = run_runtime_diagnostics(
        data_root=tmp_path,
        executable_resolver=lambda _name: None,
    )
    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    components = {item.component for item in report.checks}

    assert report.schema == "ducai-runtime-diagnostics-v1"
    assert components == {
        "gui-python",
        "ffmpeg",
        "paddle-ocr",
        "gemini-web",
        "gemini-cli",
        "piper",
        "vieneu-3.3",
    }
    assert report.overall_status == "blocked"
    assert "api_key" not in serialized.casefold()
    assert "token=" not in serialized.casefold()
