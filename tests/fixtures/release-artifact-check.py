"""Verify the generated release, then smoke it using an isolated application root."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio import __version__
from ducai_studio.infrastructure.offline_license import (
    PRODUCT,
    TOKEN_PREFIX,
    canonical_payload,
    encode_base64,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_probe_output(
    result: subprocess.CompletedProcess[str], name: str, directory: Path
) -> None:
    (directory / f"{name}-stdout.txt").write_text(result.stdout, encoding="utf-8")
    (directory / f"{name}-stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    for error in ("Traceback (most recent call last):", "ReferenceError:", "TypeError:",
                  "QQuickImage: Invalid image provider:"):
        assert error not in result.stderr, result.stderr


def validation_wheel(
    parser: argparse.ArgumentParser,
    requested: Path | None,
    *,
    root: Path,
    release: Path,
) -> Path:
    wheel = requested.resolve() if requested is not None else next(
        iter(sorted((root / "dist").glob(f"ducai_studio-{__version__}-*.whl"))),
        None,
    )
    if wheel is None or not wheel.is_file():
        parser.error(
            "Provide --validation-wheel or build the exact-version wheel in local dist/."
        )
    if not wheel.name.startswith(f"ducai_studio-{__version__}-"):
        parser.error("The validation wheel version must match the customer release.")
    if wheel.is_relative_to(release):
        parser.error("The validation wheel must remain outside the customer release.")
    return wheel


def validation_license_environment(directory: Path) -> dict[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    device_id = hashlib.sha256(b"ducai-release-validation-device-v1").hexdigest()
    key_id = "release-validation-key"
    payload = {
        "schema": 1,
        "product": PRODUCT,
        "key_id": key_id,
        "license_id": uuid.uuid4().hex,
        "device_schema": "windows-v1",
        "device_id": device_id,
        "issued_at": "2026-10-03T00:00:00Z",
        "edition": "full",
    }
    signature = private_key.sign(canonical_payload(payload))
    envelope = json.dumps(
        {"payload": payload, "signature": encode_base64(signature)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    license_path = directory / "license-v1.txt"
    license_path.write_text(TOKEN_PREFIX + encode_base64(envelope), encoding="utf-8")
    return {
        "DUCAI_TEST_LICENSE_BOOTSTRAP": "1",
        "DUCAI_TEST_LICENSE_PATH": str(license_path),
        "DUCAI_TEST_LICENSE_DEVICE_ID": device_id,
        "DUCAI_TEST_LICENSE_KEY_ID": key_id,
        "DUCAI_TEST_LICENSE_PUBLIC_KEY": base64.urlsafe_b64encode(public_key).decode(
            "ascii"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--validation-wheel", type=Path)
    parser.add_argument("--offscreen-only", action="store_true",
                        help="Keep every Qt fixture offscreen without raising desktop windows")
    parser.add_argument("--hidden-native-parity", action="store_true",
                        help="Use Windows fonts for parity with never-shown software windows")
    parser.add_argument("--ocr-database", type=Path)
    parser.add_argument("--ocr-job")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    release, report = args.release.resolve(), args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["version"] == __version__
    entries = manifest["files_sha256"]
    for name, expected in entries.items():
        path = (release / name).resolve()
        assert path.is_relative_to(release), name
        assert path.is_file() and digest(path) == expected, name
    wheel = validation_wheel(
        parser, args.validation_wheel, root=root, release=release
    )
    modules = 0
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("ducai_studio/") and name.endswith(".py"):
                source = root / "src" / name
                assert source.is_file() and archive.read(name) == source.read_bytes(), name
                modules += 1
    qml_files = tuple((root / "src/ducai_studio/ui/qml").rglob("*.qml"))
    for source in qml_files:
        target = release / "ui/qml" / source.relative_to(root / "src/ducai_studio/ui/qml")
        assert digest(source) == digest(target), target
    for host in ("ocr_engine_host", "tts_engine_host", "whisper_engine_host"):
        python = (release.parent / "DUCAI_STUDIO_DATA/paddle_runtime/.venv/Scripts/python.exe"
                  if host == "ocr_engine_host" else Path(sys.executable))
        compiled = report.parent / f"check-{host}.pyc"
        subprocess.run([str(python), "-c",
            "import py_compile,sys; py_compile.compile(sys.argv[1],cfile=sys.argv[2],"
            "dfile='engine_hosts/'+sys.argv[3]+'.py',doraise=True)",
            str(root / f"src/ducai_studio/engine_hosts/{host}.py"), str(compiled), host],
            check=True, timeout=30)
        shipped = release / f"engine_hosts/{host}.pyc"
        assert compiled.read_bytes()[16:] == shipped.read_bytes()[16:]
    health = report.parent / "exe-health.json"
    nonce = uuid.uuid4().hex
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": "1.25",
        "DUCAI_APP_DATA_ROOT": str(report.parent / "exe-data"), "DUCAI_SMOKE_EXIT_MS": "6000",
        "DUCAI_SMOKE_SIZE": "1180x720", "DUCAI_UPDATE_HEALTH_PATH": str(health),
        "DUCAI_UPDATE_HEALTH_NONCE": nonce, "DUCAI_UPDATE_HEALTH_VERSION": manifest["version"]}
    result = subprocess.run([str(release / "DucaiStudio2.exe")], cwd=release,
                            env=environment, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=90)
    verify_probe_output(result, "exe-smoke", report.parent)
    receipt = json.loads(health.read_text(encoding="utf-8"))
    assert receipt["version"] == manifest["version"] and receipt["nonce"] == nonce
    with tempfile.TemporaryDirectory(prefix="wheel-check-", dir=report.parent) as temporary:
        extracted = Path(temporary).resolve()
        assert extracted.is_relative_to(report.parent)
        with zipfile.ZipFile(wheel) as archive:
            assert all((extracted / name).resolve().is_relative_to(extracted)
                       for name in archive.namelist())
            archive.extractall(extracted)
        fixture_env = {**os.environ,
            "PYTHONPATH": os.pathsep.join((
                str(root / "tests/fixtures/license-bootstrap"), str(extracted))),
            "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": "1.25",
            "DUCAI_TEST_DEFERRED_EDITS": "1", "DUCAI_TEST_QML_ROOT": str(release / "ui/qml"),
            **validation_license_environment(extracted)}
        gesture = subprocess.run([sys.executable, "-X", "utf8",
            str(root / "tests/fixtures/timeline-delete-scissors.py"),
            str(report.parent / "packaged-save-gestures")], cwd=extracted, env=fixture_env,
            capture_output=True, text=True, encoding="utf-8", timeout=90)
        verify_probe_output(gesture, "save-gestures", report.parent)
        controls = subprocess.run([sys.executable, "-X", "utf8",
            str(root / "tests/fixtures/translation-controls.py"),
            str(report.parent / "packaged-controls")], cwd=extracted, env=fixture_env,
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        verify_probe_output(controls, "translation-controls", report.parent)
        styles = subprocess.run([sys.executable, "-X", "utf8",
            str(root / "tests/fixtures/translation-style-presets.py"),
            str(report.parent / "packaged-translation-styles")],
            cwd=extracted, env=fixture_env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30)
        verify_probe_output(styles, "translation-styles", report.parent)
        preview = subprocess.run([sys.executable, "-X", "utf8",
            str(root / "tests/fixtures/preview-cue-interactions.py"),
            str(report.parent / "packaged-preview-interactions")],
            cwd=extracted, env=fixture_env, capture_output=True,
            text=True, encoding="utf-8", timeout=90)
        verify_probe_output(preview, "preview-interactions", report.parent)
        native_env = {**fixture_env, "QT_QPA_PLATFORM": (
            "offscreen" if args.offscreen_only else "windows"), "QT_SCALE_FACTOR": "1"}
        parity_env = ({**native_env, "QT_QPA_PLATFORM": "windows",
                       "QT_QUICK_BACKEND": "software", "DUCAI_TEST_HIDE_WINDOW": "1"}
                      if args.hidden_native_parity else native_env)
        parity = subprocess.run([sys.executable, "-X", "utf8",
            str(root / "tests/fixtures/subtitle-project-parity.py"),
            str(report.parent / "packaged-font-parity"), "ffmpeg"],
            cwd=extracted, env=parity_env, capture_output=True,
            text=True, encoding="utf-8", timeout=90)
        verify_probe_output(parity, "font-export", report.parent)
        titles = subprocess.run([sys.executable, "-X", "utf8",
            str(root / "tests/fixtures/timeline-media-titles.py"),
            str(report.parent / "packaged-timeline-media-titles")],
            cwd=extracted, env={**native_env, "QT_SCALE_FACTOR": "1.25"}, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=90)
        verify_probe_output(titles, "media-titles", report.parent)
        title_parity = subprocess.run([sys.executable, "-X", "utf8",
            str(root / "tests/fixtures/subtitle-project-parity.py"),
            str(report.parent / "packaged-title-parity"), "ffmpeg"],
            cwd=extracted, env={**parity_env, "DUCAI_TEST_TITLE_MODE": "1",
                               "QT_SCALE_FACTOR": "1.25"}, capture_output=True,
            text=True, encoding="utf-8", timeout=90)
        verify_probe_output(title_parity, "title-export", report.parent)
        frozen_ocr = None
        if args.ocr_database:
            assert args.ocr_job, "Supply the immutable job identity with the database"
            frozen_ocr = subprocess.run([sys.executable, "-X", "utf8",
                str(root / "tests/fixtures/ocr-review-cut-replay.py"),
                str(args.ocr_database.resolve()), args.ocr_job,
                str(report.parent / "packaged-ocr-cut")],
                cwd=extracted, env=fixture_env, capture_output=True,
                text=True, encoding="utf-8", timeout=30)
            verify_probe_output(frozen_ocr, "ocr-cut", report.parent)
    payload = {"status": "PASS", "version": manifest["version"],
        "qt_display_mode": ("offscreen-ui-with-never-shown-native-parity"
                            if args.offscreen_only and args.hidden_native_parity else
                            "offscreen-only" if args.offscreen_only else "native-windows"),
        "verified_manifest_entries": len(entries), "matching_source_python_modules": modules,
        "matching_runtime_qml_resources": len(qml_files), "engine_host_parity": "PASS",
        "exe_sha256": digest(release / "DucaiStudio2.exe"), "exe_smoke_exit": result.returncode,
        "exe_health": receipt, "wheel_save_gestures_exit": gesture.returncode,
        "wheel_controls_exit": controls.returncode, "native_font_export_exit": parity.returncode,
        "wheel_translation_styles_exit": styles.returncode,
        "wheel_preview_interactions_exit": preview.returncode,
        "wheel_timeline_media_titles_exit": titles.returncode,
        "native_title_export_exit": title_parity.returncode,
        "frozen_ocr_cut_exit": frozen_ocr.returncode if frozen_ocr else None}
    report.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
