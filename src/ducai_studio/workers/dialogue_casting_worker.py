from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from ducai_studio.application.voice_pipeline import atomic_write_json, file_sha256
from ducai_studio.domain.dialogue_casting import CastingResult
from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.infrastructure.dialogue_casting_runtime import (
    BLOCK_US,
    CONTEXT_US,
    MAX_CUE_US,
    engine_bundle_fingerprint,
)
from ducai_studio.infrastructure.process_tree import ProcessTreeGuard
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage


class CastingStopped(Exception):
    pass


def analysis_blocks(cues: list[dict], durations: dict[str, int]) -> tuple[list[dict], list[dict]]:
    groups, fallback = {}, []
    for cue in cues:
        parts = cue["parts"]
        if len(parts) != 1 or parts[0]["end_us"] - parts[0]["start_us"] > MAX_CUE_US:
            fallback.append({"cue_id": cue["id"], "audio_key": cue["audio_key"],
                             "label": "unknown", "reason": "discontinuous_or_long_source",
                             "confidence": 0.0})
            continue
        part = parts[0]
        block = part["start_us"] // BLOCK_US
        key = (part["media_id"], block)
        if key not in groups:
            groups[key] = {"media_id": part["media_id"], "media_path": part["media_path"],
                           "start_us": max(0, block * BLOCK_US - CONTEXT_US),
                           "end_us": min(durations[part["media_id"]],
                                         (block + 1) * BLOCK_US + CONTEXT_US),
                           "cues": []}
        group = groups[key]
        group["end_us"] = min(durations[part["media_id"]],
                              max(group["end_us"], part["end_us"] + CONTEXT_US))
        group["cues"].append({**cue, "start_us": part["start_us"], "end_us": part["end_us"]})
    for group in groups.values():
        group["window_id"] = f"{group['media_id']}:{group['start_us']}:{group['end_us']}"
    return list(groups.values()), fallback


