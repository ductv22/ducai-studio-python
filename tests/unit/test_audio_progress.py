from __future__ import annotations

import sys

from ducai_studio.providers.audio_processing import _run_ffmpeg_with_progress


def test_ffmpeg_progress_reports_current_total_and_eta() -> None:
    events: list[tuple[str, int, int, int | None]] = []
    script = (
        "import sys,time; "
        "print('out_time_us=250000', flush=True); time.sleep(0.03); "
        "print('out_time_us=750000', flush=True); time.sleep(0.03); "
        "print('out_time_us=1000000', flush=True); "
        "print('ffmpeg-report', file=sys.stderr, flush=True)"
    )

    returncode, stderr = _run_ffmpeg_with_progress(
        [sys.executable, "-c", script],
        stage="measure",
        duration_us=1_000_000,
        progress=lambda stage, current, total, eta: events.append(
            (stage, current, total, eta)
        ),
        timeout_seconds=5,
    )

    assert returncode == 0
    assert stderr.strip() == "ffmpeg-report"
    assert events
    assert events[-1] == ("measure", 1_000_000, 1_000_000, 0)
    assert all(current <= total == 1_000_000 for _stage, current, total, _eta in events)
