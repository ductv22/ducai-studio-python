"""Measure a real large selection off-thread; commit and flush through the real service."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtWidgets import QApplication

from ducai_studio.infrastructure.project_repository import decode_project_snapshot
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel

app = QApplication([])
directory = Path(sys.argv[1]).resolve()
directory.mkdir(parents=True, exist_ok=True)
model = ApplicationViewModel(app)
model.settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
cues = [{"id": f"c{i}", "index": i + 1, "text": f"Câu thoại số {i}", "language": "vi",
         "range": {"start_us": i * 1_000_000, "end_us": i * 1_000_000 + 900_000}}
        for i in range(12_000)]
project = {"id": "bulk", "name": "Bulk", "revision": 1, "source_cues": cues}
model.project_state.replace(project)
service = JobManagerService(database_path=directory / "state.db",
                            artifact_root=directory / "artifacts")
service.projects.save(project_path=directory / "bulk.ducai",
                      snapshot=decode_project_snapshot(project))
model.selectSubtitleCueIds([cue["id"] for cue in cues])
requests = []
model.runtime.request = lambda a, p=None, context=None: requests.append((a, p, context))
start = monotonic()
assert model.transformSelectedSubtitleGeometry(0, -.1, 1, 1, 1920, 1080)
dispatch_ms = (monotonic() - start) * 1000
assert dispatch_ms < 500, dispatch_ms
assert not requests and model.subtitleBusy
flushed = []
assert model._defer_until_subtitle_saves_flushed("fixture", lambda: flushed.append(True), "Chờ lưu")
# A real earlier save advances the cue/project revision while measurement runs,
# without changing any measured text/style. It must not cancel the group drag.
background_patch = service.subtitle_command(project_id="bulk", command="update_text",
    cue_id="c0", language="source", params={"text": cues[0]["text"]}, compact=True)
model._subtitle_save_in_flight = True
model._action_succeeded("subtitle_command", background_patch,
    {"project_id": "bulk", "background_subtitle_save": True})
assert model.subtitleBusy and not flushed
gaps = []
previous = monotonic()
while not requests and monotonic() - start < 15:
    app.processEvents()
    now = monotonic()
    gaps.append((now - previous) * 1000)
    previous = now
assert len(requests) == 1 and not flushed
assert max(gaps) < 250, max(gaps)
action, payload, context = requests[0]
assert len(payload["values"]["panel_seeds"]) == 12_000
prepared_ms = (monotonic() - start) * 1000
patch = service.subtitle_command(project_id="bulk", command=payload["command"],
            cue_id=payload["cue_id"], params=payload["values"], compact=True)
model._action_succeeded(action, patch, context)
deadline = monotonic() + 10
while model.subtitleBusy and monotonic() < deadline:
    app.processEvents()
app.processEvents()  # Run the zero-delay flush callbacks queued by the final ACK.
assert flushed and not model.subtitleBusy and not model._subtitle_geometry_context, (
    model.subtitleBusy, model.statusMessage, bool(model._subtitle_geometry_context),
    model.project_state.revision)
saved = service.projects.load("bulk")
assert saved.revision == 3 and all(cue.panel_box for cue in saved.source_cues)
# A duplicate old ACK cannot revive or overwrite the finished edit.
model._action_succeeded(action, patch, context)
assert not model.subtitleBusy
QThreadPool.globalInstance().waitForDone()
report = {"status": "PASS", "cue_count": len(cues), "dispatch_ms": dispatch_ms,
          "measurement_ms": prepared_ms, "max_ui_gap_ms": max(gaps),
          "ipc_count": len(requests), "saved_revision": saved.revision,
          "prior_save_ack_preserved": True}
(directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
