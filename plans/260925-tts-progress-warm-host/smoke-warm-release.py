"""Two real voice jobs through an isolated compiled manager; never edit production DB."""
import argparse
import copy
import json
import os
import sqlite3
import subprocess
import time
import wave
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.infrastructure.ipc.messages import FrameDecoder, IpcMessage, encode_frame
from ducai_studio.infrastructure.process_tree import ProcessTreeGuard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-manager", action="store_true")
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        original = dict(db.execute("SELECT * FROM jobs WHERE id=?", (args.job,)).fetchone())
    base = json.loads(original["config_json"])
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    app = QCoreApplication.instance() or QCoreApplication([])
    server_name = "ducai-warm-smoke-" + uuid4().hex
    manager = None
    local_server = None
    guard = ProcessTreeGuard()
    if args.source_manager:
        from ducai_studio.job_manager import server as server_module
        from ducai_studio.job_manager.server import JobManagerServer
        from ducai_studio.job_manager.service import JobManagerService
        server_module._engine_host_path = lambda name: args.exe.parent / "engine_hosts" / name
        local_server = JobManagerServer(name=server_name, service=JobManagerService(
            database_path=root / "state.db", artifact_root=root / "artifacts"))
        original_release = local_server._warm_tts.release
        original_discard = local_server._warm_tts._discard_failed_session
        def trace_release(job_id, **kwargs):
            pool = local_server._warm_tts
            print('RELEASE', kwargs, 'host', bool(pool._host),
                  'state', local_server.service.jobs.get(job_id).state, flush=True)
            return original_release(job_id, **kwargs)
        def trace_discard(session):
            import traceback
            print('DISCARD', ''.join(traceback.format_stack(limit=4)), flush=True)
            return original_discard(session)
        local_server._warm_tts.release = trace_release
        local_server._warm_tts._discard_failed_session = trace_discard
        local_server.workers.workerFinished.connect(
            lambda job, code: print('WORKER_EXIT', job, code, flush=True))
        local_server.workers.workerLog.connect(
            lambda job, message: print('WORKER_LOG', message[-2500:], flush=True))
        local_server.listen()
    else:
        manager = subprocess.Popen([
        str(args.exe), "-m", "ducai_studio.job_manager", "--server-name", server_name,
        "--database", str(root / "state.db"), "--artifacts", str(root / "artifacts"),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        guard.assign_pid(manager.pid)
    print(json.dumps({"manager_pid": manager.pid if manager else os.getpid(),
                      "output": str(root)}), flush=True)
    sock = QLocalSocket()
    decoder = FrameDecoder()

    def request(action, params):
        request_id = uuid4().hex
        sock.write(encode_frame(IpcMessage(1, request_id, "request", {
            "action": action, "params": params,
        })))
        sock.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            app.processEvents()
            sock.waitForReadyRead(50)
            for msg in decoder.feed(bytes(sock.readAll())):
                if msg.kind == "response" and msg.payload.get("request_id") == request_id:
                    if msg.payload.get("ok") is not True:
                        raise RuntimeError(str(msg.payload.get("error")))
                    return msg.payload["result"]
        raise TimeoutError(action)

    results = []
    try:
        for _ in range(100):
            sock.connectToServer(server_name)
            if sock.waitForConnected(100):
                break
            sock.abort()
            time.sleep(0.05)
        assert sock.state() == QLocalSocket.LocalSocketState.ConnectedState
        request("ping", {})
        for number, texts in enumerate((
            ("Xin chào, hôm nay chúng ta cùng nghe thử giọng đọc mới.",
             "Câu tiếp theo dùng để kiểm tra âm thanh và tiến độ."),
            ("Mô hình đã sẵn sàng cho lượt tạo giọng thứ hai.",
             "Giọng đọc vẫn được chọn riêng cho từng tác vụ."),
        ), 1):
            config = copy.deepcopy(base)
            output = root / f"run-{number}"
            output.mkdir()
            config["cues"] = [
                {"id": f"smoke-{number}-{i}", "index": i + 1,
                 "start_us": i * 10_000_000, "end_us": (i + 1) * 10_000_000, "text": text}
                for i, text in enumerate(texts)
            ]
            config.update({
                "purpose": "preview", "media_duration_us": 20_000_000,
                "cache_root": str(output / "cache"), "output_path": str(output / "master.wav"),
                "qa_path": str(output / "qa.json"),
                "engine_host_path": str(args.exe.parent / "engine_hosts" / "tts_engine_host.py"),
            })
            config.pop("staging_output_path", None)
            config.pop("staging_qa_path", None)
            spec = {key: original[key] for key in (
                "kind", "input_revision", "provider_id", "provider_version", "model_id",
            )}
            spec.update(project_id=f"isolated-{number}", provider_config=config,
                        input_fingerprint=stable_fingerprint(config["cues"]),
                        settings_fingerprint=stable_fingerprint(config))
            created = request("create_job", spec)
            job_id = created["id"]
            started = time.monotonic()
            request("start_job", {"job_id": job_id, "program": str(args.exe),
                                 "arguments": ["-m", "ducai_studio.workers.tts_worker", job_id],
                                 "resume_stage_key": "tts"})
            deadline = started + 180
            while time.monotonic() < deadline:
                job = request("get_job", {"job_id": job_id})
                if job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.2)
            assert job["state"] == "succeeded", (job["state"], job.get("error_code"))
            with wave.open(str(output / "master.wav"), "rb") as wav:
                assert wav.getframerate() == 48_000 and wav.getnframes() > 0
                frames = wav.getnframes()
            qa = json.loads((output / "qa.json").read_text(encoding="utf-8"))
            assert qa["status"] == "PASS" and qa["coverage_ratio"] == 1.0
            result = {"run": number, "seconds": round(time.monotonic() - started, 3),
                      "frames": frames, "qa": "PASS", "cues": 2}
            results.append(result)
            print(json.dumps(result), flush=True)
            # Allow the worker-finished lifecycle to return its lease before the next job.
            end = time.monotonic() + 1
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.02)
        if manager:
            request("shutdown", {})
            manager.wait(timeout=15)
        print(json.dumps({"manager_exit": manager.returncode if manager else 0,
                          "runs": results}), flush=True)
    finally:
        sock.abort()
        guard.close()
        if local_server:
            local_server.close()
        if manager and manager.poll() is None:
            manager.terminate()
            manager.wait(timeout=5)


if __name__ == "__main__":
    main()
