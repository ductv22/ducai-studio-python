"""Replay a small job through a release EXE without changing the source project."""

import argparse
import json
import os
import sqlite3
import subprocess
import time
import wave
from pathlib import Path
from uuid import uuid4

from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--release-exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("select * from jobs where id=?", (args.job,)).fetchone())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads(row["config_json"])
    config["cues"] = config["cues"][:32]
    config["media_duration_us"] = max(c["end_us"] for c in config["cues"])
    config["cache_root"] = str(output / "cache")
    config["output_path"] = str(output / "master.wav")
    config["qa_path"] = str(output / "qa.json")
    config.pop("staging_output_path", None)
    config.pop("staging_qa_path", None)
    config["engine_host_path"] = str(args.release_exe.parent / "engine_hosts/tts_engine_host.py")
    job_id = str(uuid4())
    spec = {key: row[key] for key in (
        "kind", "project_id", "input_revision", "provider_id", "provider_version",
        "model_id", "settings_fingerprint", "input_fingerprint",
    )}
    spec.update(id=job_id, provider_config=config)
    message = WorkerMessage(PROTOCOL_VERSION, job_id, str(uuid4()), "control", {
        "action": "start", "command_id": str(uuid4()), "job": spec,
    })
    env = os.environ.copy()
    env["DUCAI_ARTIFACT_STAGING_ROOT"] = str(output / "staging")
    env["DUCAI_STUDIO_DATA_ROOT"] = config["data_root"]
    started = time.monotonic()
    process = subprocess.Popen(
        [str(args.release_exe), "-m", "ducai_studio.workers.tts_worker", job_id],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    print(json.dumps({"worker_pid": process.pid}), flush=True)
    try:
        stdout, stderr = process.communicate(message.to_json_line(), timeout=600)
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    terminal = events[-1] if events else {}
    result = {"exit_code": process.returncode, "event": terminal.get("kind"),
              "seconds": round(time.monotonic() - started, 3), "cue_count": 32,
              "stderr_bytes": len(stderr)}
    if process.returncode or terminal.get("kind") != "succeeded":
        result["failure"] = terminal.get("payload", {})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        raise RuntimeError("frozen_worker_smoke_failed")
    with wave.open(str(output / "master.wav"), "rb") as wav:
        result.update(sample_rate=wav.getframerate(), frames=wav.getnframes())
        assert wav.getnframes() > 0 and wav.getframerate() == 48_000
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
