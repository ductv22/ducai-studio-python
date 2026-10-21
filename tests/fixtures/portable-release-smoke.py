"""Check a real extracted portable package without importing application source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("portable", type=Path)
    parser.add_argument("report_directory", type=Path)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--expected-version", default="2.0.0a138")
    parser.add_argument("--comparison-root", type=Path)
    parser.add_argument("--expected-file-inventory", type=Path)
    parser.add_argument("--stable-runtime-manifest", type=Path)
    parser.add_argument("--expect-activation", action="store_true")
    args = parser.parse_args()
    portable = args.portable.resolve(strict=True)
    reports = args.report_directory.resolve()
    reports.mkdir(parents=True, exist_ok=True)
    data = portable / "DUCAI_STUDIO_DATA"
    manifest = json.loads((portable / "portable-manifest.json").read_text(encoding="utf-8-sig"))
    checks: dict[str, object] = {}
    processes: list[dict[str, object]] = []

    def checkpoint() -> None:
        (reports / "report.json").write_text(
            json.dumps({"checks": checks, "processes": processes}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def record(name: str, value: object) -> None:
        checks[name] = value
        checkpoint()
        print(name, json.dumps(value, ensure_ascii=False), flush=True)

    assert manifest["version"] == args.expected_version
    assert manifest["whisper_included"] is True
    files = tuple(path for path in portable.rglob("*") if path.is_file())
    assert len(files) == manifest["package_files"], (len(files), manifest["package_files"])
    assert sum(path.stat().st_size for path in files) == manifest["package_bytes"]
    for name, expected in manifest["critical_files_sha256"].items():
        path = (portable / name).resolve(strict=True)
        assert path.is_relative_to(portable), name
        assert digest(path) == expected, name
    record(
        "inventory",
        {
            "files": len(files),
            "bytes": manifest["package_bytes"],
            "critical_hashes": len(manifest["critical_files_sha256"]),
            "whisper_included": True,
        },
    )
    if args.comparison_root:
        original = args.comparison_root.resolve(strict=True)
        original_files = {
            path.relative_to(original).as_posix(): path
            for path in original.rglob("*") if path.is_file()
        }
        extracted_files = {path.relative_to(portable).as_posix(): path for path in files}
        assert original_files.keys() == extracted_files.keys(), "Archive changed the file set"
        for index, (relative, path) in enumerate(extracted_files.items(), start=1):
            assert digest(path) == digest(original_files[relative]), relative
            if index % 10_000 == 0:
                print(f"lossless_hashes {index}/{len(files)}", flush=True)
        record("archive_lossless_all_files", {"files": len(files), "result": "identical"})
    if args.expected_file_inventory:
        expected_inventory = json.loads(
            args.expected_file_inventory.read_text(encoding="utf-8-sig")
        )
        expected_hashes = expected_inventory["files_sha256"]
        assert expected_inventory["version"] == args.expected_version
        assert expected_inventory["bytes"] == manifest["package_bytes"]
        extracted_files = {path.relative_to(portable).as_posix(): path for path in files}
        assert extracted_files.keys() == expected_hashes.keys(), "Archive changed the file set"
        for index, (relative, path) in enumerate(extracted_files.items(), start=1):
            assert digest(path) == expected_hashes[relative], relative
            if index % 10_000 == 0:
                print(f"lossless_hashes {index}/{len(files)}", flush=True)
        record("archive_lossless_all_files", {"files": len(files), "result": "identical"})
    if args.stable_runtime_manifest:
        baseline = json.loads(args.stable_runtime_manifest.read_text(encoding="utf-8-sig"))
        checked = 0
        for relative, expected in baseline["critical_files_sha256"].items():
            if relative.startswith("DUCAI_STUDIO_DATA/"):
                assert digest((portable / relative).resolve(strict=True)) == expected, relative
                checked += 1
        assert checked > 0, "Baseline does not contain any runtime/model hashes"
        record("stable_runtime_and_model_bytes", {"files": checked, "result": "unchanged"})
    forbidden = {
        "projects",
        "voice_cue_cache",
        "export_cache",
        "ocr_jobs",
        "voice_previews",
        "gemini-web-profile",
        "gemini_web_profile",
        "credentials",
        "browser_cookies",
    }
    assert not any((data / name).exists() for name in forbidden)
    assert not list((data / "vieneu_profiles").glob("*.npz"))
    private_key = re.compile(
        rb"-----BEGIN (?:ENCRYPTED |ED25519 |RSA |EC )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{20}"
    )
    activation_token = re.compile(
        rb"(?:DUCAI-L1\.[A-Za-z0-9_-]{200,}|DUCAI-L2\.[A-Za-z0-9_-]{119}|"
        rb"DUCAI-L3\.[A-Za-z0-9_-]{130})"
    )
    owner_names = {
        "ducai-key-issuer.exe", "license-issuer.py", "license-issuer.exe",
        "key-info.json", "issued-licenses.json", "license-v1.txt",
    }
    public_crypto_fixtures = set()
    known_runtime_hashes = (
        baseline["critical_files_sha256"] if args.stable_runtime_manifest
        else manifest["critical_files_sha256"]
    )
    for path in files:
        relative = path.relative_to(portable)
        parts = {part.casefold() for part in relative.parts}
        assert "ducai_studio" not in parts, path
        if relative.parts[0] != "DUCAI_STUDIO_DATA":
            assert "src" not in parts, path
        assert not ("ducai" in path.name.casefold() and path.suffix in {".whl", ".py"}), path
        assert path.name.casefold() not in owner_names and path.suffix != ".ducai-license", path
        with path.open("rb") as stream:
            previous = b""
            while chunk := stream.read(1024 * 1024):
                block = previous + chunk
                assert not activation_token.search(block), path
                if private_key.search(block):
                    # Public cryptography self-tests contain published sample keys.
                    # Accept only those exact, verified library files, never tokens.
                    relative_name = relative.as_posix()
                    assert relative_name.startswith("DUCAI_STUDIO_DATA/"), path
                    assert "/Crypto/SelfTest/" in relative_name, path
                    assert digest(path) == known_runtime_hashes.get(relative_name), path
                    public_crypto_fixtures.add(relative_name)
                previous = block[-8192:]
    assert not list(portable.glob("*.py"))
    assert not list((portable / "engine_hosts").glob("*.py"))
    for host in ("ocr_engine_host", "tts_engine_host", "whisper_engine_host"):
        assert (portable / "engine_hosts" / f"{host}.pyc").is_file()
    record("private_source_and_data", "absent; three compiled engine hosts present")
    record(
        "private_signing_and_activation_material",
        {
            "owner_or_personal_material": "absent",
            "public_crypto_fixtures": len(public_crypto_fixtures),
        },
    )
    if args.inventory_only:
        return 0

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "DUCAI_", "QT_", "HF_", "HUGGINGFACE", "PADDLE"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    windows = Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
    environment.update(
        {
            "PATH": os.pathsep.join(
                [str(portable / "bin"), str(windows / "System32"), str(windows)]
            ),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "HF_HUB_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HOME": str(data / "model_cache/huggingface"),
            "HUGGINGFACE_HUB_CACHE": str(data / "model_cache/huggingface/hub"),
            "PADDLE_PDX_CACHE_HOME": str(data / "paddle_model_cache"),
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "DUCAI_STUDIO_DATA_ROOT": str(data),
            "DUCAI_APP_DATA_ROOT": str(reports / "app-data"),
            "LOCALAPPDATA": str(reports / "localappdata"),
            "TEMP": str(reports / "temporary"),
            "TMP": str(reports / "temporary"),
        }
    )
    (reports / "temporary").mkdir(exist_ok=True)

    def run(
        name: str,
        command: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        overrides: dict[str, str] | None = None,
        timeout: int = 180,
        expected_exit_code: int = 0,
    ) -> str:
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env={**environment, **(overrides or {})},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        entry = {
            "name": name,
            "pid": process.pid,
            "cwd": str(cwd),
            "port": None,
            "executable": command[0],
            "state": "running",
        }
        processes.append(entry)
        checkpoint()
        try:
            stdout, stderr = process.communicate(input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(
                [str(windows / "System32/taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=20,
            )
            stdout, stderr = process.communicate(timeout=20)
            entry["state"] = "timed-out"
            checkpoint()
            raise
        (reports / f"{name}.stdout.txt").write_text(stdout, encoding="utf-8")
        (reports / f"{name}.stderr.txt").write_text(stderr, encoding="utf-8")
        entry.update(
            {
                "state": "exited",
                "exit_code": process.returncode,
                "seconds": round(time.monotonic() - started, 3),
            }
        )
        checkpoint()
        assert process.returncode == expected_exit_code, (name, process.returncode, stderr[-3000:])
        return stdout

    health = reports / "exe-health.json"
    nonce = uuid.uuid4().hex
    run(
        "native-gui",
        [str(portable / "DucaiStudio2.exe")],
        cwd=portable,
        timeout=90,
        overrides={
            "QT_QPA_PLATFORM": "offscreen",
            "DUCAI_SMOKE_EXIT_MS": "12000",
            "DUCAI_UPDATE_HEALTH_PATH": str(health),
            "DUCAI_UPDATE_HEALTH_NONCE": nonce,
            "DUCAI_UPDATE_HEALTH_VERSION": args.expected_version,
        },
    )
    receipt = json.loads(health.read_text(encoding="utf-8"))
    assert receipt["version"] == args.expected_version and receipt["nonce"] == nonce
    record("native_gui_and_job_manager", receipt)
    if args.expect_activation:
        assert not (reports / "localappdata/DUCAI STUDIO/Licensing/license-v1.txt").exists()
        for module in ("ocr_worker", "translation_worker", "tts_worker", "export_worker"):
            run(
                "unlicensed-" + module,
                [str(portable / "DucaiStudio2.exe"), "-m", "ducai_studio.workers." + module],
                cwd=portable,
                expected_exit_code=3,
                timeout=45,
            )
        record("unlicensed_processing_workers", "denied; Settings GUI opens normally")
    for runtime, modules in {
        "paddle_runtime": ["cv2", "paddle", "paddleocr"],
        "piper_runtime": ["piper", "onnxruntime"],
        "vieneu_3_8_runtime": ["torch", "torchaudio", "vieneu"],
        "whisper_runtime": ["ctranslate2", "faster_whisper"],
    }.items():
        python = data / runtime / ".venv/Scripts/python.exe"
        code = (
            "import importlib,json,sys; "
            f"modules={modules!r}; "
            "loaded={n:importlib.import_module(n).__file__ for n in modules}; "
            "print(json.dumps({'python':sys.executable,'prefix':sys.prefix,"
            "'base_prefix':sys.base_prefix,'modules':loaded}))"
        )
        result = json.loads(
            run(
                runtime + "-imports",
                [str(python), "-I", "-X", "utf8", "-c", code],
                cwd=python.parent,
            )
            .strip()
            .splitlines()[-1]
        )
        assert Path(result["base_prefix"]).resolve().is_relative_to(data), result
        assert all(
            Path(value).resolve().is_relative_to(data) for value in result["modules"].values()
        )
        record(runtime + "_relocated_imports", result)

    ffmpeg = str(portable / "bin/ffmpeg.exe")
    ffprobe = str(portable / "bin/ffprobe.exe")
    media = reports / "ocr-fixture.mp4"
    run(
        "ffmpeg-video",
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1280x720:r=5",
            "-t",
            "1",
            "-vf",
            "drawtext=fontfile='ui/assets/fonts/NotoSansCJKsc-Regular.otf':text='你好世界':"
            "fontcolor=white:fontsize=58:x=(w-tw)/2:y=h*0.8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        cwd=portable,
    )
    info = json.loads(
        run(
            "ffprobe-video",
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(media)],
            cwd=portable,
        )
    )
    assert 0.9 <= float(info["format"]["duration"]) <= 1.1
    record(
        "ffmpeg_export",
        {"duration": info["format"]["duration"], "codec": info["streams"][0]["codec_name"]},
    )

    def host(
        name: str, runtime: str, script: str, requests: list[tuple[str, dict]], argument: str = ""
    ) -> dict[str, object]:
        python = data / runtime / ".venv/Scripts/python.exe"
        command = [str(python), "-I", "-X", "utf8", str(portable / "engine_hosts" / script)]
        if argument:
            command.append(argument)
        packets = [
            json.dumps(
                {"protocol_version": 1, "id": str(index), "action": action, "payload": payload},
                ensure_ascii=False,
            )
            for index, (action, payload) in enumerate(requests + [("shutdown", {})])
        ]
        output = run(
            name, command, cwd=python.parent, input_text="\n".join(packets) + "\n", timeout=360
        )
        responses = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        assert responses and all(response.get("ok") is True for response in responses), responses
        assert {str(index) for index in range(len(packets))} <= {r["id"] for r in responses}
        return {"responses": responses}

    piper_wav = reports / "piper.wav"
    voice = data / "piper_voices/vi_VN-vais1000-medium.onnx"
    piper = host(
        "piper-synthesis",
        "piper_runtime",
        "tts_engine_host.pyc",
        [
            ("health", {}),
            ("load", {"model_path": str(voice), "config_path": str(voice) + ".json"}),
            (
                "synthesize_batch",
                {
                    "jobs": [
                        {
                            "cue_id": "portable-test",
                            "text": "Xin chào, đây là kiểm tra giọng nói của bản đầy đủ.",
                            "output_path": str(piper_wav),
                        }
                    ]
                },
            ),
        ],
        "piper",
    )
    with wave.open(str(piper_wav)) as audio:
        assert audio.getnframes() > audio.getframerate()
    record("piper_real_synthesis", piper)

    for mode in ("fast", "quality"):
        result = host(
            "paddle-" + mode,
            "paddle_runtime",
            "ocr_engine_host.pyc",
            [
                ("health", {}),
                (
                    "open",
                    {"media_path": str(media), "mode": mode, "device": "gpu:0", "batch_size": 1},
                ),
                (
                    "recognize",
                    {
                        "timestamps_us": [0],
                        "bands": [
                            {
                                "name": "dialogue",
                                "x1": 0.05,
                                "x2": 0.95,
                                "y1": 0.7,
                                "y2": 0.98,
                                "scale": 1.0,
                            }
                        ],
                    },
                ),
            ],
        )
        assert "你好世界" in json.dumps(result, ensure_ascii=False), result
        record("paddle_" + mode + "_real_recognition", result)

    presets_file = (
        data / "vieneu_3_8_runtime/.venv/Lib/site-packages/vieneu/assets/voices_v3_turbo.json"
    )
    presets = json.loads(presets_file.read_text(encoding="utf-8"))["presets"]
    preset = "Trúc Ly" if "Trúc Ly" in presets else next(iter(presets))
    vieneu_wav = reports / "vieneu.wav"
    vieneu = host(
        "vieneu-synthesis",
        "vieneu_3_8_runtime",
        "tts_engine_host.pyc",
        [
            ("health", {}),
            ("load", {"runtime_version": "3.8.1", "max_batch_size": 1}),
            ("bind_preset", {"preset_name": preset}),
            (
                "synthesize_batch",
                {
                    "batch_size": 1,
                    "jobs": [
                        {
                            "cue_id": "portable-test",
                            "text": "Xin chào, đây là kiểm tra bản đầy đủ.",
                            "output_path": str(vieneu_wav),
                        }
                    ],
                },
            ),
        ],
        "vieneu",
    )
    with wave.open(str(vieneu_wav)) as audio:
        assert audio.getnframes() > audio.getframerate()
    record("vieneu_real_synthesis", vieneu)

    with wave.open(str(piper_wav)) as audio:
        duration_us = round(audio.getnframes() * 1_000_000 / audio.getframerate())
    whisper = host(
        "whisper-inference",
        "whisper_runtime",
        "whisper_engine_host.pyc",
        [
            ("health", {}),
            (
                "transcribe",
                {
                    "audio_path": str(piper_wav),
                    "model_path": str(
                        data / "model_cache/whisper/Systran--faster-whisper-large-v3"
                    ),
                    "duration_us": duration_us,
                    "start_us": 0,
                    "device": "cuda",
                    "compute_type": "float16",
                    "language": "zh",
                },
            ),
        ],
    )
    assert any(r.get("event") == "complete" and r["id"] == "1" for r in whisper["responses"])
    record("whisper_model_load_and_inference", whisper)
    record("status", "PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