class DialogueCastingWorker:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.commands: queue.Queue[WorkerMessage] = queue.Queue()
        self.acknowledgements: dict[str, WorkerMessage] = {}
        self.root = (Path(os.environ.get("DUCAI_ARTIFACT_STAGING_ROOT", ".ducai-staging"))
                     .expanduser().resolve() / job_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, dict] = {}
        self.stage_results: dict[str, dict] = {}
        self.child: subprocess.Popen | None = None
        self.child_mode: str | None = None
        self.guard: ProcessTreeGuard | None = None
        self.started = time.monotonic()
        self.spec: dict = {}

    def send(self, kind: str, payload: dict) -> WorkerMessage:
        message = WorkerMessage(PROTOCOL_VERSION, self.job_id, str(uuid4()), kind, payload)
        sys.stdout.buffer.write(message.to_json_line())
        sys.stdout.buffer.flush()
        return message

    def _ack(self, command: str, kind: str) -> None:
        self.acknowledgements[command] = self.send(kind, {"command_id": command})

    def _read(self) -> None:
        pending = bytearray()
        while chunk := os.read(sys.stdin.fileno(), 65_536):
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, tail = pending.partition(b"\n")
                pending = bytearray(tail)
                try:
                    message = WorkerMessage.from_json_line(line)
                    if message.job_id == self.job_id:
                        self.commands.put(message)
                except (ValueError, TypeError):
                    continue

    def _stop_child(self) -> None:
        child, guard = self.child, self.guard
        self.child = None
        self.child_mode = None
        self.guard = None
        if child is None:
            if guard is not None:
                guard.close()
            return
        if child.poll() is None:
            if child.stdin is not None:
                with suppress(OSError):
                    child.stdin.close()
            try:
                child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                # Closing the Windows job handle terminates the host and any
                # active ffmpeg child together. Other platforms still receive
                # a normal terminate before the hard-kill fallback.
                if guard is not None:
                    guard.close()
                    guard = None
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        else:
            child.wait()
        if guard is not None:
            guard.close()

    def _control(self) -> bool:
        interrupted = False
        while True:
            try:
                message = self.commands.get_nowait()
            except queue.Empty:
                return interrupted
            action, command = message.payload.get("action"), message.payload.get("command_id")
            if command in self.acknowledgements:
                cached = self.acknowledgements[command]
                sys.stdout.buffer.write(cached.to_json_line())
                sys.stdout.buffer.flush()
                continue
            if action in {"pause", "cancel", "stop"}:
                self._stop_child()
                interrupted = True
                self._checkpoint()
            if action == "pause" and isinstance(command, str):
                self._ack(command, "paused")
                while True:
                    message = self.commands.get()
                    action = message.payload.get("action")
                    command = message.payload.get("command_id")
                    if action == "resume" and isinstance(command, str):
                        self._ack(command, "resumed")
                        break
                    if action in {"cancel", "stop"}:
                        self.send("stopped", {"command_id": command, "status": action})
                        raise CastingStopped
            elif action in {"cancel", "stop"}:
                self.send("stopped", {"command_id": command, "status": action})
                raise CastingStopped

    def _checkpoint(self) -> None:
        path = self.root / f"casting-{uuid4().hex}.json.partial"
        atomic_write_json(path, {"signature": self.spec["input_fingerprint"],
                                 "settings_fingerprint": self.spec["settings_fingerprint"],
                                 "schema_version": 2, "stages": self.stage_results,
                                 "results": list(self.results.values())})
        self.send("checkpoint", {
            "stage_key": "dialogue_casting", "checkpoint_key": "latest",
            "schema_version": 2, "cursor_us": 0,
            "payload": {"completed_cues": len(self.results),
                        "completed_stages": sum(map(len, self.stage_results.values()))},
            "artifact_path": str(path),
            "artifact_kind": "dialogue_casting", "artifact_state": "technical",
            "expected_sha256": file_sha256(path),
        })

    def _engine_log_tail(self) -> str:
        log = self.root / "engine.log"
        if not log.is_file():
            return ""
        return log.read_text(encoding="utf-8", errors="replace")[-1200:]

    def _start_host(self, mode: str) -> None:
        config = self.spec["provider_config"]
        if self.child is not None and self.child.poll() is None and self.child_mode == mode:
            return
        if self.child is not None:
            self._stop_child()
        runtime_keys = {"diarize": "diarization_python", "classify": "classifier_python",
                        "moss": "moss_python", "refine": "classifier_python",
                        "crosscheck": "classifier_python"}
        python = config[runtime_keys[mode]]
        host = Path(config["engine_host_path"])
        if file_sha256(host) != config["engine_host_sha256"]:
            raise RuntimeError("Checksum bộ điều phối phân vai đã đổi; hãy tạo tác vụ mới")
        self._verify_bundle()
        with (self.root / "engine.log").open("ab") as log:
            child = subprocess.Popen(
                [python, "-X", "utf8", str(host), mode, "--serve"],
                stdout=log,
                stderr=log,
                stdin=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                     "HF_DATASETS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
                     "CUBLAS_WORKSPACE_CONFIG": ":4096:8"},
            )
        guard = ProcessTreeGuard()
        try:
            guard.assign_pid(child.pid)
        except Exception:
            child.terminate()
            child.wait(timeout=5)
            guard.close()
            raise
        self.child = child
        self.child_mode = mode
        self.guard = guard

    def _host(self, mode: str, request: dict) -> dict:
        call_id = f"host-{mode}-{request['start_us']}-{request['end_us']}-{uuid4().hex}"
        source = self.root / f"{call_id}.request.json"
        output = self.root / f"{call_id}.result.json"
        atomic_write_json(source, request)
        while True:
            if self._control():
                continue
            output.unlink(missing_ok=True)
            self._start_host(mode)
            child = self.child
            if child is None or child.stdin is None:
                raise RuntimeError("Không thể mở kênh yêu cầu tới bộ nhận diện phân vai")
            envelope = json.dumps(
                {"request_path": str(source), "output_path": str(output)},
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
            try:
                child.stdin.write(envelope)
                child.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                code = child.poll()
                self._stop_child()
                raise RuntimeError(
                    f"Runtime phân vai dừng bất thường ({code}): {self._engine_log_tail()}"
                ) from exc
            timeout = 3600 if mode == "moss" else 600
            deadline, restart = time.monotonic() + timeout, False
            while not output.is_file():
                if child.poll() is not None:
                    break
                if self._control():
                    restart = True
                    break
                if time.monotonic() > deadline:
                    self._stop_child()
                    raise TimeoutError(
                        f"Bộ nhận diện {mode} vượt giới hạn {timeout // 60} phút")
                time.sleep(0.1)
            if restart:
                continue
            if not output.is_file():
                code = child.returncode
                self._stop_child()
                raise RuntimeError(
                    f"Runtime phân vai dừng bất thường ({code}): {self._engine_log_tail()}")
            result = json.loads(output.read_text(encoding="utf-8"))
            if "error" in result:
                raise RuntimeError(result.get("error", "Lỗi runtime phân vai"))
            if result.get("_context") != request["_context"]:
                raise ValueError("Kết quả phân vai không khớp đầu vào của bước nhận diện")
            return result

    def _verify_bundle(self) -> None:
        config = self.spec["provider_config"]
        if engine_bundle_fingerprint(Path(config["engine_host_path"])) != config[
            "engine_bundle_fingerprint"
        ]:
            raise ValueError("Bộ nhận diện hoặc cấu hình model đã đổi; hãy tạo tác vụ mới")

    def _stage_context(self, block: dict, mode: str, request: dict) -> dict:
        config = self.spec["provider_config"]
        return {"mode": mode, "media_id": block["media_id"],
                "window_id": block["window_id"], "start_us": block["start_us"],
                "end_us": block["end_us"], "analyzer_id": config["analyzer_id"],
                "settings_fingerprint": self.spec["settings_fingerprint"],
                "engine_bundle_fingerprint": config["engine_bundle_fingerprint"],
                "input_digest": stable_fingerprint(request),
                "model_revision": config["moss_model_revision"],
                "source_revision": config["moss_source_revision"]}

    def _restore(self, saved: dict) -> None:
        if saved.get("signature") != self.spec["input_fingerprint"]:
            raise ValueError("Checkpoint phân vai không còn khớp âm thanh")
        if saved.get("settings_fingerprint") != self.spec["settings_fingerprint"]:
            raise ValueError("Checkpoint phân vai không khớp cấu hình hoặc bộ nhận diện")
        if saved.get("schema_version") != 2 or not isinstance(saved.get("stages"), dict):
            raise ValueError("Checkpoint thuộc bộ phân vai cũ; hãy tạo tác vụ mới")
        self.stage_results = saved["stages"]
        # Final labels are always derived again from the complete restored evidence.
        self.results = {}

    @staticmethod
    def _validate_stage(block: dict, mode: str, result: dict,
                        expected_context: dict | None = None) -> None:
        if not isinstance(result, dict):
            raise ValueError("Checkpoint phân vai chứa kết quả không hợp lệ")
        if expected_context is not None and result.get("_context") != expected_context:
            raise ValueError("Checkpoint chứa bằng chứng từ video, model hoặc đầu vào khác")

        def spans(rows: list, *, probabilities: bool = False) -> None:
            if not isinstance(rows, list):
                raise ValueError("Checkpoint thiếu danh sách bằng chứng âm thanh")
            seen = set()
            for row in rows:
                if (not isinstance(row, dict)
                        or type(row.get("start_us")) is not int
                        or type(row.get("end_us")) is not int
                        or not block["start_us"] <= row["start_us"]
                        < row["end_us"] <= block["end_us"]
                        or not isinstance(row.get("speaker"), str) or not row["speaker"]):
                    raise ValueError("Checkpoint chứa mốc người nói không hợp lệ")
                identity = row["speaker"], row["start_us"], row["end_us"]
                if identity in seen:
                    raise ValueError("Checkpoint chứa bằng chứng âm thanh bị lặp")
                seen.add(identity)
                if probabilities:
                    value = row.get("female_probability")
                    if (type(value) not in (int, float) or not math.isfinite(value)
                            or not 0 <= value <= 1):
                        raise ValueError("Checkpoint chứa xác suất giọng không hợp lệ")

        def decision(cue_id: str, value: dict) -> None:
            confidence = value["confidence"]
            if confidence is None and value["label"] == "unknown":
                confidence = 0.0
            if (value.get("cue_id") != cue_id or not isinstance(value["reason"], str)
                    or type(confidence) not in (int, float)):
                raise ValueError("Checkpoint chứa quyết định phân vai không hợp lệ")
            CastingResult(cue_id, "validated", value["label"], value["reason"], confidence)
        if mode == "classify":
            rows = result["results"]
            expected = {cue["id"]: cue["audio_key"] for cue in block["cues"]}
            for row in rows:
                CastingResult(**row)
            if len(rows) != len(expected) or {
                row["cue_id"]: row["audio_key"] for row in rows
            } != expected:
                raise ValueError("Phân loại không khớp câu trong khối âm thanh")
            decisions = result["diagnostics"]["decisions"]
            if (len(decisions) != len(expected)
                    or {row["cue_id"] for row in decisions} != set(expected)):
                raise ValueError("Checkpoint thiếu bằng chứng âm thanh từng câu")
            analysis = result["diagnostics"]["analysis"]
            spans(analysis["turns"])
            spans(analysis["evidence"], probabilities=True)
            for row in decisions:
                decision(row["cue_id"], row["machine"])
                if "original_machine" in row:
                    decision(row["cue_id"], row["original_machine"])
                spans(row["evidence"], probabilities=True)
        elif mode == "moss":
            if (result["id"], result["start_us"], result["end_us"]) != (
                block["window_id"], block["start_us"], block["end_us"]
            ):
                raise ValueError("Checkpoint MOSS không khớp video hoặc khoảng âm thanh")
            spans(result["turns"])
            if expected_context is not None:
                provenance = result["provenance"]
                for key in ("model_revision", "source_revision"):
                    if provenance.get(key) != expected_context[key]:
                        raise ValueError("Checkpoint MOSS dùng phiên bản model hoặc mã nguồn khác")
                if (provenance.get("attention") != "sdpa" or provenance.get("seed") != 20261024
                        or type(provenance.get("generated_tokens")) is not int
                        or not 0 <= provenance["generated_tokens"] < 4096):
                    raise ValueError("Checkpoint MOSS không khớp chế độ nghe đã chọn")
        elif mode == "diarize":
            spans(result["turns"])
            spans(result["evidence"])
        elif mode == "refine":
            if (result["id"], result["start_us"], result["end_us"]) != (
                block["window_id"], block["start_us"], block["end_us"]
            ):
                raise ValueError("Checkpoint mẫu giọng MOSS không khớp khối âm thanh")
            rows = result["evidence"]
            if not isinstance(rows, list):
                raise ValueError("Checkpoint thiếu mẫu giọng MOSS")
            cue_map = {cue["id"]: cue for cue in block["cues"]}
            seen, speaker_counts = set(), {}
            for row in rows:
                spans([row], probabilities=True)
                identity = (row["speaker"], row["start_us"], row["end_us"],
                            row.get("origin"), row.get("cue_id"))
                if (identity in seen or row.get("window_id") != block["window_id"]
                        or row.get("origin") not in {"moss_speaker", "moss_cue"}
                        or not 1_000_000 <= row["end_us"] - row["start_us"] <= 5_000_000):
                    raise ValueError("Checkpoint chứa mẫu giọng MOSS không hợp lệ")
                seen.add(identity)
                if row["origin"] == "moss_cue":
                    cue = cue_map.get(row.get("cue_id"))
                    if (cue is None or not cue["start_us"] <= row["start_us"]
                            < row["end_us"] <= cue["end_us"]):
                        raise ValueError("Checkpoint mẫu giọng không khớp câu thoại")
                else:
                    speaker_counts[row["speaker"]] = speaker_counts.get(row["speaker"], 0) + 1
                    if speaker_counts[row["speaker"]] > 3:
                        raise ValueError("Checkpoint vượt giới hạn mẫu giọng người nói")
        else:
            raise ValueError("Checkpoint chứa bước nhận diện không được hỗ trợ")

    @staticmethod
    def _validate_classification_dependency(original: dict, diagnostic: dict) -> None:
        restored = {**diagnostic, "evidence": [
            {key: value for key, value in item.items() if key != "female_probability"}
            for item in diagnostic["evidence"]]}
        if restored != original:
            raise ValueError("Phân loại không khớp bằng chứng tách người nói")

    @staticmethod
    def _validate_refinement_dependency(window: dict, result: dict) -> None:
        for row in result["evidence"]:
            first, last, speaker = row["start_us"], row["end_us"], row["speaker"]
            if (not any(turn["speaker"] == speaker
                        and turn["start_us"] <= first < last <= turn["end_us"]
                        for turn in window["turns"])
                    or any(turn["speaker"] != speaker
                           and turn["start_us"] < last and turn["end_us"] > first
                           for turn in window["turns"])):
                raise ValueError("Mẫu giọng MOSS không khớp người nói hoặc có chồng tiếng")

    def _crosscheck_request(self, blocks: list[dict]) -> dict:
        media_ids = {block["media_id"] for block in blocks}
        if len(media_ids) != 1:
            raise ValueError("Không được trộn bằng chứng người nói của nhiều video")
        cues, windows, evidence, baseline = [], [], [], {}
        for block in blocks:
            stages = self.stage_results[block["window_id"]]
            classified = stages["classify"]
            windows.append(stages["moss"])
            cues.extend(block["cues"])
            evidence.extend(classified["diagnostics"]["analysis"]["evidence"])
            evidence.extend(stages.get("refine", {}).get("evidence", []))
            for row in classified["diagnostics"]["decisions"]:
                baseline[row["cue_id"]] = row.get("original_machine", row["machine"])
                evidence.extend(row["evidence"])
        return {**self.spec["provider_config"], "media_id": next(iter(media_ids)),
                "start_us": min(block["start_us"] for block in blocks),
                "end_us": max(block["end_us"] for block in blocks),
                "cues": cues, "windows": windows, "evidence": evidence, "baseline": baseline}

    def run(self) -> int:
        threading.Thread(target=self._read, daemon=True).start()
        try:
            start = self.commands.get(timeout=30)
            if start.payload.get("action") != "start":
                raise ValueError("Thiếu lệnh bắt đầu phân vai")
            self.spec = start.payload["job"]
            config = self.spec["provider_config"]
            cues = config["cues"]
            checkpoint = start.payload.get("checkpoint")
            self._verify_bundle()
            if isinstance(checkpoint, dict) and checkpoint.get("artifact"):
                saved = json.loads(Path(checkpoint["artifact"]["uri"]).read_text(encoding="utf-8"))
                self._restore(saved)
            for row in self.results.values():
                CastingResult(**row)
            self._ack(start.payload["command_id"], "started")
            blocks, fallback = analysis_blocks(cues, config["media_durations"])
            if set(self.stage_results) - {block["window_id"] for block in blocks}:
                raise ValueError("Checkpoint chứa khối của video khác")
            self.results.update({row["cue_id"]: row for row in fallback})
            media_blocks: dict[str, list[dict]] = {}
            for block in blocks:
                media_blocks.setdefault(block["media_id"], []).append(block)
            total = max(1, 4 * len(blocks) + len(media_blocks))
            for mode, detail in (("diarize", "Đang tách và nhóm người nói"),
                                 ("classify", "Đang nhận giọng trong từng khoảng phụ đề"),
                                 ("moss", "Đang nghe mạch người nói bằng MOSS"),
                                 ("refine", "Đang kiểm tra giọng theo mạch người nói")):
                for block in blocks:
                    self._control()
                    request = {**config, **block, "scratch_root": str(self.root)}
                    stages = self.stage_results.setdefault(block["window_id"], {})
                    if set(stages) - {"diarize", "classify", "moss", "refine"}:
                        raise ValueError("Checkpoint chứa bước nhận diện lạ")
                    if mode in {"classify", "moss", "refine"}:
                        diarized = stages.get("diarize")
                        if diarized is None:
                            raise ValueError("Checkpoint thiếu bước tách người nói bắt buộc")
                        request["analysis"] = {
                            key: value for key, value in diarized.items() if key != "_context"
                        }
                    if mode == "refine":
                        if "moss" not in stages or "classify" not in stages:
                            raise ValueError("Checkpoint thiếu mạch người nói hoặc phân loại giọng")
                        request["moss_window"] = stages["moss"]
                        request["classification"] = stages["classify"]
                    stage_request = {**request}
                    context = self._stage_context(block, mode, stage_request)
                    stage_request["_context"] = context
                    if mode in stages:
                        self._validate_stage(block, mode, stages[mode], context)
                        if mode == "classify":
                            self._validate_classification_dependency(
                                request["analysis"], stages[mode]["diagnostics"]["analysis"])
                        elif mode == "refine":
                            self._validate_refinement_dependency(
                                request["moss_window"], stages[mode])
                        continue
                    self.send("progress", {
                        "phase": mode, "phase_detail": detail,
                        "current": sum(len(value) for value in self.stage_results.values()),
                        "total": total, "elapsed_ms": int((time.monotonic()-self.started)*1000),
                    })
                    result = self._host(mode, stage_request)
                    self._validate_stage(block, mode, result, context)
                    if mode == "classify":
                        self._validate_classification_dependency(
                            request["analysis"], result["diagnostics"]["analysis"])
                    elif mode == "refine":
                        self._validate_refinement_dependency(request["moss_window"], result)
                    stages[mode] = result
                    self._checkpoint()
                self._stop_child()
            for completed_media, group in enumerate(media_blocks.values()):
                self._control()
                self.send("progress", {
                    "phase": "crosscheck", "phase_detail": "Đang đối chiếu người nói với SRT gốc",
                    "current": 4 * len(blocks) + completed_media, "total": total,
                    "elapsed_ms": int((time.monotonic()-self.started)*1000),
                })
                request = self._crosscheck_request(group)
                request["_context"] = self._stage_context(
                    {**group[0], "window_id": f"{group[0]['media_id']}:crosscheck",
                     "start_us": request["start_us"], "end_us": request["end_us"]},
                    "crosscheck", request)
                result = self._host("crosscheck", request)
                expected = {cue["id"]: cue["audio_key"] for block in group for cue in block["cues"]}
                if len(result["results"]) != len(expected) or {
                    row["cue_id"]: row["audio_key"] for row in result["results"]
                } != expected:
                    raise ValueError("Đối chiếu MOSS không trả đủ câu của video")
                for row in result["results"]:
                    CastingResult(**row)
                    self.results[row["cue_id"]] = row
                self._checkpoint()
            self._control()
            if set(self.results) != {cue["id"] for cue in cues}:
                raise ValueError("Kết quả phân vai thiếu hoặc thừa câu")
            self._checkpoint()
            self.send("progress", {"phase": "crosscheck", "phase_detail": "Đã phân vai xong",
                                   "current": total, "total": total,
                                   "elapsed_ms": int((time.monotonic()-self.started)*1000)})
            output = self.root / "casting-results.json"
            atomic_write_json(output, {"signature": self.spec["input_fingerprint"],
                                       "results": list(self.results.values())})
            self.send("succeeded", {"status": "PASS", "output_path": str(output)})
            return 0
        except CastingStopped:
            return 0
        except Exception as exc:
            self.send("failed", {"code": "dialogue_casting_failed", "message": str(exc)})
            return 2
        finally:
            self._stop_child()


def main() -> int:
    from ducai_studio.infrastructure.licensing_state import worker_activation_exit_code
    denied = worker_activation_exit_code()
    if denied:
        return denied
    if len(sys.argv) != 2:
        return 2
    return DialogueCastingWorker(sys.argv[1]).run()


if __name__ == "__main__":
    raise SystemExit(main())
