"""Real Paddle/FFmpeg retained-footage smoke, using only isolated test data."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QImage, QPainter, QRawFont

from ducai_studio.domain.jobs import TERMINAL_JOB_STATES
from ducai_studio.domain.timeline_edit import project_for_timeline
from ducai_studio.job_manager.server import JobManagerServer
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.ui.viewmodels.project_job_models import ProjectStateModel


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine-python", type=Path, required=True)
    parser.add_argument("--worker-program", type=Path)
    parser.add_argument("--engine-host", type=Path)
    parser.add_argument("--scissors", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication([])
    font_id = QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"]) / "Fonts/msyh.ttc"))
    assert font_id >= 0, "Chinese fixture font could not be loaded"
    font = QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 42, QFont.Weight.DemiBold)
    assert QRawFont.fromFont(font).supportsCharacter("保"), "Fixture font lacks Chinese glyphs"
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    for name, text in (("removed", "删除测试文字"), ("kept", "保留测试字幕")):
        image = QImage(1280, 720, QImage.Format.Format_RGB32)
        image.fill(QColor("#25313c"))
        painter = QPainter(image)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(QRect(40, 550, 1200, 100), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        assert image.save(str(root / f"{name}.png"))
    source = root / "source.mp4"
    sources = (("kept", 3), ("removed", 2), ("kept", 3)) if args.scissors else (
        ("removed", 3), ("kept", 5))
    inputs = [value for name, seconds in sources for value in (
        "-loop", "1", "-t", str(seconds), "-i", str(root / f"{name}.png"))]
    concat = "".join(f"[{i}:v]" for i in range(len(sources)))
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", *inputs,
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-filter_complex",
        concat + f"concat=n={len(sources)}:v=1:a=0[v]", "-map", "[v]",
        "-map", f"{len(sources)}:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(source),
    ], check=True)
    service = JobManagerService(database_path=root / "state.db", artifact_root=root / "artifacts")
    server = JobManagerServer(name=f"cut-smoke-{uuid4()}", service=service)
    try:
        project = service.create_project(project_path=str(root / "fixture.ducai"), name="Cut smoke")
        project_id = project["id"]
        service.import_media(project_id=project_id, media_path=str(source))
        if args.scissors:
            for point in (3_000_000, 5_000_000):
                service.timeline_command(project_id=project_id, command="split_video",
                                         timeline_us=point)
            snapshot = service.projects.load(project_id)
            middle = next(c for c in snapshot.tracks[0].clips
                          if c.timeline_range.start_us == 3_000_000)
            service.timeline_command(project_id=project_id, command="delete_video_segment",
                                     clip_id=middle.id)
        else:
            service.timeline_command(project_id=project_id, command="cut", operation="keep_from",
                                     range_start_us=3_000_000, range_end_us=8_000_000)
        model = ProjectStateModel()
        model.replace(service.get_project(project_id))
        descriptor = model.previewDescriptor(0)
        assert descriptor["available"]
        assert descriptor["sourcePositionMs"] == (0 if args.scissors else 3_000)
        if args.scissors:
            assert model.previewDescriptor(3_000_000)["sourcePositionMs"] == 5_000
        expected_duration = 6_000_000 if args.scissors else 5_000_000
        ocr = service.create_ocr_job(project_id=project_id, sample_interval_us=800_000,
                                     engine_python=str(args.engine_python), device="cpu",
                                     **({"engine_host_path": str(args.engine_host)}
                                        if args.engine_host else {}))

        def execute(job, module):
            server._dispatch("start_job", {"job_id": job["id"],
                                           "program": str(args.worker_program or sys.executable),
                                           "arguments": ["-m", module, job["id"]]})
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                app.processEvents()
                current = service.jobs.get(job["id"])
                if current.state in TERMINAL_JOB_STATES:
                    assert current.state.value == "succeeded", service.list_jobs(1)
                    return
                time.sleep(.01)
            raise TimeoutError(f"Smoke worker exceeded deadline: {module}")

        execute(ocr, "ducai_studio.workers.ocr_worker")
        snapshot = service.projects.load(project_id)
        text = " ".join(cue.text for cue in snapshot.source_cues)
        assert "保留" in text and "删除" not in text, text
        visible = project_for_timeline(snapshot)
        assert visible.tracks[0].clips[-1].timeline_range.end_us == expected_duration
        srt = Path(ocr["spec"]["provider_config"]["output_path"])
        assert "删除" not in srt.read_text(encoding="utf-8-sig")
        output = root / "export.mp4"
        export = service.create_export_job(project_id=project_id, output_path=str(output),
                                           export_options={"backend": "cpu"})
        execute(export, "ducai_studio.workers.export_worker")
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
            str(output),
        ], text=True))
        assert abs(float(probe["format"]["duration"]) - expected_duration/1_000_000) < .1
        result = {"status": "PASS", "ocr_text": text, "scan_plan": ocr["spec"]["provider_config"]
                  ["scan_plan"], "preview_at_zero": descriptor, "export_duration": probe}
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(run())
