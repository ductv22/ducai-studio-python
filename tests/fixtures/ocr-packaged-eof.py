"""Run the real frozen OCR worker on isolated output, without a live manager.

The existing machine license is verified normally. No model, crop, sampling or
QA setting is weakened, and the supplied source video is never modified.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import threading
import time
from collections import Counter
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ducai_studio.infrastructure.process_tree import ProcessTreeGuard
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    release = args.release.resolve(strict=True)
    root = args.output.resolve()
    if root.exists():
        parser.error("Use a fresh isolated output directory")
    root.mkdir(parents=True)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    frozen = {key: value for key, value in config.items() if key not in {
        "output_path", "staging_output_path", "engine_host_path",
    }}
    fingerprint = sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()
    target = root / "portrait_ZH_FINAL.srt"
    config.update(output_path=str(target), staging_output_path=str(target))
    host = release / "engine_hosts/ocr_engine_host.pyc"
    environment = {**os.environ, "DUCAI_ARTIFACT_STAGING_ROOT": str(root / "staging"),
                   "DUCAI_OCR_ENGINE_HOST_PATH": str(host),
                   "DUCAI_OCR_ENGINE_HOST_SHA256": sha256(host.read_bytes()).hexdigest()}
    job_id = f"portrait-exe-{uuid4().hex}"
    messages: queue.Queue[bytes | None] = queue.Queue()
    diagnostics: list[bytes] = []
    kinds: Counter[str] = Counter()
    execution_observations: dict[str, dict] = {}
    began = time.monotonic()
    with ProcessTreeGuard() as guard:
        process = subprocess.Popen(
            [str(release / "DucaiStudio2.exe"), "-m", "ducai_studio.workers.ocr_worker", job_id],
            cwd=release, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        guard.assign_pid(process.pid)
        print(json.dumps({"worker_pid": process.pid, "job_id": job_id}), flush=True)
        assert process.stdout is not None and process.stderr is not None

        def collect_stdout() -> None:
            for line in process.stdout:
                messages.put(line)
            messages.put(None)

        def collect_stderr() -> None:
            diagnostics.extend(process.stderr)

        output_thread = threading.Thread(target=collect_stdout, daemon=True)
        error_thread = threading.Thread(target=collect_stderr, daemon=True)
        output_thread.start()
        error_thread.start()
        terminal = None
        try:
            while terminal is None:
                remaining = 240 - (time.monotonic() - began)
                if remaining <= 0:
                    raise TimeoutError("packaged_ocr_probe_timeout")
                line = messages.get(timeout=remaining)
                if line is None:
                    raise RuntimeError(f"packaged_ocr_exited_without_result:{process.poll()}")
                message = WorkerMessage.from_json_line(line)
                assert message.job_id == job_id
                kinds[message.kind] += 1
                if message.kind == "progress" and "execution_device" in message.payload:
                    execution_observations[str(message.payload.get("phase", ""))] = {
                        "device": message.payload["execution_device"],
                        "workers": message.payload["execution_workers"],
                        "detail": message.payload.get("phase_detail", ""),
                    }
                if message.kind == "ready":
                    start = WorkerMessage(PROTOCOL_VERSION, job_id, "start", "control", {
                        "action": "start", "command_id": "start",
                        "job": {"kind": "ocr", "provider_id": "paddle-fast-quality",
                                "provider_config": config, "input_fingerprint": fingerprint,
                                "settings_fingerprint": fingerprint},
                    })
                    assert process.stdin is not None
                    process.stdin.write(start.to_json_line())
                    process.stdin.flush()
                elif message.kind in {"succeeded", "failed", "review_required"}:
                    terminal = message
            exit_code = process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            error_thread.join(timeout=5)
            (root / "worker-stderr.txt").write_bytes(b"".join(diagnostics))
    assert terminal is not None
    assert terminal.kind == "succeeded", terminal.payload
    assert exit_code == 0
    qa_path = target.with_name(target.stem + "_QA.json")
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    evidence = root / "staging" / job_id / "ocr-evidence-working.ndjson"
    report = {"seconds": time.monotonic() - began, "exit_code": exit_code,
              "worker_pid": process.pid, "job_id": job_id, "events": dict(kinds),
              "execution_observations": execution_observations,
              "qa_status": qa["status"], "cue_count": qa["cue_count"],
              "blockers": qa["blockers"], "srt_path": str(target), "qa_path": str(qa_path),
              "srt_sha256": sha256(target.read_bytes()).hexdigest(),
              "decoder_video_duration_us": qa["decoder_video_duration_us"],
              "decoder_tail_request_counts": qa["decoder_tail_request_counts"]}
    assert qa["status"] == "PASS" and not qa["blockers"]
    if args.baseline:
        baseline = args.baseline.resolve(strict=True)
        previous = json.loads((baseline / "report.json").read_text(encoding="utf-8"))
        report["source_srt_identical"] = (
            target.read_bytes() == Path(previous["srt_path"]).read_bytes()
        )
        report["source_evidence_identical"] = (
            evidence.read_bytes() == Path(previous["raw_evidence_path"]).read_bytes()
        )
        assert report["source_srt_identical"] and report["source_evidence_identical"]
    (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
